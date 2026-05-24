"""ProfileAccessRepository — replaces ProfileAccessTracker JSON I/O."""
from __future__ import annotations

import json
import time
from typing import Any

from ..manager import DatabaseManager


class ProfileAccessRepository:
    """Repository for profile access attempt tracking."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def record_attempt(
        self,
        target: str,
        account: str,
        can_access: bool,
        is_public: bool | None,
        is_followed: bool,
        error: str | None = None,
    ) -> None:
        """Insert an access attempt row and upsert the summary in one transaction."""
        now = time.time()
        with self._db.get_connection() as conn:
            # Insert attempt row
            conn.execute(
                """
                INSERT INTO profile_access_attempts
                    (target_username, accessing_account, can_access, is_public,
                     is_followed, error_msg, attempt_ts)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    target,
                    account,
                    1 if can_access else 0,
                    None if is_public is None else (1 if is_public else 0),
                    1 if is_followed else 0,
                    error,
                    now,
                ),
            )

            # Fetch existing summary
            cursor = conn.execute(
                "SELECT * FROM profile_access_summary WHERE username=?", (target,)
            )
            row = cursor.fetchone()

            if row is None:
                # New summary row
                accessible_by = json.dumps([account] if can_access else [])
                conn.execute(
                    """
                    INSERT INTO profile_access_summary
                        (username, is_public, last_checked_ts, last_successful_ts,
                         total_attempts, known_accessible_by_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        target,
                        None if is_public is None else (1 if is_public else 0),
                        now,
                        now if can_access else None,
                        1,
                        accessible_by,
                    ),
                )
            else:
                # Update existing summary
                existing = dict(row)
                known = json.loads(existing.get("known_accessible_by_json") or "[]")
                if can_access and account not in known:
                    known.append(account)
                new_is_public = existing.get("is_public")
                if is_public is not None:
                    new_is_public = 1 if is_public else 0
                conn.execute(
                    """
                    UPDATE profile_access_summary SET
                        is_public               = ?,
                        last_checked_ts         = ?,
                        last_successful_ts      = CASE WHEN ? THEN ? ELSE last_successful_ts END,
                        total_attempts          = total_attempts + 1,
                        known_accessible_by_json = ?
                    WHERE username = ?
                    """,
                    (
                        new_is_public,
                        now,
                        1 if can_access else 0,
                        now,
                        json.dumps(known),
                        target,
                    ),
                )

    def get_profile_summary(self, username: str) -> dict:
        """Return the summary dict for *username*, or an empty default."""
        row = self._db.fetchone(
            "SELECT * FROM profile_access_summary WHERE username=?", (username,)
        )
        if row is None:
            return {
                "status": "unknown",
                "accessible_by": [],
                "is_public": None,
                "last_checked": None,
                "total_attempts": 0,
            }
        known = json.loads(row.get("known_accessible_by_json") or "[]")
        return {
            "status": "tracked",
            "is_public": row.get("is_public"),
            "accessible_by": known,
            "last_checked": row.get("last_checked_ts"),
            "last_successful_ts": row.get("last_successful_ts"),
            "total_attempts": row.get("total_attempts", 0),
        }

    def get_accessible_accounts(self, username: str) -> list[str]:
        """Return accounts known to be able to access *username*."""
        row = self._db.fetchone(
            "SELECT known_accessible_by_json FROM profile_access_summary WHERE username=?",
            (username,),
        )
        if row is None:
            return []
        return json.loads(row.get("known_accessible_by_json") or "[]")

    def get_best_account(self, username: str, available: list[str]) -> str | None:
        """Return the available account that most recently successfully accessed *username*."""
        if not available:
            return None
        # Find the most recent successful attempt for this username from an available account
        placeholders = ",".join("?" * len(available))
        row = self._db.fetchone(
            f"""
            SELECT accessing_account FROM profile_access_attempts
            WHERE target_username=? AND can_access=1
              AND accessing_account IN ({placeholders})
            ORDER BY attempt_ts DESC
            LIMIT 1
            """,
            (username, *available),
        )
        return row["accessing_account"] if row else None

    def cleanup_old_attempts(self, days: int = 30) -> int:
        """Delete attempt rows older than *days* days. Returns deleted count."""
        cutoff = time.time() - days * 86400
        cursor = self._db.execute(
            "DELETE FROM profile_access_attempts WHERE attempt_ts < ?", (cutoff,)
        )
        return cursor.rowcount

    def cleanup_inactive_profiles(self, days: int = 30) -> int:
        """Remove private profiles not checked in *days* days from summary."""
        cutoff = time.time() - days * 86400
        cursor = self._db.execute(
            """
            DELETE FROM profile_access_summary
            WHERE (is_public = 0 OR is_public IS NULL)
              AND (last_checked_ts IS NULL OR last_checked_ts < ?)
            """,
            (cutoff,),
        )
        return cursor.rowcount

    def get_statistics(self) -> dict:
        """Return aggregate statistics across all tracked profiles."""
        row = self._db.fetchone(
            """
            SELECT
                COUNT(*) AS total_attempts,
                SUM(can_access) AS successful_attempts
            FROM profile_access_attempts
            """
        )
        profiles_row = self._db.fetchone(
            "SELECT COUNT(*) AS unique_profiles FROM profile_access_summary"
        )
        return {
            "total_attempts": row["total_attempts"] if row else 0,
            "successful_attempts": row["successful_attempts"] if row else 0,
            "unique_profiles": profiles_row["unique_profiles"] if profiles_row else 0,
        }


__all__ = ["ProfileAccessRepository"]


