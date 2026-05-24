"""
Unit tests for Publisher (task 6.7).

Tests:
1. test_upload_skipped_when_already_uploaded
2. test_processed_media_inserted_after_processing
3. test_flood_wait_marks_bot_locked_and_retries
4. test_all_bots_locked_does_not_drop_task
5. test_topic_creation_retries_three_times

Requirements: 7.3, 8.2, 8.4, 9.3, 9.4
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

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
# Helpers
# ---------------------------------------------------------------------------

def _make_conn_mock():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
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


def _make_bot(name="bot1"):
    bot = MagicMock()
    bot.name = name
    bot.client = MagicMock()
    bot.client.send_file = AsyncMock()
    return bot


def _make_message(chat_id=1, msg_id=1):
    return {
        "source_chat_id": chat_id,
        "id": msg_id,
        "media_path": "/tmp/test_media.jpg",
        "file_unique_id": f"fuid_{chat_id}_{msg_id}",
        "message_type": "photo",
    }


# ---------------------------------------------------------------------------
# Test 1: Upload skipped when already uploaded
# Requirements: 8.2
# ---------------------------------------------------------------------------

def test_upload_skipped_when_already_uploaded():
    """When _is_already_uploaded returns True, _upload_to_topic is NOT called."""
    pool = _make_pool_mock()
    bot_pool = MagicMock()
    publisher = Publisher(pool, bot_pool)

    upload_called = {"count": 0}

    async def mock_is_already_uploaded(chat_id, msg_id, topic_id):
        return True  # already uploaded

    async def mock_ensure_topic_exists(db_topic_id):
        return 999

    async def mock_upload_to_topic(media_path, topic_id):
        upload_called["count"] += 1
        return 12345

    async def mock_record_uploaded_media(chat_id, msg_id, topic_id, hub_msg_id):
        pass

    async def mock_record_processed_media(file_unique_id, media_type, faces_found, topics_matched):
        pass

    publisher._is_already_uploaded = mock_is_already_uploaded
    publisher._ensure_topic_exists = mock_ensure_topic_exists
    publisher._upload_to_topic = mock_upload_to_topic
    publisher._record_uploaded_media = mock_record_uploaded_media
    publisher._record_processed_media = mock_record_processed_media

    message = _make_message()
    asyncio.run(publisher.process_message_faces(message, [42]))

    assert upload_called["count"] == 0, (
        "_upload_to_topic should NOT be called when already uploaded"
    )


# ---------------------------------------------------------------------------
# Test 2: _record_processed_media called exactly once after processing
# Requirements: 8.4
# ---------------------------------------------------------------------------

def test_processed_media_inserted_after_processing():
    """After process_message_faces completes, _record_processed_media is called exactly once."""
    pool = _make_pool_mock()
    bot_pool = MagicMock()
    publisher = Publisher(pool, bot_pool)

    processed_calls = {"count": 0}

    async def mock_is_already_uploaded(chat_id, msg_id, topic_id):
        return False

    async def mock_ensure_topic_exists(db_topic_id):
        return db_topic_id + 100

    async def mock_upload_to_topic(media_path, topic_id):
        return 99999

    async def mock_record_uploaded_media(chat_id, msg_id, topic_id, hub_msg_id):
        pass

    async def mock_record_processed_media(file_unique_id, media_type, faces_found, topics_matched):
        processed_calls["count"] += 1

    publisher._is_already_uploaded = mock_is_already_uploaded
    publisher._ensure_topic_exists = mock_ensure_topic_exists
    publisher._upload_to_topic = mock_upload_to_topic
    publisher._record_uploaded_media = mock_record_uploaded_media
    publisher._record_processed_media = mock_record_processed_media

    message = _make_message()
    # Multiple topic_ids — _record_processed_media should still be called once
    asyncio.run(publisher.process_message_faces(message, [1, 2, 3]))

    assert processed_calls["count"] == 1, (
        f"_record_processed_media should be called exactly once, got {processed_calls['count']}"
    )


# ---------------------------------------------------------------------------
# Test 3: FloodWaitError marks bot locked and retries with next bot
# Requirements: 9.3
# ---------------------------------------------------------------------------

def test_flood_wait_marks_bot_locked_and_retries():
    """
    When bot.client.send_file raises FloodWaitError, bot_pool.mark_locked is called
    with the bot name and duration, and the upload retries with the next bot.
    """
    # Build a mock FloodWaitError with a seconds attribute
    class MockFloodWaitError(Exception):
        def __init__(self, seconds):
            self.seconds = seconds
            super().__init__(f"FloodWait for {seconds}s")

    bot1 = _make_bot("bot1")
    bot2 = _make_bot("bot2")

    # bot1 raises FloodWait, bot2 succeeds
    flood_error = MockFloodWaitError(seconds=30)
    bot1.client.send_file = AsyncMock(side_effect=flood_error)

    fake_message = MagicMock()
    fake_message.id = 777
    bot2.client.send_file = AsyncMock(return_value=fake_message)

    call_count = {"n": 0}

    def get_bot_side_effect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return bot1
        return bot2

    bot_pool = MagicMock()
    bot_pool.get_bot = MagicMock(side_effect=get_bot_side_effect)
    bot_pool.mark_locked = MagicMock()

    pool = _make_pool_mock()
    publisher = Publisher(pool, bot_pool)

    with patch("services.face_recognition.publisher.get_hub_group_id", return_value=-100123456789), \
         patch("telethon.errors.FloodWaitError", MockFloodWaitError):
        result = asyncio.run(publisher._upload_to_topic("/tmp/test.jpg", topic_id=5))

    # mark_locked called with bot1's name and the flood wait duration
    bot_pool.mark_locked.assert_called_once_with("bot1", 30)

    # Upload succeeded with bot2
    assert result == 777, f"Expected hub_message_id=777, got {result}"

    # bot2.send_file was called
    bot2.client.send_file.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: All bots locked does not drop the task
# Requirements: 9.4
# ---------------------------------------------------------------------------

def test_all_bots_locked_does_not_drop_task():
    """
    When all bots are locked (get_bot raises RuntimeError), the upload waits
    and eventually succeeds when a bot becomes available.
    """
    bot = _make_bot("bot1")
    fake_message = MagicMock()
    fake_message.id = 555
    bot.client.send_file = AsyncMock(return_value=fake_message)

    call_count = {"n": 0}

    def get_bot_side_effect():
        call_count["n"] += 1
        # Fail first 2 calls (all bots locked), succeed on 3rd
        if call_count["n"] <= 2:
            raise RuntimeError("All bots are locked")
        return bot

    bot_pool = MagicMock()
    bot_pool.get_bot = MagicMock(side_effect=get_bot_side_effect)

    pool = _make_pool_mock()
    publisher = Publisher(pool, bot_pool)

    with patch("services.face_recognition.publisher.get_hub_group_id", return_value=-100123456789), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = asyncio.run(publisher._upload_to_topic("/tmp/test.jpg", topic_id=7))

    # Task was not dropped — eventually succeeded
    assert result == 555, f"Expected hub_message_id=555, got {result}"

    # get_bot was called at least 3 times (2 failures + 1 success)
    assert call_count["n"] >= 3, (
        f"Expected get_bot to be called at least 3 times, got {call_count['n']}"
    )


# ---------------------------------------------------------------------------
# Test 5: Topic creation retries three times before raising
# Requirements: 7.3
# ---------------------------------------------------------------------------

def test_topic_creation_retries_three_times():
    """
    When _ensure_topic_exists fails (Telegram API error), it retries up to 3 times
    before raising.
    """
    # DB row: topic_id=0 (needs creation)
    conn = _make_conn_mock()
    conn.fetchrow = AsyncMock(return_value={"topic_id": 0, "label": "Unknown Person"})
    pool = _make_pool_mock(conn_mock=conn)

    bot = _make_bot("bot1")
    attempt_count = {"n": 0}

    async def failing_create(*args, **kwargs):
        attempt_count["n"] += 1
        raise Exception("Telegram API error")

    bot.client = MagicMock()
    bot.client.__call__ = failing_create
    # The publisher calls bot.client(request) — mock it as a coroutine
    bot.client = AsyncMock(side_effect=Exception("Telegram API error"))

    bot_pool = MagicMock()
    bot_pool.get_bot = MagicMock(return_value=bot)

    pool = _make_pool_mock(conn_mock=conn)
    publisher = Publisher(pool, bot_pool)

    with patch("services.face_recognition.publisher.get_hub_group_id", return_value=-100123456789), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        try:
            asyncio.run(publisher._ensure_topic_exists(db_topic_id=1))
            assert False, "Expected RuntimeError to be raised after 3 retries"
        except RuntimeError as e:
            assert "3 attempts" in str(e), f"Unexpected error message: {e}"

    # Should have slept between retries (2 sleeps for 3 attempts)
    assert mock_sleep.call_count == 2, (
        f"Expected 2 sleep calls between 3 attempts, got {mock_sleep.call_count}"
    )
