"""Wave 1 Phase 2 — repository for matrix_backfill_state.

Single-table CRUD around the backfill cursor. Mirrors the style of
`MatrixSyncStateRepository` in matrix_client.py: an asyncpg pool in,
small async methods out, no business logic beyond what the SQL needs.

The driver (`src.core.matrix_backfill.MatrixBackfillDriver`) is the only
expected caller in production; the operator script in
`scripts/run_matrix_backfill.py` reaches in only via the driver.

All write methods are idempotent and tolerate a missing row by upserting.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MatrixBackfillStateRepo:
    """Asyncpg-backed CRUD for the matrix_backfill_state table.

    The pool is whatever the rest of the system uses
    (`src.db.connection.get_pool()`); tests inject a duck-typed stub
    exposing `acquire()` -> async context manager with `.execute /
    .fetch / .fetchrow`.
    """

    def __init__(self, pool: Any) -> None:
        if pool is None:
            raise ValueError("pool is required")
        self.pool = pool

    # ── reads ──────────────────────────────────────────────────────────

    async def get(self, room_id: str) -> Optional[dict]:
        """Return the row for `room_id` as a dict, or None if missing."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT room_id, last_token, earliest_ts,
                       events_fetched, pages_used, done,
                       last_error, last_attempt_at, completed_at
                  FROM matrix_backfill_state
                 WHERE room_id = $1
                """,
                room_id,
            )
        return dict(row) if row else None

    async def fetch_pending(self, limit: int = 100) -> list[dict]:
        """Rooms whose backfill is not yet complete.

        Returns rows where done=FALSE, oldest-attempted first so retry
        pressure is spread evenly. NULL last_attempt_at sorts first
        (NULLS FIRST) so brand-new rooms get attention before we revisit
        ones we just touched.
        """
        if limit <= 0:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT room_id, last_token, earliest_ts,
                       events_fetched, pages_used, done,
                       last_error, last_attempt_at, completed_at
                  FROM matrix_backfill_state
                 WHERE done = FALSE
                 ORDER BY last_attempt_at ASC NULLS FIRST
                 LIMIT $1
                """,
                limit,
            )
        return [dict(r) for r in rows]

    # ── writes ─────────────────────────────────────────────────────────

    async def upsert_progress(
        self,
        room_id: str,
        last_token: Optional[str],
        events_fetched_inc: int = 0,
        pages_inc: int = 0,
        error: Optional[str] = None,
        earliest_ts: Optional[datetime] = None,
    ) -> None:
        """Record progress for a backfill page.

        Increments events_fetched + pages_used (rather than overwriting)
        so partial progress accumulates across resume cycles. `last_token`
        is overwritten with the most recent value (it's the cursor for
        the NEXT page). `error` is stored on `last_error` and cleared
        when None is passed; callers should pass None on success.
        """
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO matrix_backfill_state (
                    room_id, last_token, earliest_ts,
                    events_fetched, pages_used,
                    last_error, last_attempt_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (room_id) DO UPDATE SET
                    last_token       = EXCLUDED.last_token,
                    earliest_ts      = COALESCE(
                        LEAST(matrix_backfill_state.earliest_ts, EXCLUDED.earliest_ts),
                        EXCLUDED.earliest_ts,
                        matrix_backfill_state.earliest_ts
                    ),
                    events_fetched   = matrix_backfill_state.events_fetched + $4,
                    pages_used       = matrix_backfill_state.pages_used + $5,
                    last_error       = EXCLUDED.last_error,
                    last_attempt_at  = EXCLUDED.last_attempt_at
                """,
                room_id,
                last_token,
                earliest_ts,
                int(events_fetched_inc or 0),
                int(pages_inc or 0),
                error,
                now,
            )

    async def mark_done(self, room_id: str) -> None:
        """Set done=TRUE + completed_at=NOW() for a room.

        If the row doesn't yet exist (empty room hit on first page),
        we create it pre-marked-done so future cycles skip cleanly.
        """
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO matrix_backfill_state (
                    room_id, done, last_attempt_at, completed_at
                )
                VALUES ($1, TRUE, $2, $2)
                ON CONFLICT (room_id) DO UPDATE SET
                    done            = TRUE,
                    completed_at    = COALESCE(matrix_backfill_state.completed_at, $2),
                    last_attempt_at = $2,
                    last_error      = NULL
                """,
                room_id,
                now,
            )


__all__ = ["MatrixBackfillStateRepo"]
