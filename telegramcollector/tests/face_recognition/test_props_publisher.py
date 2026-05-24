"""Property-based tests for Publisher (task 6.6).

Tests Properties 12–14 from the design document.
All tests use Hypothesis with @given and @settings(max_examples=100).

**Validates: Requirements 8.2, 8.5, 8.6**
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

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

from services.face_recognition.publisher import Publisher  # noqa: E402


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_conn_mock():
    """Build a mock asyncpg connection."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
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


def _make_bot_pool_mock():
    """Build a minimal BotPool mock (not used in these properties)."""
    bot_pool = MagicMock()
    return bot_pool


def _make_message(chat_id=1, msg_id=1, topic_id=1):
    """Build a minimal raw_messages-style dict."""
    return {
        "source_chat_id": chat_id,
        "id": msg_id,
        "media_path": "/tmp/test_media.jpg",
        "file_unique_id": f"fuid_{chat_id}_{msg_id}",
        "message_type": "photo",
    }


# ---------------------------------------------------------------------------
# Property 12: Uploaded Media Dedup Invariant
# Validates: Requirements 8.2
#
# Calling process_message_faces N times with the same (chat_id, msg_id, topic_id)
# produces exactly 1 INSERT into uploaded_media.
# ---------------------------------------------------------------------------

@given(n=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_12_uploaded_media_dedup_invariant(n: int) -> None:
    """**Validates: Requirements 8.2**

    Calling process_message_faces N times with the same (chat_id, msg_id, topic_id)
    produces exactly 1 INSERT into uploaded_media regardless of N.

    The first call: _is_already_uploaded returns False → INSERT happens.
    Subsequent calls: _is_already_uploaded returns True → INSERT skipped.
    """
    uploaded_media_inserts = {"count": 0}
    call_count = {"n": 0}

    async def mock_is_already_uploaded(source_chat_id, source_message_id, topic_id):
        # First call: not yet uploaded; subsequent calls: already uploaded
        return call_count["n"] > 0

    async def mock_ensure_topic_exists(db_topic_id):
        return 999  # fake Telegram topic ID

    async def mock_upload_to_topic(media_path, topic_id):
        return 12345  # fake hub_message_id

    # Track INSERT calls to uploaded_media via _record_uploaded_media
    async def mock_record_uploaded_media(
        source_chat_id, source_message_id, topic_id, hub_message_id
    ):
        uploaded_media_inserts["count"] += 1

    async def mock_record_processed_media(
        file_unique_id, media_type, faces_found, topics_matched
    ):
        pass

    message = _make_message(chat_id=1, msg_id=1)
    topic_ids = [42]

    pool = _make_pool_mock()
    bot_pool = _make_bot_pool_mock()
    publisher = Publisher(pool, bot_pool)

    # Patch internal methods
    publisher._is_already_uploaded = mock_is_already_uploaded
    publisher._ensure_topic_exists = mock_ensure_topic_exists
    publisher._upload_to_topic = mock_upload_to_topic
    publisher._record_uploaded_media = mock_record_uploaded_media
    publisher._record_processed_media = mock_record_processed_media

    async def run():
        for _ in range(n):
            await publisher.process_message_faces(message, topic_ids)
            call_count["n"] += 1

    asyncio.run(run())

    assert uploaded_media_inserts["count"] == 1, (
        f"Expected exactly 1 INSERT into uploaded_media for {n} calls, "
        f"got {uploaded_media_inserts['count']}"
    )


# ---------------------------------------------------------------------------
# Property 13: Processed Media Dedup Invariant
# Validates: Requirements 8.5
#
# Calling _record_processed_media N times with the same file_unique_id
# always uses ON CONFLICT (file_unique_id) DO NOTHING in the SQL.
# ---------------------------------------------------------------------------

@given(n=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_13_processed_media_dedup_invariant(n: int) -> None:
    """**Validates: Requirements 8.5**

    Calling _record_processed_media N times with the same file_unique_id
    always issues the INSERT with ON CONFLICT (file_unique_id) DO NOTHING.

    The DB-level dedup is enforced by the conflict clause — we verify that
    every call uses the correct SQL (with ON CONFLICT DO NOTHING).
    """
    executed_sqls = []

    async def mock_execute(query, *args):
        executed_sqls.append(query)

    conn = _make_conn_mock()
    conn.execute = AsyncMock(side_effect=mock_execute)
    pool = _make_pool_mock(conn_mock=conn)
    bot_pool = _make_bot_pool_mock()
    publisher = Publisher(pool, bot_pool)

    async def run():
        for _ in range(n):
            await publisher._record_processed_media(
                file_unique_id="unique_file_abc",
                media_type="photo",
                faces_found=1,
                topics_matched=[1],
            )

    asyncio.run(run())

    assert len(executed_sqls) == n, (
        f"Expected {n} execute calls, got {len(executed_sqls)}"
    )

    for i, sql in enumerate(executed_sqls):
        assert "ON CONFLICT (file_unique_id) DO NOTHING" in sql, (
            f"Call {i + 1}: expected SQL to contain "
            f"'ON CONFLICT (file_unique_id) DO NOTHING', got:\n{sql}"
        )


# ---------------------------------------------------------------------------
# Property 14: Multi-Identity Upload
# Validates: Requirements 8.6
#
# N distinct topic_ids produce exactly N INSERTs into uploaded_media.
# ---------------------------------------------------------------------------

@given(n=st.integers(min_value=1, max_value=5))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_14_multi_identity_upload(n: int) -> None:
    """**Validates: Requirements 8.6**

    N distinct topic_ids produce exactly N INSERTs into uploaded_media.

    _is_already_uploaded always returns False (no prior uploads),
    so every topic triggers a full upload + record cycle.
    """
    uploaded_media_inserts = {"count": 0}

    async def mock_is_already_uploaded(source_chat_id, source_message_id, topic_id):
        return False  # never already uploaded

    async def mock_ensure_topic_exists(db_topic_id):
        return db_topic_id + 1000  # fake Telegram topic ID

    async def mock_upload_to_topic(media_path, topic_id):
        return topic_id + 9000  # fake hub_message_id

    async def mock_record_uploaded_media(
        source_chat_id, source_message_id, topic_id, hub_message_id
    ):
        uploaded_media_inserts["count"] += 1

    async def mock_record_processed_media(
        file_unique_id, media_type, faces_found, topics_matched
    ):
        pass

    # N distinct topic_ids: [1, 2, ..., N]
    topic_ids = list(range(1, n + 1))
    message = _make_message(chat_id=10, msg_id=20)

    pool = _make_pool_mock()
    bot_pool = _make_bot_pool_mock()
    publisher = Publisher(pool, bot_pool)

    publisher._is_already_uploaded = mock_is_already_uploaded
    publisher._ensure_topic_exists = mock_ensure_topic_exists
    publisher._upload_to_topic = mock_upload_to_topic
    publisher._record_uploaded_media = mock_record_uploaded_media
    publisher._record_processed_media = mock_record_processed_media

    asyncio.run(publisher.process_message_faces(message, topic_ids))

    assert uploaded_media_inserts["count"] == n, (
        f"Expected exactly {n} INSERTs into uploaded_media for {n} distinct topic_ids, "
        f"got {uploaded_media_inserts['count']}"
    )
