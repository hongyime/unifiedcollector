"""Utility functions for managing delays to prevent rate limiting issues with Strava API."""

from __future__ import annotations

import random
import time
from threading import Event
from typing import Callable


def random_delay(
    delay_range: tuple[float, float],
    *,
    jitter: float = 0.2,
    delay_func: Callable[[], None] = time.sleep,
    debug: bool = False,
    shutdown_event: Event | None = None,
) -> None:
    """
    Apply a random delay within the specified range with optional jitter.

    Args:
        delay_range: (min_delay, max_delay) in seconds
        jitter: Additional random variation as a fraction of the base delay (0-1)
        delay_func: Function to handle the delay (default: time.sleep)
        debug: If True, print delay information
        shutdown_event: Optional threading.Event to allow immediate interruption on shutdown

    Example:
        random_delay((1.0, 3.0))  # Delay between 1.0 and 3.0 seconds
        random_delay((2.0, 4.0), jitter=0.1)  # Delay with 10% jitter
        random_delay((1.0, 3.0), shutdown_event=event)  # Interruptible delay
    """
    min_delay, max_delay = delay_range
    if max_delay < min_delay:
        min_delay, max_delay = max_delay, min_delay
    
    base_delay = random.uniform(min_delay, max_delay)
    
    # Add jitter for additional randomness
    if jitter > 0:
        jitter_amount = base_delay * jitter * random.uniform(-1, 1)
        final_delay = base_delay + jitter_amount
    else:
        final_delay = base_delay
    
    final_delay = max(0, final_delay)  # Ensure non-negative
    
    if debug:
        print(f"[delay] Sleeping for {final_delay:.2f}s (base: {base_delay:.2f}s)")
    
    # Use interruptible delay if shutdown_event is provided
    if shutdown_event is not None:
        # shutdown_event.wait(timeout) returns True if event is set, False if timeout expires
        # If event is set during delay, return immediately
        shutdown_event.wait(timeout=final_delay)
    else:
        # Backward compatibility: use time.sleep() if no shutdown_event
        delay_func(final_delay)


def exponential_backoff(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.5,
    *,
    delay_func: Callable[[], None] = time.sleep,
    debug: bool = False,
) -> float:
    """
    Calculate delay using exponential backoff strategy.

    Args:
        attempt: Current attempt number (0-based or 1-based)
        base_delay: Starting delay time in seconds
        max_delay: Maximum allowed delay in seconds
        backoff_factor: Multiplier for each retry attempt (typically 2.0)
        jitter: Random variation as a fraction of the calculated delay (0-1)
        delay_func: Function to handle the delay (default: time.sleep)
        debug: If True, print delay information

    Returns:
        The calculated delay time in seconds

    Example:
        delay = exponential_backoff(attempt=1, base_delay=2.0)
        delay_func(delay)
    """
    # Calculate base exponential delay
    exp_delay = base_delay * (backoff_factor ** attempt)
    
    # Cap at maximum delay
    exp_delay = min(exp_delay, max_delay)
    
    # Add jitter to avoid thundering herd
    if jitter > 0:
        jitter_amount = exp_delay * jitter * random.uniform(-1, 1)
        final_delay = exp_delay + jitter_amount
    else:
        final_delay = exp_delay
    
    final_delay = max(0, final_delay)  # Ensure non-negative
    
    if debug:
        print(
            f"[backoff] Attempt {attempt}: calculated delay {final_delay:.2f}s "
            f"(exp: {exp_delay:.2f}s, base: {base_delay:.2f}s, max: {max_delay:.2f}s)"
        )
    
    return final_delay


class DelayManager:
    """
    Context-aware delay manager for different types of API calls.

    This helps maintain appropriate delays for different API endpoints
    and use cases.
    """

    def __init__(
        self,
        *,
        api_delay_range: tuple[float, float] = (1.0, 3.0),
        feed_delay_range: tuple[float, float] = (1.5, 4.0),
        backfill_delay_range: tuple[float, float] = (2.0, 5.0),
        stream_delay_range: tuple[float, float] = (1.0, 2.5),
        roster_delay_range: tuple[float, float] = (1.5, 3.5),
        debug: bool = False,
    ):
        self.api_delay_range = api_delay_range
        self.feed_delay_range = feed_delay_range
        self.backfill_delay_range = backfill_delay_range
        self.stream_delay_range = stream_delay_range
        self.roster_delay_range = roster_delay_range
        self.debug = debug

    def api_delay(self) -> None:
        """Delay for general API calls."""
        random_delay(self.api_delay_range, debug=self.debug)

    def feed_delay(self) -> None:
        """Delay for feed API calls."""
        random_delay(self.feed_delay_range, debug=self.debug)

    def backfill_delay(self) -> None:
        """Delay for backfill/history API calls."""
        random_delay(self.backfill_delay_range, debug=self.debug)

    def stream_delay(self) -> None:
        """Delay for stream data API calls."""
        random_delay(self.stream_delay_range, debug=self.debug)

    def roster_delay(self) -> None:
        """Delay for roster/following API calls."""
        random_delay(self.roster_delay_range, debug=self.debug)


def create_delay_manager(settings: dict) -> DelayManager:
    """
    Factory function to create a DelayManager from settings dictionary.

    Args:
        settings: Dictionary containing delay configuration

    Returns:
        DelayManager instance configured with provided settings
    """
    return DelayManager(
        api_delay_range=(
            settings.get("api_delay_min_seconds", 1.0),
            settings.get("api_delay_max_seconds", 3.0),
        ),
        feed_delay_range=(
            settings.get("feed_delay_min_seconds", 1.5),
            settings.get("feed_delay_max_seconds", 4.0),
        ),
        backfill_delay_range=(
            settings.get("backfill_delay_min_seconds", 2.0),
            settings.get("backfill_delay_max_seconds", 5.0),
        ),
        stream_delay_range=(
            settings.get("stream_delay_min_seconds", 1.0),
            settings.get("stream_delay_max_seconds", 2.5),
        ),
        roster_delay_range=(
            settings.get("roster_delay_min_seconds", 1.5),
            settings.get("roster_delay_max_seconds", 3.5),
        ),
        debug=settings.get("debug_delays", False),
    )


import socket


def _is_internet_available(host: str = "8.8.8.8", port: int = 53, timeout: float = 3) -> bool:
    """Fast internet check via DNS socket (no HTTP overhead)."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except (socket.error, OSError):
        return False


def wait_for_internet(shutdown_event=None, check_interval: float = 5.0) -> bool:
    """
    Block until internet is available or shutdown is requested.
    Returns True if internet came back, False if shutdown was requested.
    """
    if _is_internet_available():
        return True
    print("[OFFLINE] No internet connection detected. Waiting for connection to restore...")
    print("          Press Ctrl+C to stop waiting and exit.")
    while not _is_internet_available():
        if shutdown_event and shutdown_event.is_set():
            print("[STOPPED] Shutdown requested while waiting for internet.")
            return False
        if shutdown_event:
            shutdown_event.wait(timeout=check_interval)
        else:
            time.sleep(check_interval)
    print("[ONLINE] Internet connection restored. Resuming...")
    return True