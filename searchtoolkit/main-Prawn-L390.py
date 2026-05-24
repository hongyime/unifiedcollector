#!/usr/bin/env python3
"""Entry point for the Unified Search Toolkit."""

import sys
import os

# Force UTF-8 on Windows — prevents emoji/unicode crash in CP1252 terminals
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")

import signal
from src.resilience import _SHUTDOWN


def _handle_sigint(signum, frame):
    if _SHUTDOWN.is_set():
        print("\n[FORCE EXIT] Forcing exit now.")
        raise SystemExit(1)
    _SHUTDOWN.set()
    print("\n[STOPPING] Finishing current operation... Ctrl+C again to force exit.")


signal.signal(signal.SIGINT, _handle_sigint)

from src.app import main

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    main(mode=mode)

