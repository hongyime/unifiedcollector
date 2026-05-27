import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db.connection import get_pool, close_pool

logger = logging.getLogger(__name__)


class Scheduler:
    """Triggers collection runs on a per-source interval schedule."""

    def __init__(self):
        self.pool = None
        self._stop = asyncio.Event()
        self.check_interval = 60

    async def start(self):
        logger.info("Scheduler starting")
        self.pool = await get_pool()
        await self._init_db()
        await self._register_beeper_if_enabled()
        await self._register_strava_feed_if_enabled()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: self._stop.set())
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._stop.set())

        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Scheduler tick error: %s", e)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_interval)
                break
            except asyncio.TimeoutError:
                pass

        await close_pool()
        logger.info("Scheduler stopped")

    async def _init_db(self):
        schema_dir = Path(__file__).resolve().parent.parent / "db" / "schemas"
        async with self.pool.acquire() as conn:
            for sql_file in sorted(schema_dir.glob("*.sql")):
                await conn.execute(sql_file.read_text())

    async def _register_beeper_if_enabled(self):
        """Register the polymorphic Beeper Desktop Local API collector.

        Gated on `BEEPER_COLLECTOR_ENABLED` + presence of `BEEPER_DESKTOP_API_TOKEN`.
        When both are set, we ensure a `collection_schedules` row exists for
        source='beeper' on a 5-minute cadence — short enough that incremental
        tail catches new messages quickly, long enough not to thrash the
        local API.

        Replaces the prior `_register_matrix_if_enabled` / `_register_matrix_backfill_if_enabled`
        pair from Wave 1 (matrix-nio path). The new Beeper Desktop Local API
        on 127.0.0.1:23373 spans every connected network in one collector,
        so a single schedule replaces the matrix + matrix_backfill duo.
        """
        try:
            from src.collectors.beeper import is_enabled as beeper_enabled
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Beeper collector module unavailable: %s", exc)
            return

        if not beeper_enabled():
            logger.info(
                "Beeper collector disabled (BEEPER_COLLECTOR_ENABLED unset or no token); "
                "skipping schedule registration"
            )
            return

        try:
            # Use 5-minute cadence (interval_hours=1/12 ≈ 5 min). Reuse the
            # existing add_schedule helper which currently takes hours; the
            # collector caps per-cycle work via BEEPER_MAX_CHATS_PER_CYCLE.
            await self.add_schedule("beeper", interval_hours=1)
            logger.info("Beeper collector registered on schedule (every 1h)")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to register beeper schedule: %s", exc)

    async def _register_strava_feed_if_enabled(self):
        """Register a weekly Strava following-feed backfill schedule.

        Gated on STRAVA_FEED_BACKFILL_ENABLED. The collector reads cookies
        and walks /dashboard/feed for `STRAVA_FEED_BACKFILL_DAYS` (default 30)
        days back, upserting any newly-discovered activities into
        strava_activities. Cadence is weekly (168h) — long enough to avoid
        hammering the cookie session; short enough to keep recent feed
        history fresh.
        """
        val = os.environ.get("STRAVA_FEED_BACKFILL_ENABLED", "").strip().lower()
        if val not in {"1", "true", "yes", "on"}:
            logger.info(
                "Strava feed backfill disabled (STRAVA_FEED_BACKFILL_ENABLED unset); "
                "skipping schedule registration"
            )
            return
        try:
            await self.add_schedule("strava_feed_backfill", interval_hours=168)
            logger.info("Strava feed backfill registered on schedule (every 168h)")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to register strava_feed_backfill schedule: %s", exc)

    async def _tick(self):
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            # Serialize multiple scheduler instances per source via advisory lock.
            # We claim each due row in its own transaction with FOR UPDATE SKIP LOCKED
            # so a second scheduler running in parallel just skips the row.
            async with conn.transaction():
                due = await conn.fetch(
                    "SELECT id, source, interval_hours FROM collection_schedules "
                    "WHERE enabled = true AND (next_run IS NULL OR next_run <= $1) "
                    "FOR UPDATE SKIP LOCKED",
                    now,
                )
                for row in due:
                    source = row["source"]
                    interval = row["interval_hours"]
                    logger.info("Schedule triggered for %s", source)

                    await conn.execute(
                        "INSERT INTO collection_runs (source, status) VALUES ($1, 'queued')",
                        source,
                    )
                    await conn.execute(
                        "UPDATE collection_targets SET status = 'pending' "
                        "WHERE source = $1 AND status IN ('completed', 'error')",
                        source,
                    )
                    next_run = now + timedelta(hours=interval)
                    await conn.execute(
                        "UPDATE collection_schedules "
                        "SET last_run = $1, next_run = $2 WHERE id = $3",
                        now, next_run, row["id"],
                    )
                    logger.info("Next run for %s at %s", source, next_run.isoformat())

    async def add_schedule(self, source: str, interval_hours: int = 24):
        if self.pool is None:
            self.pool = await get_pool()
        now = datetime.now(timezone.utc)
        next_run = now + timedelta(hours=interval_hours)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO collection_schedules (source, interval_hours, enabled, next_run) "
                "VALUES ($1, $2, true, $3) "
                "ON CONFLICT (source) DO UPDATE "
                "SET interval_hours = $2, enabled = true, next_run = $3",
                source, interval_hours, next_run,
            )
        logger.info("Schedule set: %s every %dh, next at %s", source, interval_hours, next_run)

    async def remove_schedule(self, source: str):
        if self.pool is None:
            self.pool = await get_pool()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM collection_schedules WHERE source = $1", source,
            )

    async def list_schedules(self) -> list[dict]:
        if self.pool is None:
            self.pool = await get_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM collection_schedules ORDER BY source"
            )
        return [dict(r) for r in rows]


async def run_scheduler():
    scheduler = Scheduler()
    await scheduler.start()
