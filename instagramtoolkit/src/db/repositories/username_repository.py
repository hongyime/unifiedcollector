"""UsernameRepository — replaces UsernameDatabase JSON persistence."""
from __future__ import annotations

import json
import time

from ..manager import DatabaseManager


class UsernameRepository:
    """Repository for username records and per-account following status."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def add_username(
        self,
        username: str,
        source_account: str,
        metadata: dict | None = None,
    ) -> bool:
        """Insert *username* if it doesn't exist. Returns True on insert, False if duplicate."""
        existing = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if existing is not None:
            return False
        now = time.time()
        self._db.execute(
            """
            INSERT INTO usernames
                (username, source_account, added_ts, metadata_json, created_at)
            VALUES (?,?,?,?,?)
            """,
            (username, source_account, now, json.dumps(metadata or {}), now),
        )
        return True

    def get_by_source(self, source_account: str) -> list[dict]:
        """Return all username records for *source_account*."""
        return self._db.fetchall(
            "SELECT * FROM usernames WHERE source_account=? ORDER BY added_ts",
            (source_account,),
        )

    def get_all(self) -> list[dict]:
        """Return all username records ordered by added_ts."""
        return self._db.fetchall("SELECT * FROM usernames ORDER BY added_ts")

    def update_metadata(self, username: str, metadata: dict) -> bool:
        """Merge *metadata* into the existing metadata_json. Returns False if not found."""
        row = self._db.fetchone(
            "SELECT metadata_json FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        existing = json.loads(row["metadata_json"] or "{}")
        existing.update(metadata)
        self._db.execute(
            "UPDATE usernames SET metadata_json=? WHERE username=?",
            (json.dumps(existing), username),
        )
        return True

    def update_last_accessed(self, username: str) -> bool:
        """Update last_accessed_ts to now. Returns False if not found."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        self._db.execute(
            "UPDATE usernames SET last_accessed_ts=? WHERE username=?",
            (time.time(), username),
        )
        return True

    def update_following_status(
        self, username: str, account: str, following: bool
    ) -> bool:
        """Upsert the following status for (username, account). Returns False if username not found."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        self._db.execute(
            """
            INSERT INTO username_following_status
                (username, account_name, is_following, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(username, account_name) DO UPDATE SET
                is_following = excluded.is_following,
                updated_at   = excluded.updated_at
            """,
            (username, account, 1 if following else 0, time.time()),
        )
        return True

    def remove(self, username: str) -> bool:
        """Delete *username* and cascade-delete following_status rows. Returns False if not found."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        # Cascade is handled by FK ON DELETE CASCADE (foreign_keys=ON)
        self._db.execute("DELETE FROM usernames WHERE username=?", (username,))
        return True

    def exists(self, username: str) -> bool:
        """Return True if *username* is in the database."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        return row is not None


__all__ = ["UsernameRepository"]


