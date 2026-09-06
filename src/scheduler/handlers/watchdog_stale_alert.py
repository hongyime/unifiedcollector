"""WatchdogStaleAlertHandler — escalation alert on persistently stale sources.

Extracted from ``Scheduler._maybe_alert_watchdog_stale`` in the LOGIC-005
refactor (``docs/plans/scheduler-refactor.md`` step 14). Owns its own
last-fire timestamp; env-var reads happen inside ``should_run`` / ``run``.

The freshness watchdog (``src/watchdog/freshness.py``) restarts stale
realtime containers on its own cooldown and only alerts on the first cycle
after entering the ``stale`` state. Once its alert cooldown ticks over, a
persistent failure becomes invisible from Telegram alone — audit evidence:
DM hook stale 35 h with "alert in cooldown" log line. This handler catches
that class of failure by reading ``source_health`` directly and emitting a
distinct "still stale" escalation.

Cadence: ``WATCHDOG_STALE_ALERT_INTERVAL_SECONDS`` (default 21600 = 6h,
min 300). Escalation gate: ``WATCHDOG_STALE_ALERT_THRESHOLD_HOURS``
(default 12, min 1) of ``updated_at`` age. See INTR-003 / REL-005.

Same "update timer only after successful alert" semantics as the other
alert handlers.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class WatchdogStaleAlertHandler:
    """Escalate persistently-stale sources past the watchdog's own cooldown."""

    name = "watchdog_stale_alert"

    def __init__(self) -> None:
        self._last_alert: float = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        interval = ctx.get_env_int(
            "WATCHDOG_STALE_ALERT_INTERVAL_SECONDS", 21600, min_value=300,
        )
        return _time.monotonic() - self._last_alert >= interval

    async def run(self, ctx: SchedulerContext) -> None:
        threshold_hours = ctx.get_env_int(
            "WATCHDOG_STALE_ALERT_THRESHOLD_HOURS", 12, min_value=1,
        )
        try:
            async with ctx.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT source, status, EXTRACT(EPOCH FROM (NOW() - updated_at))/3600 AS age_hours
                    FROM source_health
                    WHERE status IN ('degraded', 'stale', 'unhealthy')
                       OR updated_at < NOW() - ($1 || ' hours')::interval
                    ORDER BY updated_at ASC
                    """,
                    str(threshold_hours),
                )
        except Exception:
            logger.debug("watchdog_stale probe query failed", exc_info=True)
            return
        stale = [
            r for r in rows
            if (r["status"] in ("degraded", "stale", "unhealthy"))
            or (r["age_hours"] and r["age_hours"] > threshold_hours)
        ]
        if not stale:
            # Do not update the timer — next tick re-probes.
            return
        self._last_alert = _time.monotonic()
        try:
            from src.notifications import telegram as tg
            lines = [f"⚠️ <b>Watchdog still-stale escalation</b>"]
            for r in stale[:10]:
                lines.append(
                    f"• <code>{r['source']}</code>: {r['status']} "
                    f"(age {r['age_hours']:.1f}h)"
                )
            if len(stale) > 10:
                lines.append(f"… and {len(stale) - 10} more.")
            await tg.send("\n".join(lines))
            logger.info("watchdog_stale escalation alert sent (n=%d)", len(stale))
        except Exception:
            logger.warning("watchdog_stale escalation alert send failed", exc_info=True)


__all__ = ["WatchdogStaleAlertHandler"]
