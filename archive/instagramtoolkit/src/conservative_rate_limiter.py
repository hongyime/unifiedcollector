"""
Conservative Rate Limiter - Enhanced rate limiting with operation-specific delays
to avoid Instagram bans without proxy infrastructure.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""
from __future__ import annotations

import math
import random
import time
import logging
from typing import Optional

from src.operation_classifier import OperationType
from src.account_cooldown import AccountCooldownManager
from src.config import (
    MIN_DELAY,
    MAX_DELAY,
    ACCOUNT_SWITCH_DELAY_MIN,
    ACCOUNT_SWITCH_DELAY_MAX,
    ENUM_PAUSE_EVERY,
    ENUM_PAUSE_SECONDS,
    ACCOUNT_COOLDOWN_MINUTES,
    DELAY_RANGE_VARIATION,
    DISTRIBUTION_GAUSSIAN_WEIGHT,
    DISTRIBUTION_UNIFORM_WEIGHT,
    MICRO_PAUSE_PROBABILITY,
    MICRO_PAUSE_MIN,
    MICRO_PAUSE_MAX,
)

logger = logging.getLogger(__name__)

# Delay multipliers per operation type (Requirement 4.2, 4.3, 4.4)
_DELAY_MULTIPLIERS = {
    OperationType.PUBLIC: 1.0,
    OperationType.FOLLOWING_REQUIRED: 1.5,
    OperationType.MUTUAL_FOLLOWING: 2.0,
}


class ConservativeRateLimiter:
    """
    Enhanced rate limiter with operation-specific delays and account cooldown enforcement.

    Delay scaling:
      - PUBLIC:             1.0x base delay
      - FOLLOWING_REQUIRED: 1.5x base delay
      - MUTUAL_FOLLOWING:   2.0x base delay

    Additional features:
      - Random jitter for human-like behaviour
      - Mandatory account-switch delays
      - Progressive delays every N operations (following enumeration)
      - Emergency cooldown on rate-limit hits (≥15 minutes)
      - Account availability checking via AccountCooldownManager

    Requirements: 4.1–4.8
    """

    def __init__(
        self,
        min_delay: float = MIN_DELAY,
        max_delay: float = MAX_DELAY,
        cooldown_manager: Optional[AccountCooldownManager] = None,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._cooldown_manager = cooldown_manager or AccountCooldownManager()

    # ------------------------------------------------------------------
    # Core delay helpers
    # ------------------------------------------------------------------

    def _base_delay(self) -> float:
        """Return a base delay using Gaussian sampling within [min_delay, max_delay].

        Gaussian distribution is more human-like than flat uniform (most values
        cluster near the midpoint with occasional extremes), while the clamp
        guarantees we never exceed the configured bounds (preservation 3.1).
        """
        mean = (self.min_delay + self.max_delay) / 2
        stddev = (self.max_delay - self.min_delay) / 4  # ~95% within range
        delay = random.gauss(mean, stddev)
        return max(self.min_delay, min(delay, self.max_delay))

    def _jitter(self, base: float) -> float:
        """Add jitter drawn from Gaussian, Uniform, or Exponential distributions.

        Distribution weights match the main RateLimiter (60/30/10) so the two
        limiters produce statistically indistinguishable timing profiles.
        """
        r = random.random()
        if r < DISTRIBUTION_GAUSSIAN_WEIGHT:
            return max(self.min_delay * 0.5, base + random.gauss(0, base * 0.2))
        elif r < DISTRIBUTION_GAUSSIAN_WEIGHT + DISTRIBUTION_UNIFORM_WEIGHT:
            return max(self.min_delay * 0.5, base + random.uniform(-base * 0.2, base * 0.2))
        else:
            # Exponential: occasional longer pauses (capped at +50%)
            u = max(random.random(), 1e-10)
            extra = min(-base * 0.3 * math.log(u), base * 0.5)
            return base + extra

    def _sleep(self, seconds: float, reason: str = ""):
        if seconds <= 0:
            return
        from src.rate_limiter import _interruptible_sleep
        _interruptible_sleep(seconds, label="rate", message=reason, show_countdown=seconds >= 30)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def operation_delay(self, operation_type: OperationType) -> None:
        """
        Apply operation-specific rate limiting delay.

        Delay = base_delay × multiplier + jitter, followed by an optional
        micro-pause (70% probability) to simulate human thinking time.

        Requirements: 4.1, 4.2, 4.3, 4.4
        """
        multiplier = _DELAY_MULTIPLIERS.get(operation_type, 1.0)
        base = self._base_delay() * multiplier
        delay = self._jitter(base)
        self._sleep(delay, reason=f"operation_delay({operation_type.value})")

        if random.random() < MICRO_PAUSE_PROBABILITY:
            from src.rate_limiter import _interruptible_sleep
            mean = (MICRO_PAUSE_MIN + MICRO_PAUSE_MAX) / 2
            u = max(random.random(), 1e-10)
            mp = max(MICRO_PAUSE_MIN, min(-mean * math.log(u), MICRO_PAUSE_MAX))
            _interruptible_sleep(mp, label="rate", check_interval=0.1)

    def account_switch_delay(self) -> None:
        """
        Enforce a mandatory delay between account switches.

        Requirement: 4.5
        """
        delay = random.uniform(ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX)
        delay = self._jitter(delay)
        self._sleep(delay, reason="account_switch_delay")

    def following_enumeration_delay(self, count: int) -> None:
        """
        Apply progressive delays during follower/following enumeration.

        Triggers every ENUM_PAUSE_EVERY operations.

        Requirement: 4.8
        """
        if count > 0 and count % ENUM_PAUSE_EVERY == 0:
            # Progressive: longer delay as count grows
            multiplier = 1.0 + (count // ENUM_PAUSE_EVERY) * 0.1
            delay = self._jitter(ENUM_PAUSE_SECONDS * multiplier)
            self._sleep(delay, reason=f"enumeration_pause at count={count}")

    def emergency_cooldown(self, account: str, duration_minutes: int = ACCOUNT_COOLDOWN_MINUTES) -> None:
        """
        Apply emergency cooldown to an account after a rate-limit hit.

        Enforces a minimum of 15 minutes.

        Requirement: 4.6
        """
        effective_minutes = max(15, duration_minutes)
        logger.warning(
            "Emergency cooldown: account '%s' for %d minutes", account, effective_minutes
        )
        self._cooldown_manager.put_on_cooldown(
            account, minutes=effective_minutes, reason="rate-limit-emergency"
        )

    def check_account_available(self, account: str) -> bool:
        """
        Return True if account is NOT in cooldown.

        Requirement: 4.7
        """
        return not self._cooldown_manager.is_on_cooldown(account)

    def get_cooldown_remaining(self, account: str) -> float:
        """Return seconds remaining on cooldown for account (0 if not in cooldown)."""
        return self._cooldown_manager.get_cooldown_remaining(account)

    def get_available_accounts(self, account_names: list[str]) -> list[str]:
        """Filter list to accounts not currently in cooldown."""
        return self._cooldown_manager.get_available_accounts(account_names)


