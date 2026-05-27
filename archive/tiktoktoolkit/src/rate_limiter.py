"""Adaptive rate limiting with per-domain throttling.

Adapted from searchtoolkit's rate_limiter.py to use tiktoktoolkit's resilience layer.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from . import resilience


class RateLimiter:
    """Smart rate limiter with per-domain throttling and exponential backoff."""

    def __init__(
        self,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        jitter: float = 1.0,
    ):
        """
        Initialize rate limiter.

        Args:
            base_delay: Minimum delay between requests to same domain (seconds).
            max_delay: Maximum backoff delay after repeated failures (seconds).
            jitter: Random jitter to add to delays (seconds).
        """
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter

        self._domain_delays: dict[str, float] = {}
        self._domain_failures: dict[str, int] = {}
        self._lock = threading.RLock()

    def wait(self, url: str) -> None:
        """
        Wait before making a request to the given URL.
        
        Implements per-domain throttling with random jitter.
        """
        domain = urlparse(url).netloc

        with self._lock:
            now = time.time()
            last_request = self._domain_delays.get(domain, 0)
            elapsed = now - last_request

            if elapsed < self.base_delay:
                wait_time = self.base_delay - elapsed + random.uniform(0, self.jitter)
                resilience.interruptible_sleep(wait_time)

            self._domain_delays[domain] = time.time()

    def record_success(self, url: str) -> None:
        """
        Record a successful request.
        
        Reduces failure counter for the domain.
        """
        domain = urlparse(url).netloc

        with self._lock:
            failures = self._domain_failures.get(domain, 0)
            if failures > 0:
                self._domain_failures[domain] = max(0, failures - 1)

    def record_failure(self, url: str, status_code: int = 0) -> None:
        """
        Record a failed request.
        
        Increases failure counter and calculates exponential backoff.
        """
        domain = urlparse(url).netloc

        with self._lock:
            self._domain_failures[domain] = self._domain_failures.get(domain, 0) + 1
            failures = self._domain_failures[domain]
            backoff = min(2 ** failures, self.max_delay)
            self._domain_delays[domain] = time.time() + backoff

    def get_domain_delay(self, url: str) -> float:
        """
        Get the current delay for a domain (for debugging).
        
        Returns:
            Seconds until next allowed request.
        """
        domain = urlparse(url).netloc

        with self._lock:
            last_request = self._domain_delays.get(domain, 0)
            elapsed = time.time() - last_request
            return max(0, self.base_delay - elapsed)

    def reset_domain(self, url: str) -> None:
        """
        Reset rate limiting for a specific domain.
        """
        domain = urlparse(url).netloc

        with self._lock:
            if domain in self._domain_delays:
                del self._domain_delays[domain]
            if domain in self._domain_failures:
                del self._domain_failures[domain]

    def reset_all(self) -> None:
        """
        Reset all rate limiting state.
        """
        with self._lock:
            self._domain_delays.clear()
            self._domain_failures.clear()


class AdaptiveRateLimiter(RateLimiter):
    """
    Adaptive rate limiter that adjusts delays based on server response patterns.
    
    Increases delays when seeing rate limit responses (429, 503),
    decreases delays when seeing consistent success.
    """

    def __init__(
        self,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        min_delay: float = 0.5,
        jitter: float = 1.0,
        adjustment_factor: float = 0.1
    ):
        super().__init__(base_delay, max_delay, jitter)
        self.min_delay = min_delay
        self.adjustment_factor = adjustment_factor
        self._domain_success_streaks: dict[str, int] = {}

    def record_success(self, url: str) -> None:
        """Record success and potentially reduce delay."""
        domain = urlparse(url).netloc

        with self._lock:
            self._domain_success_streaks[domain] = self._domain_success_streaks.get(domain, 0) + 1

            if self._domain_success_streaks[domain] >= 5:
                current_delay = self.base_delay
                new_delay = max(self.min_delay, current_delay * (1 - self.adjustment_factor))
                self.base_delay = new_delay
                self._domain_success_streaks[domain] = 0

            super().record_success(url)

    def record_failure(self, url: str, status_code: int = 0) -> None:
        """Record failure and increase delay."""
        domain = urlparse(url).netloc

        with self._lock:
            self._domain_success_streaks[domain] = 0

            if status_code in (429, 503):
                self.base_delay = min(self.max_delay, self.base_delay * (1 + self.adjustment_factor))

            super().record_failure(url, status_code)
