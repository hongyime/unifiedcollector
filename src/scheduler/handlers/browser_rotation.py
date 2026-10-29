"""BrowserRotationHandler — rotate the browser-group collection schedules.

``src.core.browser_rotator`` owns the deterministic, stateless slot logic and
writes ``collection_schedules`` directly; this handler only supplies the
scheduler cadence. Runs every ``COLLECTOR_BROWSER_ROTATION_INTERVAL_MINUTES``
(default 30), matching the rotator's own default.

Fail-soft: any exception is logged and swallowed so a bad schedule row never
disturbs the scheduling loop.
"""
from __future__ import annotations

import logging
import time as _time

from src.core.browser_rotator import rotate_browser_schedules
from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class BrowserRotationHandler:
    """Re-apply the current browser rotation slot on the scheduler cadence."""

    name = "browser_rotation"

    def __init__(self) -> None:
        # 0.0 forces a run on the first should_run so a restart re-syncs the
        # active browser set immediately.
        self._last_run: float = 0.0
        self._interval_seconds: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_seconds is None:
            minutes = ctx.get_env_int(
                "COLLECTOR_BROWSER_ROTATION_INTERVAL_MINUTES", 30, min_value=1,
            )
            self._interval_seconds = minutes * 60
        return _time.monotonic() - self._last_run >= self._interval_seconds

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        try:
            await rotate_browser_schedules(ctx.pool)
        except Exception:
            logger.warning("browser_rotation failed", exc_info=True)


__all__ = ["BrowserRotationHandler"]
