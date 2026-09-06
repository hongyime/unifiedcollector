"""ReconcileIdentitiesHandler — periodic social_users reconciliation.

Extracted from ``Scheduler._maybe_reconcile_identities`` in the LOGIC-005
refactor (``docs/plans/scheduler-refactor.md`` step 6). Owns its own last-fire
timestamp and env-var read.

Merges fragmented ``social_users`` rows (username-keyed -> id-keyed) on the
first tick after startup, then every ``RECONCILE_INTERVAL_HOURS`` hours.
Default 12h; ``0`` disables the reconcile entirely. Fail-soft: any exception
is logged and swallowed so a bad reconcile never disturbs the scheduling loop.

See ``src/core/identity_reconcile.py`` for the actual merge implementation.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class ReconcileIdentitiesHandler:
    """Merge fragmented social_users rows on first tick, then every N hours."""

    name = "reconcile_identities"

    def __init__(self) -> None:
        # 0.0 forces a run on the first should_run so a scheduler restart
        # always triggers one reconciliation pass.
        self._last_run: float = 0.0
        # Interval resolved lazily on first should_run.
        self._interval_hours: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_hours is None:
            self._interval_hours = ctx.get_env_int(
                "RECONCILE_INTERVAL_HOURS", 12, min_value=0,
            )
        if self._interval_hours <= 0:
            return False
        return _time.monotonic() - self._last_run >= self._interval_hours * 3600

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        try:
            from src.core.identity_reconcile import reconcile_social_users
            await reconcile_social_users(ctx.pool)
        except Exception as e:
            logger.warning("identity reconcile failed: %s", e)


__all__ = ["ReconcileIdentitiesHandler"]
