import time
from collections.abc import Callable
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class CircuitOpenError(Exception):
    """Raised when a call is attempted while the circuit is Open."""
    pass


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.name = name
        self.state = self.CLOSED
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._opened_at: float | None = None

    async def call(self, fn: Callable[[], Any]) -> Any:
        if self.state == self.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.recovery_timeout:
                self.state = self.HALF_OPEN
                logger.info("circuit_half_open", name=self.name, elapsed=round(elapsed, 1))
            else:
                raise CircuitOpenError(f"Circuit {self.name} is OPEN")

        try:
            result = await fn()
            self._on_success()
            return result
        except CircuitOpenError:
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        if self.state == self.HALF_OPEN or self.failure_count > 0:
            logger.info("dedup_circuit_closed", name=self.name)
        self.failure_count = 0
        self.state = self.CLOSED

    def _on_failure(self) -> None:
        if self.state == self.HALF_OPEN:
            self.state = self.OPEN
            self._opened_at = time.monotonic()
            logger.warning("circuit_half_open_probe_failed", name=self.name)
            return

        # CLOSED state
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = self.OPEN
            self._opened_at = time.monotonic()
            logger.warning(
                "dedup_circuit_opened",
                name=self.name,
                failure_count=self.failure_count,
            )
