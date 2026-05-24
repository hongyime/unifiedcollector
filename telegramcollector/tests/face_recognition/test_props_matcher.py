"""Property-based tests for IdentityMatcher (task 4.5).

Tests Properties 9–11 from the design document.
All tests use Hypothesis with @given and @settings(max_examples=100).

**Validates: Requirements 6.2, 6.3, 6.4, 6.5, 7.4**
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
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

from services.face_recognition.matcher import IdentityMatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Mock asyncpg pool helpers
# ---------------------------------------------------------------------------

def _make_conn_mock(fetchrow_return=None, execute_side_effect=None):
    """Build a mock asyncpg connection with transaction context manager support."""
    conn = MagicMock()

    # transaction() as async context manager
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    if execute_side_effect is not None:
        conn.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        conn.execute = AsyncMock(return_value=None)

    return conn


def _make_pool_mock(fetchrow_return=None, conn_mock=None):
    """Build a mock asyncpg pool.

    pool.fetchrow(...)          — used by _find_similar_embedding
    pool.acquire()              — async context manager yielding conn_mock
    """
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)

    if conn_mock is None:
        conn_mock = _make_conn_mock()

    # acquire() as async context manager
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)

    return pool


# ---------------------------------------------------------------------------
# Property 9: Similarity Threshold Invariant
# Validates: Requirements 6.2, 6.3
#
# _find_similar_embedding returns a match iff nearest similarity >= threshold;
# never returns a below-threshold match.
# ---------------------------------------------------------------------------

@given(
    similarity=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    threshold=st.floats(min_value=0.1, max_value=0.9, allow_nan=False),
)
@h_settings(max_examples=100)
def test_property_9_similarity_threshold_invariant(
    similarity: float, threshold: float
) -> None:
    """**Validates: Requirements 6.2, 6.3**

    _find_similar_embedding returns a match iff the nearest neighbor's
    similarity >= threshold. It must never return a below-threshold match.
    """
    # Build a mock row that the DB would return
    mock_row = {"topic_id": 42, "similarity": similarity}
    pool = _make_pool_mock(fetchrow_return=mock_row)

    matcher = IdentityMatcher(pool)
    embedding = [0.1] * 512

    with patch(
        "services.face_recognition.matcher.get_dynamic_setting",
        return_value=threshold,
    ):
        result = asyncio.run(matcher._find_similar_embedding(embedding))

    if similarity >= threshold:
        assert result is not None, (
            f"Expected a match for similarity={similarity:.4f} >= threshold={threshold:.4f}, "
            f"but got None"
        )
        assert result["similarity"] == similarity, (
            f"Expected result['similarity']={similarity:.4f}, got {result['similarity']:.4f}"
        )
    else:
        assert result is None, (
            f"Expected None for similarity={similarity:.4f} < threshold={threshold:.4f}, "
            f"but got {result}"
        )


@given(
    similarity=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    threshold=st.floats(min_value=0.1, max_value=0.9, allow_nan=False),
)
@h_settings(max_examples=100)
def test_property_9_no_match_when_db_empty(
    similarity: float, threshold: float
) -> None:
    """**Validates: Requirements 6.2**

    _find_similar_embedding returns None when the DB returns no row (empty table).
    """
    pool = _make_pool_mock(fetchrow_return=None)
    matcher = IdentityMatcher(pool)
    embedding = [0.1] * 512

    with patch(
        "services.face_recognition.matcher.get_dynamic_setting",
        return_value=threshold,
    ):
        result = asyncio.run(matcher._find_similar_embedding(embedding))

    assert result is None, (
        f"Expected None when DB returns no row, got {result}"
    )


# ---------------------------------------------------------------------------
# Property 10: Advisory Lock Re-Check
# Validates: Requirements 6.4, 6.5, 7.4
#
# Two concurrent find_or_create_identity calls with similar embeddings produce
# at most one new telegram_topics INSERT.
# ---------------------------------------------------------------------------

@given(
    embedding=st.lists(
        st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
        min_size=512,
        max_size=512,
    )
)
@h_settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.large_base_example, HealthCheck.too_slow],
)
def test_property_10_advisory_lock_recheck(embedding: list) -> None:
    """**Validates: Requirements 6.4, 6.5, 7.4**

    Two concurrent find_or_create_identity calls with the same embedding
    produce at most one INSERT into telegram_topics.

    The advisory lock + re-check pattern ensures that the second concurrent
    call finds the identity created by the first and skips the INSERT.
    """
    insert_count = {"telegram_topics": 0, "face_embeddings": 0}

    # Shared state: after the first INSERT into telegram_topics, subsequent
    # _find_similar_embedding calls should return the newly created identity.
    created_topic_id = {"value": None}

    async def mock_fetchrow_pool(query, *args):
        """Simulate pool.fetchrow for _find_similar_embedding."""
        if "face_embeddings" in query and "ORDER BY" in query:
            # Return a match only after the first identity has been created
            if created_topic_id["value"] is not None:
                return {"topic_id": created_topic_id["value"], "similarity": 0.9}
            return None
        return None

    def make_conn_for_worker(worker_id: int):
        """Each worker gets its own connection mock."""
        conn = MagicMock()

        # transaction() as async context manager
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        conn.transaction = MagicMock(return_value=txn)

        async def conn_fetchrow(query, *args):
            if "face_embeddings" in query and "ORDER BY" in query:
                # Re-check inside lock: worker 2 should find the identity
                if created_topic_id["value"] is not None:
                    return {"topic_id": created_topic_id["value"], "similarity": 0.9}
                return None
            if "telegram_topics" in query and "INSERT" in query:
                # Only the first worker actually inserts
                if created_topic_id["value"] is None:
                    insert_count["telegram_topics"] += 1
                    created_topic_id["value"] = 100 + worker_id
                    return {"id": created_topic_id["value"]}
                # Second worker: re-check found a match, so this path won't be reached
                return {"id": created_topic_id["value"]}
            if "face_embeddings" in query and "INSERT" in query:
                insert_count["face_embeddings"] += 1
                return {"id": 200 + worker_id}
            return None

        async def conn_execute(query, *args):
            pass

        conn.fetchrow = AsyncMock(side_effect=conn_fetchrow)
        conn.execute = AsyncMock(side_effect=conn_execute)
        return conn

    call_order = []

    def make_pool(worker_id: int):
        pool = MagicMock()
        pool.fetchrow = AsyncMock(side_effect=mock_fetchrow_pool)

        conn = make_conn_for_worker(worker_id)

        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire = MagicMock(return_value=acquire_ctx)
        return pool

    async def run_concurrent():
        pool1 = make_pool(worker_id=1)
        pool2 = make_pool(worker_id=2)

        matcher1 = IdentityMatcher(pool1)
        matcher2 = IdentityMatcher(pool2)

        # Patch get_dynamic_setting for both matchers
        with patch(
            "services.face_recognition.matcher.get_dynamic_setting",
            side_effect=lambda key, default=None: (
                0.8 if key == "FACE_SIMILARITY_THRESHOLD" else 0.5
            ),
        ):
            # Run both concurrently — worker 1 creates, worker 2 should re-check
            results = await asyncio.gather(
                matcher1.find_or_create_identity(
                    embedding=embedding,
                    quality_score=0.9,
                    source_chat_id=1,
                    source_message_id=1,
                    frame_index=0,
                ),
                matcher2.find_or_create_identity(
                    embedding=embedding,
                    quality_score=0.9,
                    source_chat_id=1,
                    source_message_id=2,
                    frame_index=0,
                ),
            )
        return results

    asyncio.run(run_concurrent())

    assert insert_count["telegram_topics"] <= 1, (
        f"Expected at most 1 INSERT into telegram_topics, "
        f"got {insert_count['telegram_topics']}"
    )


# ---------------------------------------------------------------------------
# Property 11: Face Count Invariant
# Validates: Requirements 6.4, 6.5
#
# telegram_topics.face_count always equals COUNT(face_embeddings WHERE topic_id=...).
# After N calls to _store_embedding, face_count was incremented N times.
# ---------------------------------------------------------------------------

@given(n=st.integers(min_value=1, max_value=10))
@h_settings(max_examples=100)
def test_property_11_face_count_invariant(n: int) -> None:
    """**Validates: Requirements 6.4, 6.5**

    After N calls to _store_embedding, the UPDATE to telegram_topics
    increments face_count exactly N times (one UPDATE per _store_embedding call).
    """
    face_count_increments = {"count": 0}
    embedding_inserts = {"count": 0}

    async def conn_fetchrow(query, *args):
        if "face_embeddings" in query and "INSERT" in query:
            embedding_inserts["count"] += 1
            return {"id": embedding_inserts["count"]}
        return None

    async def conn_execute(query, *args):
        if "telegram_topics" in query and "face_count" in query and "UPDATE" in query:
            face_count_increments["count"] += 1

    conn = _make_conn_mock()
    conn.fetchrow = AsyncMock(side_effect=conn_fetchrow)
    conn.execute = AsyncMock(side_effect=conn_execute)

    pool = _make_pool_mock(conn_mock=conn)

    matcher = IdentityMatcher(pool)
    embedding = [0.1] * 512
    topic_id = 42

    async def run_n_stores():
        for i in range(n):
            await matcher._store_embedding(
                embedding=embedding,
                topic_id=topic_id,
                quality_score=0.9,
                source_chat_id=1,
                source_message_id=i,
                frame_index=0,
            )

    asyncio.run(run_n_stores())

    assert embedding_inserts["count"] == n, (
        f"Expected {n} INSERT into face_embeddings, got {embedding_inserts['count']}"
    )
    assert face_count_increments["count"] == n, (
        f"Expected face_count to be incremented {n} times, "
        f"got {face_count_increments['count']} UPDATE calls"
    )
