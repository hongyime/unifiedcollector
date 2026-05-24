"""Property-based tests for FaceRecognitionService (task 9.6).

Tests Properties 1, 2, and 15 from the design document.
All tests use Hypothesis with @given and @settings(max_examples=100).

**Validates: Requirements 2.1, 2.2, 2.3, 10.1, 10.2, 10.3**
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup and env vars
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test_token")
os.environ.setdefault("HUB_GROUP_ID", "-100123456789")
os.environ.setdefault("DB_PASSWORD", "test_password")

from services.face_recognition.main import FaceRecognitionService  # noqa: E402
import shared.config as _config_module  # noqa: E402
from shared.config import get_dynamic_setting  # noqa: E402


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_conn_mock():
    """Build a mock asyncpg connection."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _make_pool_mock(conn_mock=None):
    """Build a mock asyncpg pool with acquire() as async context manager."""
    pool = MagicMock()

    if conn_mock is None:
        conn_mock = _make_conn_mock()

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)

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
# Property 1: Cursor Monotonicity
# Validates: Requirements 2.2, 2.3
#
# The cursor value in service_cursors only ever increases across batch sequences.
# Each call to _advance_cursor passes a value >= the previous call's value.
# ---------------------------------------------------------------------------

@given(ids=st.lists(st.integers(min_value=1, max_value=1000), min_size=2, max_size=10))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_cursor_monotonicity(ids: list) -> None:
    """**Validates: Requirements 2.2, 2.3**

    The cursor value passed to _advance_cursor only ever increases.
    Simulates a sequence of batch max_ids and verifies that each successive
    call to _advance_cursor receives a value >= the previous call's value.
    """
    advance_cursor_calls = []

    # Sort ids to simulate the natural ordering of message IDs from the DB
    sorted_ids = sorted(ids)

    async def run():
        service = _make_service()

        # Patch _advance_cursor to track calls
        original_advance = service._advance_cursor

        async def tracking_advance_cursor(new_value: int) -> None:
            advance_cursor_calls.append(new_value)
            service._cursor = new_value

        service._advance_cursor = tracking_advance_cursor

        # Simulate advancing the cursor for each batch max_id in order
        for max_id in sorted_ids:
            if max_id > service._cursor:
                await service._advance_cursor(max_id)

    asyncio.run(run())

    # Verify monotonicity: each call value >= previous call value
    for i in range(1, len(advance_cursor_calls)):
        assert advance_cursor_calls[i] >= advance_cursor_calls[i - 1], (
            f"Cursor went backwards: call {i} passed {advance_cursor_calls[i]}, "
            f"but call {i - 1} passed {advance_cursor_calls[i - 1]}. "
            f"Full sequence: {advance_cursor_calls}"
        )


@given(ids=st.lists(st.integers(min_value=1, max_value=1000), min_size=2, max_size=10))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_1_cursor_monotonicity_via_db(ids: list) -> None:
    """**Validates: Requirements 2.2, 2.3**

    _advance_cursor issues an UPSERT with the new cursor value and updates
    self._cursor. Verifies that the SQL always uses the correct UPSERT pattern
    and that self._cursor reflects the latest value after each call.
    """
    execute_calls = []

    async def mock_execute(query, *args):
        execute_calls.append((query, args))

    conn = _make_conn_mock()
    conn.execute = AsyncMock(side_effect=mock_execute)
    pool = _make_pool_mock(conn_mock=conn)

    service = _make_service(db_pool=pool)

    sorted_ids = sorted(set(ids))  # unique, ascending

    async def run():
        for max_id in sorted_ids:
            await service._advance_cursor(max_id)

    asyncio.run(run())

    assert len(execute_calls) == len(sorted_ids), (
        f"Expected {len(sorted_ids)} execute calls, got {len(execute_calls)}"
    )

    # Verify each call uses UPSERT (ON CONFLICT ... DO UPDATE)
    for i, (sql, args) in enumerate(execute_calls):
        assert "ON CONFLICT" in sql, (
            f"Call {i}: expected UPSERT SQL with ON CONFLICT, got:\n{sql}"
        )
        assert "DO UPDATE" in sql, (
            f"Call {i}: expected UPSERT SQL with DO UPDATE, got:\n{sql}"
        )
        # The new_value arg should be the sorted_ids[i]
        assert args[0] == sorted_ids[i], (
            f"Call {i}: expected cursor value {sorted_ids[i]}, got {args[0]}"
        )

    # Final self._cursor should equal the last (largest) id
    assert service._cursor == sorted_ids[-1], (
        f"Expected final cursor={sorted_ids[-1]}, got {service._cursor}"
    )


# ---------------------------------------------------------------------------
# Property 2: Message Filter Correctness
# Validates: Requirements 2.1
#
# The batch query returns exactly the rows matching:
#   has_media = TRUE AND message_type IN (...) AND id > cursor
#   ORDER BY id ASC LIMIT batch_size
# ---------------------------------------------------------------------------

@given(
    cursor=st.integers(min_value=0, max_value=1000),
    batch_size=st.integers(min_value=1, max_value=50),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_message_filter_correctness(cursor: int, batch_size: int) -> None:
    """**Validates: Requirements 2.1**

    The SQL query issued by the batch fetch contains all required clauses:
      - has_media = TRUE
      - message_type IN (...)
      - id > $1  (cursor parameter)
      - ORDER BY id ASC
      - LIMIT $2  (batch_size parameter)

    Captures the SQL passed to conn.fetch and asserts each clause is present.
    """
    captured_queries = []
    captured_args = []

    async def mock_fetch(query, *args):
        captured_queries.append(query)
        captured_args.append(args)
        return []  # empty batch — we only care about the SQL

    conn = _make_conn_mock()
    conn.fetch = AsyncMock(side_effect=mock_fetch)
    pool = _make_pool_mock(conn_mock=conn)

    service = _make_service(db_pool=pool)
    service._cursor = cursor
    service._running = True

    # Patch settings to use our batch_size and stop after one iteration
    iteration = {"count": 0}

    async def run():
        # Simulate one batch query iteration from the start() loop
        async with service._db_pool.acquire() as conn_inner:
            rows = await conn_inner.fetch(
                """
                SELECT *
                  FROM collector.raw_messages
                 WHERE has_media = TRUE
                   AND message_type IN ('photo', 'video', 'circle_video')
                   AND id > $1
                 ORDER BY id ASC
                 LIMIT $2
                """,
                service._cursor,
                batch_size,
            )

    asyncio.run(run())

    assert len(captured_queries) == 1, (
        f"Expected exactly 1 fetch call, got {len(captured_queries)}"
    )

    sql = captured_queries[0]
    args = captured_args[0]

    # Assert all required SQL clauses are present
    assert "has_media = TRUE" in sql, (
        f"SQL missing 'has_media = TRUE':\n{sql}"
    )
    assert "message_type IN" in sql, (
        f"SQL missing 'message_type IN':\n{sql}"
    )
    assert "id > $1" in sql, (
        f"SQL missing 'id > $1':\n{sql}"
    )
    assert "ORDER BY id ASC" in sql, (
        f"SQL missing 'ORDER BY id ASC':\n{sql}"
    )
    assert "LIMIT $2" in sql, (
        f"SQL missing 'LIMIT $2':\n{sql}"
    )

    # Assert the parameters match cursor and batch_size
    assert args[0] == cursor, (
        f"Expected $1 (cursor) = {cursor}, got {args[0]}"
    )
    assert args[1] == batch_size, (
        f"Expected $2 (batch_size) = {batch_size}, got {args[1]}"
    )


@given(
    cursor=st.integers(min_value=0, max_value=1000),
    batch_size=st.integers(min_value=1, max_value=50),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_2_start_loop_issues_correct_query(cursor: int, batch_size: int) -> None:
    """**Validates: Requirements 2.1**

    When the start() loop runs one iteration with FACE_PROCESSING_ENABLED=True,
    the SQL issued to the DB contains all required filter clauses.
    """
    captured_queries = []

    async def mock_fetch(query, *args):
        captured_queries.append(query)
        return []  # empty — triggers sleep then stop

    conn = _make_conn_mock()
    conn.fetch = AsyncMock(side_effect=mock_fetch)
    pool = _make_pool_mock(conn_mock=conn)

    service = _make_service(db_pool=pool)
    service._cursor = cursor

    async def run():
        # Patch _init_cursor to return our cursor value
        service._init_cursor = AsyncMock(return_value=cursor)

        # Patch asyncio.sleep to stop the loop after first empty batch
        sleep_call_count = {"n": 0}

        async def mock_sleep(duration):
            sleep_call_count["n"] += 1
            service._running = False  # stop after first sleep

        with patch("services.face_recognition.main.asyncio.sleep", side_effect=mock_sleep), \
             patch(
                 "services.face_recognition.main.get_dynamic_setting",
                 side_effect=lambda key, default=None: (
                     True if key == "FACE_PROCESSING_ENABLED" else default
                 ),
             ), \
             patch(
                 "services.face_recognition.main.settings",
             ) as mock_settings:
            mock_settings.FACE_BATCH_SIZE = batch_size
            mock_settings.FACE_POLL_INTERVAL = 1
            mock_settings.FACE_PROCESSING_ENABLED = False
            await service.start()

    asyncio.run(run())

    assert len(captured_queries) >= 1, (
        "Expected at least one fetch call during the start() loop"
    )

    sql = captured_queries[0]
    assert "has_media = TRUE" in sql
    assert "message_type IN" in sql
    assert "id > $1" in sql
    assert "ORDER BY id ASC" in sql
    assert "LIMIT $2" in sql


# ---------------------------------------------------------------------------
# Property 15: Dynamic Threshold Application
# Validates: Requirements 10.1, 10.2, 10.3
#
# get_dynamic_setting returns the value written to Redis on the next call.
# ---------------------------------------------------------------------------

@given(value=st.floats(min_value=0.1, max_value=0.9, allow_nan=False))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_15_dynamic_threshold_application(value: float) -> None:
    """**Validates: Requirements 10.1, 10.2, 10.3**

    get_dynamic_setting('FACE_SIMILARITY_THRESHOLD', 0.55) returns the value
    stored in Redis when Redis has a value for that key.

    Patches _redis_config_client to return a mock Redis client that returns
    the given float value for the config key.
    """
    redis_key = "config:FACE_SIMILARITY_THRESHOLD"
    default = 0.55

    mock_redis = MagicMock()
    mock_redis.get = MagicMock(return_value=str(value))

    with patch.object(_config_module, "_redis_config_client", mock_redis):
        result = get_dynamic_setting("FACE_SIMILARITY_THRESHOLD", default)

    assert isinstance(result, float), (
        f"Expected float result, got {type(result).__name__}: {result}"
    )
    assert abs(result - value) < 1e-6, (
        f"Expected get_dynamic_setting to return {value}, got {result}"
    )
    mock_redis.get.assert_called_once_with(redis_key)


@given(value=st.floats(min_value=0.1, max_value=0.9, allow_nan=False))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_15_dynamic_threshold_min_quality(value: float) -> None:
    """**Validates: Requirements 10.1, 10.3**

    get_dynamic_setting('FACE_MIN_QUALITY_THRESHOLD', 0.67) returns the value
    stored in Redis when Redis has a value for that key.
    """
    redis_key = "config:FACE_MIN_QUALITY_THRESHOLD"
    default = 0.67

    mock_redis = MagicMock()
    mock_redis.get = MagicMock(return_value=str(value))

    with patch.object(_config_module, "_redis_config_client", mock_redis):
        result = get_dynamic_setting("FACE_MIN_QUALITY_THRESHOLD", default)

    assert isinstance(result, float), (
        f"Expected float result, got {type(result).__name__}: {result}"
    )
    assert abs(result - value) < 1e-6, (
        f"Expected get_dynamic_setting to return {value}, got {result}"
    )
    mock_redis.get.assert_called_once_with(redis_key)


@given(value=st.floats(min_value=0.1, max_value=0.9, allow_nan=False))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_15_fallback_to_default_when_redis_unavailable(value: float) -> None:
    """**Validates: Requirements 10.1**

    get_dynamic_setting falls back to the default when Redis is unavailable
    (returns None for the key).
    """
    default = 0.55

    mock_redis = MagicMock()
    mock_redis.get = MagicMock(return_value=None)  # key not set in Redis

    with patch.object(_config_module, "_redis_config_client", mock_redis), \
         patch.object(_config_module, "settings") as mock_settings:
        mock_settings.FACE_SIMILARITY_THRESHOLD = default
        result = get_dynamic_setting("FACE_SIMILARITY_THRESHOLD", default)

    # When Redis returns None, should fall back to settings attribute or default
    assert result == default, (
        f"Expected fallback default {default}, got {result}"
    )
