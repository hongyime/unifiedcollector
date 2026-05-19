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

    async def load_progress(self):
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_processed_id, last_processed_at, status FROM service_cursors WHERE service = $1",
                self._service,
            )
            if row:
                self.last_processed_id = row["last_processed_id"]
                self.last_processed_at = row["last_processed_at"]
                self.status = row["status"] or "idle"
                logger.info(
                    "Loaded cursor for %s — last_id=%s status=%s",
                    self._service, self.last_processed_id, self.status,
                )
            else:
                await conn.execute(
                    "INSERT INTO service_cursors (service, status) VALUES ($1, 'idle')",
                    self._service,
                )
                logger.info("Created new cursor row for %s", self._service)

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
