import logging
import threading
import time

logger = logging.getLogger(__name__)


class AdaptiveRateLimiter:
    """Self-tuning per-domain rate limiter.

    Tracks delay per domain. After `success_streak` consecutive successes the
    delay shrinks toward `min_delay`. On any failure the delay grows toward
    `max_delay`.
    """

    def __init__(
        self,
        default_delay: float = 2.0,
        min_delay: float = 0.5,
        max_delay: float = 60.0,
        success_streak: int = 5,
        backoff_factor: float = 2.0,
        cooldown_factor: float = 0.8,
    ):
        self.default_delay = default_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.success_streak = success_streak
        self.backoff_factor = backoff_factor
        self.cooldown_factor = cooldown_factor

        self._domains: dict[str, _DomainState] = {}
        self._lock = threading.Lock()

    def _get(self, domain: str) -> "_DomainState":
        if domain not in self._domains:
            self._domains[domain] = _DomainState(self.default_delay)
        return self._domains[domain]

    def get_delay(self, domain: str) -> float:
        """Return how many seconds to wait before the next request to `domain`."""
        with self._lock:
            return self._get(domain).delay

    def wait(self, domain: str, stop_event: threading.Event | None = None):
        """Sleep for the current delay, then update last-request time."""
        delay = self.get_delay(domain)
        if delay > 0:
            if stop_event:
                stop_event.wait(delay)
            else:
                time.sleep(delay)

    def record_success(self, domain: str):
        with self._lock:
            state = self._get(domain)
            state.consecutive_successes += 1
            state.consecutive_failures = 0
            if state.consecutive_successes >= self.success_streak:
                state.delay = max(state.delay * self.cooldown_factor, self.min_delay)
                state.consecutive_successes = 0
                logger.debug("Rate limiter [%s] delay reduced to %.2fs", domain, state.delay)

    def record_failure(self, domain: str):
        with self._lock:
            state = self._get(domain)
            state.consecutive_successes = 0
            state.consecutive_failures += 1
            state.delay = min(state.delay * self.backoff_factor, self.max_delay)
            logger.info("Rate limiter [%s] delay increased to %.2fs", domain, state.delay)

    def reset(self, domain: str):
        with self._lock:
            self._domains.pop(domain, None)


class _DomainState:
    __slots__ = ("delay", "consecutive_successes", "consecutive_failures")

    def __init__(self, delay: float):
        self.delay = delay
        self.consecutive_successes = 0
        self.consecutive_failures = 0
