"""Shared resilience utilities for Instagram Toolkit.

Provides:
- Graceful Ctrl+C handling with shutdown event
- Interruptible sleep for delays > 1s
- Internet outage detection and resilience
- Automatic retry on network errors

Based on CROSS_TOOLKIT_ANALYSIS.md Section 11.
"""
import signal
import threading
import time
import socket

# Global shutdown event — set on Ctrl+C
_SHUTDOWN = threading.Event()


def _handle_sigint(signum, frame):
    """First Ctrl+C: request graceful stop. Second Ctrl+C: force exit."""
    if _SHUTDOWN.is_set():
        print("\n[FORCE EXIT] Second Ctrl+C — forcing exit now.")
        raise SystemExit(1)
    _SHUTDOWN.set()
    print("\n[STOPPING] Ctrl+C received. Finishing current operation then stopping...")
    print("           Press Ctrl+C again to force exit immediately.")


# Install signal handler
signal.signal(signal.SIGINT, _handle_sigint)


def _interruptible_sleep(seconds: float, check_interval: float = 0.2, shutdown_event=None) -> None:
    """Sleep in short slices so Ctrl+C is observed quickly.

    Delegates to rate_limiter._interruptible_sleep for consistent output.
    """
    if seconds <= 0:
        return
    end_time = time.time() + seconds
    while True:
        if _SHUTDOWN.is_set():
            return
        if shutdown_event and shutdown_event.is_set():
            return
        remaining = end_time - time.time()
        if remaining <= 0:
            return
        time.sleep(min(check_interval, remaining))


def _is_internet_available(host: str = "8.8.8.8", port: int = 53, timeout: int = 3) -> bool:
    """Fast internet check via DNS socket (no HTTP overhead).

    Args:
        host: DNS server to connect to (default Google DNS)
        port: Port to connect (default DNS port 53)
        timeout: Connection timeout in seconds

    Returns:
        True if internet is available, False otherwise
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except (socket.error, OSError):
        return False


def wait_for_internet(
    check_interval: float = 5.0,
    shutdown_event=None,
    label: str = "internet"
) -> bool:
    """
    Block until internet is available or shutdown is requested.

    Args:
        check_interval: Seconds between connection checks
        shutdown_event: Optional threading.Event to check for shutdown
        label: Label for log messages

    Returns:
        True if internet came back, False if shutdown was requested
    """
    if _is_internet_available():
        return True

    print(f"\n[OFFLINE] No internet connection detected.")
    print(f"          Waiting for connection to restore...")
    print(f"          Press Ctrl+C to stop waiting and exit.")

    while not _is_internet_available():
        # Check shutdown if event provided
        if shutdown_event and shutdown_event.is_set():
            print(f"[STOPPED] Shutdown requested while waiting for internet.")
            return False
        # Also check global shutdown
        if _SHUTDOWN.is_set():
            print(f"[STOPPED] Global shutdown requested while waiting for internet.")
            return False
        _interruptible_sleep(check_interval)

    print(f"[ONLINE] Internet connection restored. Resuming...")
    return True


def with_internet_retry(
    func,
    *args,
    max_retries: int = 3,
    base_delay: float = 2.0,
    shutdown_event=None,
    **kwargs
):
    """
    Call func(*args, **kwargs) with retry on network errors.

    On connection error: wait for internet, then retry.
    On shutdown: return None immediately.

    Args:
        func: Function to call
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay for exponential backoff
        shutdown_event: Optional threading.Event to check for shutdown
        **kwargs: Keyword arguments for func

    Returns:
        Result of func(*args, **kwargs), or None on shutdown
    """
    for attempt in range(max_retries + 1):
        # Check shutdown at start of each attempt
        if shutdown_event and shutdown_event.is_set():
            return None
        if _SHUTDOWN.is_set():
            return None

        try:
            return func(*args, **kwargs)
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt == max_retries:
                raise
            # Check if it's an internet outage
            if not _is_internet_available():
                restored = wait_for_internet(shutdown_event=shutdown_event)
                if not restored:
                    return None
            else:
                # Transient error — exponential backoff
                delay = base_delay * (2 ** attempt)
                print(f"[RETRY] Attempt {attempt+1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
                _interruptible_sleep(delay, shutdown_event=shutdown_event)


__all__ = [
    "_SHUTDOWN",
    "_handle_sigint",
    "_interruptible_sleep",
    "_is_internet_available",
    "wait_for_internet",
    "with_internet_retry",
]


