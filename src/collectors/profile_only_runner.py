"""Round-robin driver for the 5 ProfileOnlyCollectors.

Runs under docker service ``collector_profile_only``. All 5 subclasses
share one process and one asyncpg pool. Each subclass has its own env
gate (SOURCE_PROBE_ENABLED); if none are enabled, the service idles.

This is intentionally NOT a BaseCollector worker loop - the profile-only
pattern is queue-driven, not firehose-driven. We poll the queue every
``PROFILE_ROUND_ROBIN_INTERVAL_SECONDS`` (default 60s) and probe up to
``<SOURCE>_PROBE_BATCH`` per source per tick.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Iterable

import asyncpg

from src.collectors.airbnb import AirbnbCollector
from src.collectors.bluesky import BlueskyCollector
from src.collectors.paypal import PayPalCollector
from src.collectors.pinterest import PinterestCollector
from src.collectors.snapchat import SnapchatCollector
from src.core.profile_only_collector import ProfileOnlyCollector

logger = logging.getLogger(__name__)

_INTERVAL = float(os.getenv("PROFILE_ROUND_ROBIN_INTERVAL_SECONDS", "60"))


def _build_collectors() -> Iterable[ProfileOnlyCollector]:
    yield SnapchatCollector()
    yield PayPalCollector()
    yield AirbnbCollector()
    yield BlueskyCollector()
    yield PinterestCollector()


async def run() -> None:
    """Service entry point. Loops the enabled collectors round-robin
    until SIGTERM/SIGINT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

    collectors: list[ProfileOnlyCollector] = []
    for c in _build_collectors():
        c.set_pool(pool)
        collectors.append(c)
    logger.info("collector_profile_only: %d collectors initialized", len(collectors))

    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("collector_profile_only: shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            # Windows can't set POSIX signal handlers; container is Linux
            # so this branch is a no-op safety net.
            pass

    while not stop_event.is_set():
        for c in collectors:
            if stop_event.is_set():
                break
            if not c.enabled:
                continue
            try:
                summary = await c.run_cycle()
                if summary.get("processed", 0) or summary.get("skipped"):
                    logger.info("%s: %s", c.SOURCE_NAME, summary)
            except Exception:
                logger.exception("%s: cycle failed", c.SOURCE_NAME)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INTERVAL)
        except asyncio.TimeoutError:
            pass

    logger.info("collector_profile_only: exiting")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
