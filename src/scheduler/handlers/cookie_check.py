"""CookieCheckHandler — periodic active cookie-health validation.

Extracted from ``Scheduler._maybe_check_cookies`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md`` step 7). Owns its own last-fire
timestamp and env-var read.

Actively probes every stored social cookie so the dashboard never shows
'untested'. Runs on the first tick after startup, then every
``COOKIE_CHECK_INTERVAL_HOURS`` hours (default 6). ``0`` disables the check
entirely. Instagram is gated off inside the checker itself (collector-driven
IG cookies), so this handler is safe to leave on for all environments.

Fail-soft: any exception is logged and swallowed so a probe failure never
disturbs the scheduling loop. See ``src/core/cookie_health.py``.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class CookieCheckHandler:
    """Probe cookie validity on first tick, then every N hours."""

    name = "cookie_check"

    def __init__(self) -> None:
        # 0.0 forces a check on the first should_run.
        self._last_run: float = 0.0
        self._interval_hours: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_hours is None:
            self._interval_hours = ctx.get_env_int(
                "COOKIE_CHECK_INTERVAL_HOURS", 6, min_value=0,
            )
        if self._interval_hours <= 0:
            return False
        return _time.monotonic() - self._last_run >= self._interval_hours * 3600

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        try:
            from src.core.cookie_health import check_all_cookies
            await check_all_cookies(ctx.pool)
        except Exception as e:
            logger.warning("cookie health check failed: %s", e)


__all__ = ["CookieCheckHandler"]
