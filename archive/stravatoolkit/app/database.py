from __future__ import annotations

import threading
from collections.abc import Generator

from ingestion.config import load_settings
from ingestion.db import connect_readonly, init_db

_DB_READY = False
_DB_LOCK = threading.Lock()


def get_db() -> Generator:
    global _DB_READY
    settings = load_settings()
    if not _DB_READY:
        with _DB_LOCK:
            if not _DB_READY:
                init_db(settings.db_path)
                _DB_READY = True
    conn = connect_readonly(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()
