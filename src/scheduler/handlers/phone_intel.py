"""PhoneIntelHandler — periodic offline WhatsApp phone-JID enrichment.

Extracted from ``Scheduler._maybe_run_phone_intel`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md`` step 10). Owns its own last-fire
timestamp; env-var reads happen inside ``should_run`` / ``run``.

Runs ``src.core.wa_phone_intel`` on a bounded batch of unenriched WhatsApp
JIDs. Enrichment is offline via the ``phonenumbers`` library (no API, no
network) and outputs carrier / region / line-type / timezone into the
``wa_phone_intel`` table. That table is **enrichment-only** — its contents
must never be promoted to ``identity_signals`` because carrier + region do
not identify an individual (see ``src/core/wa_phone_intel.py`` docstring).

Cadence: ``WA_PHONE_INTEL_INTERVAL_SECONDS`` (default 43200 = 12h, min 300).
Batch size: ``WA_PHONE_INTEL_TICK_LIMIT`` (default 500, min 1).

Fail-soft: any exception is logged and swallowed.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class PhoneIntelHandler:
    """Enrich WhatsApp phone JIDs offline on a ~12h cadence."""

    name = "phone_intel"

    def __init__(self) -> None:
        # 0.0 forces one enrichment run on the first should_run.
        self._last_run: float = 0.0
        self._interval_seconds: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_seconds is None:
            self._interval_seconds = ctx.get_env_int(
                "WA_PHONE_INTEL_INTERVAL_SECONDS", 43200, min_value=300,
            )
        return _time.monotonic() - self._last_run >= self._interval_seconds

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        batch = ctx.get_env_int("WA_PHONE_INTEL_TICK_LIMIT", 500, min_value=1)
        try:
            from src.core.wa_phone_intel import run as wa_phone_intel_run
            stats = await wa_phone_intel_run(batch, dry_run=False)
            logger.info("wa_phone_intel tick: %s", stats)
        except Exception:
            logger.warning("wa_phone_intel tick failed", exc_info=True)


__all__ = ["PhoneIntelHandler"]
