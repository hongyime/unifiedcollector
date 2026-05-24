"""
P0.2 Bug-Condition Exploration Test — Stuck Worker Never Respawned

**Validates: Requirements 1.2, 2.2**

BUG CONDITION:
  (now() - worker.last_heartbeat_ts) > HEARTBEAT_TIMEOUT
  AND worker.task_handle IS NOT cancelled

EXPECTED OUTCOME (on UNFIXED code):
  This test FAILS — _monitor_heartbeats() only logs a warning and sends a
  Telegram alert; it never cancels the stuck worker task or spawns a
  replacement.  The same Task object remains in _workers[worker_id].

HOW THE BUG MANIFESTS:
  _monitor_heartbeats() identifies stuck workers (heartbeat > 300 s old)
  and appends them to `stuck_workers`, then logs a message and tries to
  send a Telegram bot message.  It does NOT call task.cancel() and does
  NOT create a replacement task.  The worker slot is permanently occupied.

DOCUMENTED COUNTEREXAMPLE (from first run on unfixed code):
  Falsifying example: test_stuck_workers_never_respawned(stuck_worker_ids={0})
  stuck_worker_ids={0} → after _monitor_heartbeats() one cycle:
    pq._workers[0] is the SAME Task object as before the monitor ran.
    The task was NOT cancelled (task.cancelled() == False).
    No replacement was spawned.
  AssertionError: BUG CONFIRMED: Worker 0 has stale heartbeat (400 s overdue)
    but _monitor_heartbeats() did NOT replace it. Same Task object still in
    _workers[0]. task.cancelled()=False
  Conclusion: worker 0 stale 400 s → same Task object in _workers[0]
              after one monitor cycle.

NOTE: This test is intentionally written to FAIL on unfixed code.
  Do NOT fix the code when this test fails — the failure IS the proof.
"""

import asyncio
import sys
import os
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEARTBEAT_TIMEOUT = 300  # seconds — matches processing_queue.py


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_queue_with_stuck_workers(loop, stuck_worker_ids: set):
    """
    Build a ProcessingQueue where the workers in `stuck_worker_ids` have
    stale heartbeats (now - 400 s) and the rest have fresh heartbeats.

    Returns (pq, original_worker_tasks) where original_worker_tasks is a
    dict mapping worker_id -> asyncio.Task (the task object before monitor).
    """
    from shared.processing_queue import ProcessingQueue

    num_workers = 3  # fixed pool size for this test

    pq = ProcessingQueue.__new__(ProcessingQueue)

    # Minimal attribute setup
    pq.redis_available = True
    pq.fallback_queue = asyncio.Queue()
    pq.queue_key = "processing_queue:tasks"
    pq.dead_letter_key = "processing_queue:dead_letter"
    pq.num_workers = num_workers
    pq._running = True
    pq._workers = []
    pq._active_tasks = {}
    pq._monitor_task = None
    pq.stats = {"processed": 0, "faces_found": 0, "new_identities": 0, "errors": 0}
    pq.max_task_retries = 10
    pq.task_timeout_seconds = 600
    pq.worker_memory_limit_mb = 2048
    pq._backpressure_callbacks = []
    pq._per_chat_times = {}
    pq._processing_times = []
    pq._last_known_redis_size = 0
    pq.high_watermark = 100
    pq.low_watermark = 20
    pq.manual_pause = False

    now = int(time.time())

    # Build a mock Redis client that returns heartbeat timestamps
    mock_redis = MagicMock()

    def _fake_redis_get(key):
        # key format: "worker_heartbeat:{worker_id}"
        try:
            wid = int(key.split(":")[-1])
        except (ValueError, IndexError):
            return None
        if wid in stuck_worker_ids:
            # Stale: 400 seconds ago (well past the 300 s timeout)
            return str(now - 400).encode()
        else:
            # Fresh: 10 seconds ago
            return str(now - 10).encode()

    mock_redis.get.side_effect = _fake_redis_get
    pq.redis_client = mock_redis

    # Create real asyncio tasks for each worker (long-sleeping, simulating stuck)
    original_tasks = {}
    worker_dict = {}

    for i in range(num_workers):
        async def _long_sleep(wid=i):
            try:
                await asyncio.sleep(3600)  # "stuck" — never finishes
            except asyncio.CancelledError:
                pass

        t = loop.create_task(_long_sleep())
        original_tasks[i] = t
        worker_dict[i] = t

    pq._workers = worker_dict
    return pq, original_tasks


async def _run_one_heartbeat_cycle_real(pq):
    """
    Run exactly ONE iteration of the real _monitor_heartbeats inner body
    by patching asyncio.sleep to raise CancelledError after the first sleep,
    which exits the while loop after one pass.

    We patch:
      - asyncio.sleep  → raises CancelledError on first call (exits the loop)
      - collector.account_manager.get_bot_client → returns a mock bot
    so the real code path is exercised without network calls.
    """
    import asyncio as _asyncio

    sleep_call_count = 0
    original_sleep = _asyncio.sleep

    async def _patched_sleep(delay):
        nonlocal sleep_call_count
        sleep_call_count += 1
        if sleep_call_count == 1:
            # First sleep(60) at the top of the loop — skip it (return immediately)
            return
        # Any subsequent sleep should not happen in one cycle, but just in case:
        raise asyncio.CancelledError("test: exit after one cycle")

    # Mock bot so the Telegram alert path doesn't crash
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock()

    with patch("asyncio.sleep", side_effect=_patched_sleep), \
         patch("shared.processing_queue.asyncio.sleep", side_effect=_patched_sleep), \
         patch("services.collector.account_manager.get_bot_client", new=AsyncMock(return_value=mock_bot)), \
         patch("shared.config.get_hub_group_id", return_value=None), \
         patch("shared.config.resolve_hub_group_id", new=AsyncMock(return_value=None)):
        try:
            await pq._monitor_heartbeats()
        except asyncio.CancelledError:
            pass  # Expected — we forced exit after one cycle


# ---------------------------------------------------------------------------
# Bug-Condition Property Test
# ---------------------------------------------------------------------------

@given(stuck_worker_ids=st.sets(st.integers(0, 2)))
@h_settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_stuck_workers_never_respawned(stuck_worker_ids):
    """
    **Validates: Requirements 1.2, 2.2**

    BUG CONDITION: Workers with stale heartbeats (> HEARTBEAT_TIMEOUT) are
    detected by _monitor_heartbeats() but are never cancelled or replaced.

    EXPECTED OUTCOME on UNFIXED code: FAILS
      - The same Task object remains in _workers[worker_id] after the monitor
      - task.cancelled() is False for stuck workers
      - No new Task was spawned to replace the stuck one

    When this test FAILS it proves the bug exists.
    """
    if not stuck_worker_ids:
        # Empty set: no stuck workers → nothing to assert, skip
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        pq, original_tasks = _build_queue_with_stuck_workers(loop, stuck_worker_ids)

        # Run one real heartbeat monitor cycle
        await _run_one_heartbeat_cycle_real(pq)

        # --- Assert: stuck workers WERE replaced (expected behavior after fix) ---
        # On UNFIXED code this assertion FAILS because the same Task object
        # is still in _workers[wid] — proving the bug exists.
        for wid in stuck_worker_ids:
            original_task = original_tasks[wid]
            current_task = pq._workers[wid]

            # After fix: current_task should be a NEW Task (not the original)
            # On unfixed code: current_task IS the original → assertion fails
            assert current_task is not original_task, (
                f"BUG CONFIRMED: Worker {wid} has stale heartbeat "
                f"({HEARTBEAT_TIMEOUT + 100} s overdue) but _monitor_heartbeats() "
                f"did NOT replace it. Same Task object still in _workers[{wid}]. "
                f"task.cancelled()={original_task.cancelled()}"
            )

        # Also assert: total worker count is still num_workers (replacement spawned)
        assert len(pq._workers) == pq.num_workers, (
            f"BUG CONFIRMED: After monitor cycle, len(_workers)={len(pq._workers)} "
            f"but expected {pq.num_workers}. Replacement workers were not spawned."
        )

    try:
        loop.run_until_complete(_run())
    finally:
        # Cancel all remaining tasks
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# P0.2 Preservation Test — Healthy Workers Untouched by Monitor
# ---------------------------------------------------------------------------

def _build_queue_with_healthy_workers(loop, healthy_worker_ids: set):
    """
    Build a ProcessingQueue where ALL workers in healthy_worker_ids have
    FRESH heartbeats (now - 10 s, well within the 300 s timeout).

    Returns (pq, original_worker_tasks) mapping worker_id -> asyncio.Task.
    """
    from shared.processing_queue import ProcessingQueue

    num_workers = 3

    pq = ProcessingQueue.__new__(ProcessingQueue)

    pq.redis_available = True
    pq.fallback_queue = asyncio.Queue()
    pq.queue_key = "processing_queue:tasks"
    pq.dead_letter_key = "processing_queue:dead_letter"
    pq.num_workers = num_workers
    pq._running = True
    pq._workers = []
    pq._active_tasks = {}
    pq._monitor_task = None
    pq.stats = {"processed": 0, "faces_found": 0, "new_identities": 0, "errors": 0}
    pq.max_task_retries = 10
    pq.task_timeout_seconds = 600
    pq.worker_memory_limit_mb = 2048
    pq._backpressure_callbacks = []
    pq._per_chat_times = {}
    pq._processing_times = []
    pq._last_known_redis_size = 0
    pq.high_watermark = 100
    pq.low_watermark = 20
    pq.manual_pause = False

    now = int(time.time())

    mock_redis = MagicMock()

    def _fake_redis_get(key):
        try:
            wid = int(key.split(":")[-1])
        except (ValueError, IndexError):
            return None
        # ALL workers get a FRESH heartbeat (10 s ago — well within 300 s timeout)
        return str(now - 10).encode()

    mock_redis.get.side_effect = _fake_redis_get
    pq.redis_client = mock_redis

    original_tasks = {}
    worker_dict = {}

    for i in range(num_workers):
        async def _long_sleep(wid=i):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass

        t = loop.create_task(_long_sleep())
        original_tasks[i] = t
        worker_dict[i] = t

    pq._workers = worker_dict
    return pq, original_tasks


@given(healthy_worker_ids=st.sets(st.integers(0, 2)))
@h_settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_healthy_workers_untouched_by_monitor(healthy_worker_ids):
    """
    **Validates: Requirements 3.1**

    PRESERVATION TEST — Non-bug condition:
      (now() - worker.last_heartbeat_ts) <= HEARTBEAT_TIMEOUT  (fresh heartbeat)

    Inject FRESH heartbeat timestamps (now - 10 s) for all workers,
    call _monitor_heartbeats() once, and assert that every Task object
    in _workers is UNCHANGED (same object, not cancelled).

    EXPECTED OUTCOME on UNFIXED code: PASSES (healthy workers are never touched).
    This documents the baseline that must NOT regress after the fix.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        pq, original_tasks = _build_queue_with_healthy_workers(loop, healthy_worker_ids)

        # Run one real heartbeat monitor cycle
        await _run_one_heartbeat_cycle_real(pq)

        # Preservation assertion: every worker Task must be the SAME object
        # (monitor must not have touched healthy workers)
        for wid in range(pq.num_workers):
            original_task = original_tasks[wid]
            current_task = pq._workers[wid]

            assert current_task is original_task, (
                f"REGRESSION: Worker {wid} has a FRESH heartbeat "
                f"({HEARTBEAT_TIMEOUT - 10} s remaining) but _monitor_heartbeats() "
                f"replaced or cancelled it. Task object changed after monitor cycle."
            )

            assert not original_task.cancelled(), (
                f"REGRESSION: Worker {wid} was cancelled by the monitor despite "
                f"having a fresh heartbeat (now - 10 s, within {HEARTBEAT_TIMEOUT} s timeout)."
            )

    try:
        loop.run_until_complete(_run())
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)
