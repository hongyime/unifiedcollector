#!/usr/bin/env python3
"""
Shared SQLite helpers for consistent connection tuning and lock handling.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


DEFAULT_SQLITE_TIMEOUT_SECONDS = 30.0
DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30000

_LOCK_ERROR_TOKENS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "database is busy",
    "database table is busy",
)


class DatabaseLockError(sqlite3.OperationalError):
    """Raised when a SQLite operation cannot proceed because the database is locked."""


def is_database_lock_error(error: BaseException) -> bool:
    """Return True when an exception represents a SQLite lock/busy condition."""
    if not isinstance(error, sqlite3.OperationalError):
        return False

    message = str(error).lower()
    return any(token in message for token in _LOCK_ERROR_TOKENS)


def describe_database_lock(context: str, db_path: Optional[Path | str] = None) -> str:
    """Return a user-facing message for a locked database."""
    suffix = f" ({db_path})" if db_path else ""
    return (
        f"Database is locked while {context}{suffix}. "
        "Another toolkit process is likely writing to SQLite right now. "
        "Wait for the active operation to finish and retry."
    )


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    row_factory: Optional[type] = sqlite3.Row,
    wal: bool = False,
    synchronous: str = "NORMAL",
    cache_size: Optional[int] = None,
    wal_autocheckpoint: Optional[int] = None,
) -> sqlite3.Connection:
    """Apply connection settings that improve concurrent SQLite access."""
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")

    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={synchronous}")
        if cache_size is not None:
            conn.execute(f"PRAGMA cache_size={int(cache_size)}")
        if wal_autocheckpoint is not None:
            conn.execute(f"PRAGMA wal_autocheckpoint={int(wal_autocheckpoint)}")

    if row_factory is not None:
        conn.row_factory = row_factory

    return conn


def connect_sqlite(
    db_path: Path | str,
    *,
    timeout: float = DEFAULT_SQLITE_TIMEOUT_SECONDS,
    check_same_thread: bool = True,
    row_factory: Optional[type] = sqlite3.Row,
    wal: bool = False,
    synchronous: str = "NORMAL",
    cache_size: Optional[int] = None,
    wal_autocheckpoint: Optional[int] = None,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Create a SQLite connection with the project's standard settings."""
    conn = sqlite3.connect(
        str(db_path),
        timeout=timeout,
        check_same_thread=check_same_thread,
    )
    return configure_sqlite_connection(
        conn,
        busy_timeout_ms=busy_timeout_ms,
        row_factory=row_factory,
        wal=wal,
        synchronous=synchronous,
        cache_size=cache_size,
        wal_autocheckpoint=wal_autocheckpoint,
    )
