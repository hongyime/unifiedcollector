"""Scheduler startup helpers — DB init, conditional collector registration,
and notification emission around lifecycle transitions.

Extracted from ``src/scheduler/__init__.py`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md``). These are entry-time / exit-time
concerns rather than per-tick scheduling, so they belong out of the ``_tick``
loop path.

Functions here take a ``Scheduler`` instance or its ``pool`` and never touch
class state beyond what the original methods used. ``Scheduler.start`` /
``Scheduler`` delegate to these; existing tests that construct ``Scheduler``
still work because the instance-method wrappers on the class remain.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid circular import at runtime
    from src.scheduler import Scheduler

logger = logging.getLogger(__name__)


async def init_db(pool) -> None:
    """Apply schemas/ + migrations/ + collector maintenance to the given pool.

    Mirrors the old ``Scheduler._init_db`` verbatim. Split off so the DB
    boot path is importable and testable without a full Scheduler.
    """
    # P0-1/P0-2: ledger-backed runner applies schemas/ + migrations/.
    from src.db.migrate import apply_all
    from src.core.maintenance import run_collector_maintenance
    await apply_all(pool)
    await run_collector_maintenance(pool)


async def register_beeper_if_enabled(scheduler: "Scheduler") -> None:
    """Register the polymorphic Beeper Desktop Local API collector.

    Gated on `BEEPER_COLLECTOR_ENABLED` + presence of `BEEPER_DESKTOP_API_TOKEN`.
    When both are set, we ensure a `collection_schedules` row exists for
    source='beeper' on a 5-minute cadence — short enough that incremental
    tail catches new messages quickly, long enough not to thrash the
    local API.

    Replaces the prior `_register_matrix_if_enabled` / `_register_matrix_backfill_if_enabled`
    pair from Wave 1 (matrix-nio path). The new Beeper Desktop Local API
    on 127.0.0.1:23373 spans every connected network in one collector,
    so a single schedule replaces the matrix + matrix_backfill duo.
    """
    try:
        from src.collectors.beeper import is_enabled as beeper_enabled
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Beeper collector module unavailable: %s", exc)
        return

    if not beeper_enabled():
        logger.info(
            "Beeper collector disabled (BEEPER_COLLECTOR_ENABLED unset or no token); "
            "skipping schedule registration"
        )
        return

    try:
        # Use 5-minute cadence (interval_hours=1/12 ≈ 5 min). Reuse the
        # existing add_schedule helper which currently takes hours; the
        # collector caps per-cycle work via BEEPER_MAX_CHATS_PER_CYCLE.
        await scheduler.add_schedule("beeper", interval_hours=1)
        logger.info("Beeper collector registered on schedule (every 1h)")
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to register beeper schedule: %s", exc)


async def register_strava_feed_if_enabled(scheduler: "Scheduler") -> None:
    """Register a weekly Strava following-feed backfill schedule.

    Gated on STRAVA_FEED_BACKFILL_ENABLED. The collector reads cookies
    and walks /dashboard/feed for `STRAVA_FEED_BACKFILL_DAYS` (default 30)
    days back, upserting any newly-discovered activities into
    strava_activities. Cadence is weekly (168h) — long enough to avoid
    hammering the cookie session; short enough to keep recent feed
    history fresh.
    """
    val = os.environ.get("STRAVA_FEED_BACKFILL_ENABLED", "").strip().lower()
    if val not in {"1", "true", "yes", "on"}:
        logger.info(
            "Strava feed backfill disabled (STRAVA_FEED_BACKFILL_ENABLED unset); "
            "skipping schedule registration"
        )
        return
    try:
        await scheduler.add_schedule("strava_feed_backfill", interval_hours=168)
        logger.info("Strava feed backfill registered on schedule (every 168h)")
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to register strava_feed_backfill schedule: %s", exc)


async def notify_startup_safe() -> None:
    """Fire the startup Telegram notification, swallowing any failure.

    A notification hiccup must never block scheduler startup.
    """
    try:
        from src.notifications import alerts
        await alerts.notify_startup()
    except Exception as e:
        logger.warning("notify_startup failed: %s", e)


async def notify_shutdown_safe() -> None:
    """Fire the shutdown Telegram notification, swallowing any failure.

    A notification hiccup must never block scheduler shutdown.
    """
    try:
        from src.notifications import alerts
        await alerts.notify_shutdown()
    except Exception as e:
        logger.warning("notify_shutdown failed: %s", e)


__all__ = [
    "init_db",
    "register_beeper_if_enabled",
    "register_strava_feed_if_enabled",
    "notify_startup_safe",
    "notify_shutdown_safe",
]
