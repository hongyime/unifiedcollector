"""ArchiveFallbackHandler — periodically drain the Wayback fallback queue.

``src.core.archive_fallback`` owns the pipeline and its own enabled check;
this handler only supplies the scheduler cadence. Disabled unless
``ARCHIVE_FALLBACK_ENABLED=1``; runs every
``ARCHIVE_FALLBACK_INTERVAL_MINUTES`` (default 360).

Fail-soft: any exception is logged and swallowed so a Wayback outage never
disturbs the scheduling loop.
"""
from __future__ import annotations

import logging
import time as _time

from src.core.archive_fallback import run_archive_fallback
from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class ArchiveFallbackHandler:
    """Drain pending ``archive_fallback_queue`` rows on a long cadence."""

    name = "archive_fallback"

    def __init__(self) -> None:
        # 0.0 forces a run on the first should_run so a restart picks up
        # whatever accumulated in the queue.
        self._last_run: float = 0.0
        self._interval_seconds: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if ctx.get_env_int("ARCHIVE_FALLBACK_ENABLED", 0, min_value=0) != 1:
            return False
        if self._interval_seconds is None:
            minutes = ctx.get_env_int(
                "ARCHIVE_FALLBACK_INTERVAL_MINUTES", 360, min_value=1,
            )
            self._interval_seconds = minutes * 60
        return _time.monotonic() - self._last_run >= self._interval_seconds

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        try:
            await run_archive_fallback(ctx.pool)
        except Exception:
            logger.warning("archive_fallback failed", exc_info=True)


__all__ = ["ArchiveFallbackHandler"]
