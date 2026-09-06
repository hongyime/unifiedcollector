"""StatusDeltaHandler — 15-minute delta status update (Feature 2).

Extracted from ``Scheduler._maybe_status_delta`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md``). Owns its own last-fire timestamp and
env-var read; scheduler state no longer carries it.

Independent from ``HeartbeatHandler``: the hourly digest keeps its own cadence
and content; this is a smaller supplementary tick reporting only what changed
since the previous delta. ``build_status_delta`` persists its last-tick
timestamp in ``service_cursors`` so a scheduler restart never double-sends,
even if the in-memory monotonic clock resets to 0.

``STATUS_DELTA_INTERVAL_MINUTES=0`` (default) disables the feature entirely.
The old default (15) was a source of chronic Telegram spam; the hourly digest
already covers the same content, hence the flip to off-by-default.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler import status_builder
from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class StatusDeltaHandler:
    """Fire the 15-minute delta on cadence; off by default."""

    name = "status_delta"

    def __init__(self) -> None:
        # 0.0 forces a run on the first should_run once the feature is enabled.
        self._last_fired: float = 0.0
        # Interval is resolved lazily on first should_run.
        self._interval_minutes: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_minutes is None:
            self._interval_minutes = ctx.get_env_int(
                "STATUS_DELTA_INTERVAL_MINUTES", 0, min_value=0,
            )
        if self._interval_minutes <= 0:
            return False
        return _time.monotonic() - self._last_fired >= self._interval_minutes * 60

    async def run(self, ctx: SchedulerContext) -> None:
        # Update the monotonic gate before dispatching so a slow / hung delta
        # cannot repeatedly fire back-to-back on subsequent ticks.
        self._last_fired = _time.monotonic()
        assert self._interval_minutes is not None  # set in should_run
        try:
            snapshot = await status_builder.build_status_delta(
                ctx.pool, self._interval_minutes,
            )
            if snapshot is None:
                return  # not yet due per persisted service_cursors row
            await ctx.notifier.notify_status_delta(snapshot)
        except Exception as e:
            logger.warning("status delta failed: %s", e)


__all__ = ["StatusDeltaHandler"]
