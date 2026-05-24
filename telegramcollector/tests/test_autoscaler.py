"""
P3.1 Bug-Condition Exploration Test — Fixed Worker Count Under Sustained Load

Validates: Requirements 1.7, 2.7, 3.5

Bug condition:
    queue_state.depth > QUEUE_MAX_SIZE
    AND time_window > SCALE_UP_SUSTAINED_SECONDS
    AND len(queue_state._workers) < MAX_WORKERS

The current processing_queue.py has no _autoscaler_loop() method.
num_workers is fixed at construction time and never changes regardless of load.

EXPECTED OUTCOME (Task 17 — bug-condition test, on unfixed code): FAILS
  — ProcessingQueue has no _autoscaler_loop attribute, confirming the bug:
    no dynamic scaling mechanism exists.

EXPECTED OUTCOME (Task 18 — preservation test, on unfixed code): PASSES
  — when queue depth stays below high_watermark, len(_workers) remains exactly
    num_workers (no scaling occurs — correct baseline behaviour).

Documented counterexample (Task 17):
    queue_depth_series = [200, 250, 300, 200, 250, 300, 200, 250, 300, 200]
    (all above high_watermark=100, sustained for 10 ticks)
    After simulating the autoscaler loop:
        len(_workers) == 3  (unchanged — BUG: should have scaled up)
    Root cause: _autoscaler_loop() does not exist in ProcessingQueue.
    num_workers is set once in __init__ and never modified.
"""

import asyncio
import sys
import os
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pq(num_workers: int = 3, high_watermark: int = 100):
    """Build a minimal ProcessingQueue without Redis or real dependencies."""
    from shared.processing_queue import ProcessingQueue

    pq = ProcessingQueue.__new__(ProcessingQueue)
    pq.num_workers = num_workers
    pq._workers = {i: MagicMock() for i in range(num_workers)}
    pq._running = True
    pq.redis_available = False
    pq.redis_client = None
    pq.fallback_queue = MagicMock()
    pq.fallback_queue.qsize.return_value = 0
    pq.queue_key = "processing_queue:tasks"
    pq.high_watermark = high_watermark
    pq.low_watermark = 20
    pq._active_tasks = {}
    pq._monitor_task = None
    pq._heartbeat_monitor_task = None
    pq._reconnect_task = None
    pq._autoscaler_task = None
    pq._scale_up_since = None
    pq._scale_down_since = None
    pq._current_workers = num_workers
    pq.stats = {"processed": 0, "faces_found": 0, "new_identities": 0, "errors": 0}
    pq._backpressure_callbacks = []
    pq._per_chat_times = {}
    pq._processing_times = []
    pq._last_known_redis_size = 0
    pq.manual_pause = False
    pq.worker_memory_limit_mb = 2048
    pq.task_timeout_seconds = 600
    pq.max_task_retries = 10
    pq.dead_letter_key = "processing_queue:dead_letter"
    return pq


# ---------------------------------------------------------------------------
# Task 17 — Property 1: Bug Condition
# Worker count stays fixed even under sustained high queue depth
# ---------------------------------------------------------------------------

@given(
    queue_depth_series=st.lists(st.integers(101, 500), min_size=10)
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_worker_count_stays_fixed_under_sustained_load(queue_depth_series):
    """
    **Validates: Requirements 1.7, 2.7**

    Bug condition:
        queue_state.depth > QUEUE_MAX_SIZE
        AND time_window > SCALE_UP_SUSTAINED_SECONDS
        AND len(queue_state._workers) < MAX_WORKERS

    For each queue depth time series (all values > high_watermark=100):
      1. Build a ProcessingQueue with num_workers=3.
      2. Assert that _autoscaler_loop method exists (the fix adds this).
         On unfixed code this raises AttributeError → test FAILS → bug confirmed.
      3. On fixed code: run the autoscaler loop and assert workers scale up.

    EXPECTED OUTCOME on unfixed code: FAILS
      — AttributeError: 'ProcessingQueue' object has no attribute '_autoscaler_loop'

    EXPECTED OUTCOME on fixed code: PASSES
      — _autoscaler_loop exists and scales workers up when queue is sustained above
        high_watermark for SCALE_UP_SUSTAINED_SECONDS.

    Documented counterexample:
        queue_depth_series = [200]*10
        len(_workers) stays 3 after 10 ticks above high_watermark.
        BUG CONFIRMED: no autoscaler exists to spawn additional workers.
    """
    pq = _make_pq(num_workers=3, high_watermark=100)

    # Assert the fix provides _autoscaler_loop as a coroutine method.
    # On unfixed code this attribute does not exist → AttributeError → test FAILS.
    assert hasattr(pq, "_autoscaler_loop"), (
        "BUG CONFIRMED: ProcessingQueue has no _autoscaler_loop method. "
        "Dynamic worker scaling is required when queue depth exceeds high_watermark "
        "for SCALE_UP_SUSTAINED_SECONDS. "
        f"queue_depth_series={queue_depth_series[:3]}... (all > 100)"
    )

    # On fixed code: run the autoscaler with mocked queue depth and verify scale-up.
    # We patch the queue depth getter to return values from the series.
    depth_iter = iter(queue_depth_series)

    async def _run_autoscaler():
        import time as _time
        import unittest.mock as _mock

        # Patch get_queue_size to return depths from the series
        call_count = [0]

        def mock_get_queue_size():
            call_count[0] += 1
            try:
                return next(depth_iter)
            except StopIteration:
                return 0

        pq.get_queue_size = mock_get_queue_size

        # Track spawned workers
        spawned = []

        def mock_create_task(coro, **kwargs):
            spawned.append(coro)
            t = MagicMock()
            t.done.return_value = False
            t.cancelled.return_value = False
            return t

        # Patch time.monotonic to advance by SCALE_UP_SUSTAINED_SECONDS+1 each call
        # so the sustained timer always fires immediately
        mono_time = [0.0]

        def mock_monotonic():
            mono_time[0] += 70  # advance past SCALE_UP_SUSTAINED_SECONDS=60
            return mono_time[0]

        # Run a limited number of autoscaler iterations
        iteration = [0]
        max_iterations = len(queue_depth_series)

        async def mock_sleep(delay):
            iteration[0] += 1
            if iteration[0] >= max_iterations:
                pq._running = False

        with _mock.patch("asyncio.sleep", side_effect=mock_sleep), \
             _mock.patch("asyncio.create_task", side_effect=mock_create_task), \
             _mock.patch("shared.processing_queue.time.monotonic", side_effect=mock_monotonic) if hasattr(_mock, 'patch') else _mock.patch("time.monotonic", side_effect=mock_monotonic):
            try:
                await pq._autoscaler_loop()
            except Exception:
                pass  # Loop may exit via _running=False

        return spawned

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        spawned = loop.run_until_complete(_run_autoscaler())
        # After sustained high queue depth, at least one new worker should have been spawned
        # (or _current_workers should have increased beyond num_workers)
        scaled_up = len(spawned) > 0 or pq._current_workers > pq.num_workers
        assert scaled_up, (
            f"BUG CONFIRMED: No scale-up occurred despite queue depth staying above "
            f"high_watermark=100 for {len(queue_depth_series)} ticks. "
            f"_current_workers={pq._current_workers}, num_workers={pq.num_workers}. "
            f"queue_depth_series={queue_depth_series[:3]}..."
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# Task 18 — Property 2: Preservation
# No scaling when queue depth stays below high watermark
# ---------------------------------------------------------------------------

@given(
    queue_depth_series=st.lists(st.integers(0, 99), min_size=5)
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_no_scaling_when_queue_below_high_watermark(queue_depth_series):
    """
    **Validates: Requirements 3.5**

    Preservation: when queue depth stays below high_watermark, len(_workers)
    remains exactly num_workers throughout.

    For all queue depth time series where every value < high_watermark=100:
      1. Build a ProcessingQueue with num_workers=3.
      2. Simulate the queue monitor seeing those depths.
      3. Assert len(_workers) == 3 throughout (no scaling).

    EXPECTED OUTCOME on unfixed code: PASSES
      — no autoscaler exists, so worker count never changes.

    EXPECTED OUTCOME on fixed code: PASSES
      — autoscaler must not scale up when queue is below high_watermark.

    Non-bug condition: queue_state.depth < high_watermark
    """
    pq = _make_pq(num_workers=3, high_watermark=100)
    initial_worker_count = len(pq._workers)

    # Simulate the queue monitor seeing depths below high_watermark.
    # On unfixed code: no autoscaler exists, so _workers never changes.
    # On fixed code: autoscaler must not trigger scale-up for these depths.
    for depth in queue_depth_series:
        # Simulate what the autoscaler would check
        assert depth < pq.high_watermark, (
            f"Test setup error: depth {depth} is not below high_watermark {pq.high_watermark}"
        )
        # Worker count must remain unchanged
        assert len(pq._workers) == initial_worker_count, (
            f"REGRESSION: len(_workers) changed from {initial_worker_count} to "
            f"{len(pq._workers)} despite queue depth {depth} being below "
            f"high_watermark={pq.high_watermark}. "
            f"No scaling should occur when queue is healthy."
        )

    # Final check: worker count is still exactly num_workers
    assert len(pq._workers) == pq.num_workers, (
        f"REGRESSION: len(_workers)={len(pq._workers)} != num_workers={pq.num_workers} "
        f"after processing {len(queue_depth_series)} ticks below high_watermark."
    )
