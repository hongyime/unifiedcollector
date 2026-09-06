import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from src.db.connection import get_pool, close_pool
from src.core.env import env_int, env_float
from src.core.source_freshness import FRESHNESS as _CANONICAL_FRESHNESS

# Status/heartbeat snapshot assembly is a pure reporting concern — moved to
# status_builder.py in the LOGIC-005 refactor (docs/plans/scheduler-refactor.md).
# Scheduler._build_status / _build_status_delta / _delta_* remain as thin
# instance-method delegates so existing callers and tests keep working.
from src.scheduler import status_builder as _status_builder
# Startup / shutdown lifecycle helpers (DB init, conditional collector
# registration, Telegram notifications) live in startup.py. Same delegate
# pattern: instance methods on Scheduler forward here.
from src.scheduler import startup as _startup

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
        await _startup.init_db(self.pool)
        await _startup.register_beeper_if_enabled(self)
        await _startup.register_strava_feed_if_enabled(self)

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: self._stop.set())
            except NotImplementedError:
                signal.signal(sig, lambda *_: self._stop.set())

        # Telegram notifications (best-effort; never block/raise the scheduler).
        await _startup.notify_startup_safe()
        # NOTE: Per-handler state (last-fire timestamps + interval env-reads) is
        # now owned by the handler classes in `src/scheduler/handlers/`. Legacy
        # `_maybe_*` shims that remain below still cache state on `self` — they
        # will be extracted in follow-up steps (docs/plans/scheduler-refactor.md
        # steps 6-15).

        # Collector callback bot: getUpdates long-poll for [Restart]/[Ignore]
        # decision-card buttons. Runs as a single asyncio Task piggybacking on
        # this scheduler loop (mirrors analyzer merge_bot pattern). Only starts
        # if a bot token is configured; safe to leave off in dev.
        self._collector_bot_task = None
        if os.getenv("COLLECTOR_TELEGRAM_BOT_ENABLED", "1") == "1":
            _tok = os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
            if _tok:
                try:
                    from src.notifications.collector_bot import run_callback_poller
                    self._collector_bot_task = asyncio.create_task(
                        run_callback_poller(), name="collector_bot_poller",
                    )
                    logger.info("collector-bot: callback poller task created (COLLECTOR_TELEGRAM_BOT_ENABLED=1)")
                except Exception:
                    logger.exception("collector-bot: failed to start callback poller (non-fatal)")
            else:
                logger.info("collector-bot: no bot token configured — poller disabled")
        else:
            logger.info("collector-bot: COLLECTOR_TELEGRAM_BOT_ENABLED=0 — poller disabled")

        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Scheduler tick error: %s", e)

            # Registry-based dispatch for extracted handlers (LOGIC-005).
            # Currently: HeartbeatHandler, StatusDeltaHandler. Follow-up steps
            # will move the remaining _maybe_* gates into this same registry.
            await self._run_periodic_handlers()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_interval)
                break
            except asyncio.TimeoutError:
                pass

        await _startup.notify_shutdown_safe()
        # Cancel the collector-bot poller before closing the DB pool so the
        # long-poll HTTP is torn down cleanly (mirrors analyzer merge_bot).
        if getattr(self, "_collector_bot_task", None) is not None:
            try:
                self._collector_bot_task.cancel()
                await asyncio.gather(self._collector_bot_task, return_exceptions=True)
            except Exception:
                logger.debug("collector-bot: task cancel/await failed (non-fatal)", exc_info=True)
        await close_pool()
        logger.info("Scheduler stopped")

    # --- Telegram status notifications (additive, fail-safe) ---

    async def _notify_startup_safe(self):
        """Delegate to startup.notify_startup_safe; see startup.py."""
        await _startup.notify_startup_safe()

    async def _notify_shutdown_safe(self):
        """Delegate to startup.notify_shutdown_safe; see startup.py."""
        await _startup.notify_shutdown_safe()

    async def _run_periodic_handlers(self):
        """Registry-driven dispatch for extracted periodic handlers.

        Iterates ``handlers.HANDLERS`` in order, calling ``should_run(ctx)`` then
        ``run(ctx)``. Fault isolation: one failing handler must not stop the
        others. Legacy ``_maybe_*`` methods on the class are still called
        separately in ``start()`` until they are extracted in follow-up steps
        (docs/plans/scheduler-refactor.md steps 6-15).
        """
        from src.scheduler.handlers import HANDLERS, SchedulerContext
        from src.notifications import alerts as _notifier
        ctx = SchedulerContext(
            pool=self.pool,
            now=datetime.now(timezone.utc),
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

    # Per-source newest-activity freshness — the ACCURATE liveness signal, read
    # from the real data tables. Delegates to the canonical FRESHNESS table in
    # src.core.source_freshness so scheduler alerts, watchdog restarts, and the
    # dashboard freshness UI all read the SAME per-source query set. Retained
    # as a class attribute for the ``_build_status`` delegate; handlers should
    # import ``FRESHNESS`` directly from ``src.core.source_freshness``.
    _FRESHNESS: list[tuple[str, str, int]] = _CANONICAL_FRESHNESS

    async def _build_status(self) -> dict:
        """Delegate to status_builder.build_status; see status_builder.py.

        Kept as an instance method so existing callers (``_maybe_heartbeat``)
        and tests that instantiate ``Scheduler`` directly continue to work.
        """
        return await _status_builder.build_status(self.pool, self._FRESHNESS)

    # --- 15-minute delta snapshot (Feature 2) -----------------------------

    async def _build_status_delta(self, interval_minutes: int) -> dict | None:
        """Delegate to status_builder.build_status_delta; see status_builder.py."""
        return await _status_builder.build_status_delta(self.pool, interval_minutes)

    async def _delta_per_source_counts(self, conn, since) -> dict[str, dict[str, int]]:
        """Delegate to status_builder.delta_per_source_counts."""
        return await _status_builder.delta_per_source_counts(conn, since)

    async def _delta_new_cooldowns(self, conn, since) -> list[dict]:
        """Delegate to status_builder.delta_new_cooldowns."""
        return await _status_builder.delta_new_cooldowns(conn, since)

    async def _delta_new_dead_sources(self, conn, since) -> list[str]:
        """Delegate to status_builder.delta_new_dead_sources."""
        return await _status_builder.delta_new_dead_sources(conn, since)

    async def _delta_extension_hooks(self, conn) -> list[dict]:
        """Delegate to status_builder.delta_extension_hooks."""
        return await _status_builder.delta_extension_hooks(conn)


    async def _init_db(self):
        """Delegate to startup.init_db; see startup.py."""
        await _startup.init_db(self.pool)

    async def _register_beeper_if_enabled(self):
        """Delegate to startup.register_beeper_if_enabled; see startup.py."""
        await _startup.register_beeper_if_enabled(self)

    async def _register_strava_feed_if_enabled(self):
        """Delegate to startup.register_strava_feed_if_enabled; see startup.py."""
        await _startup.register_strava_feed_if_enabled(self)

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

                    # P1-1: record the run through a real lifecycle instead of
                    # leaving it stuck in 'queued' forever (292 dead rows found).
                    # A schedule tick is a trigger event, not a long-lived job the
                    # worker reports back on, so we open it 'running' and close it
                    # 'completed' once targets are re-armed. completed_at gives the
                    # dashboard a real run history + enables retention GC (P3-7).
                    run_id = await conn.fetchval(
                        "INSERT INTO collection_runs (source, status, started_at) "
                        "VALUES ($1, 'running', NOW()) RETURNING id",
                        source,
                    )
                    # P1-1: only re-pend targets that are NOT actively being
                    # collected. The old query flipped ALL completed/error rows to
                    # pending every tick, which could yank a still-running collector
                    # back to pending mid-cycle. Excluding rows touched within the
                    # interval window protects in-flight work.
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
                        "UPDATE collection_runs "
                        "SET status = 'completed', completed_at = NOW(), "
                        "    items_collected = $2 WHERE id = $1",
                        run_id, rearmed or 0,
                    )
                    next_run = now + timedelta(hours=interval)
                    await conn.execute(
                        "UPDATE collection_schedules "
                        "SET last_run = $1, next_run = $2 WHERE id = $3",
                        now, next_run, row["id"],
                    )
                    logger.info("Next run for %s at %s (re-armed %d targets)",
                                source, next_run.isoformat(), rearmed or 0)

        # P3-7: collection_runs GC + graph_edges build now run via the handler
        # registry (see src/scheduler/handlers/). Only schedule-tick-specific
        # work stays inline here.
        # OSS enrichment automation runs via the handler registry
        # (see src/scheduler/handlers/recon_seed.py, phone_intel.py).
        # Health-alert ticks (INTR-002, REL-004, REL-005 / INTR-003).
        # Each is self-gated and idempotent; a failure is logged and never
        # disturbs the main schedule loop.
        await self._maybe_alert_watchdog_stale()
        # NOTE: maigret FP-blocklist refresh runs INSIDE the recon worker
        # (src/recon_spiderfoot_service.py::_fp_blocklist_refresh_loop) because
        # this scheduler container does not have the ``maigret`` binary on
        # PATH.  See that module for the periodic loop.

    # ---- OSS-enrichment automation ticks (self-gated) ----

    # ---- Health-alert self-gated ticks (INTR-002, REL-004, REL-005 / INTR-003) ----

    async def _maybe_alert_watchdog_stale(self):
        """Escalate persistently-stale sources past the watchdog's own cooldown.

        The freshness watchdog restarts stale realtime containers on a 30-min
        cooldown, and only alerts on the first cycle after entering 'stale'.
        Once its alert cooldown ticks, a persistent failure becomes invisible
        (audit evidence: DM hook stale 35h with 'alert in cooldown' log line).
        This tick catches that class by reading source_health directly and
        emitting a distinct "still stale" alert.

        Self-gated to `WATCHDOG_STALE_ALERT_INTERVAL_SECONDS` (default 6h).
        Uses `updated_at` age > threshold (default 12h) as the escalation gate.
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("WATCHDOG_STALE_ALERT_INTERVAL_SECONDS", 21600, min_value=300)
        threshold_hours = env_int("WATCHDOG_STALE_ALERT_THRESHOLD_HOURS", 12, min_value=1)
        if now - getattr(self, "_last_stale_alert", 0) < interval:
            return
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT source, status, EXTRACT(EPOCH FROM (NOW() - updated_at))/3600 AS age_hours
                    FROM source_health
                    WHERE status IN ('degraded', 'stale', 'unhealthy')
                       OR updated_at < NOW() - ($1 || ' hours')::interval
                    ORDER BY updated_at ASC
                    """,
                    str(threshold_hours),
                )
        except Exception:
            logger.debug("watchdog_stale probe query failed", exc_info=True)
            return
        stale = [
            r for r in rows
            if (r["status"] in ("degraded", "stale", "unhealthy"))
            or (r["age_hours"] and r["age_hours"] > threshold_hours)
        ]
        if not stale:
            return
        self._last_stale_alert = now
        try:
            from src.notifications import telegram as tg
            lines = [f"⚠️ <b>Watchdog still-stale escalation</b>"]
            for r in stale[:10]:
                lines.append(
                    f"• <code>{r['source']}</code>: {r['status']} "
                    f"(age {r['age_hours']:.1f}h)"
                )
            if len(stale) > 10:
                lines.append(f"… and {len(stale) - 10} more.")
            await tg.send("\n".join(lines))
            logger.info("watchdog_stale escalation alert sent (n=%d)", len(stale))
        except Exception:
            logger.warning("watchdog_stale escalation alert send failed", exc_info=True)


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
