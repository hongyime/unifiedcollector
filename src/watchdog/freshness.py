"""Data-freshness watchdog — the safety net for silent connection death.

The Docker healthcheck on collector_telegram / wa_bridge_* only tests an HTTP
endpoint, which keeps replying even when the underlying MTProto / WhatsApp
connection is dead. And the worker's own watchdog EXEMPTS realtime sources
(telegram/whatsapp/beeper) from restart because a quiet chat legitimately looks
idle. Result: telegram once sat dead ~26h and whatsapp ~4 days while reporting
"healthy". This watchdog closes that gap: it checks the NEWEST row per realtime
source and restarts the owning container(s) when a source goes stale, via the
Docker socket. Idempotent, cooldown-guarded, no image rebuild (runs the existing
collector image with the source bind-mounted).
"""
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] watchdog: %(message)s")
log = logging.getLogger("watchdog")

DB = os.environ["DATABASE_URL"]
INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "300"))              # check every 5 min
COOLDOWN = int(os.getenv("WATCHDOG_RESTART_COOLDOWN", "1800"))     # ≤1 restart / 30 min / container
DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")

# Heartbeat: the loop touches this file every tick so the container healthcheck
# can tell a WEDGED loop from a live one (a bare `python -c exit(0)` check would
# pass even if the watchdog's own loop had died — which is the exact SPOF this
# service is meant to close for others). See --healthcheck below.
HEARTBEAT_FILE = Path(os.getenv("WATCHDOG_HEARTBEAT_FILE", "/tmp/watchdog_heartbeat"))
# Stale if older than 2 intervals + slack — a healthy loop rewrites it every INTERVAL.
HEARTBEAT_MAX_AGE = INTERVAL * 2 + 60


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.write_text(str(int(time.time())))
    except Exception as e:  # pragma: no cover - defensive; never let heartbeat kill the loop
        log.debug("heartbeat write failed: %s", e)


def _run_healthcheck() -> None:
    """Exit 0 if the loop wrote a recent heartbeat, else exit 1 (container --healthcheck)."""
    try:
        age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    except FileNotFoundError:
        # start_period covers the pre-first-tick window; after that, missing = unhealthy.
        print("UNHEALTHY: no heartbeat file yet", file=sys.stderr)
        sys.exit(1)
    if age > HEARTBEAT_MAX_AGE:
        print(f"UNHEALTHY: heartbeat {age:.0f}s old (> {HEARTBEAT_MAX_AGE}s)", file=sys.stderr)
        sys.exit(1)
    print(f"OK: heartbeat {age:.0f}s old")
    sys.exit(0)


# Keep the Docker healthcheck path tiny. Under host load, importing the async DB
# and HTTP stacks can exceed the 10s healthcheck timeout even when the watchdog
# heartbeat is fresh.
if "--healthcheck" in sys.argv:
    _run_healthcheck()

import asyncio

import aiohttp
import asyncpg

# source -> (freshness_query, stale_threshold_seconds, [containers that own the connection])
# Thresholds are generous: an account with many active chats will always have SOME
# activity inside the window, so exceeding it ≈ a dead connection, not a quiet spell.
CHECKS = {
    "telegram": (
        "SELECT extract(epoch FROM now()-max(collected_at)) FROM telegram_messages",
        int(os.getenv("WATCHDOG_STALE_TELEGRAM", "7200")),   # 2h
        ["unifiedcollector_collector_telegram"],
    ),
    "whatsapp": (
        "SELECT extract(epoch FROM now()-max(collected_at)) FROM whatsapp_messages",
        int(os.getenv("WATCHDOG_STALE_WHATSAPP", "14400")),  # 4h — the bridge holds the connection
        ["unifiedcollector_wa_bridge_1", "unifiedcollector_wa_bridge_2"],
    ),
    "beeper": (
        "SELECT extract(epoch FROM now()-max(ingested_at)) FROM beeper_shadow_messages",
        int(os.getenv("WATCHDOG_STALE_BEEPER", "10800")),    # 3h
        ["unifiedcollector_collector_beeper"],
    ),
}

# ── Headless-source freshness (P2 review §2) ─────────────────────────────────
# The realtime checks above never covered the 8 headless collectors, so a source
# could sit producing ZERO rows (expired cookies, ban, drained spider queue)
# while its container stayed green. These add per-source freshness.
#
# IMPORTANT nuances vs realtime:
#  * Thresholds are MUCH more generous (day+). Headless sources idle for long
#    stretches by design (github 900s/strava 600s/instagram 1200s cycle sleeps,
#    API ceilings), and a quiet upstream (few active follows) is legitimate — a
#    false restart is cheap/non-destructive, but we still avoid twitchiness.
#  * For instagram/tiktok/lemon8 the freshness table is shared with the browser
#    extension (same `source`), so this detects "source stopped flowing" broadly,
#    not headless-specifically; restarting the headless container is the best
#    available action and harmless if the extension is the live path. Once
#    media_items.ingest_path lands (task #7) these can filter to ingest_path=
#    'headless' for precision.
#  * github/strava/search all live in collector_lowrisk — a stale one restarts
#    the shared container (COOLDOWN prevents triple-restart storms).
# Opt-out with WATCHDOG_HEADLESS_ENABLED=0.
if os.getenv("WATCHDOG_HEADLESS_ENABLED", "1") == "1":
    _D = 86400  # 1 day, the generous baseline
    CHECKS.update({
        "instagram": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='instagram'",
            int(os.getenv("WATCHDOG_STALE_INSTAGRAM", str(_D * 2))),   # 48h
            ["unifiedcollector_collector_instagram"],
        ),
        "tiktok": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='tiktok'",
            int(os.getenv("WATCHDOG_STALE_TIKTOK", str(_D * 2))),      # 48h
            ["unifiedcollector_collector_tiktok"],
        ),
        "youtube": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM youtube_videos",
            int(os.getenv("WATCHDOG_STALE_YOUTUBE", str(_D * 2))),     # 48h
            ["unifiedcollector_collector_youtube"],
        ),
        "lemon8": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='lemon8'",
            int(os.getenv("WATCHDOG_STALE_LEMON8", str(_D * 2))),      # 48h
            ["unifiedcollector_collector_lemon8"],
        ),
        "website": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM website_pages",
            int(os.getenv("WATCHDOG_STALE_WEBSITE", str(_D * 3))),     # 72h (multi-hour crawls)
            ["unifiedcollector_collector_website"],
        ),
        "github": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM github_commits",
            int(os.getenv("WATCHDOG_STALE_GITHUB", str(_D * 3))),      # 72h (900s cycle sleep)
            ["unifiedcollector_collector_lowrisk"],
        ),
        "strava": (
            """
            SELECT extract(epoch FROM now()-max(ts))
            FROM (
                SELECT max(collected_at) AS ts FROM strava_activities
                UNION ALL
                SELECT max(collected_at) AS ts FROM strava_gps_streams
                UNION ALL
                SELECT max(collected_at) AS ts FROM media_items WHERE source='strava'
            ) progress
            """,
            int(os.getenv("WATCHDOG_STALE_STRAVA", str(_D * 3))),      # 72h
            ["unifiedcollector_collector_lowrisk"],
        ),
        "search": (
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM search_results",
            int(os.getenv("WATCHDOG_STALE_SEARCH", str(_D * 3))),      # 72h
            ["unifiedcollector_collector_lowrisk"],
        ),
    })

_last_restart: dict[str, float] = {}
_last_cooldown_stale_alert: dict[str, float] = {}


# Browser-only / browser-primary source monitoring. The normal CHECKS above can
# restart containers, but these sources live in Chrome. Restarting a container
# cannot revive a dead content script, so this path marks/alerts only.
BROWSER_SOURCE_WATCH_ENABLED = os.getenv("WATCHDOG_BROWSER_SOURCES_ENABLED", "1") == "1"
BROWSER_SOURCE_ALERT_COOLDOWN = int(os.getenv("WATCHDOG_BROWSER_SOURCE_ALERT_COOLDOWN", "3600"))
BROWSER_SOURCE_WATCH_SOURCES = {
    s.strip()
    for s in os.getenv(
        "WATCHDOG_BROWSER_SOURCE_SOURCES",
        "facebook,threads,x,tiktok",
    ).split(",")
    if s.strip()
}
_last_browser_source_alert: dict[str, float] = {}


async def _notify(text: str) -> None:
    """Best-effort Telegram alert. Never raises (notifier is send-only/fail-safe).

    Requires the watchdog service to load ../.env (TELEGRAM_* tokens); if unset,
    telegram.send() no-ops. Import is lazy so a missing module can't crash the loop.
    """
    try:
        from src.notifications import telegram as _tg
        await _tg.send(text)
    except Exception as e:
        log.debug("notify failed: %s", e)


async def _mark_degraded(
    db: asyncpg.Connection,
    source: str,
    age: float,
    restarted: bool,
    detail: str | None = None,
) -> None:
    """Record a stale source in source_health so it's queryable, not just a log line.

    Self-clearing: the worker's _mark_source_healthy UPSERTs status='running' on the
    next observed progress, so a source that recovers flips back automatically.
    """
    note = f"stale {age:.0f}s — watchdog {detail or ('restarted container' if restarted else 'in cooldown')}"
    try:
        await db.execute(
            "INSERT INTO source_health (source, status, last_error, crash_count, updated_at) "
            "VALUES ($1, 'degraded', $2, 1, NOW()) "
            "ON CONFLICT (source) DO UPDATE "
            "SET status='degraded', last_error=$2, "
            "    crash_count=source_health.crash_count + 1, updated_at=NOW()",
            source, note,
        )
    except Exception as e:
        log.error("source_health upsert failed for %s: %s", source, e)


async def _mark_running_if_stale_watchdog(db: asyncpg.Connection, source: str) -> None:
    """Clear stale-watchdog source_health rows after fresh source data resumes."""
    try:
        await db.execute(
            """
            UPDATE source_health
            SET status='running',
                last_error=NULL,
                crash_count=0,
                last_success_at=COALESCE(last_success_at, NOW()),
                updated_at=NOW()
            WHERE source=$1
              AND status='degraded'
              AND lower(coalesce(last_error, '')) LIKE 'stale %watchdog%'
            """,
            source,
        )
    except Exception as e:
        log.error("source_health recovery update failed for %s: %s", source, e)


async def _mark_running_if_dlq_watchdog(db: asyncpg.Connection, source: str) -> None:
    """Clear DLQ-watchdog source_health rows once the pending queue drains."""
    try:
        await db.execute(
            """
            UPDATE source_health
            SET status='running',
                last_error=NULL,
                crash_count=0,
                last_success_at=COALESCE(last_success_at, NOW()),
                updated_at=NOW()
            WHERE source=$1
              AND status='degraded'
              AND lower(coalesce(last_error, '')) LIKE 'dlq backlog:%watchdog%'
            """,
            source,
        )
    except Exception as e:
        log.error("source_health DLQ recovery update failed for %s: %s", source, e)


async def _mark_degraded_browser_source(db: asyncpg.Connection, source: str, detail: str) -> None:
    note = f"browser capture stalled: {detail} (watchdog)"
    try:
        await db.execute(
            "INSERT INTO source_health (source, status, last_error, updated_at) "
            "VALUES ($1, 'degraded', $2, NOW()) "
            "ON CONFLICT (source) DO UPDATE "
            "SET status='degraded', last_error=$2, updated_at=NOW()",
            source, note,
        )
    except Exception as e:
        log.error("source_health browser upsert failed for %s: %s", source, e)


async def _mark_running_if_browser_watchdog(db: asyncpg.Connection, source: str) -> None:
    try:
        await db.execute(
            """
            UPDATE source_health
            SET status='running',
                last_error=NULL,
                crash_count=0,
                last_success_at=COALESCE(last_success_at, NOW()),
                updated_at=NOW()
            WHERE source=$1
              AND status='degraded'
              AND lower(coalesce(last_error, '')) LIKE 'browser capture stalled:%watchdog%'
            """,
            source,
        )
    except Exception as e:
        log.error("source_health browser recovery update failed for %s: %s", source, e)


async def _restart(container: str) -> None:
    try:
        connector = aiohttp.UnixConnector(path=DOCKER_SOCK)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.post(f"http://docker/containers/{container}/restart?t=15") as r:
                log.warning("restarted %s -> HTTP %d", container, r.status)
    except Exception as e:
        log.error("restart %s failed: %s", container, e)


def _row_get(row, key: str, default=None):
    try:
        return row[key]
    except Exception:
        return default


def _parse_cursor_cooldown(raw: str, now: float) -> dict | None:
    if ":" not in raw:
        return None
    left, right = raw.split(":", 1)
    try:
        expiry_ts = float(left)
    except Exception:
        return None
    if expiry_ts <= now:
        return None
    try:
        streak = int(right)
    except Exception:
        streak = None
    return {
        "seconds_remaining": int(expiry_ts - now),
        "streak": streak,
        "basis": "service_cursor",
    }


async def _active_source_cooldown(db: asyncpg.Connection, source: str, now: float) -> dict | None:
    """Return active source-level HTTP 429 cooldown metadata, if known.

    The freshness watchdog should not restart a collector that is deliberately
    resting after HTTP 429s. Restarts reset process-local pacing and tend to
    probe the same blocked endpoint again, which creates noisy stale/restart
    loops without improving freshness.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT service, last_processed_id
            FROM service_cursors
            WHERE status = 'blocked'
              AND service IN ($1, $2)
            ORDER BY last_processed_at DESC NULLS LAST
            LIMIT 1
            """,
            f"{source}_rate_limit",
            f"{source}_ratelimit",
        )
        if row:
            parsed = _parse_cursor_cooldown(str(_row_get(row, "last_processed_id") or ""), now)
            if parsed:
                parsed["basis"] = str(_row_get(row, "service") or parsed["basis"])
                return parsed
    except Exception as e:
        log.debug("cooldown cursor check failed for %s: %s", source, e)

    try:
        row = await db.fetchrow(
            """
            SELECT extract(epoch FROM max(created_at + cooldown_seconds * interval '1 second')) AS expiry_ts,
                   count(*)::int AS events,
                   max(reason) AS reason
            FROM rate_limit_events
            WHERE source = $1
              AND status_code = 429
              AND cooldown_seconds IS NOT NULL
              AND created_at + cooldown_seconds * interval '1 second' > now()
            """,
            source,
        )
        expiry_ts = _row_get(row, "expiry_ts") if row else None
        if expiry_ts is not None and float(expiry_ts) > now:
            return {
                "seconds_remaining": int(float(expiry_ts) - now),
                "streak": None,
                "basis": "rate_limit_events",
                "events": int(_row_get(row, "events", 0) or 0),
                "reason": _row_get(row, "reason"),
            }
    except Exception as e:
        log.debug("cooldown event check failed for %s: %s", source, e)
    return None


async def _whatsapp_pairing_needed() -> str | None:
    """Return an operator-action detail when WhatsApp is waiting for QR pairing."""
    try:
        from src.core.whatsapp_bridge_health import (
            fetch_whatsapp_bridge_health,
            summarize_whatsapp_bridge_health,
        )

        summary = summarize_whatsapp_bridge_health(await fetch_whatsapp_bridge_health(timeout=4))
    except Exception as e:
        log.debug("whatsapp bridge health check failed: %s", e)
        if os.getenv("WATCHDOG_WHATSAPP_RESTART_ON_HEALTH_UNAVAILABLE", "0") == "1":
            return None
        return "bridge health unavailable; restart deferred to avoid QR pairing churn"

    if summary.get("status") == "unpaired":
        return "waiting for QR pairing; not restarted"
    if (
        summary.get("status") == "unreachable"
        and os.getenv("WATCHDOG_WHATSAPP_RESTART_ON_HEALTH_UNAVAILABLE", "0") != "1"
    ):
        return "bridge health unavailable; restart deferred to avoid QR pairing churn"
    return None


async def _tick(db: asyncpg.Connection) -> None:
    now = time.time()
    for src, (query, thresh, containers) in CHECKS.items():
        try:
            age = await db.fetchval(query)
        except Exception as e:
            log.error("freshness query failed for %s: %s", src, e)
            continue
        if age is None:
            continue
        if age > thresh:
            if src == "whatsapp":
                pairing_detail = await _whatsapp_pairing_needed()
                if pairing_detail:
                    log.warning(
                        "%s STALE (%.0fs > %ds) but bridge is awaiting QR pairing — not restarting",
                        src, age, thresh,
                    )
                    await _mark_degraded(db, src, age, False, pairing_detail)
                    continue

            cooldown = await _active_source_cooldown(db, src, now)
            if cooldown:
                seconds = int(cooldown.get("seconds_remaining") or 0)
                streak = cooldown.get("streak")
                streak_text = f", streak {streak}" if streak else ""
                log.warning(
                    "%s STALE (%.0fs > %ds) but active HTTP 429 cooldown has %ds left%s — not restarting",
                    src, age, thresh, seconds, streak_text,
                )
                await _mark_degraded(
                    db,
                    src,
                    age,
                    False,
                    f"active HTTP 429 cooldown {seconds}s left; not restarted",
                )
                if now - _last_cooldown_stale_alert.get(src, 0) >= max(COOLDOWN, 3600):
                    _last_cooldown_stale_alert[src] = now
                    await _notify(
                        f"⚠️ <b>watchdog: {src} stale but cooling down</b>\n"
                        f"newest row {age:.0f}s ago (&gt; {thresh}s). "
                        f"Active HTTP 429 cooldown has {seconds // 60}m left"
                        + (f" after streak {streak}" if streak else "")
                        + "; not restarting during cooldown."
                    )
                continue

            restarted_any = False
            for c in containers:
                if now - _last_restart.get(c, 0) < COOLDOWN:
                    log.info("%s STALE %.0fs but %s in restart-cooldown — skipping", src, age, c)
                    continue
                log.warning("%s STALE (%.0fs > %ds) — restarting %s", src, age, thresh, c)
                _last_restart[c] = now
                await _restart(c)
                restarted_any = True
            # Record the degradation + alert regardless of cooldown, so a source
            # stuck stale (cooldown suppressing repeat restarts) is still visible.
            await _mark_degraded(db, src, age, restarted_any)
            if restarted_any:
                await _notify(
                    f"⚠️ <b>watchdog: {src} stale</b>\n"
                    f"newest row {age:.0f}s ago (&gt; {thresh}s) — restarted "
                    + ", ".join(containers)
                )
        else:
            await _mark_running_if_stale_watchdog(db, src)
            log.info("%s ok (newest %.0fs ago)", src, age)


async def _browser_source_tick(db: asyncpg.Connection) -> None:
    """Surface stalled Chrome-extension sources as operator-actionable state.

    The collector cannot safely restart Chrome from inside Docker. This turns a
    silent extension/tab stall into source_health + Telegram evidence, while
    leaving the fix to the operator/browser control path.
    """
    if not BROWSER_SOURCE_WATCH_SOURCES:
        return
    now = time.time()
    try:
        from src.core.source_freshness import compute_liveness

        rows = await compute_liveness(db)
    except Exception as e:
        log.error("browser source liveness query failed: %s", e)
        return

    for row in rows:
        source = str(row.get("source") or "")
        if source not in BROWSER_SOURCE_WATCH_SOURCES:
            continue
        status = row.get("status")
        heartbeat_age = row.get("browser_heartbeat_age_seconds")
        content_stale = bool(row.get("browser_content_stale"))
        if status in {"degraded", "stale", "dead", "unknown"} and (
            heartbeat_age is not None or content_stale
        ):
            detail = str(row.get("detail") or "browser extension/content path is stale")
            log.warning("%s browser capture stalled: %s", source, detail)
            await _mark_degraded_browser_source(db, source, detail)
            if now - _last_browser_source_alert.get(source, 0) >= BROWSER_SOURCE_ALERT_COOLDOWN:
                _last_browser_source_alert[source] = now
                await _notify(
                    f"⚠️ <b>watchdog: {source} browser collection stalled</b>\n"
                    f"{detail}\n"
                    "Container restart will not fix this path. Reload the UnifiedCollector Chrome "
                    "extension, refresh/reopen the platform tab, then press Scrape now on Social Tabs."
                )
        else:
            await _mark_running_if_browser_watchdog(db, source)
            if heartbeat_age is None:
                log.info("%s browser source ok (no heartbeat requirement active)", source)
            else:
                log.info("%s browser source ok (heartbeat %.0fs ago)", source, heartbeat_age)


# ── DLQ-growth monitoring (P2 review §2) ─────────────────────────────────────
# Freshness checks can't see a source that ingests metadata fine (fresh
# collected_at) while failing every media download into dead_letter_queue. This
# alerts when pending DLQ rows PILE UP and go STALE per source. Stateless (no
# in-memory history) so it survives watchdog restarts: alert when a source has
# ≥ threshold pending rows AND the oldest is older than the stall window — i.e.
# accumulating faster than the DLQ retry drains. No restart (a restart won't
# drain the DLQ); degrade + notify only.
DLQ_ENABLED = os.getenv("WATCHDOG_DLQ_ENABLED", "1") == "1"
DLQ_PENDING_THRESHOLD = int(os.getenv("WATCHDOG_DLQ_PENDING_THRESHOLD", "100"))
DLQ_STALL_SECONDS = int(os.getenv("WATCHDOG_DLQ_STALL_SECONDS", "21600"))  # 6h
DLQ_ALERT_COOLDOWN = int(os.getenv("WATCHDOG_DLQ_ALERT_COOLDOWN", "21600"))  # ≤1 alert / 6h / source
_last_dlq_alert: dict[str, float] = {}


async def _dlq_tick(db: asyncpg.Connection) -> None:
    now = time.time()
    try:
        rows = await db.fetch(
            "SELECT source, count(*) AS n, "
            "       extract(epoch FROM now()-min(created_at)) AS oldest "
            "FROM dead_letter_queue WHERE status='pending' GROUP BY source"
        )
    except Exception as e:
        log.error("DLQ query failed: %s", e)
        return
    for r in rows:
        src, n, oldest = r["source"], r["n"], r["oldest"] or 0
        if n >= DLQ_PENDING_THRESHOLD and oldest > DLQ_STALL_SECONDS:
            await _mark_degraded_dlq(db, src, n, oldest)
            if now - _last_dlq_alert.get(src, 0) >= DLQ_ALERT_COOLDOWN:
                _last_dlq_alert[src] = now
                await _notify(
                    f"⚠️ <b>watchdog: {src} DLQ piling up</b>\n"
                    f"{n} pending, oldest {oldest / 3600:.1f}h — media/ingest failing, not draining"
                )
        else:
            await _mark_running_if_dlq_watchdog(db, src)
            log.info("%s DLQ ok (%d pending, oldest %.0fs)", src, n, oldest)

    # Also clear sources that previously had a DLQ-watchdog degradation but now
    # have zero pending rows, so source_health cannot stay degraded forever after
    # the queue is fully drained.
    try:
        stale_sources = await db.fetch(
            """
            SELECT source
            FROM source_health
            WHERE status='degraded'
              AND lower(coalesce(last_error, '')) LIKE 'dlq backlog:%watchdog%'
              AND source NOT IN (
                  SELECT DISTINCT source
                  FROM dead_letter_queue
                  WHERE status='pending'
              )
            """
        )
        for row in stale_sources:
            await _mark_running_if_dlq_watchdog(db, row["source"])
            log.info("%s DLQ recovered (0 pending)", row["source"])
    except Exception as e:
        log.error("DLQ recovery query failed: %s", e)


async def _mark_degraded_dlq(db: asyncpg.Connection, source: str, n: int, oldest: float) -> None:
    note = f"DLQ backlog: {n} pending, oldest {oldest / 3600:.1f}h (watchdog)"
    try:
        await db.execute(
            "INSERT INTO source_health (source, status, last_error, updated_at) "
            "VALUES ($1, 'degraded', $2, NOW()) "
            "ON CONFLICT (source) DO UPDATE "
            "SET status='degraded', last_error=$2, updated_at=NOW()",
            source, note,
        )
    except Exception as e:
        log.error("source_health DLQ upsert failed for %s: %s", source, e)


# ── DM WS-hook liveness (P1.3) ───────────────────────────────────────────────
# The browser extension's DM WebSocket wrapper (see extension/inject.js
# "DM investigation" block) posts a heartbeat every 5 min. If IG or TikTok
# update their bundle and the wrapper silently breaks, samples just stop
# arriving — indistinguishable from "user isn't DMing". This alerts when the
# newest heartbeat per platform goes stale.
#
# Alert-only, NO restart: the hook lives in the browser, not a container. If
# we've never seen a heartbeat for a platform we do NOT alert (can't tell
# "never installed" from "just broken"); the freshness check only fires when
# a previously-alive heartbeat has since gone quiet.
DM_HOOK_ENABLED = os.getenv("WATCHDOG_DM_HOOK_ENABLED", "1") == "1"
DM_HOOK_STALE_SECONDS = int(os.getenv("WATCHDOG_STALE_DM_HOOK", "3600"))       # 1h
DM_HOOK_ALERT_COOLDOWN = int(os.getenv("WATCHDOG_DM_HOOK_ALERT_COOLDOWN", "3600"))  # ≤1 alert/h/platform
_last_dm_hook_alert: dict[str, float] = {}


async def _dm_hook_tick(db: asyncpg.Connection) -> None:
    now = time.time()
    try:
        # to_regclass check keeps the loop happy on a boot before the P1.3
        # migration is applied.
        exists = await db.fetchval("SELECT to_regclass('dm_hook_heartbeat')")
        if exists is None:
            return
        rows = await db.fetch(
            """
            SELECT platform,
                   extract(epoch FROM now() - max(last_seen)) AS age
            FROM dm_hook_heartbeat
            GROUP BY platform
            """
        )
    except Exception as e:
        log.error("dm_hook_heartbeat query failed: %s", e)
        return
    for r in rows:
        platform, age = r["platform"], r["age"] or 0
        if age > DM_HOOK_STALE_SECONDS:
            if now - _last_dm_hook_alert.get(platform, 0) >= DM_HOOK_ALERT_COOLDOWN:
                _last_dm_hook_alert[platform] = now
                log.warning(
                    "DM WS hook STALE for %s (last beat %.0fs ago > %ds)",
                    platform, age, DM_HOOK_STALE_SECONDS,
                )
                await _notify(
                    f"⚠️ <b>DM WS hook stale: {platform}</b>\n"
                    f"last heartbeat {age / 60:.0f}m ago (&gt; {DM_HOOK_STALE_SECONDS // 60}m).\n"
                    "Extension bundle change on the platform may have broken the "
                    "passive WebSocket wrapper — check extension/inject.js against "
                    "current window.WebSocket behaviour on the platform tab."
                )
            else:
                log.info("DM hook %s stale (%.0fs) — alert in cooldown", platform, age)
        else:
            log.info("DM hook %s ok (last beat %.0fs ago)", platform, age)


async def main() -> None:
    log.info("freshness watchdog started (interval=%ds, cooldown=%ds)", INTERVAL, COOLDOWN)
    _touch_heartbeat()  # write immediately so healthcheck has a file before first tick
    while True:
        try:
            db = await asyncpg.connect(DB)
            try:
                await _tick(db)
                if BROWSER_SOURCE_WATCH_ENABLED:
                    await _browser_source_tick(db)
                if DLQ_ENABLED:
                    await _dlq_tick(db)
                if DM_HOOK_ENABLED:
                    await _dm_hook_tick(db)
            finally:
                await db.close()
        except Exception as e:
            log.error("watchdog loop error: %s", e)
        # Heartbeat AFTER the tick (even on DB error) — proves the loop is alive
        # and cycling, which is exactly what the container healthcheck verifies.
        _touch_heartbeat()
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
