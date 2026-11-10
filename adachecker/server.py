"""Local web server for the app window. Listens on 127.0.0.1 only and requires a per-launch token."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import queue
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from . import engine, report
from .common import CHECKER_VERSION, short
from .store import Store, data_dir

UI_DIR = Path(__file__).resolve().parent / "ui"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_path(path: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa
        return False


class App:
    def __init__(self, store: Store | None = None, dialogs=None):
        self.store = store or Store()
        self.token = secrets.token_urlsafe(24)
        self.dialogs = dialogs            # object with pick(kind) -> list[str] | None, or None
        self.last_ping = time.time()
        self.lock = threading.RLock()
        self.job = {"running": False}
        self.cancel = threading.Event()
        self.batch = []                   # scan ids of the latest batch
        self.batch_info = {}
        self.upload_dir = Path(tempfile.mkdtemp(prefix="adachecker_uploads_"))

    # ------------------------------------------------------------- helpers
    def marks_for(self, key):
        return self.store.marks(key)

    def status_for(self, result):
        return engine.status_of(result, self.marks_for)

    def summarize(self, scan_id):
        s = self.store.scan(scan_id)
        if not s:
            return None
        r = json.loads(s["result"])
        st = self.status_for(r)
        c = engine.counts_of(r, self.marks_for)
        return {"id": scan_id, "name": s["name"], "kind": s["kind"], "file_key": s["file_key"], "status": st,
                "counts": c, "scanned_at": s["scanned_at"], "error": r.get("error", ""),
                "children": len(r.get("children", []))}

    # ------------------------------------------------------------- scanning
    def gather(self, paths, folder, subfolders):
        files = []
        report_root = os.path.realpath(self.store.settings().get("report_folder", ""))
        if folder:
            if subfolders:
                for dp, dns, fns in os.walk(folder):
                    if os.path.realpath(dp).startswith(report_root) and report_root:
                        dns[:] = []
                        continue
                    dns[:] = [d for d in dns if not d.startswith(".")]
                    files += [os.path.join(dp, f) for f in fns if engine.is_supported(f)]
            else:
                files = [os.path.join(folder, f) for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f)) and engine.is_supported(f)]
        for p in paths or []:
            if os.path.isdir(p):
                files += self.gather([], p, subfolders)
            elif os.path.isfile(p):
                files.append(p)
        return sorted(dict.fromkeys(files), key=lambda x: x.lower())

    def start_scan(self, items, folder=None, subfolders=True, force=False):
        """items: list of {"path", "name" (optional), "key" (optional)}"""
        with self.lock:
            if self.job.get("running"):
                return {"error": "A check is already running."}
            self.cancel.clear()
            self.job = {"running": True, "kind": "files", "done": 0, "total": 0, "current": "Finding files…",
                        "log": [], "folder": folder, "summary": None, "started": time.time()}
        threading.Thread(target=self._scan_worker, args=(items, folder, subfolders, force), daemon=True).start()
        return {"ok": True}

    def _scan_worker(self, items, folder, subfolders, force):
        try:
            settings = self.store.settings()
            options = {"check_links": settings.get("check_links", True), "colorblind": settings.get("colorblind", True)}
            entries = list(items or [])
            if folder:
                entries += [{"path": p} for p in self.gather([], folder, subfolders)]
            # expand any folders passed as items
            expanded = []
            for it in entries:
                if os.path.isdir(it["path"]):
                    expanded += [{"path": p} for p in self.gather([], it["path"], subfolders)]
                else:
                    expanded.append(it)
            entries = expanded
            self.job["total"] = len(entries)
            batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            ids = []
            # Word files next to PDFs, for "fix this in Word" hints
            sources = {}
            for it in entries:
                if Path(it["path"]).suffix.lower() in (".docx", ".docm"):
                    sources[(str(Path(it["path"]).parent).lower(), Path(it["path"]).stem.lower())] = it.get("name") or Path(it["path"]).name
            for n, it in enumerate(entries, 1):
                if self.cancel.is_set():
                    self.job["log"].append("Stopped by you.")
                    break
                path = it["path"]
                name = it.get("name") or Path(path).name
                key = it.get("key") or os.path.abspath(path)
                self.job["current"] = name
                try:
                    sha = sha256_of(path)
                except OSError as e:
                    self.job["log"].append(f"Skipped {name}: {e}")
                    self.job["done"] = n
                    continue
                last = self.store.last_scan(key)
                if last and not force and last["sha"] == sha and last["checker"] == CHECKER_VERSION:
                    ids.append(last["id"])
                    self.job["log"].append(f"{name}: unchanged since {last['scanned_at'][:10]}, reused")
                    self.job["done"] = n
                    continue
                sib = {k[1]: v for k, v in sources.items() if k[0] == str(Path(path).parent).lower()}

                def prog(cur, i, tot, _name=name):
                    self.job["current"] = f"{_name}: {cur} ({i} of {tot})"
                res = engine.check_file(path, name, options, sib, progress=prog)
                res.path = key
                rd = res.to_dict()
                self._rekey_children(rd, key)
                st = self.status_for(rd)
                c = engine.counts_of(rd, self.marks_for)
                sid = self.store.add_scan(key, name, rd["kind"], sha, CHECKER_VERSION, st, c, batch_id, rd)
                ids.append(sid)
                self.job["done"] = n
            self.batch = ids
            self.batch_info = {"folder": folder, "batch": batch_id}
            summary = None
            if folder and ids and not self.cancel.is_set():
                summary = self.write_folder_reports(ids, folder)
            self.job.update({"running": False, "current": "", "summary": summary, "finished": time.time()})
        except Exception as e:  # noqa
            traceback.print_exc()
            self.job.update({"running": False, "error": short(str(e), 300)})

    def _rekey_children(self, rd, key):
        for c in rd.get("children", []):
            rel = c["path"].split("::", 1)[-1]
            c["path"] = f"{key}::{rel}"

    def write_folder_reports(self, ids, folder):
        settings = self.store.settings()
        base = Path(settings.get("report_folder") or (Path.home() / "Documents" / "ADA Checker Reports"))
        out = base / report.safe_filename(f"{Path(folder).name or 'Folder'} {datetime.now().strftime('%Y-%m-%d %H%M')}")
        (out / "Reports").mkdir(parents=True, exist_ok=True)
        rows = []
        used = set()
        for sid in ids:
            s = self.store.scan(sid)
            r = json.loads(s["result"])
            st = self.status_for(r)
            fn = report.safe_filename(Path(s["name"]).name)
            stem = fn
            k = 2
            while stem.lower() in used:
                stem = f"{fn} ({k})"
                k += 1
            used.add(stem.lower())
            rp = out / "Reports" / f"{stem}.html"
            rp.write_text(report.report_html(r, st, self.marks_for, self.status_for, s["scanned_at"].replace("T", " ")), encoding="utf-8")
            try:
                rel_folder = os.path.relpath(os.path.dirname(s["file_key"]), folder)
            except ValueError:
                rel_folder = os.path.dirname(s["file_key"])
            c = engine.counts_of(r, self.marks_for)
            kinds = {"word": "Word", "pdf": "PDF", "html": "Web page", "powerpoint": "PowerPoint", "excel": "Excel", "zip": "Zip"}
            rows.append({"name": s["name"], "folder": "" if rel_folder == "." else rel_folder, "kind": kinds.get(r["kind"], r["kind"]), "status": st,
                         "change": c["change"], "manual": c["manual"], "export": c["export"],
                         "top": report.top_issues(r, self.marks_for), "report": f"Reports/{stem}.html"})
        xlsx = out / "Accessibility summary.xlsx"
        report.write_summary_xlsx(xlsx, rows, folder)
        return {"folder": str(out), "spreadsheet": str(xlsx),
                "counts": {st: sum(1 for r in rows if r["status"] == st) for st in ("needs work", "needs review", "pass", "error")}}

    # ------------------------------------------------------------- history numbers
    def progress_stats(self):
        latest = self.store.latest_per_file()
        today = date.today()
        this_m = today.strftime("%Y-%m")
        last_m = (today.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
        hist = self.store.history(100000)

        def month_stats(m):
            files = {}
            for h in reversed(hist):  # oldest first, so the last scan in the month wins
                if h["scanned_at"].startswith(m):
                    files[h["file_key"]] = h["status"]
            return {"files": len(files), "ready": sum(1 for s in files.values() if s == "pass"),
                    "needs work": sum(1 for s in files.values() if s == "needs work"),
                    "needs review": sum(1 for s in files.values() if s == "needs review")}
        return {
            "files_tracked": len(latest),
            "now": {st: sum(1 for r in latest if r["status"] == st) for st in ("pass", "needs review", "needs work", "error")},
            "this_month": {"label": today.strftime("%B %Y"), **month_stats(this_m)},
            "last_month": {"label": datetime.strptime(last_m, "%Y-%m").strftime("%B %Y"), **month_stats(last_m)},
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ADAChecker"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            return secrets.compare_digest(self.headers.get("X-Token", ""), app.token)

        def _body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            u = urlparse(self.path)
            if not u.path.startswith("/api/"):
                p = u.path.lstrip("/") or "index.html"
                f = (UI_DIR / p).resolve()
                if not str(f).startswith(str(UI_DIR)) or not f.is_file():
                    return self._send(404, b"Not found", "text/plain")
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                return self._send(200, f.read_bytes(), ctype)
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                return self._send(200, self.route_get(u.path, q))
            except Exception as e:  # noqa
                traceback.print_exc()
                return self._send(500, {"error": short(str(e), 300)})

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authed():
                return self._send(403, {"error": "forbidden"})
            try:
                if u.path == "/api/upload":
                    name = unquote(self.headers.get("X-Filename", "upload"))
                    name = Path(name).name or "upload"
                    n = int(self.headers.get("Content-Length", "0") or 0)
                    folder = Path(tempfile.mkdtemp(dir=app.upload_dir))
                    dest = folder / name
                    with open(dest, "wb") as f:
                        remaining = n
                        while remaining > 0:
                            chunk = self.rfile.read(min(remaining, 1 << 20))
                            if not chunk:
                                break
                            f.write(chunk)
                            remaining -= len(chunk)
                    return self._send(200, {"path": str(dest), "name": name})
                return self._send(200, self.route_post(u.path, self._body()))
            except Exception as e:  # noqa
                traceback.print_exc()
                return self._send(500, {"error": short(str(e), 300)})

        # ------------------------------------------------------------- routes
        def route_get(self, path, q):
            if path == "/api/ping":
                app.last_ping = time.time()
                return {"ok": True}
            if path == "/api/job":
                j = dict(app.job)
                j["log"] = j.get("log", [])[-8:]
                if not j.get("running") and j.get("kind") == "files":
                    j["results"] = [app.summarize(i) for i in app.batch]
                return j
            if path == "/api/batch":
                return {"results": [app.summarize(i) for i in app.batch], "info": app.batch_info}
            if path == "/api/result":
                s = app.store.scan(int(q["id"]))
                if not s:
                    return {"error": "not found"}
                r = json.loads(s["result"])
                return {"scan": {k: s[k] for k in ("id", "name", "kind", "file_key", "scanned_at")},
                        "result": r, "status": app.status_for(r), "counts": engine.counts_of(r, app.marks_for),
                        "marks": _marks_tree(app, r)}
            if path == "/api/history":
                return {"latest": [dict(r, status=r["status"]) for r in app.store.latest_per_file()[:500]],
                        "progress": app.progress_stats(), "ignored": app.store.all_marks("ignored")[:300]}
            if path == "/api/settings":
                return {"settings": app.store.settings(), "version": CHECKER_VERSION, "data": str(data_dir()),
                        "dialogs": app.dialogs is not None}
            return {"error": "unknown"}

        def route_post(self, path, b):
            if path == "/api/pick":
                if app.dialogs is None:
                    return {"error": "no-dialog"}
                return {"paths": app.dialogs.pick(b.get("type", "file")) or []}
            if path == "/api/scan":
                items = b.get("items") or []
                return app.start_scan(items, b.get("folder"), b.get("subfolders", True), b.get("force", False))
            if path == "/api/rescan":
                s = app.store.scan(int(b["id"]))
                if not s:
                    return {"error": "not found"}
                p = s["file_key"]
                if not os.path.isfile(p):
                    return {"error": "The original file is no longer at this location. If you dragged it in, drag the updated file in again."}
                return app.start_scan([{"path": p, "name": s["name"], "key": p}], None, True, True)
            if path == "/api/cancel":
                app.cancel.set()
                return {"ok": True}
            if path == "/api/mark":
                app.store.set_mark(b["file_key"], b["fingerprint"], b["kind"], bool(b.get("on", True)), b.get("note", ""), b.get("title", ""))
                # keep the stored status current
                if b.get("scan_id") and str(b["scan_id"]).isdigit():
                    s = app.store.scan(int(b["scan_id"]))
                    if s:
                        r = json.loads(s["result"])
                        app.store.update_status(s["id"], app.status_for(r), engine.counts_of(r, app.marks_for))
                return {"ok": True}
            if path == "/api/save_report":
                s = app.store.scan(int(b["id"]))
                r = json.loads(s["result"])
                when = s["scanned_at"]
                base = Path(app.store.settings().get("report_folder"))
                base.mkdir(parents=True, exist_ok=True)
                out = base / f"{report.safe_filename(Path(r['name']).name)} report {datetime.now().strftime('%Y-%m-%d %H%M')}.html"
                out.write_text(report.report_html(r, app.status_for(r), app.marks_for, app.status_for, when.replace("T", " ")), encoding="utf-8")
                open_path(str(out))
                return {"path": str(out)}
            if path == "/api/open":
                p = b.get("path", "")
                if b.get("reveal") and os.path.exists(p):
                    p = p if os.path.isdir(p) else os.path.dirname(p)
                if not os.path.exists(p) and not p.startswith("http"):
                    return {"error": "That file is no longer there."}
                return {"ok": open_path(p)}
            if path == "/api/export_record":
                base = Path(app.store.settings().get("report_folder"))
                base.mkdir(parents=True, exist_ok=True)
                out = base / f"Accessibility compliance record {datetime.now().strftime('%Y-%m-%d')}.xlsx"
                report.write_compliance_xlsx(out, app.store)
                open_path(str(out))
                return {"path": str(out)}
            if path == "/api/settings":
                vals = b.get("settings", {})
                app.store.save_settings(vals)
                return {"settings": app.store.settings()}
            if path == "/api/clear_history":
                app.store.clear_history()
                app.batch = []
                return {"ok": True}
            return {"error": "unknown"}

    return Handler


def _marks_tree(app, r):
    out = {}

    def walk(x):
        ig, rv = app.marks_for(x["path"])
        out[x["path"]] = {"ignored": sorted(ig), "reviewed": sorted(rv)}
        for c in x.get("children", []):
            walk(c)
    walk(r)
    return out


def serve(app: App, port: int = 0):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
