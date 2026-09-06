"""RealtimeFeedAlertHandler — alert on realtime-feed failed-queue depth.

Extracted from ``Scheduler._maybe_alert_realtime_feed_failed`` in the
LOGIC-005 refactor (``docs/plans/scheduler-refactor.md`` step 13). Owns its
own last-fire timestamp; env-var reads happen inside ``should_run`` /
``run``.

The realtime post-feed drains ``uc:realtime_post_feed``; failed sends land
in ``uc:realtime_post_feed:failed``. There's no automatic drainer — left
alone the failed queue grows unboundedly. This alert gives the operator
visibility so they can inspect and drain it manually (REL-004).

Cadence: ``REALTIME_FAILED_ALERT_INTERVAL_SECONDS`` (default 21600 = 6h,
min 300). Threshold: ``REALTIME_FAILED_ALERT_THRESHOLD`` (default 10,
min 1).

Same "update timer only after successful alert" semantics as
``BridgeUnpairedAlertHandler``.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class RealtimeFeedAlertHandler:
    """Alert when the realtime post-feed's failed queue grows past threshold."""

    name = "realtime_feed_alert"

    def __init__(self) -> None:
        self._last_alert: float = 0.0

    async def should_run(self, ctx: SchedulerContext) -> bool:
        interval = ctx.get_env_int(
            "REALTIME_FAILED_ALERT_INTERVAL_SECONDS", 21600, min_value=300,
        )
        return _time.monotonic() - self._last_alert >= interval

    async def run(self, ctx: SchedulerContext) -> None:
        threshold = ctx.get_env_int(
            "REALTIME_FAILED_ALERT_THRESHOLD", 10, min_value=1,
        )
        try:
            from src.notifications import realtime_feed
            client = await realtime_feed._redis_client()
            if client is None:
                return
            try:
                depth = await client.llen(realtime_feed.FAILED_KEY_DEFAULT)
            finally:
                try:
                    await client.aclose()
                except Exception:
                    pass
        except Exception:
            logger.debug("realtime_feed failed-queue probe failed", exc_info=True)
            return
        if not depth or depth < threshold:
            # Do not update the timer — next tick re-probes.
            return
        self._last_alert = _time.monotonic()
        try:
            from src.notifications import telegram as tg
            await tg.send(
                f"⚠️ <b>Realtime post-feed: {depth} failed items</b>\n"
                f"<code>uc:realtime_post_feed:failed</code> has {depth} unsent items. "
                f"Inspect and drain manually if needed."
            )
            logger.info("realtime_failed alert sent (depth=%d)", depth)
        except Exception:
            logger.warning("realtime_failed alert send failed", exc_info=True)


__all__ = ["RealtimeFeedAlertHandler"]
