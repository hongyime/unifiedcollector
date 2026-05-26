"""Async circuit breaker — three-state (CLOSED/OPEN/HALF_OPEN).

Ported from whatsappcollector/shared/circuit_breaker.py with hardening:

* asyncio.Lock around state transitions — without it, multiple tasks
  racing through HALF_OPEN can all probe simultaneously and one slow
  failure trips the breaker even though others succeeded. The toolkit
  version had this bug; first task that finishes flips state.
* ``expected_exception`` filter so transient unrelated exceptions
  (asyncio.CancelledError, KeyboardInterrupt) don't count as failures.
  Default counts every Exception EXCEPT BaseException-only subclasses.
* Stdlib ``logging`` instead of ``structlog`` (no extra dep).
* ``state`` / ``stats`` properties for ops + tests.
* ``reset()`` for explicit recovery (e.g. operator says "the upstream
  is back, don't wait the full recovery_timeout").

States
------
CLOSED:    Normal operation. Failures increment failure_count. When
           failure_count >= failure_threshold, transition to OPEN.
OPEN:      All calls raise CircuitOpenError immediately. After
           recovery_timeout has elapsed, the NEXT call transitions to
           HALF_OPEN.
HALF_OPEN: Exactly ONE probe call is allowed through. Success ->
           CLOSED. Failure -> OPEN with timer reset.

Usage
-----

    breaker = CircuitBreaker("upstream-api", failure_threshold=5,
                             recovery_timeout=60.0)
    try:
        result = await breaker.call(lambda: client.get(url))
    except CircuitOpenError:
        # fail fast — upstream is known-bad
        return None

The protected function ``fn`` MUST be a zero-arg async callable. Wrap
arguments in a closure: ``lambda: client.get(url, params=p)``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is OPEN."""


class CircuitBreaker:
    """Three-state circuit breaker for async callables."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: type[BaseException] | tuple[type[BaseException], ...] = Exception,
    ):
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >=1, got {failure_threshold!r}")
        if recovery_timeout <= 0:
            raise ValueError(f"recovery_timeout must be >0, got {recovery_timeout!r}")

        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self._state: str = self.CLOSED
        self._failure_count: int = 0
        self._opened_at: Optional[float] = None
        self._half_open_in_flight: bool = False
        self._lock = asyncio.Lock()

    # -- read-only state accessors --------------------------------------

    @property
    def state(self) -> str:
        """Current state. Read-only; modify via call()/reset()."""
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def stats(self) -> dict:
        """Snapshot of breaker state. Useful for /metrics or admin UI."""
        return {
            "name": self.name,
            "state": self._state,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "opened_at": self._opened_at,
            "elapsed_since_open": (time.monotonic() - self._opened_at) if self._opened_at else None,
        }

    # -- public ops -----------------------------------------------------

    async def call(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Execute ``fn()`` through the breaker.

        Raises CircuitOpenError immediately if state == OPEN and we're
        not yet past the recovery window.
        """
        # ---- entry gate -----------------------------------------------
        async with self._lock:
            if self._state == self.OPEN:
                elapsed = time.monotonic() - (self._opened_at or 0)
                if elapsed >= self.recovery_timeout:
                    self._state = self.HALF_OPEN
                    self._half_open_in_flight = False
                    logger.info(
                        "circuit %r: OPEN -> HALF_OPEN after %.1fs",
                        self.name, elapsed,
                    )
                else:
                    raise CircuitOpenError(
                        f"circuit {self.name!r} is OPEN "
                        f"({elapsed:.1f}s of {self.recovery_timeout:.1f}s elapsed)"
                    )

            if self._state == self.HALF_OPEN:
                if self._half_open_in_flight:
                    # Another probe is already running. Don't allow a
                    # second concurrent probe — that would defeat the
                    # purpose of single-probe gating.
                    raise CircuitOpenError(
                        f"circuit {self.name!r} is HALF_OPEN with probe in-flight"
                    )
                self._half_open_in_flight = True
                we_are_probe = True
            else:
                we_are_probe = False

        # ---- run the function (NOT under lock) ------------------------
        try:
            result = await fn()
        except CircuitOpenError:
            # Don't trip ourselves on our own gate
            raise
        except BaseException as exc:
            should_count = isinstance(exc, self.expected_exception)
            await self._on_failure(was_probe=we_are_probe, counted=should_count)
            raise
        else:
            await self._on_success(was_probe=we_are_probe)
            return result

    async def reset(self):
        """Force back to CLOSED. Use when operator knows upstream recovered."""
        async with self._lock:
            prev = self._state
            self._state = self.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._half_open_in_flight = False
            if prev != self.CLOSED:
                logger.info("circuit %r: forced reset (was %s)", self.name, prev)

    # -- internal transitions ------------------------------------------

    async def _on_success(self, *, was_probe: bool):
        async with self._lock:
            if was_probe:
                self._half_open_in_flight = False
            if self._state == self.HALF_OPEN or self._failure_count > 0:
                logger.info("circuit %r: -> CLOSED (recovered)", self.name)
            self._state = self.CLOSED
            self._failure_count = 0
            self._opened_at = None

    async def _on_failure(self, *, was_probe: bool, counted: bool):
        async with self._lock:
            if was_probe:
                self._half_open_in_flight = False
                if not counted:
                    # Probe failed but for unexpected reason (e.g. user
                    # cancelled). Stay HALF_OPEN, don't reopen.
                    return
                # Counted failure during probe -> reopen with fresh timer
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit %r: HALF_OPEN probe failed -> OPEN", self.name,
                )
                return

            if not counted:
                return  # exception not in expected_exception

            self._failure_count += 1
            if self._state == self.CLOSED and self._failure_count >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit %r: CLOSED -> OPEN (failures=%d/%d)",
                    self.name, self._failure_count, self.failure_threshold,
                )


__all__ = ["CircuitBreaker", "CircuitOpenError"]
