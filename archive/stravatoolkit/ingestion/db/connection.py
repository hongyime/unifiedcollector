"""Database connection and initialization."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ingestion.config import now_utc_iso
from ingestion.db.schema import SCHEMA_SQL


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Create a read-write connection to the database."""
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Create a read-only connection to the database."""
    db_file = Path(db_path)
    conn = sqlite3.connect(
        f"file:{db_file.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """Initialize the database schema."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_athlete_photo_dedup
            ON athlete_photo_history(athlete_id, md5_hash)
            """
        )
        # One-time cleanup: fix avatar URLs stored with JSON unicode escapes (e.g. & → &)
        # caused by a parser bug where html.unescape() was not decoding \uXXXX sequences.
        conn.execute(
            "UPDATE athletes SET avatar_url = REPLACE(avatar_url, '\\u0026', '&')"
            " WHERE avatar_url LIKE '%\\u0026%'"
        )
        conn.execute(
            "UPDATE athlete_photo_history SET source_url = REPLACE(source_url, '\\u0026', '&')"
            " WHERE source_url LIKE '%\\u0026%'"
        )
    finally:
        conn.close()


def save_session_state(conn: sqlite3.Connection, cookie_value: str, auth_mode: str) -> None:
    """Save session state to the database."""
    with transaction(conn):
        conn.execute("UPDATE session_state SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            """
            INSERT INTO session_state (cookie_value, auth_mode, captured_at, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (cookie_value, auth_mode, now_utc_iso()),
        )


def repair_backfill_state(conn: sqlite3.Connection) -> int:
    """Repair backfill state for athletes with inconsistent status."""
    repaired = 0
    cursor = conn.execute(
        """
        UPDATE athletes
        SET backfill_status = 'pending',
            backfill_completed_at = NULL,
            backfill_recent_completed_at = NULL,
            backfill_last_coverage_check_at = NULL,
            last_crawl_status = NULL,
            backfill_last_issue_code = NULL,
            backfill_last_issue_message = NULL,
            backfill_last_issue_at = NULL
        WHERE is_tracked = 1
          AND backfill_status = 'forbidden'
          AND backfill_completed_at IS NOT NULL
          AND backfill_oldest_seen_utc IS NULL
        """
    )
    repaired += int(cursor.rowcount or 0)
    cursor = conn.execute(
        """
        UPDATE athletes
        SET backfill_status = 'pending',
            last_crawl_status = NULL,
            backfill_last_issue_code = NULL,
            backfill_last_issue_message = NULL,
            backfill_last_issue_at = NULL
        WHERE is_tracked = 1
          AND backfill_status = 'needs_endpoint'
        """
    )
    repaired += int(cursor.rowcount or 0)
    return repaired


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager for explicit transactions."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def checkpoint(conn: sqlite3.Connection) -> None:
    """Flush the WAL file into the main DB file."""
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
