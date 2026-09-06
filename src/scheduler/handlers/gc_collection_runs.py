"""GcCollectionRunsHandler — hourly retention GC for ``collection_runs``.

Extracted from ``Scheduler._gc_collection_runs`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md`` step 8). Owns its own last-fire
timestamp; env-var read is lazy on ``run``.

P3-7 fix: ``collection_runs`` has no consumer and previously grew unbounded
(307+ aborted rows observed). The dashboard shows recent run history so the
table can't just be deleted — we retain a window and prune the rest.
Retention is ``COLLECTION_RUNS_RETENTION_DAYS`` (default 7, min 1). Cadence
is fixed hourly; there's no benefit to running it more often because
schedule-tick inserts are low-rate.

Fail-soft: any exception is logged and swallowed so a GC failure never
disturbs the scheduling loop.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)

_GC_INTERVAL_SECONDS = 3600  # hourly; not env-tunable (schedule-tick rate is low)


class GcCollectionRunsHandler:
    """Prune ``collection_runs`` rows older than the retention window."""

    name = "gc_collection_runs"

    def __init__(self) -> None:
        # 0.0 forces a run on the first should_run so a scheduler restart
        # triggers one prune pass immediately.
        self._last_run: float = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        return _time.monotonic() - self._last_run >= _GC_INTERVAL_SECONDS

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        retention_days = ctx.get_env_int(
            "COLLECTION_RUNS_RETENTION_DAYS", 7, min_value=1,
        )
        try:
            async with ctx.pool.acquire() as conn:
                deleted = await conn.fetchval(
                    "WITH d AS (DELETE FROM collection_runs "
                    "WHERE COALESCE(completed_at, started_at) "
                    "      < NOW() - ($1 || ' days')::interval "
                    "RETURNING 1) SELECT COUNT(*) FROM d",
                    str(retention_days),
                )
            if deleted:
                logger.info("collection_runs GC: pruned %d rows older than %dd",
                            deleted, retention_days)
        except Exception:
            logger.warning("collection_runs GC failed", exc_info=True)


__all__ = ["GcCollectionRunsHandler"]
