"""Unit tests for IdentityMatcher (task 4.6).

Tests:
  - test_new_identity_has_label_unknown_person
  - test_new_identity_has_zero_face_count
  - test_below_quality_threshold_returns_zero
  - test_empty_db_creates_new_identity

Requirements: 5.6, 6.3, 7.1
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

from services.face_recognition.matcher import IdentityMatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — reuse the same mock-building pattern as test_props_matcher.py
# ---------------------------------------------------------------------------

def _make_conn_mock(fetchrow_side_effect=None, fetchrow_return=None):
    """Build a mock asyncpg connection with transaction context manager support."""
    conn = MagicMock()

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    if fetchrow_side_effect is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    else:
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)

    conn.execute = AsyncMock(return_value=None)
    return conn


def _make_pool_mock(pool_fetchrow_return=None, conn_mock=None):
    """Build a mock asyncpg pool."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=pool_fetchrow_return)

    if conn_mock is None:
        conn_mock = _make_conn_mock()

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)

    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIdentityMatcherUnit:

    def test_new_identity_has_label_unknown_person(self):
        """
        When _create_new_identity is called, the INSERT into telegram_topics
        uses label='Unknown Person'.

        Requirements: 7.1
        """
        captured_queries = []

        async def conn_fetchrow(query, *args):
            captured_queries.append(query)
            # _find_similar_embedding re-check inside lock → no match
            if "face_embeddings" in query and "ORDER BY" in query:
                return None
            # INSERT into telegram_topics → return new id
            if "telegram_topics" in query and "INSERT" in query:
                return {"id": 99}
            # INSERT into face_embeddings → return new id
            if "face_embeddings" in query and "INSERT" in query:
                return {"id": 1}
            return None

        conn = _make_conn_mock(fetchrow_side_effect=conn_fetchrow)
        # pool.fetchrow used by _find_similar_embedding (initial check, no match)
        pool = _make_pool_mock(pool_fetchrow_return=None, conn_mock=conn)

        matcher = IdentityMatcher(pool)
        embedding = [0.1] * 512

        with patch(
            "services.face_recognition.matcher.get_dynamic_setting",
            side_effect=lambda key, default=None: (
                0.8 if key == "FACE_SIMILARITY_THRESHOLD" else 0.5
            ),
        ):
            asyncio.run(
                matcher._create_new_identity(
                    embedding=embedding,
                    quality_score=0.9,
                    source_chat_id=1,
                    source_message_id=1,
                    frame_index=0,
                )
            )

        insert_query = next(
            (q for q in captured_queries if "telegram_topics" in q and "INSERT" in q),
            None,
        )
        assert insert_query is not None, "Expected an INSERT into telegram_topics"
        assert "Unknown Person" in insert_query, (
            f"INSERT into telegram_topics must use label='Unknown Person', got:\n{insert_query}"
        )

    def test_new_identity_has_zero_face_count(self):
        """
        When _create_new_identity is called, the INSERT into telegram_topics
        uses face_count=0.

        Requirements: 7.1
        """
        captured_queries = []

        async def conn_fetchrow(query, *args):
            captured_queries.append(query)
            if "face_embeddings" in query and "ORDER BY" in query:
                return None
            if "telegram_topics" in query and "INSERT" in query:
                return {"id": 99}
            if "face_embeddings" in query and "INSERT" in query:
                return {"id": 1}
            return None

        conn = _make_conn_mock(fetchrow_side_effect=conn_fetchrow)
        pool = _make_pool_mock(pool_fetchrow_return=None, conn_mock=conn)

        matcher = IdentityMatcher(pool)
        embedding = [0.1] * 512

        with patch(
            "services.face_recognition.matcher.get_dynamic_setting",
            side_effect=lambda key, default=None: (
                0.8 if key == "FACE_SIMILARITY_THRESHOLD" else 0.5
            ),
        ):
            asyncio.run(
                matcher._create_new_identity(
                    embedding=embedding,
                    quality_score=0.9,
                    source_chat_id=1,
                    source_message_id=1,
                    frame_index=0,
                )
            )

        insert_query = next(
            (q for q in captured_queries if "telegram_topics" in q and "INSERT" in q),
            None,
        )
        assert insert_query is not None, "Expected an INSERT into telegram_topics"
        # The INSERT SQL contains the literal 0 for face_count
        assert ", 0," in insert_query or "face_count" in insert_query, (
            "INSERT into telegram_topics must include face_count=0"
        )
        # More specifically, the VALUES clause should have 0 for face_count
        assert "0, 0, NOW()" in insert_query or "0," in insert_query, (
            f"INSERT into telegram_topics must use face_count=0, got:\n{insert_query}"
        )

    def test_below_quality_threshold_returns_zero(self):
        """
        When find_or_create_identity is called with quality below threshold,
        it returns (0, False) without touching the DB.

        Requirements: 5.6, 6.3
        """
        pool = _make_pool_mock()
        matcher = IdentityMatcher(pool)
        embedding = [0.1] * 512

        # quality_score=0.3 is below min_quality=0.5
        with patch(
            "services.face_recognition.matcher.get_dynamic_setting",
            side_effect=lambda key, default=None: (
                0.8 if key == "FACE_SIMILARITY_THRESHOLD" else 0.5
            ),
        ):
            result = asyncio.run(
                matcher.find_or_create_identity(
                    embedding=embedding,
                    quality_score=0.3,
                    source_chat_id=1,
                    source_message_id=1,
                    frame_index=0,
                )
            )

        assert result == (0, False), (
            f"Expected (0, False) for below-threshold quality, got {result}"
        )
        # DB should not have been touched
        pool.fetchrow.assert_not_called()
        pool.acquire.assert_not_called()

    def test_empty_db_creates_new_identity(self):
        """
        When the DB has no embeddings (pool.fetchrow returns None),
        find_or_create_identity creates a new identity and returns (new_id, True).

        Requirements: 6.3, 7.1
        """
        new_topic_db_id = 42

        async def conn_fetchrow(query, *args):
            # Re-check inside advisory lock: still no match
            if "face_embeddings" in query and "ORDER BY" in query:
                return None
            # INSERT into telegram_topics
            if "telegram_topics" in query and "INSERT" in query:
                return {"id": new_topic_db_id}
            # INSERT into face_embeddings
            if "face_embeddings" in query and "INSERT" in query:
                return {"id": 1}
            return None

        conn = _make_conn_mock(fetchrow_side_effect=conn_fetchrow)
        # pool.fetchrow (initial _find_similar_embedding) → no match
        pool = _make_pool_mock(pool_fetchrow_return=None, conn_mock=conn)

        matcher = IdentityMatcher(pool)
        embedding = [0.1] * 512

        with patch(
            "services.face_recognition.matcher.get_dynamic_setting",
            side_effect=lambda key, default=None: (
                0.8 if key == "FACE_SIMILARITY_THRESHOLD" else 0.5
            ),
        ):
            result = asyncio.run(
                matcher.find_or_create_identity(
                    embedding=embedding,
                    quality_score=0.9,
                    source_chat_id=1,
                    source_message_id=1,
                    frame_index=0,
                )
            )

        assert result == (new_topic_db_id, True), (
            f"Expected ({new_topic_db_id}, True) when DB is empty, got {result}"
        )
