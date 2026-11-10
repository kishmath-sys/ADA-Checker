"""Starts ADA Checker: local server, native file dialogs, and an app window in Microsoft Edge."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .server import App, serve
from .store import Store, data_dir

FILE_TYPES = [
    ("Documents to check", "*.docx *.docm *.pdf *.html *.htm *.pptx *.pptm *.xlsx *.xlsm *.zip"),
    ("Word", "*.docx *.docm"), ("PDF", "*.pdf"), ("Web pages", "*.html *.htm"),
    ("PowerPoint", "*.pptx *.pptm"), ("Excel", "*.xlsx *.xlsm"), ("Course export zip", "*.zip"),
    ("All files", "*.*"),
]


class TkDialogs:
    """Native Windows file pickers. tkinter must run on the main thread, so requests are queued."""

    def __init__(self):
        import tkinter as tk
        from tkinter import filedialog
        self.tk = tk
        self.fd = filedialog
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.root.attributes("-topmost", True)
        except Exception:  # noqa
            pass
        self.requests = queue.Queue()
        self.last_dir = str(Path.home() / "Documents")

    def pick(self, kind):
        done = threading.Event()
        box = {}
        self.requests.put((kind, box, done))
        done.wait(600)
        return box.get("paths")

    def _handle(self, kind, box, done):
        try:
            self.root.lift()
            self.root.focus_force()
            if kind == "folder":
                p = self.fd.askdirectory(parent=self.root, title="Choose a folder to check", initialdir=self.last_dir, mustexist=True)
                box["paths"] = [p] if p else []
            elif kind == "report_folder":
                p = self.fd.askdirectory(parent=self.root, title="Choose where to save reports", initialdir=self.last_dir)
                box["paths"] = [p] if p else []
            else:
                ps = self.fd.askopenfilenames(parent=self.root, title="Choose file(s) to check", initialdir=self.last_dir, filetypes=FILE_TYPES)
                box["paths"] = list(ps) if ps else []
            if box["paths"]:
                first = box["paths"][0]
                self.last_dir = first if os.path.isdir(first) else os.path.dirname(first)
        finally:
            done.set()

    def run(self, should_stop):
        def poll():
            try:
                while True:
                    kind, box, done = self.requests.get_nowait()
                    self._handle(kind, box, done)
            except queue.Empty:
                pass
            if should_stop():
                self.root.quit()
                return
            self.root.after(150, poll)
        self.root.after(150, poll)
        self.root.mainloop()


def find_edge():
    candidates = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("msedge") or shutil.which("microsoft-edge")


def open_window(url):
    edge = find_edge()
    if edge:
        profile = data_dir() / "window"
        try:
            subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,860",
                              "--no-first-run", "--disable-features=Translate"], close_fds=True)
            return
        except Exception:  # noqa
            pass
    webbrowser.open(url)


def already_running(paths):
    """If another copy is running, hand it any files, open a window to it and return True."""
    info = data_dir() / "running.json"
    try:
        d = json.loads(info.read_text())
        base = f"http://127.0.0.1:{d['port']}"
        req = urllib.request.Request(f"{base}/api/ping", headers={"X-Token": d["token"]})
        urllib.request.urlopen(req, timeout=2).read()
        if paths:
            body = json.dumps({"items": [{"path": p} for p in paths], "subfolders": True}).encode()
            req = urllib.request.Request(f"{base}/api/scan", data=body, method="POST",
                                         headers={"X-Token": d["token"], "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        open_window(f"{base}/?t={d['token']}")
        return True
    except Exception:  # noqa
        return False


def main():
    # files passed on the command line (right click, Send to, ADA Checker)
    paths = [os.path.abspath(p) for p in sys.argv[1:] if os.path.exists(p)]
    if already_running(paths):
        return
    try:
        dialogs = TkDialogs()
    except Exception:  # noqa
        dialogs = None
    app = App(Store(), dialogs)
    httpd = serve(app, 0)
    port = httpd.server_address[1]
    (data_dir() / "running.json").write_text(json.dumps({"port": port, "token": app.token}))
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    app.last_ping = time.time() + 40  # give the window time to open
    if paths:
        app.start_scan([{"path": p} for p in paths], None, True, False)
    open_window(url)

    def should_stop():
        # close when the window has been gone for a while and nothing is running
        return time.time() - app.last_ping > 180 and not app.job.get("running")

    try:
        if dialogs:
            dialogs.run(should_stop)
        else:
            while not should_stop():
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        try:
            (data_dir() / "running.json").unlink()
        except Exception:  # noqa
            pass
        shutil.rmtree(app.upload_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
