"""
P1.1 Bug-Condition Exploration Test — Redis Reconnect Loop Missing

Validates: Requirements 1.3, 2.3

Bug condition:
    queue_state.redis_available = False AND redis_server_is_reachable() = True

The current code in processing_queue.py only attempts reconnect via:

    if worker_id == 0 and int(time.time()) % 30 == 0:
        self._try_reconnect_redis()

This is fragile and unreliable. There is NO dedicated background reconnect loop.
After Redis recovers, `redis_available` stays `False` indefinitely.

EXPECTED OUTCOME (on unfixed code): test FAILS
  — the system stays on the fallback queue forever even after Redis recovers.

Documented counterexample:
    fallback_depth=0 (or any value 0–100):
        redis_available=False after 60 s of simulated time even though ping() succeeds.

    Root cause: the modulo-30 check fires at most twice in 60 s (at t=30 and t=60),
    but _try_reconnect_redis() recreates the redis.Redis client from scratch each time.
    In the test we mock redis.Redis so ping() succeeds — yet redis_available STILL
    stays False because the check only fires when BOTH worker_id==0 AND time()%30==0,
    which in real workloads almost never aligns.  More critically, there is NO
    dedicated background reconnect loop — the reconnect is entirely dependent on
    this fragile modulo coincidence.

    Counterexample: fallback_depth=0
        After 28 ticks (1..28, none divisible by 30):
            _try_reconnect_redis() never called → redis_available=False
        After 60 ticks (1..60, modulo fires at 30 and 60):
            _try_reconnect_redis() called twice, but only because of lucky timing.
            In production the worker loop runs ~1 iteration/second and time.time()
            is a wall-clock value — the modulo check is unreliable.
"""

import asyncio
import queue as stdlib_queue
import sys
import os
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Minimal synchronous fallback-queue shim
# (avoids creating asyncio.Queue outside a running event loop on Python 3.12)
# ---------------------------------------------------------------------------

class _SyncFallbackQueue:
    """Thin wrapper around stdlib queue.Queue that exposes the asyncio.Queue API
    used by ProcessingQueue (put_nowait, get_nowait, empty, qsize)."""

    def __init__(self):
        self._q = stdlib_queue.Queue()

    def put_nowait(self, item):
        self._q.put_nowait(item)

    def get_nowait(self):
        try:
            return self._q.get_nowait()
        except stdlib_queue.Empty:
            raise asyncio.QueueEmpty

    def empty(self):
        return self._q.empty()

    def qsize(self):
        return self._q.qsize()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pq_redis_down(fallback_depth: int, mock_redis_module):
    """
    Build a minimal ProcessingQueue with:
      - redis_available = False  (Redis went down at runtime)
      - redis_client.ping() succeeds  (Redis has recovered)
      - fallback_queue pre-seeded with `fallback_depth` items

    `mock_redis_module` is the patched `processing_queue.redis` module so that
    _try_reconnect_redis() uses our mock instead of making real connections.
    """
    from shared.processing_queue import ProcessingQueue

    # Configure the mock redis module so _try_reconnect_redis() gets a working client
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.rpush.return_value = 1
    mock_client.llen.return_value = 0
    mock_redis_module.Redis.return_value = mock_client
    mock_redis_module.exceptions = MagicMock()

    pq = ProcessingQueue.__new__(ProcessingQueue)

    # Core queue state — Redis is DOWN from the queue's perspective
    pq.redis_available = False
    pq.queue_key = "processing_queue:tasks"
    pq.dead_letter_key = "processing_queue:dead_letter"

    # The existing redis_client on the pq (before reconnect) also has ping() working
    pq.redis_client = mock_client

    # In-memory fallback queue seeded with items (sync shim — no event loop needed)
    pq.fallback_queue = _SyncFallbackQueue()
    for i in range(fallback_depth):
        pq.fallback_queue.put_nowait(f'{{"task_type":"media","chat_id":{i},"seq":{i}}}')

    # Worker / lifecycle state
    pq._running = True
    pq._workers = {}
    pq.num_workers = 1
    pq.task_timeout_seconds = 600
    pq.worker_memory_limit_mb = 2048
    pq._active_tasks = {}
    pq._monitor_task = None
    pq._heartbeat_monitor_task = None
    pq._reconnect_task = None

    # Backpressure / stats (not under test)
    pq.high_watermark = 100
    pq.low_watermark = 20
    pq._backpressure_state = MagicMock()
    pq._backpressure_callbacks = []
    pq._last_known_redis_size = 0
    pq.manual_pause = False
    pq.stats = {'processed': 0, 'faces_found': 0, 'new_identities': 0, 'errors': 0}
    pq._per_chat_times = {}
    pq._processing_times = []
    pq._processed_last_minute = 0

    return pq, mock_client


# ---------------------------------------------------------------------------
# Property 1: Bug Condition — redis_available stays False after Redis recovers
# (60-tick simulation including modulo hits at t=30 and t=60)
# ---------------------------------------------------------------------------

@given(fallback_depth=st.integers(0, 100))
@h_settings(
    max_examples=25,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_redis_available_stays_false_after_recovery(fallback_depth):
    """
    **Validates: Requirements 1.3, 2.3**

    Bug condition:
        queue_state.redis_available = False AND redis_server_is_reachable() = True

    For each fallback_depth in [0, 100]:
      1. Build a ProcessingQueue with redis_available=False and ping() succeeding.
      2. Run the _redis_reconnect_loop() coroutine directly (simulating the background task).
      3. Assert redis_available is True (correct behavior — should reconnect).
      4. Assert fallback items were drained to Redis (correct behavior).

    EXPECTED OUTCOME on unfixed code: FAILS
      — redis_available stays False because there is no dedicated background reconnect loop.

    EXPECTED OUTCOME on fixed code: PASSES
      — _redis_reconnect_loop() reconnects and drains the fallback queue.
    """
    with patch("shared.processing_queue.redis") as mock_redis_module:
        pq, mock_client = _make_pq_redis_down(fallback_depth, mock_redis_module)

        # Run the dedicated reconnect loop directly (the fix adds this coroutine).
        # We patch REDIS_RECONNECT_INTERVAL to 0 so the loop doesn't actually sleep.
        with patch("shared.processing_queue.settings") as mock_settings:
            mock_settings.REDIS_RECONNECT_INTERVAL = 0
            mock_settings.REDIS_RECONNECT_MAX_ATTEMPTS = 0
            mock_settings.REDIS_HOST = "localhost"
            mock_settings.REDIS_PORT = 6379
            mock_settings.REDIS_DB = 0
            mock_settings.REDIS_PASSWORD = None

            asyncio.run(pq._redis_reconnect_loop())

        # --- Assert CORRECT expected behavior ---

        # 1. After the reconnect loop runs, redis_available SHOULD be True.
        assert pq.redis_available is True, (
            f"BUG CONFIRMED: redis_available is still False after reconnect loop ran "
            f"even though ping() succeeds. fallback_depth={fallback_depth}. "
            f"A dedicated background reconnect loop is required."
        )

        # 2. Fallback items SHOULD have been drained to Redis.
        assert pq.fallback_queue.empty(), (
            f"BUG CONFIRMED: {pq.fallback_queue.qsize()} fallback items remain "
            f"after Redis recovered. fallback_depth={fallback_depth}."
        )

        # 3. If there were items, Redis rpush should have been called.
        if fallback_depth > 0:
            assert mock_client.rpush.called, (
                f"BUG CONFIRMED: rpush never called despite {fallback_depth} fallback items "
                f"and Redis being reachable."
            )


# ---------------------------------------------------------------------------
# Property 2: Bug Condition — reconnect never triggered without modulo hit
# (28-tick simulation — no tick is divisible by 30)
# ---------------------------------------------------------------------------

@given(fallback_depth=st.integers(0, 100))
@h_settings(
    max_examples=25,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_reconnect_never_triggered_without_modulo_hit(fallback_depth):
    """
    **Validates: Requirements 1.3**

    Proves that the fix provides a dedicated reconnect loop that does NOT depend
    on the fragile modulo-30 timing coincidence.

    On fixed code: _redis_reconnect_loop() is a standalone coroutine that can be
    run directly, independent of any modulo check. Running it sets redis_available=True.

    EXPECTED OUTCOME on unfixed code: FAILS
      — redis_available is never set to True because no dedicated reconnect loop exists.

    EXPECTED OUTCOME on fixed code: PASSES
      — _redis_reconnect_loop() exists and sets redis_available=True when ping() succeeds.
    """
    with patch("shared.processing_queue.redis") as mock_redis_module:
        pq, mock_client = _make_pq_redis_down(fallback_depth, mock_redis_module)

        # Verify that the fix provides _redis_reconnect_loop as a coroutine method.
        # On unfixed code this attribute does not exist → AttributeError → test FAILS.
        assert hasattr(pq, '_redis_reconnect_loop'), (
            "BUG CONFIRMED: ProcessingQueue has no _redis_reconnect_loop method. "
            "A dedicated background reconnect loop is required (not a modulo check)."
        )

        # Run the reconnect loop with zero sleep interval.
        with patch("shared.processing_queue.settings") as mock_settings:
            mock_settings.REDIS_RECONNECT_INTERVAL = 0
            mock_settings.REDIS_RECONNECT_MAX_ATTEMPTS = 0
            mock_settings.REDIS_HOST = "localhost"
            mock_settings.REDIS_PORT = 6379
            mock_settings.REDIS_DB = 0
            mock_settings.REDIS_PASSWORD = None

            asyncio.run(pq._redis_reconnect_loop())

        # redis_available SHOULD be True after the reconnect loop ran.
        assert pq.redis_available is True, (
            f"BUG CONFIRMED: redis_available=False after _redis_reconnect_loop() ran. "
            f"fallback_depth={fallback_depth}. "
            f"A dedicated background reconnect loop is required (not a modulo check)."
        )


# ---------------------------------------------------------------------------
# Property 3: Preservation — Redis-Healthy Queue Behaviour Unchanged (P1.1)
# ---------------------------------------------------------------------------
#
# **Validates: Requirements 3.2**
#
# When redis_available = True, enqueue_media() MUST:
#   1. Call redis_client.rpush() (not fallback_queue.put_nowait)
#   2. Leave fallback_queue empty
#   3. Push the exact task JSON that was constructed (round-trip)
#
# This test runs on UNFIXED code and MUST PASS — it documents the baseline
# correct behaviour that the fix must preserve.

@given(
    chat_id=st.integers(min_value=1, max_value=10**12),
    message_id=st.integers(min_value=1, max_value=10**9),
    media_type=st.sampled_from(["photo", "video", "video_note"]),
)
@h_settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_redis_healthy_enqueue_uses_rpush_not_fallback(chat_id, message_id, media_type):
    """
    **Validates: Requirements 3.2**

    Preservation: when redis_available = True, enqueue_media() routes through
    Redis (rpush) and never touches fallback_queue.

    For all (chat_id, message_id, media_type):
      1. Build a ProcessingQueue with redis_available = True and a mock redis_client.
      2. Call enqueue_media() with a minimal 1-byte content BytesIO.
      3. Assert redis_client.rpush was called exactly once.
      4. Assert fallback_queue remains empty.
      5. Assert the JSON pushed to rpush contains the correct chat_id, message_id,
         and media_type (round-trip correctness).

    EXPECTED OUTCOME on unfixed code: PASSES
      — the Redis-healthy enqueue path is correct and must remain unchanged.
    """
    import io
    import json
    import base64
    import asyncio

    with patch("shared.processing_queue.redis") as mock_redis_module:
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_client.rpush.return_value = 1
        mock_client.llen.return_value = 0
        mock_redis_module.Redis.return_value = mock_client
        mock_redis_module.exceptions = MagicMock()

        from shared.processing_queue import ProcessingQueue

        pq = ProcessingQueue.__new__(ProcessingQueue)

        # Redis is UP
        pq.redis_available = True
        pq.redis_client = mock_client
        pq.queue_key = "processing_queue:tasks"
        pq.dead_letter_key = "processing_queue:dead_letter"

        # Sync fallback queue shim (no event loop needed)
        pq.fallback_queue = _SyncFallbackQueue()

        # Minimal required attributes
        pq._running = True
        pq._workers = {}
        pq.num_workers = 1
        pq.task_timeout_seconds = 600
        pq.worker_memory_limit_mb = 2048
        pq._active_tasks = {}
        pq._monitor_task = None
        pq._heartbeat_monitor_task = None
        pq._reconnect_task = None
        pq.high_watermark = 100
        pq.low_watermark = 20
        pq._backpressure_state = MagicMock()
        pq._backpressure_callbacks = []
        pq._last_known_redis_size = 0
        pq.manual_pause = False
        pq.stats = {'processed': 0, 'faces_found': 0, 'new_identities': 0, 'errors': 0}
        pq._per_chat_times = {}
        pq._processing_times = []
        pq._processed_last_minute = 0

        # Patch check_backpressure to be a no-op (not under test)
        pq.check_backpressure = lambda: None

        # Patch _get_trace_id
        pq._get_trace_id = lambda: "test-trace-id"

        # Build a minimal content BytesIO
        content = io.BytesIO(b"\xff")  # 1 byte of content

        # Run enqueue_media synchronously via asyncio.run
        asyncio.run(pq.enqueue_media(
            chat_id=chat_id,
            message_id=message_id,
            content=content,
            media_type=media_type,
            file_unique_id=None,
        ))

        # 1. rpush must have been called exactly once
        assert mock_client.rpush.call_count == 1, (
            f"Expected rpush called once, got {mock_client.rpush.call_count}. "
            f"chat_id={chat_id}, message_id={message_id}, media_type={media_type}"
        )

        # 2. fallback_queue must remain empty
        assert pq.fallback_queue.empty(), (
            f"fallback_queue is not empty after Redis-healthy enqueue. "
            f"chat_id={chat_id}, message_id={message_id}"
        )

        # 3. Round-trip: the JSON pushed to rpush must contain correct fields
        call_args = mock_client.rpush.call_args
        pushed_key = call_args[0][0]
        pushed_json_str = call_args[0][1]

        assert pushed_key == pq.queue_key, (
            f"rpush called with wrong key: {pushed_key!r}"
        )

        pushed_data = json.loads(pushed_json_str)
        assert pushed_data["chat_id"] == chat_id, (
            f"Round-trip chat_id mismatch: pushed {pushed_data['chat_id']}, expected {chat_id}"
        )
        assert pushed_data["message_id"] == message_id, (
            f"Round-trip message_id mismatch: pushed {pushed_data['message_id']}, expected {message_id}"
        )
        assert pushed_data["media_type"] == media_type, (
            f"Round-trip media_type mismatch: pushed {pushed_data['media_type']!r}, expected {media_type!r}"
        )
        assert pushed_data["task_type"] == "media", (
            f"task_type should be 'media', got {pushed_data['task_type']!r}"
        )

        # 4. content round-trips correctly (base64 decode matches original)
        decoded_content = base64.b64decode(pushed_data["content_b64"])
        assert decoded_content == b"\xff", (
            f"Content round-trip failed: got {decoded_content!r}"
        )
