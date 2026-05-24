"""
Account Scheduler - Time-based scheduling for shared Telegram accounts.

When two separate projects share the same Telegram accounts (different session files,
same phone numbers), MTProto message ID conflicts arise because both clients receive
updates simultaneously. This scheduler solves the problem by connecting/disconnecting
accounts on a configurable time window, so only one project is active at a time.

Configuration via environment variables:
    ACCOUNT_SCHEDULE_ENABLED=true
    ACCOUNT_ACTIVE_START=00:00      # UTC time to start (connect accounts)
    ACCOUNT_ACTIVE_END=12:00        # UTC time to stop (disconnect accounts)
    ACCOUNT_SCHEDULE_TIMEZONE=UTC   # Timezone for schedule

Example: If this project should run 00:00-12:00 UTC and the other project
runs 12:00-00:00 UTC, set ACCOUNT_ACTIVE_START=00:00 and ACCOUNT_ACTIVE_END=12:00.

When outside the active window:
    - User account clients are gracefully disconnected
    - Scanners and story scanners are paused
    - Bot clients (for commands/publishing) remain connected
    - The scheduler checks every 60s and reconnects when the window opens

When no schedule is configured (ACCOUNT_SCHEDULE_ENABLED=false, the default),
accounts stay connected 24/7 as before.
"""
import logging
import asyncio
from datetime import datetime, time as dt_time, timezone, timedelta
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


class AccountScheduler:
    """
    Manages time-based connect/disconnect of Telegram user accounts.
    
    This allows two projects to share the same Telegram accounts by
    giving each project a non-overlapping active time window.
    """

    def __init__(
        self,
        enabled: bool = False,
        active_start: str = "00:00",
        active_end: str = "24:00",
        on_activate: Optional[Callable[[], Awaitable[None]]] = None,
        on_deactivate: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        Args:
            enabled: Whether scheduling is active. If False, accounts are always active.
            active_start: UTC time string "HH:MM" when accounts should connect.
            active_end: UTC time string "HH:MM" when accounts should disconnect.
                        Use "24:00" to mean midnight end-of-day.
            on_activate: Async callback when entering the active window (reconnect accounts).
            on_deactivate: Async callback when leaving the active window (disconnect accounts).
        """
        self.enabled = enabled
        self.active_start = self._parse_time(active_start)
        self.active_end = self._parse_time(active_end)
        self.on_activate = on_activate
        self.on_deactivate = on_deactivate
        
        self._is_active = True  # Start as active (accounts connected)
        self._task: Optional[asyncio.Task] = None
        self._running = False

        if enabled:
            logger.info(
                f"Account scheduler enabled: active window {active_start} - {active_end} UTC"
            )
        else:
            logger.info("Account scheduler disabled — accounts active 24/7")

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        """Parses 'HH:MM' string to a time object."""
        time_str = time_str.strip()
        # Handle "24:00" as a special end-of-day marker
        if time_str == "24:00":
            return dt_time(23, 59, 59, tzinfo=timezone.utc)
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return dt_time(hour, minute, tzinfo=timezone.utc)

    def is_within_active_window(self, now: Optional[datetime] = None) -> bool:
        """
        Returns True if the current UTC time is within the active window.

        Handles both normal windows (start < end, e.g. 08:00-20:00) and
        overnight windows (start > end, e.g. 22:00-06:00).
        """
        if not self.enabled:
            return True  # Always active when scheduling is disabled

        if now is None:
            now = datetime.now(timezone.utc)

        current_time = now.timetz()
        # Normalize to UTC-aware time for comparison
        current = dt_time(current_time.hour, current_time.minute, current_time.second, tzinfo=timezone.utc)

        start = self.active_start
        end = self.active_end

        if start <= end:
            # Normal window: e.g. 08:00 - 20:00
            return start <= current <= end
        else:
            # Overnight window: e.g. 22:00 - 06:00
            return current >= start or current <= end

    @property
    def is_active(self) -> bool:
        """Returns True if accounts are currently in the active state."""
        return self._is_active

    def time_until_next_transition(self, now: Optional[datetime] = None) -> timedelta:
        """Returns time until the next activate/deactivate transition."""
        if not self.enabled:
            return timedelta(hours=24)  # Check again tomorrow

        if now is None:
            now = datetime.now(timezone.utc)

        currently_active = self.is_within_active_window(now)
        today = now.date()

        if currently_active:
            # Next transition is deactivation (at active_end)
            target = datetime.combine(today, self.active_end, tzinfo=timezone.utc)
            if target <= now:
                target += timedelta(days=1)
        else:
            # Next transition is activation (at active_start)
            target = datetime.combine(today, self.active_start, tzinfo=timezone.utc)
            if target <= now:
                target += timedelta(days=1)

        return target - now

    async def start(self):
        """Starts the scheduler background loop."""
        if not self.enabled:
            logger.debug("Scheduler not enabled, skipping start")
            return

        self._running = True

        # Check initial state
        should_be_active = self.is_within_active_window()
        if not should_be_active:
            logger.warning(
                f"⏸️ Outside active window — accounts will be paused until "
                f"{self.active_start.strftime('%H:%M')} UTC"
            )
            self._is_active = False
            if self.on_deactivate:
                await self.on_deactivate()

        self._task = asyncio.create_task(self._schedule_loop(), name="account_scheduler")

    async def _schedule_loop(self):
        """Main loop that checks the schedule and triggers transitions."""
        while self._running:
            try:
                should_be_active = self.is_within_active_window()

                if should_be_active and not self._is_active:
                    # Transition: INACTIVE -> ACTIVE
                    logger.info("🟢 Active window started — connecting accounts...")
                    self._is_active = True
                    if self.on_activate:
                        try:
                            await self.on_activate()
                        except Exception as e:
                            logger.error(f"Error during activation callback: {e}")

                elif not should_be_active and self._is_active:
                    # Transition: ACTIVE -> INACTIVE
                    logger.info("🔴 Active window ended — disconnecting accounts...")
                    self._is_active = False
                    if self.on_deactivate:
                        try:
                            await self.on_deactivate()
                        except Exception as e:
                            logger.error(f"Error during deactivation callback: {e}")

                # Sleep until near the next transition (check every 30s for responsiveness)
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)

    async def stop(self):
        """Stops the scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Account scheduler stopped")

    def get_status(self) -> dict:
        """Returns current scheduler status."""
        now = datetime.now(timezone.utc)
        return {
            "enabled": self.enabled,
            "is_active": self._is_active,
            "current_utc": now.strftime("%H:%M:%S"),
            "active_window": f"{self.active_start.strftime('%H:%M')}-{self.active_end.strftime('%H:%M')} UTC",
            "within_window": self.is_within_active_window(now),
            "next_transition_minutes": round(self.time_until_next_transition(now).total_seconds() / 60, 1),
        }
