"""Driver for the profile-only probes.

Compose runs one source per container via ``--source``. With
PROFILE_ONLY_SOURCE unset, the process still runs all five. Each source
has its own env gate (``<SOURCE>_PROBE_ENABLED``).

This is intentionally NOT a BaseCollector worker loop. The profile-only
pattern is queue-driven. The process polls every
``PROFILE_ROUND_ROBIN_INTERVAL_SECONDS`` (default 60s) and probes up to
``<SOURCE>_PROBE_BATCH`` per enabled source per tick.
"""

from __future__ import annotations

import argparse
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

_COLLECTORS: dict[str, type[ProfileOnlyCollector]] = {
    "snapchat": SnapchatCollector,
    "paypal": PayPalCollector,
    "airbnb": AirbnbCollector,
    "bluesky": BlueskyCollector,
    "pinterest": PinterestCollector,
}

_INTERVAL = float(os.getenv("PROFILE_ROUND_ROBIN_INTERVAL_SECONDS", "60"))


def _selected_source() -> str:
    return os.getenv("PROFILE_ONLY_SOURCE", "").strip().lower()


def _build_collectors() -> Iterable[ProfileOnlyCollector]:
    chosen = _selected_source()
    if not chosen:
        for name in ("snapchat", "paypal", "airbnb", "bluesky", "pinterest"):
            yield _COLLECTORS[name]()
        return
    factory = _COLLECTORS.get(chosen)
    if factory is None:
        raise SystemExit(f"unknown profile-only source: {chosen}")
    yield factory()


async def run() -> None:
    """Service entry point. Loops the enabled collectors round-robin
    until SIGTERM/SIGINT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL not set")

    single = bool(_selected_source())
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2 if single else 4)

    collectors: list[ProfileOnlyCollector] = []
    for c in _build_collectors():
        c.set_pool(pool)
        collectors.append(c)
    label = _selected_source() or "all"
    logger.info("profile-only %s: %d collectors initialized", label, len(collectors))

    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("profile-only %s: shutdown signal received", label)
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

    logger.info("profile-only %s: exiting", label)
    await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="")
    args = parser.parse_args()
    if args.source:
        os.environ["PROFILE_ONLY_SOURCE"] = args.source
    asyncio.run(run())


if __name__ == "__main__":
    main()
