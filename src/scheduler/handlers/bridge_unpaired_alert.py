"""BridgeUnpairedAlertHandler — Telegram alert on WhatsApp bridge outages.

Extracted from ``Scheduler._maybe_alert_bridge_unpaired`` in the LOGIC-005
refactor (``docs/plans/scheduler-refactor.md`` step 12). Owns its own
last-fire timestamp; env-var reads happen inside ``should_run`` / ``run``.

``src/collectors/whatsapp/__init__.py`` stamps
``rate_limit_events(source='whatsapp', status_code=503,
metadata->>'error_code'='bridge_unpaired')`` on every deferred decrypt that
fails because the bridge is unpaired. Encrypted history messages sit in the
DLQ-like deferral state until the bridge is re-paired; nothing self-heals,
so this scheduler alert is the only way the operator sees the outage
without eyeballing container logs (INTR-002).

Cadence: ``WA_BRIDGE_UNPAIRED_ALERT_INTERVAL_SECONDS`` (default 3600 = 1h,
min 300). Threshold: ``WA_BRIDGE_UNPAIRED_ALERT_THRESHOLD`` (default 20,
min 1) events within
``WA_BRIDGE_UNPAIRED_ALERT_WINDOW_MINUTES`` (default 30, min 5).

Semantics preserved from the original: the last-fire timestamp is updated
**only after a successful alert send**. If the probe returns below
threshold, the timer stays put and the next tick re-probes. That means
during a healthy stretch the handler continues to poll the events table on
every tick — matches pre-refactor behavior.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class BridgeUnpairedAlertHandler:
    """Send a Telegram alert when the WhatsApp bridge is deferring decrypts."""

    name = "bridge_unpaired_alert"

    def __init__(self) -> None:
        # 0.0 means "no alert has fired yet"; combined with a large
        # ``_time.monotonic()`` this lets the first tick probe immediately.
        self._last_alert: float = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        interval = ctx.get_env_int(
            "WA_BRIDGE_UNPAIRED_ALERT_INTERVAL_SECONDS", 3600, min_value=300,
        )
        return _time.monotonic() - self._last_alert >= interval

    async def run(self, ctx: SchedulerContext) -> None:
        threshold = ctx.get_env_int(
            "WA_BRIDGE_UNPAIRED_ALERT_THRESHOLD", 20, min_value=1,
        )
        window_minutes = ctx.get_env_int(
            "WA_BRIDGE_UNPAIRED_ALERT_WINDOW_MINUTES", 30, min_value=5,
        )
        try:
            async with ctx.pool.acquire() as conn:
                count = await conn.fetchval(
                    """
                    SELECT count(*)
                    FROM rate_limit_events
                    WHERE source = 'whatsapp'
                      AND status_code = 503
                      AND metadata->>'error_code' = 'bridge_unpaired'
                      AND created_at > NOW() - ($1 || ' minutes')::interval
                    """,
                    str(window_minutes),
                ) or 0
        except Exception:
            logger.debug("bridge_unpaired probe query failed", exc_info=True)
            return
        if count < threshold:
            # Deliberately do NOT update ``_last_alert`` so the next tick
            # re-probes. Only a successful alert resets the throttle.
            return
        self._last_alert = _time.monotonic()
        try:
            from src.notifications import telegram as tg
            await tg.send(
                f"⚠️ <b>WhatsApp bridge unpaired</b>\n"
                f"{count} decrypt-deferred events in the last {window_minutes} min "
                f"(HTTP 503 bridge_unpaired).\n"
                f"Encrypted history is not landing. Re-pair the affected bridge "
                f"(<code>docker logs unifiedcollector_wa_bridge_1</code> for the QR)."
            )
            logger.info("bridge_unpaired alert sent (count=%d, window=%dm)",
                        count, window_minutes)
        except Exception:
            logger.warning("bridge_unpaired alert send failed", exc_info=True)


__all__ = ["BridgeUnpairedAlertHandler"]
