import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db.connection import get_pool, close_pool
from src.core.env import env_int

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

        # Telegram notifications (best-effort; never block/raise the scheduler).
        await self._notify_startup_safe()
        self._heartbeat_hours = env_int("STATUS_HEARTBEAT_INTERVAL_HOURS", 6, min_value=0)
        self._last_status = 0.0  # monotonic; 0 forces a heartbeat on first tick
        # Identity reconciliation cadence (P2 review §3). 0 disables.
        self._reconcile_hours = env_int("RECONCILE_INTERVAL_HOURS", 12, min_value=0)
        self._last_reconcile = 0.0  # monotonic; 0 forces a run on first tick
        # Cookie-health check cadence (no untested cookies). 0 disables.
        self._cookie_check_hours = env_int("COOKIE_CHECK_INTERVAL_HOURS", 6, min_value=0)
        self._last_cookie_check = 0.0  # 0 forces a check on first tick

        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Scheduler tick error: %s", e)

            await self._maybe_heartbeat()
            await self._maybe_reconcile_identities()
            await self._maybe_check_cookies()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.check_interval)
                break
            except asyncio.TimeoutError:
                pass

        await self._notify_shutdown_safe()
        await close_pool()
        logger.info("Scheduler stopped")

    # --- Telegram status notifications (additive, fail-safe) ---

    async def _notify_startup_safe(self):
        try:
            from src.notifications import alerts
            await alerts.notify_startup()
        except Exception as e:
            logger.warning("notify_startup failed: %s", e)

    async def _notify_shutdown_safe(self):
        try:
            from src.notifications import alerts
            await alerts.notify_shutdown()
        except Exception as e:
            logger.warning("notify_shutdown failed: %s", e)

    async def _maybe_heartbeat(self):
        """Fire the status heartbeat on the first tick, then every N hours.
        0 hours disables. Wrapped so a failure never disturbs scheduling."""
        if getattr(self, "_heartbeat_hours", 0) <= 0:
            return
        import time as _time
        now = _time.monotonic()
        if now - self._last_status < self._heartbeat_hours * 3600:
            return
        self._last_status = now
        try:
            from src.notifications import alerts
            snapshot = await self._build_status()
            await alerts.notify_status(snapshot)
        except Exception as e:
            logger.warning("status heartbeat failed: %s", e)

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
    # from the real data tables (mirrors src/watchdog/freshness.py). The old status
    # derived "quiet" from collection_runs, which the realtime collectors
    # (telegram/whatsapp/beeper) never populate — so they were ALWAYS reported quiet
    # even while actively ingesting. (source, freshness_query, stale_threshold_secs).
    _FRESHNESS: list[tuple[str, str, int]] = [
        ("telegram",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM telegram_messages", 7200),
        ("whatsapp",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM whatsapp_messages", 14400),
        ("beeper",    "SELECT extract(epoch FROM now()-max(ingested_at)) FROM beeper_shadow_messages", 10800),
        ("instagram", "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='instagram'", 172800),
        ("tiktok",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='tiktok'", 172800),
        ("lemon8",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='lemon8'", 172800),
        ("threads",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM threads_posts", 172800),
        ("facebook",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM facebook_posts", 172800),
        ("x",         "SELECT extract(epoch FROM now()-max(collected_at)) FROM x_posts", 172800),
        ("youtube",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM youtube_videos", 172800),
        ("website",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM website_pages", 259200),
        ("github",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM github_commits", 259200),
        ("strava",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM strava_activities", 259200),
        ("search",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM search_results", 259200),
    ]

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
        """Accurate collector-health snapshot for the heartbeat. Every piece is
        independently guarded so a missing table/slow query degrades gracefully."""
        snap: dict = {"ok": True}
        try:
            async with self.pool.acquire() as conn:
                # Operator heartbeats must be quick. Use the planner estimate for
                # all-time media total; exact 24h/hourly counts below stay real.
                try:
                    snap["media_items"] = int(await conn.fetchval(
                        "SELECT reltuples::bigint FROM pg_class WHERE relname='media_items'",
                        timeout=5) or 0)
                    snap["media_items_estimate"] = True
                except Exception:
                    pass

                # Real 24h media ingestion (uses idx_media_collected — fast).
                try:
                    snap["media_24h"] = int(await conn.fetchval(
                        "SELECT count(*) FROM media_items WHERE collected_at > now()-interval '24 hours'",
                        timeout=30) or 0)
                except Exception:
                    pass

                # Real 24h messages across all 3 realtime platforms.
                try:
                    snap["msgs_24h"] = int(await conn.fetchval(
                        """
                        SELECT (SELECT count(*) FROM telegram_messages     WHERE collected_at > now()-interval '24 hours')
                             + (SELECT count(*) FROM whatsapp_messages      WHERE collected_at > now()-interval '24 hours')
                             + (SELECT count(*) FROM beeper_shadow_messages WHERE ingested_at  > now()-interval '24 hours')
                        """, timeout=30) or 0)
                except Exception:
                    pass

                # Current clock-hour ingestion for the Telegram heartbeat. This
                # mirrors the dashboard's early-warning view but keeps the
                # heartbeat payload compact.
                try:
                    content_parts = (
                        ("telegram", "telegram_messages", "collected_at", "messages"),
                        ("whatsapp", "whatsapp_messages", "collected_at", "messages"),
                        ("beeper", "beeper_shadow_messages", "ingested_at", "messages"),
                        ("instagram", "instagram_posts", "collected_at", "posts"),
                        ("tiktok", "tiktok_posts", "collected_at", "posts"),
                        ("lemon8", "lemon8_posts", "collected_at", "posts"),
                        ("threads", "threads_posts", "collected_at", "posts"),
                        ("facebook", "facebook_posts", "collected_at", "posts"),
                        ("x", "x_posts", "collected_at", "posts"),
                        ("youtube", "youtube_videos", "collected_at", "videos"),
                        ("github", "github_commits", "collected_at", "commits"),
                        ("website", "website_pages", "collected_at", "pages"),
                        ("strava", "strava_activities", "collected_at", "activities"),
                        ("search", "search_results", "collected_at", "results"),
                    )
                    by_source: dict[str, dict[str, int]] = {}
                    for src, tbl, col, label in content_parts:
                        try:
                            n = int(await conn.fetchval(
                                f"SELECT count(*) FROM {tbl} "
                                f"WHERE {col} >= date_trunc('hour', now())",
                                timeout=10,
                            ) or 0)
                        except Exception:
                            continue
                        if n:
                            by_source[src] = {
                                "records": n,
                                "messages": n if label == "messages" else 0,
                                "files": 0,
                                "rate_limits": 0,
                            }
                    try:
                        for row in await conn.fetch(
                            """
                            SELECT source, count(*)::int AS files
                            FROM media_items
                            WHERE collected_at >= date_trunc('hour', now())
                            GROUP BY source
                            """,
                            timeout=15,
                        ):
                            d = by_source.setdefault(row["source"], {
                                "records": 0, "messages": 0, "files": 0, "rate_limits": 0,
                            })
                            d["files"] = int(row["files"] or 0)
                    except Exception:
                        pass
                    try:
                        for row in await conn.fetch(
                            """
                            SELECT source, count(*)::int AS rate_limits
                            FROM rate_limit_events
                            WHERE created_at >= date_trunc('hour', now())
                            GROUP BY source
                            """,
                            timeout=10,
                        ):
                            d = by_source.setdefault(row["source"], {
                                "records": 0, "messages": 0, "files": 0, "rate_limits": 0,
                            })
                            d["rate_limits"] = int(row["rate_limits"] or 0)
                    except Exception:
                        pass
                    totals = {
                        "records": sum(v["records"] for v in by_source.values()),
                        "messages": sum(v["messages"] for v in by_source.values()),
                        "files": sum(v["files"] for v in by_source.values()),
                        "rate_limits": sum(v["rate_limits"] for v in by_source.values()),
                    }
                    top = sorted(
                        ({"source": k, **v} for k, v in by_source.items()),
                        key=lambda r: (r["records"] + r["files"] + r["rate_limits"], r["source"]),
                        reverse=True,
                    )[:6]
                    snap["hourly_ingestion"] = {"totals": totals, "sources": top}
                except Exception:
                    pass

                try:
                    snap["rate_limit_events"] = [dict(r) for r in await conn.fetch(
                        """
                        SELECT source, account, scope,
                               max(status_code)::int AS status_code,
                               count(*)::int AS count,
                               max(cooldown_seconds)::int AS cooldown_seconds,
                               max(reason) AS reason,
                               max(created_at) AS last_seen_at
                        FROM rate_limit_events
                        WHERE created_at >= date_trunc('hour', now())
                        GROUP BY source, account, scope
                        ORDER BY last_seen_at DESC
                        LIMIT 5
                        """,
                        timeout=10,
                    )]
                except Exception:
                    pass

                try:
                    active_limits = []
                    now_ts = datetime.now(timezone.utc).timestamp()
                    for row in await conn.fetch(
                        """
                        SELECT service, last_processed_id, status
                        FROM service_cursors
                        WHERE status = 'blocked'
                          AND (service ILIKE '%rate_limit' OR service ILIKE '%ratelimit')
                        ORDER BY last_processed_at DESC NULLS LAST
                        """,
                        timeout=10,
                    ):
                        raw = str(row["last_processed_id"] or "")
                        if ":" not in raw:
                            continue
                        left, right = raw.split(":", 1)
                        try:
                            expiry_ts = float(left)
                        except Exception:
                            continue
                        if expiry_ts <= now_ts:
                            continue
                        try:
                            streak = int(right)
                        except Exception:
                            streak = None
                        active_limits.append({
                            "service": row["service"],
                            "seconds_remaining": int(expiry_ts - now_ts),
                            "streak": streak,
                        })
                    snap["active_rate_limits"] = active_limits[:5]
                except Exception:
                    pass

                # Per-source freshness from real data (accurate for realtime too).
                ages: dict[str, int] = {}
                stale: list[str] = []
                for name, query, thresh in self._FRESHNESS:
                    try:
                        age = await conn.fetchval(query, timeout=15)
                    except Exception:
                        continue
                    if age is None:
                        continue  # source has no data yet — not "stale", just unseen
                    ages[name] = int(age)
                    if age > thresh:
                        stale.append(name)
                snap["source_ages"] = ages
                snap["stale_sources"] = sorted(stale)

                # Backfill-vs-realtime signals (for the "Backfill:" heartbeat line).
                # (a) messaging realtime %: of rows INGESTED in the last hour, how
                # many carry a message timestamp also within the hour. ~100% = caught
                # up; low = still draining history under recent collected_at.
                rt: dict[str, float] = {}
                for src, tbl, ins_col, ts_col in (
                    ("telegram", "telegram_messages", "collected_at", "platform_created_at"),
                    ("whatsapp", "whatsapp_messages", "collected_at", "timestamp"),
                    ("beeper", "beeper_shadow_messages", "ingested_at", "timestamp"),
                ):
                    try:
                        pct = await conn.fetchval(
                            f"SELECT round(100.0*count(*) FILTER "
                            f"(WHERE {ts_col} > now()-interval '1 hour') "
                            f"/ NULLIF(count(*),0),1) "
                            f"FROM {tbl} "
                            f"WHERE {ins_col} > now()-interval '1 hour'", timeout=20)
                        if pct is not None:
                            rt[src] = float(pct)
                    except Exception:
                        continue
                snap["realtime_pct"] = rt

                # (b) spider-queue pending depth per source (remaining discovery/
                # backfill work). Some are true backfill (telegram dialogs); github/
                # strava are perpetual crawl frontiers that never reach 0.
                qp: dict[str, int] = {}
                for src in ("telegram", "instagram", "lemon8", "tiktok", "youtube", "github", "strava"):
                    try:
                        n = await conn.fetchval(
                            f"SELECT count(*) FROM {src}_spider_queue WHERE status='pending'", timeout=15)
                        qp[src] = int(n or 0)
                    except Exception:
                        continue
                snap["queue_pending"] = qp

                # Health flags from source_health (dead + degraded/auth_paused).
                try:
                    rows = await conn.fetch(
                        "SELECT source, status FROM source_health "
                        "WHERE status IN ('dead','degraded','auth_paused')"
                    )
                    snap["dead_sources"] = sorted(r["source"] for r in rows if r["status"] == "dead")
                    snap["degraded_sources"] = sorted(
                        r["source"] for r in rows if r["status"] in ("degraded", "auth_paused"))
                except Exception:
                    pass
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # ok reflects REAL health: green only when nothing dead or stale.
        snap["ok"] = not (snap.get("dead_sources") or snap.get("stale_sources"))
        return snap

    async def _init_db(self):
        # P0-1/P0-2: ledger-backed runner applies schemas/ + migrations/.
        from src.db.migrate import apply_all
        from src.core.maintenance import run_collector_maintenance
        await apply_all(self.pool)
        await run_collector_maintenance(self.pool)

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

    async def _build_graph_edges(self):
        """Compute social graph edges from WhatsApp messages into graph_edges.

        Co-group: pairs of users who both sent messages in the same WhatsApp group.
        Weight = number of shared groups.

        DM: users who sent direct messages to another user.

        Self-gated; default every 6 hours because the co-group upsert touches a
        large derived pair set and is not needed minute-by-minute.
        """
        import time as _time
        now = _time.monotonic()
        interval = env_int("GRAPH_EDGES_BUILD_INTERVAL_SECONDS", 21600, min_value=1800)
        if now - getattr(self, "_last_graph_build", 0) < interval:
            return
        self._last_graph_build = now
        try:
            async with self.pool.acquire() as conn:
                # Co-group edges: distinct (chat, sender) pairs joined against themselves.
                # Using CTEs to avoid O(n²) message self-join.
                inserted_cg = await conn.fetchval("""
                    WITH distinct_senders AS (
                        SELECT DISTINCT wm.chat_id, wm.sender_id
                        FROM whatsapp_messages wm
                        JOIN whatsapp_chats wc ON wm.chat_id = wc.id
                        WHERE wm.sender_id IS NOT NULL AND wc.is_group = true
                    ),
                    co_group AS (
                        SELECT
                            ds1.sender_id AS sender1,
                            ds2.sender_id AS sender2,
                            COUNT(DISTINCT ds1.chat_id) AS shared_groups
                        FROM distinct_senders ds1
                        JOIN distinct_senders ds2
                            ON ds1.chat_id = ds2.chat_id
                            AND ds1.sender_id < ds2.sender_id
                        GROUP BY ds1.sender_id, ds2.sender_id
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
                            NOW(),
                            NOW()
                        FROM co_group cg
                        JOIN whatsapp_users u1 ON cg.sender1 = u1.id
                        JOIN whatsapp_users u2 ON cg.sender2 = u2.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET
                            weight = EXCLUDED.weight,
                            last_seen_at = NOW()
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, timeout=180)

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
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, timeout=180)

            logger.info("graph_edges build: co_group=%d dm=%d", inserted_cg or 0, inserted_dm or 0)
        except Exception:
            logger.warning("graph_edges build failed", exc_info=True)

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
