import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Resume cursor backed by the service_cursors table.

    Each collector owns one row keyed by SOURCE_NAME. On startup, load_progress
    reads the last_processed_id so collection can resume where it left off.
    On graceful shutdown, save_progress persists the cursor.
    """

    def __init__(self, service: str, pool=None):
        self._service = service
        self._pool = pool
        self.last_processed_id: str | None = None
        self.last_processed_at: datetime | None = None
        self.status: str = "idle"

    def set_pool(self, pool):
        self._pool = pool

    # --- chat-scoped cursors -------------------------------------------------
    #
    # Chat progress lives in dedicated service_cursors rows keyed
    # "<service>:chat:<chat_id>" whose last_processed_id is always a numeric
    # message id. The service-level row is chat-ambiguous and must never be
    # used as a per-chat resume point.

    def chat_cursor_key(self, chat_id: str) -> str:
        """service_cursors primary key for one chat's numeric cursor."""
        return f"{self._service}:chat:{chat_id}"

    async def load_chat_progress(self, chat_id: str) -> int | None:
        """Return the chat's numeric message cursor, or None when absent.

        Non-numeric values (legacy/corrupt rows) are ignored and reported as
        None; callers must treat None as "no trusted cursor" and schedule a
        full backfill for that chat instead of silently resuming at 0.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_processed_id FROM service_cursors WHERE service = $1",
                self.chat_cursor_key(chat_id),
            )
        raw = row["last_processed_id"] if row else None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def save_chat_progress(self, chat_id: str, message_id: int) -> None:
        """Upsert one chat's numeric message cursor (atomic)."""
        now = datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO service_cursors
                    (service, last_processed_id, last_processed_at, status)
                VALUES ($1, $2, $3, 'idle')
                ON CONFLICT (service) DO UPDATE
                SET last_processed_id = EXCLUDED.last_processed_id,
                    last_processed_at = EXCLUDED.last_processed_at
                """,
                self.chat_cursor_key(chat_id),
                str(message_id),
                now,
            )

    async def touch(self, status: str | None = None):
        """Heartbeat the service row without advancing its cursor.

        Advances last_processed_at only; last_processed_id is never written
        here, so it can never be set to a chat target string. Per-chat
        progress belongs to chat-scoped rows via save_chat_progress.
        """
        self.last_processed_at = datetime.now(timezone.utc)
        if status is not None:
            self.status = status
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE service_cursors
                SET last_processed_at = $2, status = $3
                WHERE service = $1
                """,
                self._service,
                self.last_processed_at,
                self.status,
            )

    async def load_progress(self):
        async with self._pool.acquire() as conn:
            # Atomic upsert avoids the TOCTOU race where two workers both
            # observe a missing row and both INSERT.
            row = await conn.fetchrow(
                """
                INSERT INTO service_cursors (service, status)
                VALUES ($1, 'idle')
                ON CONFLICT (service) DO UPDATE SET service = EXCLUDED.service
                RETURNING last_processed_id, last_processed_at, status
                """,
                self._service,
            )
            self.last_processed_id = row["last_processed_id"]
            self.last_processed_at = row["last_processed_at"]
            self.status = row["status"] or "idle"
            logger.info(
                "Loaded cursor for %s — last_id=%s status=%s",
                self._service, self.last_processed_id, self.status,
            )

    async def save_progress(self, last_id: str | None = None, status: str | None = None):
        if last_id is not None:
            self.last_processed_id = last_id
            self.last_processed_at = datetime.now(timezone.utc)
        if status is not None:
            self.status = status

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE service_cursors
                SET last_processed_id = $2,
                    last_processed_at = $3,
                    status = $4
                WHERE service = $1
                """,
                self._service,
                self.last_processed_id,
                self.last_processed_at,
                self.status,
            )

    async def mark_running(self):
        await self.save_progress(status="running")

    async def mark_idle(self):
        await self.save_progress(status="idle")

    async def reset_if_stale(self):
        """On startup, reset status from 'running' to 'idle' (crash recovery)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE service_cursors SET status = 'idle' WHERE service = $1 AND status = 'running'",
                self._service,
            )
            if result != "UPDATE 0":
                logger.info("Reset stale running status for %s", self._service)
