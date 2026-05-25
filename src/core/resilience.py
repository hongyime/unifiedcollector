import asyncio
import functools
import logging
import random
import socket
import threading
import time
from enum import Enum

logger = logging.getLogger(__name__)


# --- Interruptible sleep ---

def interruptible_sleep(seconds: float, stop_event: threading.Event | None = None, slice_sec: float = 0.2):
    """Sleep in small slices so the caller can be interrupted via stop_event."""
    elapsed = 0.0
    while elapsed < seconds:
        if stop_event and stop_event.is_set():
            return
        chunk = min(slice_sec, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk


# --- Retry decorator ---

def with_retry(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 300.0,
    retryable_exceptions: tuple = (Exception,),
    stop_event: threading.Event | None = None,
):
    """Decorator: exponential backoff with full jitter.

    Usage:
        @with_retry(max_retries=3)
        def fetch_page(url): ...
    """
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    jitter = random.uniform(0, delay)
                    logger.warning(
                        "%s attempt %d/%d failed: %s — retrying in %.1fs",
                        fn.__name__, attempt, max_retries, exc, jitter,
                    )
                    interruptible_sleep(jitter, stop_event)
            # last_exc is guaranteed non-None: max_retries >= 1, and we only
            # reach here via the except branch.
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


# --- Circuit Breaker ---

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Trips open after `failure_threshold` consecutive failures.

    After `recovery_timeout` seconds, allows one probe request (half-open).
    On probe success the circuit closes; on failure it re-opens.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._lock = threading.Lock()
        # Eagerly create the asyncio lock placeholder — guarded by attribute
        # presence at call-site so we never race two instances of it.
        self._async_lock: asyncio.Lock | None = None
        # When OPEN transitions to HALF_OPEN, this token gates concurrent probes:
        # only the first caller to take the probe slot proceeds.
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = False
            return self._state

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._probe_in_flight = False
                logger.warning("Circuit breaker OPEN after %d failures", self._failure_count)

    def allow_request(self) -> bool:
        with self._lock:
            # OPEN → HALF_OPEN transition on read (atomic under lock).
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = False
                else:
                    return False
            if self._state == CircuitState.CLOSED:
                return True
            # HALF_OPEN: only the first probe gets through.
            if self._state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    return False
                self._probe_in_flight = True
                return True
            return False

    async def async_allow_request(self) -> bool:
        """Async-compatible version (the underlying check is already lock-protected)."""
        return self.allow_request()


# --- Async retry decorator ---

def async_retry(
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 300.0,
    retryable_exceptions: tuple = (Exception,),
):
    """Decorator: async exponential backoff with full jitter."""
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except retryable_exceptions as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    jitter = random.uniform(0, delay)
                    logger.warning(
                        "%s attempt %d/%d failed: %s — retrying in %.1fs",
                        fn.__name__, attempt, max_retries, exc, jitter,
                    )
                    await asyncio.sleep(jitter)
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


# --- Internet check ---

def wait_for_internet(
    host: str = "8.8.8.8",
    port: int = 53,
    timeout: float = 3.0,
    poll_interval: float = 10.0,
    stop_event: threading.Event | None = None,
) -> bool:
    """Block until internet is reachable. Returns False only if stop_event fires."""
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except OSError:
            logger.info("No internet — retrying in %.0fs", poll_interval)
            interruptible_sleep(poll_interval, stop_event)
            if stop_event and stop_event.is_set():
                return False
