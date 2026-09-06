"""HeartbeatHandler — periodic Telegram status digest.

Extracted from ``Scheduler._maybe_heartbeat`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md``). Owns its own last-fire timestamp so
scheduler state is no longer a bag of per-handler flags.

Interval is read from ``STATUS_HEARTBEAT_INTERVAL_HOURS`` on first
``should_run``. ``0`` disables the heartbeat entirely. Default is 24h — the
previous "constant text spam" from 6h + 15min defaults was intentionally
softened during Wave 2.

The handler catches every exception in ``run`` so a heartbeat failure never
disturbs the scheduling loop.
"""
from __future__ import annotations

import logging
import time as _time

from src.core.source_freshness import FRESHNESS
from src.scheduler import status_builder
from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class HeartbeatHandler:
    """Fire the status heartbeat on the first tick, then every N hours."""

    name = "heartbeat"

    def __init__(self) -> None:
        # 0.0 forces a heartbeat on the first should_run so operators see a
        # fresh status immediately after a scheduler restart.
        self._last_status: float = 0.0
        # Interval is resolved lazily on first should_run so import order does
        # not depend on env-var initialisation.
        self._interval_hours: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_hours is None:
            self._interval_hours = ctx.get_env_int(
                "STATUS_HEARTBEAT_INTERVAL_HOURS", 24, min_value=0,
            )
        if self._interval_hours <= 0:
            return False
        return _time.monotonic() - self._last_status >= self._interval_hours * 3600

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_status = _time.monotonic()
        try:
            snapshot = await status_builder.build_status(ctx.pool, FRESHNESS)
            await ctx.notifier.notify_status(snapshot)
        except Exception as e:
            logger.warning("status heartbeat failed: %s", e)


__all__ = ["HeartbeatHandler"]
