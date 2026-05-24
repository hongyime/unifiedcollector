#!/usr/bin/env python3
"""GitHub Toolkit - Main entry point."""
import sys
import os
import atexit
import signal
import sqlite3

sys.path.insert(0, os.path.dirname(__file__))

from src.cli import main


def _wal_checkpoint():
    """On clean exit: merge WAL into main DB file and close."""
    try:
        from src.config import Config
        conn = sqlite3.connect(str(Config.DB_PATH))
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.close()
    except Exception:
        pass  # DB may not exist yet on first run


def _sigterm_handler(signum, frame):
    # Trigger atexit handlers (including WAL checkpoint) then exit
    sys.exit(0)


atexit.register(_wal_checkpoint)

# SIGTERM: graceful shutdown (Docker stop, task scheduler, etc.)
try:
    signal.signal(signal.SIGTERM, _sigterm_handler)
except (ValueError, OSError):
    pass

# Windows: SIGBREAK (Ctrl+Break) — distinct from SIGINT (Ctrl+C)
if hasattr(signal, 'SIGBREAK'):
    try:
        signal.signal(signal.SIGBREAK, _sigterm_handler)
    except (ValueError, OSError):
        pass


if __name__ == '__main__':
    main()
