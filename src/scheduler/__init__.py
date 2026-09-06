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
        # 15-minute delta status update (Feature 2). Independent of the hourly
        # digest so it can be disabled with STATUS_DELTA_INTERVAL_MINUTES=0.
        # NEW DEFAULT: 0 (disabled) — was 15. The hourly digest already covers it.
        self._status_delta_minutes = env_int("STATUS_DELTA_INTERVAL_MINUTES", 0, min_value=0)
        self._last_status_delta = 0.0  # monotonic; 0 forces on first tick
        # Identity reconciliation cadence (P2 review §3). 0 disables.
        self._reconcile_hours = env_int("RECONCILE_INTERVAL_HOURS", 12, min_value=0)
        self._last_reconcile = 0.0  # monotonic; 0 forces a run on first tick
        # Cookie-health check cadence (no untested cookies). 0 disables.
        self._cookie_check_hours = env_int("COOKIE_CHECK_INTERVAL_HOURS", 6, min_value=0)
        self._last_cookie_check = 0.0  # 0 forces a check on first tick

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
            # Currently: HeartbeatHandler. Follow-up steps will move the
            # remaining _maybe_* gates into this same registry.
            await self._run_periodic_handlers()
            await self._maybe_status_delta()
            await self._maybe_reconcile_identities()
            await self._maybe_check_cookies()

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

    async def _maybe_status_delta(self):
        """Fire the 15-minute delta status update (Feature 2).

        Independent from _maybe_heartbeat: the hourly digest keeps its
        cadence and content; this is a smaller supplementary tick that
        reports only what changed since the previous delta. Persists the
        last-tick timestamp in service_cursors so a scheduler restart
        won't double-send. 0 minutes disables the feature entirely.
        Wrapped so a failure never disturbs scheduling.
        """
        interval_minutes = getattr(self, "_status_delta_minutes", 0)
        if interval_minutes <= 0:
            return
        import time as _time
        now = _time.monotonic()
        if now - self._last_status_delta < interval_minutes * 60:
            return
        self._last_status_delta = now
        try:
            from src.notifications import alerts
            snapshot = await self._build_status_delta(interval_minutes)
            if snapshot is None:
                return  # not yet due per persisted cursor
            await alerts.notify_status_delta(snapshot)
        except Exception as e:
            logger.warning("status delta failed: %s", e)

    async def _maybe_reconcile_identities(self):
        """Merge fragmented social_users rows (username-keyed -> id-keyed) on the
        first tick, then every N hours. 0 disables. Fail-soft: never disturbs
        scheduling. See src/core/identity_reconcile.py."""
        if getattr(self, "_reconcile_hours", 0) <= 0:
            return
        import time as _time
        now = _time.monotonic()
        if now - self._last_reconcile < self._reconcile_hours * 3600:
            return
        self._last_reconcile = now
        try:
            from src.core.identity_reconcile import reconcile_social_users
            await reconcile_social_users(self.pool)
        except Exception as e:
            logger.warning("identity reconcile failed: %s", e)

    # Per-source newest-activity freshness — the ACCURATE liveness signal, read
    # from the real data tables. Delegates to the canonical FRESHNESS table in
    # src.core.source_freshness so scheduler alerts, watchdog restarts, and the
    # dashboard freshness UI all read the SAME per-source query set. Previously
    # this was a hand-maintained duplicate that could drift: e.g. this file used
    # to check only `media_items` for instagram/tiktok/lemon8, so a fresh
    # profile-metadata sweep with no new media would still look stale in the
    # Telegram heartbeat while the dashboard (which already used compute_liveness)
    # reported it live. Text-heavy realtime sources (whatsapp/beeper/telegram)
    # are unchanged — they read their per-source message tables, never
    # media_items.
    _FRESHNESS: list[tuple[str, str, int]] = _CANONICAL_FRESHNESS

    async def _maybe_check_cookies(self):
        """Actively test every cookie's validity on the first tick, then every N
        hours, so the dashboard never shows 'untested'. Fail-soft. IG is gated off
        inside the checker (collector-driven). See src/core/cookie_health.py."""
        if getattr(self, "_cookie_check_hours", 0) <= 0:
            return
        import time as _time
        now = _time.monotonic()
        if now - self._last_cookie_check < self._cookie_check_hours * 3600:
            return
        self._last_cookie_check = now
        try:
            from src.core.cookie_health import check_all_cookies
            await check_all_cookies(self.pool)
        except Exception as e:
            logger.warning("cookie health check failed: %s", e)

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

    async def _gc_collection_runs(self):
        """P3-7: retention GC for collection_runs.

        The table has no consumer and grew unbounded (307+ aborted rows). Keep
        recent history for the dashboard run-view but prune anything older than
        the retention window. Runs at most hourly (gated by _last_gc).
        """
        import time as _time
        now = _time.monotonic()
        if now - getattr(self, "_last_gc", 0) < 3600:
            return
        self._last_gc = now
        retention_days = env_int("COLLECTION_RUNS_RETENTION_DAYS", 7, min_value=1)
        try:
            async with self.pool.acquire() as conn:
                deleted = await conn.fetchval(
                    "WITH d AS (DELETE FROM collection_runs "
                    "WHERE COALESCE(completed_at, started_at) "
                    "      < NOW() - ($1 || ' days')::interval "
                    "RETURNING 1) SELECT COUNT(*) FROM d",
                    str(retention_days),
                )
            if deleted:
                logger.info("collection_runs GC: pruned %d rows older than %dd",
                            deleted, retention_days)
        except Exception:
            logger.warning("collection_runs GC failed", exc_info=True)

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

        # P3-7: prune old collection_runs (self-gated to hourly).
        await self._gc_collection_runs()
        # Build social graph edges from WhatsApp co-group/DM data (self-gated to 30 min).
        await self._build_graph_edges()
        # OSS enrichment automation (self-gated, env-tunable intervals).
        # Each is idempotent and best-effort — failures are logged but never
        # disturb the main schedule loop.
        await self._maybe_seed_recon_targets()
        await self._maybe_run_phone_intel()
        # Health-alert ticks (INTR-002, REL-004, REL-005 / INTR-003).
        # Each is self-gated and idempotent; a failure is logged and never
        # disturbs the main schedule loop.
        await self._maybe_alert_bridge_unpaired()
        await self._maybe_alert_realtime_feed_failed()
        await self._maybe_alert_watchdog_stale()
        # NOTE: maigret FP-blocklist refresh runs INSIDE the recon worker
        # (src/recon_spiderfoot_service.py::_fp_blocklist_refresh_loop) because
        # this scheduler container does not have the ``maigret`` binary on
        # PATH.  See that module for the periodic loop.

    async def _build_graph_edges(self):
        """Compute social graph edges from messaging membership into graph_edges.

        Co-group: pairs of users who both sent messages in the same WhatsApp group.
        Weight = number of shared groups.

        DM: users who sent direct messages to another user.

        Telegram: pairs of users observed in the same small Telegram groups.

        Self-gated; default every 6 hours because the co-group upsert touches a
        large derived pair set and is not needed minute-by-minute.
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("GRAPH_EDGES_BUILD_INTERVAL_SECONDS", 21600, min_value=1800)
        max_group_senders = env_int("GRAPH_EDGES_MAX_GROUP_SENDERS", 80, min_value=2)
        max_telegram_group_members = env_int("GRAPH_EDGES_MAX_TELEGRAM_GROUP_MEMBERS", 40, min_value=2)
        if now - getattr(self, "_last_graph_build", 0) < interval:
            return
        self._last_graph_build = now
        try:
            async with self.pool.acquire() as conn:
                # Co-group edges: distinct (chat, sender) pairs joined against
                # themselves. Very large groups are intentionally excluded: they
                # are weak OSINT evidence and create O(n²) edge explosions.
                inserted_cg = await conn.fetchval("""
                    WITH group_members AS (
                        SELECT
                            wm.chat_id,
                            wm.sender_id,
                            MIN(wm.timestamp) AS first_seen_at,
                            MAX(wm.timestamp) AS last_seen_at
                        FROM whatsapp_messages wm
                        JOIN whatsapp_chats wc ON wm.chat_id = wc.id
                        WHERE wm.sender_id IS NOT NULL AND wc.is_group = true
                        GROUP BY wm.chat_id, wm.sender_id
                    ),
                    eligible_groups AS (
                        SELECT chat_id
                        FROM group_members
                        GROUP BY chat_id
                        HAVING COUNT(*) BETWEEN 2 AND $1
                    ),
                    co_group AS (
                        SELECT
                            gm1.sender_id AS sender1,
                            gm2.sender_id AS sender2,
                            COUNT(DISTINCT gm1.chat_id) AS shared_groups,
                            MIN(LEAST(gm1.first_seen_at, gm2.first_seen_at)) AS first_seen_at,
                            MAX(GREATEST(gm1.last_seen_at, gm2.last_seen_at)) AS last_seen_at
                        FROM group_members gm1
                        JOIN group_members gm2
                            ON gm1.chat_id = gm2.chat_id
                            AND gm1.sender_id < gm2.sender_id
                        JOIN eligible_groups eg ON eg.chat_id = gm1.chat_id
                        GROUP BY gm1.sender_id, gm2.sender_id
                    ),
                    upserted AS (
                        INSERT INTO graph_edges
                            (source, source_user, target_user, edge_type, weight,
                             first_seen_at, last_seen_at)
                        SELECT
                            'whatsapp',
                            u1.platform_user_id,
                            u2.platform_user_id,
                            'co_group',
                            cg.shared_groups::integer,
                            COALESCE(cg.first_seen_at, NOW()),
                            COALESCE(cg.last_seen_at, NOW())
                        FROM co_group cg
                        JOIN whatsapp_users u1 ON cg.sender1 = u1.id
                        JOIN whatsapp_users u2 ON cg.sender2 = u2.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET
                            weight = EXCLUDED.weight,
                            last_seen_at = GREATEST(graph_edges.last_seen_at, EXCLUDED.last_seen_at)
                        WHERE graph_edges.weight IS DISTINCT FROM EXCLUDED.weight
                           OR graph_edges.last_seen_at < EXCLUDED.last_seen_at
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, max_group_senders, timeout=180)

                # DM edges: who sent messages in which 1:1 chat.
                inserted_dm = await conn.fetchval("""
                    WITH dm_senders AS (
                        SELECT DISTINCT
                            wm.sender_id,
                            wc.platform_chat_id AS target_jid
                        FROM whatsapp_messages wm
                        JOIN whatsapp_chats wc ON wm.chat_id = wc.id
                        WHERE wc.is_group = false
                          AND wm.sender_id IS NOT NULL
                          AND wc.platform_chat_id LIKE '%@s.whatsapp.net'
                    ),
                    upserted AS (
                        INSERT INTO graph_edges
                            (source, source_user, target_user, edge_type, weight,
                             first_seen_at, last_seen_at)
                        SELECT
                            'whatsapp',
                            u.platform_user_id,
                            ds.target_jid,
                            'dm',
                            1,
                            NOW(),
                            NOW()
                        FROM dm_senders ds
                        JOIN whatsapp_users u ON ds.sender_id = u.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET last_seen_at = NOW()
                        WHERE graph_edges.last_seen_at < NOW() - interval '1 hour'
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, timeout=180)

                inserted_tg_cg = await conn.fetchval("""
                    WITH group_members AS (
                        SELECT
                            tm.chat_id,
                            tm.user_id,
                            MIN(COALESCE(tm.joined_at, tm.last_seen_at, tm.refreshed_at, NOW())) AS first_seen_at,
                            MAX(COALESCE(tm.last_seen_at, tm.refreshed_at, tm.joined_at, NOW())) AS last_seen_at
                        FROM telegram_chat_members tm
                        JOIN telegram_chats tc ON tm.chat_id = tc.id
                        WHERE tc.type = 'group'
                          AND (tc.members_count IS NULL
                               OR tc.members_count = 0
                               OR tc.members_count <= $1)
                        GROUP BY tm.chat_id, tm.user_id
                    ),
                    eligible_groups AS (
                        SELECT chat_id
                        FROM group_members
                        GROUP BY chat_id
                        HAVING COUNT(*) BETWEEN 2 AND $1
                    ),
                    co_group AS (
                        SELECT
                            gm1.user_id AS sender1,
                            gm2.user_id AS sender2,
                            COUNT(DISTINCT gm1.chat_id) AS shared_groups,
                            MIN(LEAST(gm1.first_seen_at, gm2.first_seen_at)) AS first_seen_at,
                            MAX(GREATEST(gm1.last_seen_at, gm2.last_seen_at)) AS last_seen_at
                        FROM group_members gm1
                        JOIN group_members gm2
                            ON gm1.chat_id = gm2.chat_id
                            AND gm1.user_id < gm2.user_id
                        JOIN eligible_groups eg ON eg.chat_id = gm1.chat_id
                        GROUP BY gm1.user_id, gm2.user_id
                    ),
                    upserted AS (
                        INSERT INTO graph_edges
                            (source, source_user, target_user, edge_type, weight,
                             first_seen_at, last_seen_at)
                        SELECT
                            'telegram',
                            u1.platform_user_id,
                            u2.platform_user_id,
                            'co_group',
                            cg.shared_groups::integer,
                            COALESCE(cg.first_seen_at, NOW()),
                            COALESCE(cg.last_seen_at, NOW())
                        FROM co_group cg
                        JOIN telegram_users u1 ON cg.sender1 = u1.id
                        JOIN telegram_users u2 ON cg.sender2 = u2.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET
                            weight = EXCLUDED.weight,
                            last_seen_at = GREATEST(graph_edges.last_seen_at, EXCLUDED.last_seen_at)
                        WHERE graph_edges.weight IS DISTINCT FROM EXCLUDED.weight
                           OR graph_edges.last_seen_at < EXCLUDED.last_seen_at
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, max_telegram_group_members, timeout=180)

            logger.info(
                "graph_edges build: whatsapp_co_group=%d whatsapp_dm=%d telegram_co_group=%d max_group_senders=%d max_telegram_group_members=%d",
                inserted_cg or 0,
                inserted_dm or 0,
                inserted_tg_cg or 0,
                max_group_senders,
                max_telegram_group_members,
            )
        except Exception:
            logger.warning("graph_edges build failed", exc_info=True)

    # ---- OSS-enrichment automation ticks (self-gated) ----

    async def _maybe_seed_recon_targets(self):
        """Periodically enqueue username targets from social_users -> recon_targets.

        The recon worker (unifiedcollector_spiderfoot) continuously drains
        recon_targets and, when scope.modules=['maigret'] (or
        RECON_USERNAME_ENGINE=maigret env), runs maigret against each. So all
        we need here is to keep the queue fed. Idempotent by design: the
        underlying seed path relies on ``queue_recon_target`` dedupe.

        Default cadence 6h. Bound per-cycle so the queue fills gradually
        rather than one giant dump.
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("RECON_SEED_INTERVAL_SECONDS", 21600, min_value=300)
        per_source = env_int("RECON_SEED_PER_SOURCE_LIMIT", 200, min_value=1)
        total = env_int("RECON_SEED_TOTAL_LIMIT", 2000, min_value=1)
        if now - getattr(self, "_last_recon_seed", 0) < interval:
            return
        self._last_recon_seed = now
        try:
            from src.core.recon_seed import seed_recon_targets_from_collector
            async with self.pool.acquire() as conn:
                report = await seed_recon_targets_from_collector(
                    conn,
                    include_domains=False,
                    include_urls=False,
                    include_usernames=True,
                    per_source_limit=per_source,
                    total_limit=total,
                    priority=7,
                    dry_run=False,
                )
            logger.info(
                "recon_seed tick: candidates=%s queued=%s skipped=%s",
                report.get("candidates"), report.get("queued"), report.get("skipped"),
            )
        except Exception:
            logger.warning("recon_seed tick failed", exc_info=True)

    async def _maybe_run_phone_intel(self):
        """Periodically enrich WhatsApp phone JIDs via offline phonenumbers lib.

        Enrichment-only — rows land in ``wa_phone_intel`` and are NEVER
        promoted to identity_signals (carrier/region do not identify people).
        Default cadence 12h; bounded batch per cycle.
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("WA_PHONE_INTEL_INTERVAL_SECONDS", 43200, min_value=300)
        batch = env_int("WA_PHONE_INTEL_TICK_LIMIT", 500, min_value=1)
        if now - getattr(self, "_last_phone_intel", 0) < interval:
            return
        self._last_phone_intel = now
        try:
            from src.core.wa_phone_intel import run as wa_phone_intel_run
            stats = await wa_phone_intel_run(batch, dry_run=False)
            logger.info("wa_phone_intel tick: %s", stats)
        except Exception:
            logger.warning("wa_phone_intel tick failed", exc_info=True)

    # ---- Health-alert self-gated ticks (INTR-002, REL-004, REL-005 / INTR-003) ----

    async def _maybe_alert_bridge_unpaired(self):
        """Alert when the WhatsApp bridge has been logging bridge_unpaired 503s.

        `_record_http_event(scope='media_decrypt', status_code=503, ...)` is
        already stamped by ``src/collectors/whatsapp/__init__.py`` on every
        deferred decrypt with a ``bridge_unpaired`` error code. Encrypted
        history messages sit in the DLQ-like deferral state until the bridge
        is re-paired; nothing self-heals, so a scheduler alert is the only
        way the operator sees this without eyeballing container logs.

        Self-gated to `WA_BRIDGE_UNPAIRED_ALERT_INTERVAL_SECONDS` (default 1h)
        so a persistent outage triggers at most one alert per interval.
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("WA_BRIDGE_UNPAIRED_ALERT_INTERVAL_SECONDS", 3600, min_value=300)
        threshold = env_int("WA_BRIDGE_UNPAIRED_ALERT_THRESHOLD", 20, min_value=1)
        window_minutes = env_int("WA_BRIDGE_UNPAIRED_ALERT_WINDOW_MINUTES", 30, min_value=5)
        if now - getattr(self, "_last_bridge_unpaired_alert", 0) < interval:
            return
        try:
            async with self.pool.acquire() as conn:
                count = await conn.fetchval(
                    """
                    SELECT count(*)
                    FROM rate_limit_events
                    WHERE source = 'whatsapp'
                      AND status_code = 503
                      AND metadata->>'error_code' = 'bridge_unpaired'
                      AND created_at > NOW() - ($1 || ' minutes')::interval
                    """,
                    str(window_minutes),
                ) or 0
        except Exception:
            logger.debug("bridge_unpaired probe query failed", exc_info=True)
            return
        if count < threshold:
            return
        self._last_bridge_unpaired_alert = now
        try:
            from src.notifications import telegram as tg
            await tg.send(
                f"⚠️ <b>WhatsApp bridge unpaired</b>\n"
                f"{count} decrypt-deferred events in the last {window_minutes} min "
                f"(HTTP 503 bridge_unpaired).\n"
                f"Encrypted history is not landing. Re-pair the affected bridge "
                f"(<code>docker logs unifiedcollector_wa_bridge_1</code> for the QR)."
            )
            logger.info("bridge_unpaired alert sent (count=%d, window=%dm)", count, window_minutes)
        except Exception:
            logger.warning("bridge_unpaired alert send failed", exc_info=True)

    async def _maybe_alert_realtime_feed_failed(self):
        """Alert when the realtime post-feed's failed queue is non-empty.

        Currently 14 items sit in ``uc:realtime_post_feed:failed`` with no
        automatic drainer. Left alone that queue grows unboundedly with each
        Telegram send failure and the operator has no visibility. Alert at
        threshold; drain remains a manual operator step for now.

        Self-gated to `REALTIME_FAILED_ALERT_INTERVAL_SECONDS` (default 6h).
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("REALTIME_FAILED_ALERT_INTERVAL_SECONDS", 21600, min_value=300)
        threshold = env_int("REALTIME_FAILED_ALERT_THRESHOLD", 10, min_value=1)
        if now - getattr(self, "_last_realtime_failed_alert", 0) < interval:
            return
        try:
            from src.notifications import realtime_feed
            client = await realtime_feed._redis_client()
            if client is None:
                return
            try:
                depth = await client.llen(realtime_feed.FAILED_KEY_DEFAULT)
            finally:
                try:
                    await client.aclose()
                except Exception:
                    pass
        except Exception:
            logger.debug("realtime_feed failed-queue probe failed", exc_info=True)
            return
        if not depth or depth < threshold:
            return
        self._last_realtime_failed_alert = now
        try:
            from src.notifications import telegram as tg
            await tg.send(
                f"⚠️ <b>Realtime post-feed: {depth} failed items</b>\n"
                f"<code>uc:realtime_post_feed:failed</code> has {depth} unsent items. "
                f"Inspect and drain manually if needed."
            )
            logger.info("realtime_failed alert sent (depth=%d)", depth)
        except Exception:
            logger.warning("realtime_failed alert send failed", exc_info=True)

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
