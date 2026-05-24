"""
Unit tests for FaceRecognitionService (task 9.7).

Tests:
1. test_cursor_initialized_to_zero_when_missing
2. test_paused_when_processing_enabled_false
3. test_db_retry_on_startup_failure
4. test_redis_unavailable_uses_static_config
5. test_sigterm_completes_batch_before_exit

Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 2.4
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

# ---------------------------------------------------------------------------
# Path setup and env vars — must happen BEFORE any project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test_token")
os.environ.setdefault("HUB_GROUP_ID", "-100123456789")
os.environ.setdefault("DB_PASSWORD", "test_password")

# Mock heavy/network-blocking dependencies before any project import.
# asyncpg hangs on import in some environments (Windows DNS resolution);
# redis may also attempt network I/O at import time.
for _mod in ("asyncpg", "asyncpg.pool", "asyncpg.connection"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from services.face_recognition.main import FaceRecognitionService, _create_db_pool  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn_mock():
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _make_pool_mock(conn_mock=None):
    pool = MagicMock()
    if conn_mock is None:
        conn_mock = _make_conn_mock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_service(db_pool=None, redis_client=None):
    """Build a FaceRecognitionService with all dependencies mocked."""
    if db_pool is None:
        db_pool = _make_pool_mock()
    bot_pool = MagicMock()
    processor = MagicMock()
    matcher = MagicMock()
    publisher = MagicMock()
    return FaceRecognitionService(
        db_pool=db_pool,
        redis_client=redis_client,
        bot_pool=bot_pool,
        processor=processor,
        matcher=matcher,
        publisher=publisher,
    )


# ---------------------------------------------------------------------------
# Test 1: Cursor initialized to 0 when row is missing
# Requirements: 2.4
# ---------------------------------------------------------------------------

def test_cursor_initialized_to_zero_when_missing():
    """
    When service_cursors has no row for face_recognition, _init_cursor inserts
    one with last_message_id=0 and returns 0.

    Requirements: 2.4
    """
    conn = _make_conn_mock()

    # fetchrow returns the row after the INSERT (ON CONFLICT DO NOTHING)
    conn.fetchrow = AsyncMock(return_value={"last_message_id": 0})

    pool = _make_pool_mock(conn_mock=conn)
    service = _make_service(db_pool=pool)

    result = asyncio.run(service._init_cursor())

    # Must have executed the INSERT ... ON CONFLICT DO NOTHING
    execute_calls = conn.execute.call_args_list
    insert_calls = [
        c for c in execute_calls
        if "INSERT" in str(c) and "service_cursors" in str(c)
    ]
    assert len(insert_calls) >= 1, (
        "Expected at least one INSERT into service_cursors, "
        f"got execute calls: {execute_calls}"
    )

    # Must return 0
    assert result == 0, f"Expected cursor=0 when row is missing, got {result}"


# ---------------------------------------------------------------------------
# Test 2: Service sleeps without querying raw_messages when processing disabled
# Requirements: 1.3
# ---------------------------------------------------------------------------

def test_paused_when_processing_enabled_false():
    """
    When FACE_PROCESSING_ENABLED=False, the service sleeps without querying
    raw_messages (conn.fetch is NOT called).

    Requirements: 1.3
    """
    conn = _make_conn_mock()
    # _init_cursor: INSERT + fetchrow returning cursor=0
    conn.fetchrow = AsyncMock(return_value={"last_message_id": 0})
    pool = _make_pool_mock(conn_mock=conn)

    service = _make_service(db_pool=pool)

    sleep_count = {"n": 0}

    async def mock_sleep(seconds):
        sleep_count["n"] += 1
        # Stop the loop after the first sleep
        service._running = False

    with patch(
        "services.face_recognition.main.get_dynamic_setting",
        return_value=False,  # FACE_PROCESSING_ENABLED=False
    ), patch("asyncio.sleep", side_effect=mock_sleep):
        asyncio.run(service.start())

    # asyncio.sleep must have been called (service was paused)
    assert sleep_count["n"] >= 1, "Expected at least one sleep when processing is disabled"

    # conn.fetch (batch query) must NOT have been called
    conn.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: _create_db_pool retries on connection failure
# Requirements: 1.5
# ---------------------------------------------------------------------------

def test_db_retry_on_startup_failure():
    """
    _create_db_pool retries asyncpg.create_pool on connection failure.
    Fails twice then succeeds — create_pool is called exactly 3 times.

    Requirements: 1.5
    """
    fake_pool = MagicMock()
    call_count = {"n": 0}

    async def create_pool_side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise OSError("Connection refused")
        return fake_pool

    with patch("asyncpg.create_pool", side_effect=create_pool_side_effect) as mock_create, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(_create_db_pool())

    assert mock_create.call_count == 3, (
        f"Expected create_pool to be called 3 times (2 failures + 1 success), "
        f"got {mock_create.call_count}"
    )
    assert result is fake_pool, "Expected the pool returned on the 3rd attempt"


# ---------------------------------------------------------------------------
# Test 4: Redis unavailable — _process_batch works without raising
# Requirements: 1.6
# ---------------------------------------------------------------------------

def test_redis_unavailable_uses_static_config():
    """
    When redis_client=None, _process_batch still works and DLQ push is skipped
    gracefully (no exception raised).

    Requirements: 1.6
    """
    conn = _make_conn_mock()
    # processed_media dedup check: no existing row
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _make_pool_mock(conn_mock=conn)

    # Service with redis_client=None
    service = _make_service(db_pool=pool, redis_client=None)

    # processor.process_message raises an exception to trigger DLQ push
    service._processor.process_message = AsyncMock(
        side_effect=RuntimeError("Simulated processing failure")
    )

    message = {
        "id": 1,
        "file_unique_id": "fuid_test",
        "source_chat_id": 100,
        "message_type": "photo",
        "media_path": "/tmp/test.jpg",
    }

    # Must not raise even though Redis is None and processing failed
    asyncio.run(service._process_batch([message]))


# ---------------------------------------------------------------------------
# Test 5: stop() causes the service to finish the current batch before exiting
# Requirements: 1.4
# ---------------------------------------------------------------------------

def test_sigterm_completes_batch_before_exit():
    """
    When _handle_signal() is called (simulating SIGTERM), _running is set to False
    so the loop exits after the current batch. The _stop_event is set on exit.

    We test this by:
    1. Running start() with a controlled loop that processes one batch then checks _running.
    2. Calling _handle_signal() mid-loop to set _running=False.
    3. Verifying the loop exits cleanly (_stop_event set, _running=False).

    Requirements: 1.4
    """
    conn = _make_conn_mock()
    conn.fetchrow = AsyncMock(return_value={"last_message_id": 0})
    pool = _make_pool_mock(conn_mock=conn)

    service = _make_service(db_pool=pool)

    batch_processed = {"count": 0}

    async def mock_process_batch(messages):
        batch_processed["count"] += 1
        # After processing the first batch, signal stop (simulates SIGTERM mid-loop)
        service._handle_signal()

    async def mock_advance_cursor(new_value):
        pass

    service._process_batch = mock_process_batch
    service._advance_cursor = mock_advance_cursor

    fetch_call_count = {"n": 0}

    async def mock_fetch(*args, **kwargs):
        fetch_call_count["n"] += 1
        if fetch_call_count["n"] == 1:
            # Return one fake message on the first call
            return [{"id": 1, "file_unique_id": "fuid1", "source_chat_id": 1,
                     "message_type": "photo", "media_path": "/tmp/a.jpg"}]
        # Should not be reached — loop exits after signal
        return []

    conn.fetch = AsyncMock(side_effect=mock_fetch)

    async def mock_sleep(seconds):
        # Prevent any real sleeping; if we reach here unexpectedly, stop the loop
        service._running = False

    with patch(
        "services.face_recognition.main.get_dynamic_setting",
        return_value=True,  # FACE_PROCESSING_ENABLED=True
    ), patch("asyncio.sleep", side_effect=mock_sleep):
        asyncio.run(service.start())

    # The batch was processed exactly once before the signal
    assert batch_processed["count"] == 1, (
        f"Expected exactly 1 batch processed before exit, got {batch_processed['count']}"
    )
    # The stop event must be set (loop exited cleanly)
    assert service._stop_event.is_set(), "Expected _stop_event to be set after loop exit"
    # The service must no longer be running
    assert not service._running, "Expected _running=False after stop signal"
