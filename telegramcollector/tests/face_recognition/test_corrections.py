"""Unit tests for Corrections (task 8.6).

Tests:
  - test_rename_updates_label
  - test_merge_deletes_source_topic
  - test_merge_calls_delete_telegram_topic

Requirements: 13.1, 13.3, 13.5
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup and env vars
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "test_hash")
os.environ.setdefault("BOT_TOKEN", "123:test_token")
os.environ.setdefault("HUB_GROUP_ID", "-100123456789")
os.environ.setdefault("DB_PASSWORD", "test_password")

from services.face_recognition.corrections import Corrections  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn_mock(fetchrow_return=None):
    """Build a mock asyncpg connection with transaction context manager support."""
    conn = MagicMock()

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    return conn


def _make_pool_mock(conn_mock=None):
    """Build a mock asyncpg pool whose acquire() is an async context manager."""
    pool = MagicMock()

    if conn_mock is None:
        conn_mock = _make_conn_mock()

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)

    return pool


def _make_bot_pool_mock():
    """Build a minimal BotPool mock."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCorrectionsUnit:

    def test_rename_updates_label(self):
        """
        rename_identity issues an UPDATE with the new label.

        Requirements: 13.3
        """
        captured_queries = []
        captured_args = []

        async def mock_execute(query, *args):
            captured_queries.append(query)
            captured_args.append(args)

        conn = _make_conn_mock()
        conn.execute = AsyncMock(side_effect=mock_execute)
        pool = _make_pool_mock(conn_mock=conn)
        bot_pool = _make_bot_pool_mock()

        corrections = Corrections(pool, bot_pool)

        asyncio.run(corrections.rename_identity(topic_db_id=7, new_label="Alice"))

        assert len(captured_queries) == 1, "Expected exactly one SQL statement"
        query = captured_queries[0]
        args = captured_args[0]

        assert "UPDATE" in query.upper(), "Expected an UPDATE statement"
        assert "telegram_topics" in query, "Expected UPDATE on telegram_topics"
        assert "label" in query, "Expected label column in UPDATE"

        # The new label and topic id must be passed as parameters
        assert "Alice" in args, f"Expected 'Alice' in query args, got {args}"
        assert 7 in args, f"Expected topic_db_id=7 in query args, got {args}"

    def test_merge_deletes_source_topic(self):
        """
        merge_identities issues a DELETE for the source topic row.

        Requirements: 13.1
        """
        captured_queries = []

        async def mock_execute(query, *args):
            captured_queries.append(query.strip())

        # fetchrow returns the source telegram_topic_id (used for best-effort deletion)
        conn = _make_conn_mock(fetchrow_return={"topic_id": 0})
        conn.execute = AsyncMock(side_effect=mock_execute)
        pool = _make_pool_mock(conn_mock=conn)
        bot_pool = _make_bot_pool_mock()

        corrections = Corrections(pool, bot_pool)

        asyncio.run(corrections.merge_identities(source_topic_db_id=1, target_topic_db_id=2))

        delete_queries = [q for q in captured_queries if q.upper().startswith("DELETE")]
        assert len(delete_queries) >= 1, (
            f"Expected at least one DELETE statement, got queries: {captured_queries}"
        )

        # The DELETE must reference the source topic id (1)
        source_deleted = any("telegram_topics" in q for q in delete_queries)
        assert source_deleted, (
            f"Expected DELETE FROM telegram_topics, got: {delete_queries}"
        )

    def test_merge_calls_delete_telegram_topic(self):
        """
        merge_identities calls _delete_telegram_topic with the source's Telegram topic ID.

        Requirements: 13.5
        """
        source_telegram_topic_id = 999

        # fetchrow returns the source row with a non-zero telegram topic_id
        conn = _make_conn_mock(fetchrow_return={"topic_id": source_telegram_topic_id})
        conn.execute = AsyncMock(return_value=None)
        pool = _make_pool_mock(conn_mock=conn)
        bot_pool = _make_bot_pool_mock()

        corrections = Corrections(pool, bot_pool)

        # Patch _delete_telegram_topic on the instance to capture the call
        corrections._delete_telegram_topic = AsyncMock()

        asyncio.run(corrections.merge_identities(source_topic_db_id=10, target_topic_db_id=20))

        corrections._delete_telegram_topic.assert_awaited_once_with(source_telegram_topic_id)
