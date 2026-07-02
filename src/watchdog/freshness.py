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
import time

import aiohttp
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] watchdog: %(message)s")
log = logging.getLogger("watchdog")

DB = os.environ["DATABASE_URL"]
INTERVAL = int(os.getenv("WATCHDOG_INTERVAL", "300"))              # check every 5 min
COOLDOWN = int(os.getenv("WATCHDOG_RESTART_COOLDOWN", "1800"))     # ≤1 restart / 30 min / container
DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")

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

_last_restart: dict[str, float] = {}


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
            for c in containers:
                if now - _last_restart.get(c, 0) < COOLDOWN:
                    log.info("%s STALE %.0fs but %s in restart-cooldown — skipping", src, age, c)
                    continue
                log.warning("%s STALE (%.0fs > %ds) — restarting %s", src, age, thresh, c)
                _last_restart[c] = now
                await _restart(c)
        else:
            log.info("%s ok (newest %.0fs ago)", src, age)


async def main() -> None:
    log.info("freshness watchdog started (interval=%ds, cooldown=%ds)", INTERVAL, COOLDOWN)
    while True:
        try:
            db = await asyncpg.connect(DB)
            try:
                await _tick(db)
            finally:
                await db.close()
        except Exception as e:
            log.error("watchdog loop error: %s", e)
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
