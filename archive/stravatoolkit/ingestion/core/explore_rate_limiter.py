"""AdaptiveRateLimiter for the explore scraper — per-domain throttling.

Copied from searchtoolkit/searchtoolkit/rate_limiter.py and adapted for
Strava's polite scraping needs (base_delay=5.0, ~1 req/5s).
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse


def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
    """Sleep in short slices so Ctrl+C is observed quickly."""
    if seconds <= 0:
        return
    end_time = time.time() + seconds
    while True:
        remaining = end_time - time.time()
        if remaining <= 0:
            return
        time.sleep(min(check_interval, remaining))


class AdaptiveRateLimiter:
    """
    Adaptive rate limiter: per-domain throttling, exponential backoff,
    reduces delay after 5 consecutive successes, thread-safe.
    """

    def __init__(
        self,
        base_delay: float = 5.0,
        max_delay: float = 120.0,
        min_delay: float = 2.0,
        jitter: float = 1.5,
        adjustment_factor: float = 0.1,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.min_delay = min_delay
        self.jitter = jitter
        self.adjustment_factor = adjustment_factor
        self._domain_last: dict[str, float] = {}
        self._domain_failures: dict[str, int] = {}
        self._domain_streaks: dict[str, int] = {}
        self._lock = threading.RLock()

    def wait(self, url: str, shutdown_event=None) -> None:
        """Wait the appropriate time before fetching url."""
        domain = urlparse(url).netloc
        with self._lock:
            now = time.time()
            last = self._domain_last.get(domain, 0)
            elapsed = now - last
            wait_time = self.base_delay - elapsed + random.uniform(0, self.jitter)
        if wait_time > 0:
            if shutdown_event:
                shutdown_event.wait(timeout=wait_time)
            else:
                _interruptible_sleep(wait_time)
        with self._lock:
            self._domain_last[domain] = time.time()

    def record_success(self, url: str) -> None:
        domain = urlparse(url).netloc
        with self._lock:
            self._domain_streaks[domain] = self._domain_streaks.get(domain, 0) + 1
            if self._domain_streaks[domain] >= 5:
                self.base_delay = max(self.min_delay, self.base_delay * (1 - self.adjustment_factor))
                self._domain_streaks[domain] = 0
            self._domain_failures[domain] = max(0, self._domain_failures.get(domain, 0) - 1)

    def record_failure(self, url: str, status_code: int = 0) -> None:
        domain = urlparse(url).netloc
        with self._lock:
            self._domain_streaks[domain] = 0
            self._domain_failures[domain] = self._domain_failures.get(domain, 0) + 1
            failures = self._domain_failures[domain]
            if status_code in (429, 503):
                self.base_delay = min(self.max_delay, self.base_delay * (1 + self.adjustment_factor))
            # Backoff: next request delayed by 2^failures seconds
            self._domain_last[domain] = time.time() + min(2 ** failures, self.max_delay)
