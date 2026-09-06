"""Thin ``Scheduler`` orchestrator (LOGIC-005 step 15).

All periodic work lives in ``src/scheduler/handlers/``; lifecycle helpers
(DB init, conditional collector registration, startup/shutdown Telegram
notifications, collector-bot poller) in ``src/scheduler/startup.py``;
status-snapshot reporting utilities in ``src/scheduler/status_builder.py``.

``_tick`` processes due ``collection_schedules`` rows (schedule-driven work
that must serialize across scheduler instances via ``FOR UPDATE SKIP
LOCKED``) and then iterates the ``handlers.HANDLERS`` registry with a
per-handler try/except so one bad tick cannot stop the others.
"""
import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone

from src.core.env import env_int, env_float
from src.db.connection import close_pool, get_pool
from src.scheduler import startup as _startup

logger = logging.getLogger(__name__)


class Scheduler:
    """Triggers collection runs on a per-source interval schedule."""

    def __init__(self):
        self.pool = None
        self._stop = asyncio.Event()
        self.check_interval = 60
        self._collector_bot_task = None

    async def start(self):
        logger.info("Scheduler starting")
        self.pool = await get_pool()
        await _startup.init_db(self.pool)
        await _startup.register_beeper_if_enabled(self)
        await _startup.register_strava_feed_if_enabled(self)

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: self._stop.set())
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._stop.set())

        await _startup.notify_startup_safe()
        self._collector_bot_task = await _startup.maybe_start_collector_bot()

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

        await self.stop()

    async def stop(self):
        """Send Telegram shutdown notice, cancel bot poller, close pool."""
        await _startup.notify_shutdown_safe()
        if self._collector_bot_task is not None:
            try:
                self._collector_bot_task.cancel()
                await asyncio.gather(self._collector_bot_task, return_exceptions=True)
            except Exception:
                logger.debug("collector-bot: task cancel/await failed (non-fatal)", exc_info=True)
        await close_pool()
        logger.info("Scheduler stopped")

    async def _tick(self):
        from src.notifications import alerts as _notifier
        from src.scheduler.handlers import HANDLERS, SchedulerContext

        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            # FOR UPDATE SKIP LOCKED serializes parallel scheduler instances:
            # a second scheduler skips claimed rows rather than duplicating.
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

                    # P1-1: open the run 'running' + close 'completed' so
                    # collection_runs has real lifecycle history (retention
                    # GC prunes the tail — see GcCollectionRunsHandler).
                    run_id = await conn.fetchval(
                        "INSERT INTO collection_runs (source, status, started_at) "
                        "VALUES ($1, 'running', NOW()) RETURNING id",
                        source,
                    )
                    # P1-1: only re-pend targets whose last_collection_at is
                    # outside the interval window, so a still-running collector
                    # isn't yanked back to pending mid-cycle.
                    rearmed = await conn.fetchval(
                        "WITH upd AS ("
                        "  UPDATE collection_targets SET status = 'pending' "
                        "  WHERE source = $1 AND status IN ('completed', 'error', 'active') "
                        "    AND (last_collection_at IS NULL "
                        "         OR last_collection_at < NOW() - ($2 || ' hours')::interval) "
                        "  RETURNING 1) "
                        "SELECT count(*) FROM upd",
                        source, str(interval),
                    )
                    await conn.execute(
                        "UPDATE collection_runs SET status = 'completed', completed_at = NOW(), "
                        "items_collected = $2 WHERE id = $1",
                        run_id, rearmed or 0,
                    )
                    next_run = now + timedelta(hours=interval)
                    await conn.execute(
                        "UPDATE collection_schedules SET last_run = $1, next_run = $2 WHERE id = $3",
                        now, next_run, row["id"],
                    )
                    logger.info("Next run for %s at %s (re-armed %d targets)",
                                source, next_run.isoformat(), rearmed or 0)

        # Registry-driven periodic handlers. Fault-isolated per handler so a
        # single bad tick cannot stop the others. Handlers own their own
        # last-fire timestamps and env-var reads (PeriodicHandler contract:
        # src/scheduler/handlers/base.py).
        ctx = SchedulerContext(
            pool=self.pool,
            now=now,
            notifier=_notifier,
            stop_event=self._stop,
            get_env_int=env_int,
            get_env_float=env_float,
        )
        for handler in HANDLERS:
            try:
                if await handler.should_run(ctx):
                    await handler.run(ctx)
            except Exception as e:
                logger.warning("periodic handler %s failed: %s", handler.name, e)

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
            await conn.execute("DELETE FROM collection_schedules WHERE source = $1", source)

    async def list_schedules(self) -> list[dict]:
        if self.pool is None:
            self.pool = await get_pool()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM collection_schedules ORDER BY source")
        return [dict(r) for r in rows]


async def run_scheduler():
    scheduler = Scheduler()
    await scheduler.start()
