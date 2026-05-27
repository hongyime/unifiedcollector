"""Shared resilience utilities — graceful shutdown, internet retry, interruptible sleep."""
import threading
import time
import socket
from typing import Any, Callable, Optional

# Global shutdown flag — set on first Ctrl+C, checked by all long-running loops
_SHUTDOWN = threading.Event()


def _interruptible_sleep(
    seconds: float,
    check_interval: float = 0.2,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Sleep in short slices so Ctrl+C is observed quickly."""
    if seconds <= 0:
        return
    end = time.time() + seconds
    while True:
        if _SHUTDOWN.is_set():
            return
        if shutdown_event and shutdown_event.is_set():
            return
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(check_interval, remaining))


def _is_internet_available(host: str = "8.8.8.8", port: int = 53, timeout: int = 3) -> bool:
    """Fast internet check via DNS socket (no HTTP overhead)."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except (socket.error, OSError):
        return False


def wait_for_internet(
    check_interval: float = 5.0,
    shutdown_event: Optional[threading.Event] = None,
    label: str = "internet",
) -> bool:
    """Block until internet is available or shutdown is requested.

    Returns True if internet came back, False if shutdown was requested.
    """
    if _is_internet_available():
        return True

    print("\n[OFFLINE] No internet connection detected.")
    print("          Waiting for connection to restore...")
    print("          Press Ctrl+C to stop waiting and exit.")

    while not _is_internet_available():
        if _SHUTDOWN.is_set():
            print("[STOPPED] Shutdown requested while waiting for internet.")
            return False
        if shutdown_event and shutdown_event.is_set():
            print("[STOPPED] Shutdown requested while waiting for internet.")
            return False
        _interruptible_sleep(check_interval)

    print("[ONLINE] Internet connection restored. Resuming...")
    return True


def with_internet_retry(
    func: Callable,
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 2.0,
    shutdown_event: Optional[threading.Event] = None,
    **kwargs: Any,
) -> Optional[Any]:
    """Call func(*args, **kwargs) with retry on network errors.

    On connection error: wait for internet, then retry.
    Returns None if shutdown is requested before completion.
    """
    for attempt in range(max_retries + 1):
        if _SHUTDOWN.is_set():
            return None
        if shutdown_event and shutdown_event.is_set():
            return None
        try:
            return func(*args, **kwargs)
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt == max_retries:
                raise
            if not _is_internet_available():
                restored = wait_for_internet(shutdown_event=shutdown_event)
                if not restored:
                    return None
            else:
                delay = base_delay * (2 ** attempt)
                print(f"[RETRY] Attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
                _interruptible_sleep(delay, shutdown_event=shutdown_event)
    return None
