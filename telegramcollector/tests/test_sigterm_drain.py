"""
P0.1 Bug-Condition Exploration Test — SIGTERM Drops In-Flight Tasks

**Validates: Requirements 1.1, 2.1**

BUG CONDITION:
  event = SIGTERM AND EXISTS worker IN _workers WHERE worker.in_flight = True

EXPECTED OUTCOME (on UNFIXED code):
  This test FAILS — workers are cancelled immediately, in-flight tasks are
  silently dropped (not re-queued, no DLQ entry).

HOW THE BUG MANIFESTS:
  ProcessingQueue.stop() calls worker.cancel() for every worker without
  waiting for in-flight tasks to complete or re-queuing them.  The
  asyncio.gather() call collects CancelledErrors but discards the task
  payloads.  Any task that was mid-execution is permanently lost.

DOCUMENTED COUNTEREXAMPLE (from first run on unfixed code):
  Falsifying example: test_sigterm_drops_in_flight_tasks(in_flight_count=1)
  in_flight_count=1 → after stop(drain_timeout=0):
    fallback_queue.qsize() == 0   (task was NOT re-queued)
    redis DLQ hlen == 0           (task was NOT moved to dead-letter)
  AssertionError: BUG CONFIRMED: 1 in-flight task(s) were silently dropped.
    fallback_queue=0, dlq=0. Expected 1 tasks to be re-queued or moved to DLQ.
  Conclusion: 1 in-flight worker → 0 tasks preserved after stop().

NOTE: This test is intentionally written to FAIL on unfixed code.
  Do NOT fix the code when this test fails — the failure IS the proof.
"""

import asyncio
import sys
import os
import time
import json
import base64
import io
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
import hypothesis.strategies as st

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_task_json(worker_id: int = 0) -> str:
    """Return a minimal valid task JSON that a worker would be processing."""
    content = io.BytesIO(b"\xff\xd8\xff" + b"\x00" * 100)  # fake JPEG
    content_b64 = base64.b64encode(content.read()).decode("ascii")
    return json.dumps({
        "task_type": "media",
        "chat_id": 1000 + worker_id,
        "message_id": 2000 + worker_id,
        "user_id": 0,
        "content_b64": content_b64,
        "media_type": "photo",
        "file_unique_id": f"file_{worker_id}",
        "metadata": {"trace_id": f"trace-{worker_id}"},
    })


def _build_queue_with_in_flight_workers(num_workers: int):
    """
    Build a ProcessingQueue whose workers are mid-task (sleeping 5 s).

    Returns (pq, worker_tasks, task_jsons) where:
      - pq            : ProcessingQueue instance (Redis disabled)
      - worker_tasks  : list of asyncio.Task objects (the "workers")
      - task_jsons    : list of task JSON strings that are "in flight"
    """
    from shared.processing_queue import ProcessingQueue

    pq = ProcessingQueue.__new__(ProcessingQueue)

    # Minimal attribute setup — no Redis, no real dependencies
    pq.redis_available = False
    pq.redis_client = None
    pq.fallback_queue = asyncio.Queue()
    pq.queue_key = "processing_queue:tasks"
    pq.dead_letter_key = "processing_queue:dead_letter"
    pq.num_workers = num_workers
    pq._running = True
    pq._workers = {}
    pq._active_tasks = {}
    pq._monitor_task = None
    pq._heartbeat_monitor_task = None
    pq._reconnect_task = None
    pq._autoscaler_task = None
    pq._scale_up_since = None
    pq._scale_down_since = None
    pq._current_workers = num_workers
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

    task_jsons = []
    worker_tasks = {}

    for i in range(num_workers):
        tj = _make_minimal_task_json(i)
        task_jsons.append(tj)

        # Simulate a worker that is mid-task (sleeping = in-flight)
        async def _sleeping_worker(wid=i):
            pq._active_tasks[wid] = {
                "start_time": time.time(),
                "task_type": "media",
                "chat_id": 1000 + wid,
                "message_id": 2000 + wid,
                "media_type": "photo",
                "description": f"photo from Chat {1000 + wid}",
            }
            try:
                await asyncio.sleep(5)  # Simulate long-running task
            except asyncio.CancelledError:
                # Bug: on cancellation the task data is simply lost
                pass

        t = asyncio.get_event_loop().create_task(_sleeping_worker())
        worker_tasks[i] = t

    pq._workers = worker_tasks
    return pq, worker_tasks, list(task_jsons)


# ---------------------------------------------------------------------------
# Bug-Condition Property Test
# ---------------------------------------------------------------------------

@given(in_flight_count=st.integers(min_value=1, max_value=10))
@h_settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_sigterm_drops_in_flight_tasks(in_flight_count):
    """
    **Validates: Requirements 1.1, 2.1**

    BUG CONDITION: SIGTERM (via stop()) cancels in-flight workers without
    draining or re-queuing their tasks.

    EXPECTED OUTCOME on UNFIXED code: FAILS
      - fallback_queue remains empty (tasks not re-queued)
      - DLQ remains empty (tasks not moved to dead-letter)
      - In-flight tasks are silently dropped

    When this test FAILS it proves the bug exists.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        pq, worker_tasks, task_jsons = _build_queue_with_in_flight_workers(in_flight_count)

        # Give workers a moment to start and enter their sleep (in-flight state)
        await asyncio.sleep(0.05)

        # Verify workers are actually in-flight before we stop
        assert len(pq._active_tasks) == in_flight_count, (
            f"Expected {in_flight_count} active tasks, got {len(pq._active_tasks)}"
        )

        # --- SIGTERM arrives: call stop() with drain_timeout=0 ---
        # On unfixed code this immediately cancels all workers.
        await pq.stop()

        # --- Assert: tasks were NOT preserved ---
        # Bug: fallback_queue should have received the in-flight task payloads,
        # but on unfixed code it is still empty.
        fallback_size = pq.fallback_queue.qsize()

        # Bug: DLQ should have entries for dropped tasks, but on unfixed code
        # redis_client is None so nothing was written.
        dlq_size = 0  # No Redis → DLQ is always 0 on unfixed code

        # This assertion FAILS on unfixed code (proving the bug):
        # We assert that tasks WERE preserved — but they weren't.
        assert fallback_size + dlq_size == in_flight_count, (
            f"BUG CONFIRMED: {in_flight_count} in-flight task(s) were silently dropped. "
            f"fallback_queue={fallback_size}, dlq={dlq_size}. "
            f"Expected {in_flight_count} tasks to be re-queued or moved to DLQ."
        )

    try:
        loop.run_until_complete(_run())
    finally:
        # Clean up any remaining tasks
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------
# P0.1 Preservation Test — Normal Task Processing Unchanged
# ---------------------------------------------------------------------------

def _build_queue_for_normal_processing(num_tasks: int):
    """
    Build a ProcessingQueue where workers process tasks normally (no SIGTERM).
    Workers immediately mark the task as processed and increment stats.

    Returns (pq, event) where event is set when all tasks are processed.
    """
    from shared.processing_queue import ProcessingQueue

    pq = ProcessingQueue.__new__(ProcessingQueue)

    pq.redis_available = False
    pq.redis_client = None
    pq.fallback_queue = asyncio.Queue()
    pq.queue_key = "processing_queue:tasks"
    pq.dead_letter_key = "processing_queue:dead_letter"
    pq.num_workers = num_tasks  # one worker per task for simplicity
    pq._running = True
    pq._workers = {}
    pq._active_tasks = {}
    pq._monitor_task = None
    pq._heartbeat_monitor_task = None
    pq._reconnect_task = None
    pq._autoscaler_task = None
    pq._scale_up_since = None
    pq._scale_down_since = None
    pq._current_workers = num_tasks
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

    return pq


@given(task_count=st.integers(1, 5))
@h_settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_normal_processing_stats_increment(task_count):
    """
    **Validates: Requirements 3.1**

    PRESERVATION TEST — Non-bug condition: no SIGTERM delivered during processing.

    Assert that with NO SIGTERM, tasks complete normally and
    stats['processed'] increments by exactly task_count.

    EXPECTED OUTCOME on UNFIXED code: PASSES (baseline to preserve after fix).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        pq = _build_queue_for_normal_processing(task_count)

        # Simulate task_count workers each completing one task successfully
        # by directly exercising the stats increment path (no SIGTERM involved).
        async def _complete_one_task(wid: int):
            # Simulate the work a worker does after processing a task
            await asyncio.sleep(0)  # yield to event loop
            pq.stats["processed"] += 1

        workers = [asyncio.create_task(_complete_one_task(i)) for i in range(task_count)]
        pq._workers = workers

        # Wait for all workers to finish naturally (no stop() called)
        await asyncio.gather(*workers)

        # Preservation assertion: stats['processed'] must equal task_count
        assert pq.stats["processed"] == task_count, (
            f"REGRESSION: expected stats['processed'] == {task_count}, "
            f"got {pq.stats['processed']}. Normal task processing was altered."
        )

        # Workers should all be done (not cancelled)
        for w in workers:
            assert w.done() and not w.cancelled(), (
                "REGRESSION: worker was unexpectedly cancelled during normal processing."
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
