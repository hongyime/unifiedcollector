"""OperationProgressRepository — replaces ProgressManager JSON I/O."""
from __future__ import annotations

import json
import time
from typing import Any

from ..manager import DatabaseManager


class OperationProgressRepository:
    """Repository for operation progress and batch state."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_progress(
        self,
        operation_id: str,
        username: str,
        status: str,
        details: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Insert or update a progress row for (operation_id, username)."""
        self._db.execute(
            """
            INSERT INTO operation_progress
                (operation_id, username, status, details_json, error_msg, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(operation_id, username) DO UPDATE SET
                status       = excluded.status,
                details_json = excluded.details_json,
                error_msg    = excluded.error_msg,
                updated_at   = excluded.updated_at
            """,
            (
                operation_id,
                username,
                status,
                json.dumps(details or {}),
                error,
                time.time(),
            ),
        )

    def get_status(self, operation_id: str, username: str) -> str | None:
        """Return the current status string, or None if no row exists."""
        row = self._db.fetchone(
            "SELECT status FROM operation_progress WHERE operation_id=? AND username=?",
            (operation_id, username),
        )
        return row["status"] if row else None

    def get_completed(self, operation_id: str) -> list[str]:
        """Return all usernames with status='completed' for this operation."""
        rows = self._db.fetchall(
            "SELECT username FROM operation_progress WHERE operation_id=? AND status='completed'",
            (operation_id,),
        )
        return [r["username"] for r in rows]

    def get_failed(self, operation_id: str) -> list[str]:
        """Return all usernames with status='failed' for this operation."""
        rows = self._db.fetchall(
            "SELECT username FROM operation_progress WHERE operation_id=? AND status='failed'",
            (operation_id,),
        )
        return [r["username"] for r in rows]

    def get_pending(self, operation_id: str) -> list[str]:
        """Return all usernames with status='pending' for this operation."""
        rows = self._db.fetchall(
            "SELECT username FROM operation_progress WHERE operation_id=? AND status='pending'",
            (operation_id,),
        )
        return [r["username"] for r in rows]

    def get_remaining(self, operation_id: str, all_usernames: list[str]) -> list[str]:
        """Return usernames from all_usernames not yet completed or failed."""
        rows = self._db.fetchall(
            """
            SELECT username FROM operation_progress
            WHERE operation_id=? AND status IN ('completed','failed')
            """,
            (operation_id,),
        )
        done = {r["username"] for r in rows}
        return [u for u in all_usernames if u not in done]

    def get_statistics(self, operation_id: str) -> dict:
        """Return counts of pending/completed/failed for this operation."""
        rows = self._db.fetchall(
            """
            SELECT status, COUNT(*) AS cnt
            FROM operation_progress
            WHERE operation_id=?
            GROUP BY status
            """,
            (operation_id,),
        )
        stats = {"pending": 0, "completed": 0, "failed": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        return stats

    def upsert_batch_state(self, operation_id: str, state: dict) -> None:
        """Insert or update the batch state for an operation."""
        op_type = state.get("operation_type", state.get("current_operation", "general"))
        self._db.execute(
            """
            INSERT INTO batch_state
                (operation_id, operation_type, state_json, started_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(operation_id) DO UPDATE SET
                operation_type = excluded.operation_type,
                state_json     = excluded.state_json,
                updated_at     = excluded.updated_at
            """,
            (operation_id, op_type, json.dumps(state), time.time(), time.time()),
        )

    def get_batch_state(self, operation_id: str) -> dict:
        """Return the batch state dict for an operation, or {} if not found."""
        row = self._db.fetchone(
            "SELECT state_json FROM batch_state WHERE operation_id=?",
            (operation_id,),
        )
        if row is None:
            return {}
        return json.loads(row["state_json"] or "{}")

    def archive_operation(self, operation_id: str) -> None:
        """Delete all progress and batch_state rows for this operation only."""
        with self._db.get_connection() as conn:
            conn.execute(
                "DELETE FROM operation_progress WHERE operation_id=?", (operation_id,)
            )
            conn.execute(
                "DELETE FROM batch_state WHERE operation_id=?", (operation_id,)
            )


__all__ = ["OperationProgressRepository"]


