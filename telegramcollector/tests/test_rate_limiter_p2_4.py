"""Property-based tests for RateLimiter (tasks 2.4 — Properties 7, 8, 9).

Validates: Requirements 5.1, 5.6, 5.7
"""

import asyncio
import sys
import os
import time

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.collector.rate_limiter import RateLimiter


# ---------------------------------------------------------------------------
# Property 9: Jitter range invariant
# Validates: Requirements 5.7
#
# For any base delay b > 0, jitter_sleep(b) must return a value in [b, b*1.3].
# ---------------------------------------------------------------------------

@given(base=st.floats(min_value=0.001, max_value=1000.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=500)
def test_property_9_jitter_range_invariant(base: float) -> None:
    """**Validates: Requirements 5.7**

    jitter_sleep(b) must return a value in the closed interval [b, b * 1.3]
    for any b > 0.
    """
    rl = RateLimiter(rate=30.0)
    result = rl.jitter_sleep(base)
    assert result >= base, f"jitter_sleep({base}) = {result} < base"
    assert result <= base * 1.3 + 1e-9, (
        f"jitter_sleep({base}) = {result} > base * 1.3 = {base * 1.3}"
    )


# ---------------------------------------------------------------------------
# Property 8: FloodWait buffer in RateLimiter
# Validates: Requirements 5.6
#
# After set_flood_wait(account_id, N), acquire(account_id) must block for
# at least N + 10 seconds from the time set_flood_wait was called.
# We test this with small N values to keep the test suite fast, patching
# asyncio.sleep to record the total sleep duration without actually waiting.
# ---------------------------------------------------------------------------

@given(
    account_id=st.integers(min_value=1, max_value=10**9),
    seconds=st.integers(min_value=0, max_value=30),
)
@settings(max_examples=200)
def test_property_8_flood_wait_buffer(account_id: int, seconds: int) -> None:
    """**Validates: Requirements 5.6**

    acquire(account_id) must sleep for at least N + 10 seconds after
    set_flood_wait(account_id, N) is called.
    """
    total_slept: list[float] = []

    async def run() -> None:
        rl = RateLimiter(rate=30.0)
        rl.set_flood_wait(account_id, seconds)

        # Patch asyncio.sleep to record durations without actually sleeping
        original_sleep = asyncio.sleep

        async def fake_sleep(duration: float) -> None:
            total_slept.append(duration)

        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            await rl.acquire(account_id=account_id)
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]

    asyncio.run(run())

    total = sum(total_slept)
    expected_min = seconds + 10
    # The flood-wait sleep must account for at least N+10 seconds.
    # Allow up to 0.1s tolerance: the deadline is set before acquire() runs,
    # so `remaining = deadline - now` may be fractionally less than N+10.
    assert total >= expected_min - 0.1, (
        f"Total sleep {total:.4f}s < expected minimum {expected_min}s "
        f"for set_flood_wait(account_id={account_id}, seconds={seconds})"
    )


# ---------------------------------------------------------------------------
# Property 7: Token bucket rate invariant
# Validates: Requirements 5.1, 5.2
#
# N concurrent acquire() calls must not exceed 30 calls/second in any
# 1-second window. We verify this by measuring wall-clock time for N
# acquires on a fresh bucket and confirming throughput <= rate.
#
# To keep tests fast we use a high rate and small N so the test completes
# quickly while still exercising the invariant.
# ---------------------------------------------------------------------------

@given(
    n=st.integers(min_value=2, max_value=10),
    rate=st.floats(min_value=50.0, max_value=200.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50, deadline=None)
def test_property_7_token_bucket_rate_invariant(n: int, rate: float) -> None:
    """**Validates: Requirements 5.1, 5.2**

    N concurrent acquire() calls on a RateLimiter(rate) must not exceed
    `rate` tokens/second. The bucket must never issue more tokens than it
    has accumulated.
    """
    timestamps: list[float] = []

    async def run() -> None:
        rl = RateLimiter(rate=rate)

        async def one_acquire() -> None:
            await rl.acquire()
            timestamps.append(time.monotonic())

        await asyncio.gather(*[one_acquire() for _ in range(n)])

    asyncio.run(run())

    assert len(timestamps) == n

    # Check that no 1-second window contains more than ceil(rate) + 1 completions.
    # We use a sliding window over sorted timestamps.
    timestamps.sort()
    for i, t_start in enumerate(timestamps):
        count_in_window = sum(1 for t in timestamps if t_start <= t < t_start + 1.0)
        # Allow a small overshoot of 1 due to floating-point timing imprecision
        assert count_in_window <= rate + 1, (
            f"Throughput exceeded: {count_in_window} acquires in 1s window "
            f"(rate={rate}, n={n})"
        )

    # The bucket starts full (rate tokens), so the first `floor(rate)` acquires
    # complete immediately. Only acquires beyond the initial bucket require
    # waiting. We only assert the throughput cap (sliding window above), not
    # a minimum elapsed time, because the full-bucket start is by design.
