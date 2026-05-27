"""
Property tests for CircuitBreaker

Property 16: Circuit Opens After Threshold Failures
**Validates: Requirements 9.1, 9.6**

FOR ALL sequences of Redis operations where the last N consecutive results are
failures (N >= failure_threshold), the CircuitBreaker state SHALL be Open.

Property 17: Success Resets Failure Counter
**Validates: Requirements 9.7**

FOR ALL sequences of Redis operations ending in a success while in Closed state,
the consecutive failure counter SHALL be 0.

Property 18: Circuit Round-Trip (5 failures → 60s → 1 success → CLOSED)
**Validates: Requirements 9.3, 9.4**

A sequence of 5 failures → 60s elapsed → 1 success SHALL always result in
Closed state with failure counter = 0.
"""

import asyncio
import sys
import os
from unittest.mock import patch

# Ensure workspace root is on sys.path so `shared.circuit_breaker` is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from shared.circuit_breaker import CircuitBreaker


# ---------------------------------------------------------------------------
# Property 16: Circuit Opens After Threshold Failures
# ---------------------------------------------------------------------------

@given(
    failure_count=st.integers(min_value=5, max_value=15),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_circuit_opens_after_threshold_failures(failure_count: int) -> None:
    """
    **Validates: Requirements 9.1, 9.6**

    Property 16: FOR ALL sequences of Redis operations where the last N
    consecutive results are failures (N >= failure_threshold), the
    CircuitBreaker state SHALL be Open.
    """

    async def _inner() -> None:
        cb = CircuitBreaker(
            name="test_open_after_threshold",
            failure_threshold=5,
            recovery_timeout=60.0,
        )

        async def failing_fn():
            raise RuntimeError("Redis connection refused")

        # Drive failure_count consecutive failures through the circuit breaker
        for _ in range(failure_count):
            if cb.state == CircuitBreaker.OPEN:
                # Circuit already open — no need to keep calling
                break
            try:
                await cb.call(failing_fn)
            except Exception:
                pass  # expected — we're testing state transitions

        # Core property: after >= threshold failures, circuit must be OPEN
        assert cb.state == CircuitBreaker.OPEN, (
            f"Expected state=OPEN after {failure_count} failures "
            f"(threshold=5), got state={cb.state}"
        )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Property 17: Success Resets Failure Counter
# ---------------------------------------------------------------------------

@given(
    failures_before_success=st.integers(min_value=1, max_value=4),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_success_resets_failure_counter(failures_before_success: int) -> None:
    """
    **Validates: Requirements 9.7**

    Property 17: FOR ALL sequences of Redis operations ending in a success
    while in Closed state, the consecutive failure counter SHALL be 0.
    """

    async def _inner() -> None:
        cb = CircuitBreaker(
            name="test_success_resets",
            failure_threshold=5,
            recovery_timeout=60.0,
        )

        async def failing_fn():
            raise RuntimeError("Redis error")

        async def success_fn():
            return "ok"

        # Accumulate some failures (below threshold so circuit stays CLOSED)
        for _ in range(failures_before_success):
            try:
                await cb.call(failing_fn)
            except Exception:
                pass

        # Verify we're still in CLOSED state with non-zero failure count
        assert cb.state == CircuitBreaker.CLOSED, (
            f"Expected CLOSED state before success call, got {cb.state}"
        )
        assert cb.failure_count == failures_before_success, (
            f"Expected failure_count={failures_before_success}, got {cb.failure_count}"
        )

        # Now call a successful operation
        result = await cb.call(success_fn)
        assert result == "ok"

        # Core property: failure counter must be reset to 0 after success
        assert cb.failure_count == 0, (
            f"Expected failure_count=0 after success, got {cb.failure_count}"
        )
        assert cb.state == CircuitBreaker.CLOSED, (
            f"Expected state=CLOSED after success, got {cb.state}"
        )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Property 18: Circuit Round-Trip
# ---------------------------------------------------------------------------

@given(
    # Vary the time elapsed beyond the 60s recovery timeout
    extra_elapsed=st.floats(min_value=0.0, max_value=30.0),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_circuit_round_trip(extra_elapsed: float) -> None:
    """
    **Validates: Requirements 9.3, 9.4**

    Property 18: A sequence of 5 failures → 60s elapsed → 1 success SHALL
    always result in Closed state with failure counter = 0, regardless of
    any intermediate state history.
    """

    async def _inner() -> None:
        cb = CircuitBreaker(
            name="test_round_trip",
            failure_threshold=5,
            recovery_timeout=60.0,
        )

        async def failing_fn():
            raise RuntimeError("Redis down")

        async def success_fn():
            return "recovered"

        # Step 1: 5 consecutive failures → circuit should open
        for _ in range(5):
            try:
                await cb.call(failing_fn)
            except Exception:
                pass

        assert cb.state == CircuitBreaker.OPEN, (
            f"Expected OPEN after 5 failures, got {cb.state}"
        )

        # Step 2: Simulate 60s+ elapsed by mocking time.monotonic()
        # The circuit opened at _opened_at; we mock monotonic to return
        # _opened_at + 60 + extra_elapsed so the recovery timeout has passed.
        opened_at = cb._opened_at
        simulated_now = opened_at + 60.0 + extra_elapsed

        with patch("shared.circuit_breaker.time.monotonic", return_value=simulated_now):
            # Step 3: 1 success — circuit should transition OPEN → HALF_OPEN → CLOSED
            result = await cb.call(success_fn)

        assert result == "recovered"

        # Core property: round-trip must end in CLOSED with failure_count == 0
        assert cb.state == CircuitBreaker.CLOSED, (
            f"Expected CLOSED after round-trip, got {cb.state}"
        )
        assert cb.failure_count == 0, (
            f"Expected failure_count=0 after round-trip, got {cb.failure_count}"
        )

    asyncio.run(_inner())
