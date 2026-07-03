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
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import aiohttp
import asyncpg

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
            "SELECT extract(epoch FROM now()-max(collected_at)) FROM strava_activities",
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


async def _mark_degraded(db: asyncpg.Connection, source: str, age: float, restarted: bool) -> None:
    """Record a stale source in source_health so it's queryable, not just a log line.

    Self-clearing: the worker's _mark_source_healthy UPSERTs status='running' on the
    next observed progress, so a source that recovers flips back automatically.
    """
    note = f"stale {age:.0f}s — watchdog {'restarted container' if restarted else 'in cooldown'}"
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


async def _restart(container: str) -> None:
    try:
        connector = aiohttp.UnixConnector(path=DOCKER_SOCK)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.post(f"http://docker/containers/{container}/restart?t=15") as r:
                log.warning("restarted %s -> HTTP %d", container, r.status)
    except Exception as e:
        log.error("restart %s failed: %s", container, e)


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
            log.info("%s ok (newest %.0fs ago)", src, age)


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
            log.info("%s DLQ ok (%d pending, oldest %.0fs)", src, n, oldest)


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


async def main() -> None:
    log.info("freshness watchdog started (interval=%ds, cooldown=%ds)", INTERVAL, COOLDOWN)
    _touch_heartbeat()  # write immediately so healthcheck has a file before first tick
    while True:
        try:
            db = await asyncpg.connect(DB)
            try:
                await _tick(db)
                if DLQ_ENABLED:
                    await _dlq_tick(db)
            finally:
                await db.close()
        except Exception as e:
            log.error("watchdog loop error: %s", e)
        # Heartbeat AFTER the tick (even on DB error) — proves the loop is alive
        # and cycling, which is exactly what the container healthcheck verifies.
        _touch_heartbeat()
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    # `python -m src.watchdog.freshness --healthcheck` is the container healthcheck.
    if "--healthcheck" in sys.argv:
        _run_healthcheck()
    asyncio.run(main())
