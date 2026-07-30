import asyncio
import logging
import math
import random
import time
from datetime import datetime
from enum import Enum

from .rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger(__name__)


class OperationType(Enum):
    PUBLIC = "public"
    PROFILE_VIEW = "profile_view"
    MEDIA_DOWNLOAD = "media_download"
    SEARCH = "search"
    FOLLOWING_REQUIRED = "following_required"
    PAGINATION = "pagination"


OPERATION_MULTIPLIERS = {
    OperationType.PUBLIC: 1.0,
    OperationType.PROFILE_VIEW: 1.2,
    OperationType.MEDIA_DOWNLOAD: 0.8,
    OperationType.SEARCH: 1.5,
    OperationType.FOLLOWING_REQUIRED: 1.5,
    OperationType.PAGINATION: 1.0,
}


def _time_of_day_multiplier() -> float:
    hour = datetime.now().hour
    if 0 <= hour < 6:
        return random.uniform(2.5, 4.0)
    if 6 <= hour < 9:
        return 1.0
    if 9 <= hour < 17:
        return random.uniform(1.3, 1.7)
    return 1.0


def _jittered_delay(base: float) -> float:
    """Multi-distribution jitter: Gaussian 60%, Uniform 30%, Exponential 10%."""
    r = random.random()
    if r < 0.6:
        return max(0.1, random.gauss(base, base * 0.3))
    if r < 0.9:
        return random.uniform(base * 0.5, base * 1.5)
    return random.expovariate(1.0 / base)


def _cooldown_key(domain: str, account: str | None = None) -> str:
    """Compose a per-account cooldown key. If no account, falls back to bare domain.

    Use ``f"{domain}:{account_name}"`` style so callers can isolate account-level
    cooldowns (e.g. one IG account hitting 429 must NOT freeze peers).
    """
    if account:
        return f"{domain}:{account}"
    return domain


class HumanLikeRateLimiter(AdaptiveRateLimiter):
    """Rate limiter that mimics human browsing patterns.

    Layers Gaussian jitter, time-of-day awareness, micro-pauses, and
    operation-type multipliers on top of AdaptiveRateLimiter's success/failure
    tracking.  Social-media collectors (Instagram, TikTok, Lemon8) should use
    this; API-based sources (GitHub, YouTube) should stick with the base class.

    Cooldowns are tracked per-key (typically ``f"{domain}:{account}"``) so a
    rate-limit on one account does NOT block sibling accounts of the same
    platform.  Callers that don't care about per-account isolation can still
    pass just ``domain`` and get the historical (degenerate) behaviour.
    """

    def __init__(
        self,
        base_delay: float = 3.0,
        min_delay: float = 0.5,
        max_delay: float = 120.0,
        micro_pause_probability: float = 0.7,
        micro_pause_range: tuple[float, float] = (0.5, 3.0),
        rest_interval: int = 40,
        rest_probability: float = 0.3,
        rest_range: tuple[float, float] = (30.0, 60.0),
        time_of_day_enabled: bool = True,
        emergency_cooldown: float = 900.0,
        **kwargs,
    ):
        super().__init__(
            default_delay=base_delay,
            min_delay=min_delay,
            max_delay=max_delay,
            **kwargs,
        )
        self.micro_pause_probability = micro_pause_probability
        self.micro_pause_range = micro_pause_range
        self.rest_interval = rest_interval
        self.rest_probability = rest_probability
        self.rest_range = rest_range
        self.time_of_day_enabled = time_of_day_enabled
        self.emergency_cooldown = emergency_cooldown
        self._op_counter: int = 0
        # Per-key cooldown clock: maps "domain" or "domain:account" -> expiry monotonic.
        self._in_emergency: dict[str, float] = {}

    # ---------- per-key cooldown helpers ----------
    def is_in_cooldown(self, domain: str, account: str | None = None) -> bool:
        key = _cooldown_key(domain, account)
        expiry = self._in_emergency.get(key, 0.0)
        if expiry and time.monotonic() < expiry:
            return True
        if expiry:
            # expired — clean up
            self._in_emergency.pop(key, None)
        return False

    def cooldown_remaining_seconds(self, domain: str, account: str | None = None) -> float:
        key = _cooldown_key(domain, account)
        expiry = self._in_emergency.get(key, 0.0)
        if not expiry:
            return 0.0
        remaining = expiry - time.monotonic()
        if remaining <= 0:
            self._in_emergency.pop(key, None)
            return 0.0
        return remaining

    def compute_delay(
        self,
        domain: str,
        operation: OperationType = OperationType.PUBLIC,
        pagination_depth: int = 0,
        account: str | None = None,
    ) -> float:
        remaining = self.cooldown_remaining_seconds(domain, account)
        if remaining > 0:
            key = _cooldown_key(domain, account)
            logger.info("Emergency cooldown active for %s (%.0fs remaining)", key, remaining)
            return remaining

        base = self.get_delay(domain)
        delay = _jittered_delay(base)

        op_mult = OPERATION_MULTIPLIERS.get(operation, 1.0)
        if operation == OperationType.PAGINATION and pagination_depth > 0:
            op_mult = 1.0 + (pagination_depth * 0.15)
        delay *= op_mult

        if self.time_of_day_enabled:
            delay *= _time_of_day_multiplier()

        if random.random() < self.micro_pause_probability:
            delay += random.uniform(*self.micro_pause_range)

        self._op_counter += 1
        if self._op_counter >= self.rest_interval:
            if random.random() < self.rest_probability:
                rest = random.uniform(*self.rest_range)
                logger.info("Human rest period: %.1fs after %d ops", rest, self._op_counter)
                delay += rest
            self._op_counter = 0

        return max(self.min_delay, min(delay, self.max_delay))

    async def async_wait(
        self,
        domain: str,
        operation: OperationType = OperationType.PUBLIC,
        pagination_depth: int = 0,
        stop_event: asyncio.Event | None = None,
        account: str | None = None,
    ):
        delay = self.compute_delay(domain, operation, pagination_depth, account=account)
        if delay <= 0:
            return
        if stop_event:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(delay)

    def trigger_emergency_cooldown(self, domain: str, account: str | None = None):
        key = _cooldown_key(domain, account)
        self._in_emergency[key] = time.monotonic() + self.emergency_cooldown
        logger.warning(
            "Emergency cooldown triggered for %s: %.0fs",
            key, self.emergency_cooldown,
        )
        self.record_failure(domain)

    def set_cooldown_remaining(
        self,
        domain: str,
        seconds: float,
        account: str | None = None,
    ):
        """Restore a known cooldown deadline without applying another penalty."""
        key = _cooldown_key(domain, account)
        if seconds <= 0:
            self._in_emergency.pop(key, None)
            return
        self._in_emergency[key] = max(
            self._in_emergency.get(key, 0.0),
            time.monotonic() + float(seconds),
        )

    def record_rate_limit(self, domain: str, account: str | None = None):
        self.trigger_emergency_cooldown(domain, account=account)

    def clear_cooldown(self, domain: str | None = None, account: str | None = None):
        """Clear cooldown for a specific key, or all if domain is None."""
        if domain is None:
            self._in_emergency.clear()
            return
        key = _cooldown_key(domain, account)
        self._in_emergency.pop(key, None)
