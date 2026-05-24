"""DatabaseManager — single entry point for all database access.

Parses DATABASE_URL, instantiates the correct backend, manages connection
lifecycle, and exposes a simple query interface that always returns dicts.

Thread / process safety model
------------------------------
- SQLiteBackend gives each thread its own sqlite3.Connection.
- WAL journal mode lets N readers run concurrently with 1 writer.
- PRAGMA busy_timeout=5000 means a second writer (even in another process)
  retries for up to 5 seconds before raising OperationalError.
- atexit and signal handlers call close() → WAL checkpoint so the WAL
  file is merged back into the main DB file on clean shutdown.
"""
from __future__ import annotations

import atexit
import contextlib
import os
import sqlite3
import threading
from typing import Any

from src.db.backends import SQLiteBackend, PostgreSQLBackend
from src.db.schema import SCHEMA_DDL, MIGRATION_DDL

_DEFAULT_DB_PATH = os.path.join("data", "instagram_toolkit.db")


class DatabaseManager:
    """Backend-agnostic database manager.

    Usage::

        db = DatabaseManager()                          # SQLite default
        db = DatabaseManager("sqlite:///data/foo.db")  # explicit SQLite
        db = DatabaseManager("postgresql://...")        # PostgreSQL

    Or set the DATABASE_URL environment variable and call DatabaseManager()
    with no arguments.
    """

    def __init__(self, database_url: str | None = None) -> None:
        if database_url is None:
            database_url = os.environ.get("DATABASE_URL", "")

        if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
            self._backend = PostgreSQLBackend(database_url)
            self._is_sqlite = False
        else:
            # Parse sqlite:///path or use default
            if database_url.startswith("sqlite:///"):
                db_path = database_url[len("sqlite:///"):]
            elif database_url == "sqlite:///:memory:" or database_url == ":memory:":
                db_path = ":memory:"
            elif database_url == "":
                db_path = _DEFAULT_DB_PATH
            else:
                db_path = database_url  # bare path
            self._backend = SQLiteBackend(db_path)
            self._is_sqlite = True

        # Register clean-shutdown hook: checkpoints WAL and closes connections
        # Works for normal exit, sys.exit(), and unhandled exceptions.
        # SIGKILL can't be caught — WAL auto-recovery handles that on next open.
        atexit.register(self._safe_close)

        self.create_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def create_schema(self) -> None:
        """Apply all DDL statements idempotently (CREATE TABLE IF NOT EXISTS)."""
        conn = self._backend.connect()
        try:
            for ddl in SCHEMA_DDL:
                conn.execute(ddl)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        self.apply_migrations()

    def apply_migrations(self) -> None:
        """Apply MIGRATION_DDL statements idempotently.

        Each statement is wrapped in its own try/except so that already-applied
        migrations (e.g. 'duplicate column' errors) are silently skipped.
        """
        conn = self._backend.connect()
        for ddl in MIGRATION_DDL:
            try:
                conn.execute(ddl)
                conn.commit()
            except Exception:
                # Ignore 'duplicate column', 'table already exists', etc.
                try:
                    conn.rollback()
                except Exception:
                    pass

    # ── Connection context manager ────────────────────────────────────────

    @contextlib.contextmanager
    def get_connection(self):
        """Context manager that yields a connection and commits/rolls back."""
        conn = self._backend.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Query helpers ─────────────────────────────────────────────────────

    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite3.Row or psycopg2 RealDictRow to a plain dict."""
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return dict(row)
        # psycopg2 RealDictRow is already dict-like
        return dict(row)

    def execute(self, sql: str, params: tuple = ()) -> Any:
        """Execute a DML statement and return the cursor."""
        conn = self._backend.connect()
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor

    def executemany(self, sql: str, params_seq: list) -> None:
        """Execute a DML statement for each parameter set."""
        conn = self._backend.connect()
        conn.executemany(sql, params_seq)
        conn.commit()

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute a SELECT and return the first row as a dict, or None."""
        conn = self._backend.connect()
        cursor = conn.execute(sql, params)
        row = cursor.fetchone()
        return self._row_to_dict(row)

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT and return all rows as a list of dicts."""
        conn = self._backend.connect()
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Lifecycle ─────────────────────────────────���───────────────────────

    def close(self) -> None:
        """Checkpoint WAL and close all connections. Safe to call multiple times."""
        self._backend.close()

    def _safe_close(self) -> None:
        """atexit handler — silently checkpoint and close."""
        try:
            self._backend.close()
        except Exception:
            pass


__all__ = ["DatabaseManager"]


