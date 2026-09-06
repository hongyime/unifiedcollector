"""ReconSeedHandler — periodic recon-target seeding from collector data.

Extracted from ``Scheduler._maybe_seed_recon_targets`` in the LOGIC-005
refactor (``docs/plans/scheduler-refactor.md`` step 9). Owns its own
last-fire timestamp; env-var reads happen inside ``should_run`` / ``run``.

Keeps the recon worker queue fed by enqueueing username targets discovered
in ``social_users`` and (optionally) related tables. Idempotent: relies on
``queue_recon_target`` dedupe. Bounded per cycle so the queue fills
gradually rather than one giant dump.

Cadence: ``RECON_SEED_INTERVAL_SECONDS`` (default 21600 = 6h, min 300).
Batch bounds: ``RECON_SEED_PER_SOURCE_LIMIT`` (default 200, min 1) and
``RECON_SEED_TOTAL_LIMIT`` (default 2000, min 1).

Fail-soft: any exception is logged and swallowed.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class ReconSeedHandler:
    """Enqueue recon targets from collector data on a ~6h cadence."""

    name = "recon_seed"

    def __init__(self) -> None:
        # 0.0 forces one seed run on the first should_run so a scheduler
        # restart replenishes the queue immediately.
        self._last_run: float = 0.0
        self._interval_seconds: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_seconds is None:
            self._interval_seconds = ctx.get_env_int(
                "RECON_SEED_INTERVAL_SECONDS", 21600, min_value=300,
            )
        return _time.monotonic() - self._last_run >= self._interval_seconds

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        per_source = ctx.get_env_int("RECON_SEED_PER_SOURCE_LIMIT", 200, min_value=1)
        total = ctx.get_env_int("RECON_SEED_TOTAL_LIMIT", 2000, min_value=1)
        try:
            from src.core.recon_seed import seed_recon_targets_from_collector
            async with ctx.pool.acquire() as conn:
                report = await seed_recon_targets_from_collector(
                    conn,
                    include_domains=False,
                    include_urls=False,
                    include_usernames=True,
                    per_source_limit=per_source,
                    total_limit=total,
                    priority=7,
                    dry_run=False,
                )
            logger.info(
                "recon_seed tick: candidates=%s queued=%s skipped=%s",
                report.get("candidates"), report.get("queued"), report.get("skipped"),
            )
        except Exception:
            logger.warning("recon_seed tick failed", exc_info=True)


__all__ = ["ReconSeedHandler"]
