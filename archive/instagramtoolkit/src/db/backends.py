"""Database backend implementations.

Provides BaseBackend ABC and concrete SQLiteBackend / PostgreSQLBackend classes.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import threading
from abc import ABC, abstractmethod
from typing import Any


class BaseBackend(ABC):
    """Abstract base class that all database backends must implement."""

    @abstractmethod
    def connect(self) -> Any:
        """Return a database connection."""
        ...

    @abstractmethod
    def placeholder(self) -> str:
        """Return the parameter placeholder string for this backend."""
        ...

    @abstractmethod
    def upsert_syntax(self) -> str:
        """Return the INSERT-or-replace prefix for this backend."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the backend connection / pool."""
        ...


class SQLiteBackend(BaseBackend):
    """SQLite backend using Python's built-in sqlite3 module.

    Each thread gets its own connection (threading.local) so sqlite3's
    connection object is never shared across threads.  WAL journal mode
    + busy_timeout=5000 makes concurrent multi-process access safe:
      - WAL  → N readers + 1 writer at a time without blocking reads
      - busy_timeout → retry for 5 s on write lock before raising
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._local = threading.local()       # per-thread connection storage
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()   # guards _all_conns list only
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Return the calling thread's SQLite connection, creating it if needed."""
        if not getattr(self._local, 'conn', None):
            is_new = self._db_path == ":memory:" or not os.path.exists(self._db_path)
            conn = sqlite3.connect(
                self._db_path,
                check_same_thread=True,        # strict: each thread owns its conn
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")   # retry 5 s on write lock
            conn.execute("PRAGMA synchronous=NORMAL")  # WAL default — safe + fast
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-8000")    # 8 MB page cache per conn
            if is_new and self._db_path != ":memory:":
                try:
                    os.chmod(self._db_path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return self._local.conn

    def placeholder(self) -> str:
        return "?"

    def upsert_syntax(self) -> str:
        return "INSERT OR REPLACE"

    def close(self) -> None:
        """Checkpoint WAL and close all thread-local connections."""
        with self._conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                # Passive checkpoint: merge WAL into main DB without blocking readers
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                conn.close()
            except Exception:
                pass
        # Clear this thread's reference too
        self._local.conn = None


class PostgreSQLBackend(BaseBackend):
    """PostgreSQL backend using psycopg2.

    Raises ImportError with install instructions when psycopg2 is absent.
    """

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg2  # noqa: F401
            import psycopg2.extras  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "psycopg2 is required for PostgreSQL support. "
                "Install it with: pip install psycopg2-binary"
            ) from exc
        self._database_url = database_url
        self._conn = None

    def connect(self):
        """Return a psycopg2 connection."""
        import psycopg2
        import psycopg2.extras

        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(
                self._database_url,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
        return self._conn

    def placeholder(self) -> str:
        return "%s"

    def upsert_syntax(self) -> str:
        return "INSERT"

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            self._conn = None


__all__ = ["BaseBackend", "SQLiteBackend", "PostgreSQLBackend"]


