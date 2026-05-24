"""Resilience utilities for YouTube Toolkit.

Provides:
- _SHUTDOWN: Global shutdown event for graceful Ctrl+C handling
- _interruptible_sleep: Sleep that respects shutdown event
- wait_for_internet: Wait for internet connection with shutdown support
- with_internet_retry: Retry wrapper for network operations
"""
import socket
import threading
import time
from typing import Callable, Any, Optional

# Global shutdown event - set on first Ctrl+C
_SHUTDOWN = threading.Event()


def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
    """Sleep in short slices so Ctrl+C is observed quickly.
    
    Args:
        seconds: Total time to sleep
        check_interval: How often to check shutdown flag (default 0.2s)
    """
    if seconds <= 0:
        return
    
    end_time = time.time() + seconds
    while True:
        if _SHUTDOWN.is_set():
            return
        
        remaining = end_time - time.time()
        if remaining <= 0:
            return
        
        time.sleep(min(check_interval, remaining))


def _is_internet_available(timeout: float = 3.0) -> bool:
    """Check if internet is available by attempting to connect to Google DNS.
    
    Args:
        timeout: Connection timeout in seconds
        
    Returns:
        True if internet is available, False otherwise
    """
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=timeout)
        return True
    except OSError:
        return False


def wait_for_internet(check_interval: float = 5.0, timeout: Optional[float] = None) -> bool:
    """Wait for internet connection to become available.
    
    Args:
        check_interval: How often to check for internet (default 5s)
        timeout: Maximum time to wait in seconds (None = wait forever)
        
    Returns:
        True if internet became available, False if shutdown requested or timeout
    """
    if _is_internet_available():
        return True
    
    print("[WAITING] No internet connection. Waiting for connection...")
    
    start_time = time.time()
    while True:
        if _SHUTDOWN.is_set():
            print("[SHUTDOWN] Stopped waiting for internet")
            return False
        
        if timeout and (time.time() - start_time) >= timeout:
            print("[TIMEOUT] Gave up waiting for internet")
            return False
        
        _interruptible_sleep(check_interval)
        
        if _is_internet_available():
            print("[CONNECTED] Internet connection restored")
            return True


def with_internet_retry(
    func: Callable,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    **kwargs
) -> Any:
    """Retry a function with exponential backoff on network errors.
    
    Args:
        func: Function to call
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries (exponentially increased)
        **kwargs: Keyword arguments for func
        
    Returns:
        Result of func() if successful
        None if all retries failed or shutdown requested
    """
    import requests
    from urllib3.exceptions import ProtocolError
    
    for attempt in range(max_retries + 1):
        if _SHUTDOWN.is_set():
            return None
        
        try:
            return func(*args, **kwargs)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ProtocolError,
            OSError
        ) as e:
            if attempt == max_retries:
                print(f"[ERROR] Failed after {max_retries} retries: {e}")
                return None
            
            # Check if it's an internet outage
            if not _is_internet_available():
                if not wait_for_internet():
                    return None
                # Internet restored, retry immediately
                continue
            
            # Transient error, exponential backoff
            delay = base_delay * (2 ** attempt)
            print(f"[RETRY] Attempt {attempt + 1}/{max_retries} failed: {e}")
            print(f"[RETRY] Retrying in {delay:.1f}s...")
            _interruptible_sleep(delay)
    
    return None
