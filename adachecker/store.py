"""Local history: scans, reviewed items, ignored items and settings (SQLite in the user's AppData)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share")
    d = Path(base) / "ADA Checker"
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_SETTINGS = {
    "check_links": True,
    "colorblind": True,
    "include_subfolders": True,
    "report_folder": str(Path.home() / "Documents" / "ADA Checker Reports"),
    "show_21_only": False,
}


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "history.db")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS scans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_key TEXT, name TEXT, kind TEXT, sha TEXT,
                checker TEXT, scanned_at TEXT, status TEXT, n_change INT, n_manual INT, n_export INT,
                batch TEXT, result TEXT);
            CREATE INDEX IF NOT EXISTS scans_key ON scans(file_key);
            CREATE TABLE IF NOT EXISTS marks(
                file_key TEXT, fingerprint TEXT, kind TEXT, note TEXT, title TEXT, created_at TEXT,
                PRIMARY KEY(file_key, fingerprint, kind));
            CREATE TABLE IF NOT EXISTS videos(video_id TEXT PRIMARY KEY, checked_at TEXT, status TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
            """)
            self.db.commit()

    # ------------------------------------------------------------- settings
    def settings(self) -> dict:
        out = dict(DEFAULT_SETTINGS)
        with self.lock:
            for r in self.db.execute("SELECT k, v FROM settings"):
                try:
                    out[r["k"]] = json.loads(r["v"])
                except Exception:  # noqa
                    pass
        return out

    def save_settings(self, values: dict):
        with self.lock:
            for k, v in values.items():
                if k in DEFAULT_SETTINGS:
                    self.db.execute("REPLACE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v)))
            self.db.commit()

    # ------------------------------------------------------------- scans
    def add_scan(self, file_key, name, kind, sha, checker, status, counts, batch, result) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO scans(file_key,name,kind,sha,checker,scanned_at,status,n_change,n_manual,n_export,batch,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_key, name, kind, sha, checker, datetime.now().isoformat(timespec="seconds"), status,
                 counts["change"], counts["manual"], counts["export"], batch, json.dumps(result)))
            self.db.commit()
            return cur.lastrowid

    def update_status(self, scan_id, status, counts):
        with self.lock:
            self.db.execute("UPDATE scans SET status=?, n_change=?, n_manual=?, n_export=? WHERE id=?",
                            (status, counts["change"], counts["manual"], counts["export"], scan_id))
            self.db.commit()

    def last_scan(self, file_key):
        with self.lock:
            r = self.db.execute("SELECT * FROM scans WHERE file_key=? ORDER BY id DESC LIMIT 1", (file_key,)).fetchone()
        return dict(r) if r else None

    def scan(self, scan_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        return dict(r) if r else None

    def history(self, limit=2000):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,file_key,name,kind,scanned_at,status,n_change,n_manual,n_export,batch FROM scans ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def latest_per_file(self):
        with self.lock:
            rows = self.db.execute("""SELECT s.id,s.file_key,s.name,s.kind,s.scanned_at,s.status,s.n_change,s.n_manual,s.n_export
                FROM scans s JOIN (SELECT file_key, MAX(id) mid FROM scans GROUP BY file_key) m ON s.id=m.mid
                ORDER BY s.scanned_at DESC""").fetchall()
        return [dict(r) for r in rows]

    def clear_history(self):
        with self.lock:
            self.db.execute("DELETE FROM scans")
            self.db.commit()

    # ------------------------------------------------------------- reviewed and ignored items
    def marks(self, file_key):
        with self.lock:
            rows = self.db.execute("SELECT fingerprint, kind FROM marks WHERE file_key=?", (file_key,)).fetchall()
        ignored = {r["fingerprint"] for r in rows if r["kind"] == "ignored"}
        reviewed = {r["fingerprint"] for r in rows if r["kind"] == "reviewed"}
        return ignored, reviewed

    def set_mark(self, file_key, fingerprint, kind, on, note="", title=""):
        with self.lock:
            if on:
                self.db.execute("REPLACE INTO marks(file_key,fingerprint,kind,note,title,created_at) VALUES(?,?,?,?,?,?)",
                                (file_key, fingerprint, kind, note, title, datetime.now().isoformat(timespec="seconds")))
            else:
                self.db.execute("DELETE FROM marks WHERE file_key=? AND fingerprint=? AND kind=?", (file_key, fingerprint, kind))
            self.db.commit()

    def all_marks(self, kind=None):
        with self.lock:
            q = "SELECT * FROM marks" + (" WHERE kind=?" if kind else "") + " ORDER BY created_at DESC"
            rows = self.db.execute(q, (kind,) if kind else ()).fetchall()
        return [dict(r) for r in rows]
