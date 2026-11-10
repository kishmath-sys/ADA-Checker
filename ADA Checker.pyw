"""Double-click entry point. Errors are written to the log in %LOCALAPPDATA%\\ADA Checker."""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adachecker.store import data_dir  # noqa: E402

log = open(data_dir() / "log.txt", "a", encoding="utf-8", buffering=1)
if sys.stdout is None or "pythonw" in sys.executable.lower():
    sys.stdout = log
    sys.stderr = log

try:
    from adachecker.app import main
    main()
except Exception:  # noqa
    traceback.print_exc(file=log)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "ADA Checker could not start. Details were saved to:\n" + str(data_dir() / "log.txt"),
                                         "ADA Checker", 0x10)
    except Exception:  # noqa
        pass
