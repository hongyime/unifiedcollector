"""Resilience utilities: graceful shutdown, internet health, interruptible sleep, retry.

Cross-toolkit standard per CROSS_TOOLKIT_ANALYSIS.md Section 11.
"""

from __future__ import annotations

import socket
import time
import threading
import functools
from typing import Callable, Optional, Any


# Global shutdown event for graceful Ctrl+C handling
_SHUTDOWN = threading.Event()


def signal_shutdown() -> None:
    """Signal that the toolkit should shut down."""
    _SHUTDOWN.set()


def is_shutdown() -> bool:
    """Return True if shutdown has been requested."""
    return _SHUTDOWN.is_set()


def reset_shutdown() -> None:
    """Reset the shutdown flag (useful for tests or re-runs in same process)."""
    _SHUTDOWN.clear()


def _interruptible_sleep(duration: float, check_interval: float = 0.25) -> bool:
    """Sleep in chunks, returning False if shutdown was requested.

    Args:
        duration: Seconds to sleep (may be fractional).
        check_interval: How often to check for shutdown.

    Returns:
        True if slept full duration; False if interrupted by shutdown.
    """
    if duration <= 0:
        return True
    elapsed = 0.0
    while elapsed < duration and not _SHUTDOWN.is_set():
        to_sleep = min(check_interval, duration - elapsed)
        time.sleep(to_sleep)
        elapsed += to_sleep
    return not _SHUTDOWN.is_set()


def interruptible_sleep(duration: float, check_interval: float = 0.25) -> None:
    """Sleep in chunks, stopping early if shutdown requested.

    Convenience wrapper that suppresses the boolean return for readability.
    """
    _interruptible_sleep(duration, check_interval)


def _is_internet_available(host: str = "1.1.1.1", port: int = 53, timeout: float = 3.0) -> bool:
    """Probe internet availability (Cloudflare DNS by default).

    Args:
        host: Target host to try connecting.
        port: Target port (53 for DNS; 80/443 for HTTP/HTTPS).
        timeout: Socket timeout in seconds.

    Returns:
        True if connection succeeds, False otherwise.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def wait_for_internet(poll: float = 5.0, host: str = "1.1.1.1", port: int = 53, timeout: float = 3.0) -> None:
    """Block until internet is available (with optional shutdown check).

    Args:
        poll: Seconds between probes.
        host: Target host to probe.
        port: Target port to probe.
        timeout: Connection timeout per probe.
    """
    while not _is_internet_available(host, port, timeout) and not _SHUTDOWN.is_set():
        _interruptible_sleep(poll)


def with_internet_retry(
    max_retries: int = 3,
    backoff: float = 2.0,
    max_backoff: float = 30.0,
    on_no_internet: Optional[Callable[[], Any]] = None,
) -> Callable:
    """Decorator that retries a function on failure with internet checks.

    Pattern:
        1. On exception, check if internet is available.
        2. If not, wait for internet before next attempt.
        3. Use exponential backoff capped at max_backoff.
        4. Call optional on_no_internet callback if internet was lost.

    Args:
        max_retries: Maximum number of attempts (including the first).
        backoff: Base seconds for exponential backoff.
        max_backoff: Cap on backoff sleep.
        on_no_internet: Optional callback when internet outage is detected.

    Returns:
        Decorated function.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception:
                    if _SHUTDOWN.is_set():
                        raise
                    attempt += 1
                    if attempt >= max_retries:
                        raise
                    # Check internet before sleeping
                    if not _is_internet_available():
                        if on_no_internet:
                            try:
                                on_no_internet()
                            except Exception:
                                pass
                        wait_for_internet()
                    # Exponential backoff with cap
                    sleep_time = min(backoff * (2 ** (attempt - 1)), max_backoff)
                    _interruptible_sleep(sleep_time)
        return wrapper
    return decorator


# Convenience aliases for import ergonomics
internet_available = _is_internet_available
