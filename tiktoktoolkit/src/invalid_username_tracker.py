"""Invalid username tracking and persistence."""

import sqlite3
import time
from pathlib import Path
from typing import Set, List, Optional
from contextlib import contextmanager

from .models import InvalidReason, InvalidUsernameRecord


class InvalidUsernameTracker:
    """Tracks and persists invalid username detections."""

    def __init__(self, db_path: str = "data/tiktok_toolkit.db"):
        """Initialize tracker with database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self._invalid_usernames: Set[str] = set()
        self._ensure_schema()

    @contextmanager
    def _get_conn(self):
        """Get a database connection with proper configuration."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self):
        """Create database schema for invalid_usernames table.

        Creates:
        - invalid_usernames table with columns: id, username, reason,
          error_message, detected_at, retry_count, created_at
        - UNIQUE constraint on (username, detected_at)
        - Index on username column for fast lookup
        - Index on detected_at column (descending) for time-based queries
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invalid_usernames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    error_message TEXT,
                    detected_at REAL NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    created_at REAL DEFAULT (unixepoch()),
                    UNIQUE(username, detected_at)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_invalid_usernames_username
                ON invalid_usernames(username)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_invalid_usernames_detected_at
                ON invalid_usernames(detected_at DESC)
            """)

    def record_invalid(
        self,
        username: str,
        reason: InvalidReason,
        error_message: Optional[str] = None,
    ) -> None:
        """Record a username as invalid.

        Inserts a record into the database and adds the username to the
        in-memory set for O(1) lookup.

        Args:
            username: The invalid username
            reason: Why the username is invalid
            error_message: Optional error message from API
        """
        detected_at = time.time()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO invalid_usernames
                        (username, reason, error_message, detected_at, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, reason.value, error_message, detected_at, detected_at),
                )
        except Exception:
            # Swallow DB errors so the main loop is never interrupted
            pass
        # Always update in-memory set regardless of DB outcome
        self._invalid_usernames.add(username)

    def is_confirmed_invalid(self, username: str, min_detections: int = 2) -> bool:
        """Check if username has been confirmed invalid multiple times.

        Args:
            username: Username to check
            min_detections: Minimum number of detections to confirm

        Returns:
            True if username has been detected invalid >= min_detections times
        """
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM invalid_usernames WHERE username=?",
                    (username,),
                )
                count = cur.fetchone()[0]
            return count >= min_detections
        except Exception:
            return False

    def get_invalid_usernames(self) -> Set[str]:
        """Get all invalid usernames tracked in the current session."""
        return self._invalid_usernames.copy()

    def get_invalid_records(self) -> List[InvalidUsernameRecord]:
        """Get detailed records of all invalid usernames from the database."""
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    """
                    SELECT username, reason, detected_at, error_message, retry_count
                    FROM invalid_usernames
                    ORDER BY detected_at DESC
                    """
                )
                rows = cur.fetchall()
            return [
                InvalidUsernameRecord(
                    username=row["username"],
                    reason=InvalidReason(row["reason"]),
                    detected_at=row["detected_at"],
                    error_message=row["error_message"],
                    retry_count=row["retry_count"],
                )
                for row in rows
            ]
        except Exception:
            return []

    def flush(self) -> None:
        """Flush in-memory state to database (commit pending transactions).

        Opens a new connection and immediately commits to ensure all
        WAL frames are checkpointed.
        """
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            conn.commit()
            conn.close()
        except Exception:
            pass

    def clear_username(self, username: str) -> None:
        """Remove username from invalid tracking.

        Removes from both the in-memory set and the database so the
        username is no longer considered invalid.

        Args:
            username: Username to clear
        """
        self._invalid_usernames.discard(username)
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "DELETE FROM invalid_usernames WHERE username=?",
                    (username,),
                )
        except Exception:
            pass
