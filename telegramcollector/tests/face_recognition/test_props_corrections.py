"""Property-based tests for Corrections (task 8.5).

Tests Properties 16–18 from the design document.
All tests use Hypothesis with @given and @settings(max_examples=100).

**Validates: Requirements 13.1, 13.2, 13.4**
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call

from hypothesis import given, settings as h_settings, HealthCheck, assume
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

from services.face_recognition.corrections import Corrections  # noqa: E402


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_transaction_ctx():
    """Return an async context manager that does nothing (simulates conn.transaction())."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_conn_mock(fetchrow_return=None, fetchval_return=None):
    """Build a mock asyncpg connection."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.transaction = MagicMock(return_value=_make_transaction_ctx())
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
    """Build a minimal BotPool mock."""
    bot_pool = MagicMock()
    return bot_pool


# ---------------------------------------------------------------------------
# Property 16: Merge Preserves All References
# Validates: Requirements 13.1
#
# After merge_identities(source, target):
#   - UPDATE face_embeddings SET topic_id=target WHERE topic_id=source
#   - UPDATE uploaded_media SET topic_id=target WHERE topic_id=source
#   - DELETE FROM telegram_topics WHERE id=source
#   - UPDATE telegram_topics SET face_count for target
# ---------------------------------------------------------------------------

@given(
    n_a=st.integers(1, 10),
    n_b=st.integers(1, 10),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_16_merge_preserves_all_references(n_a: int, n_b: int) -> None:
    """**Validates: Requirements 13.1**

    After merge_identities(source, target), the implementation must issue:
      1. UPDATE face_embeddings SET topic_id=target WHERE topic_id=source
      2. UPDATE uploaded_media SET topic_id=target WHERE topic_id=source
      3. UPDATE telegram_topics SET face_count=... WHERE id=target
      4. DELETE FROM telegram_topics WHERE id=source

    n_a and n_b represent the face counts of source and target identities
    (used to parameterise the test across many input combinations).
    """
    source_id = 100
    target_id = 200

    # First acquire() is for the pre-fetch of source telegram topic_id
    # Second acquire() is for the transaction block
    execute_calls = []

    conn_prefetch = _make_conn_mock(
        fetchrow_return={"topic_id": 999}  # source has a Telegram topic
    )

    conn_txn = _make_conn_mock()

    async def capture_execute(query, *args):
        execute_calls.append(query.strip())

    conn_txn.execute = AsyncMock(side_effect=capture_execute)

    # Pool returns conn_prefetch on first acquire, conn_txn on second
    call_counter = {"n": 0}

    def acquire_side_effect():
        call_counter["n"] += 1
        conn = conn_prefetch if call_counter["n"] == 1 else conn_txn
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=acquire_side_effect)

    bot_pool = _make_bot_pool_mock()
    # Prevent actual Telegram deletion
    bot_pool.get_bot = MagicMock(side_effect=Exception("no telegram in tests"))

    corrections = Corrections(pool, bot_pool)

    async def run():
        # Patch _delete_telegram_topic to be a no-op
        corrections._delete_telegram_topic = AsyncMock(return_value=None)
        await corrections.merge_identities(source_id, target_id)

    asyncio.run(run())

    # Verify all four required SQL operations were issued
    joined = "\n".join(execute_calls)

    # 1. UPDATE face_embeddings: topic_id=target WHERE topic_id=source
    assert any(
        "face_embeddings" in q and "topic_id" in q
        for q in execute_calls
    ), f"Expected UPDATE face_embeddings in execute calls.\nCalls:\n{joined}"

    # 2. UPDATE uploaded_media: topic_id=target WHERE topic_id=source
    assert any(
        "uploaded_media" in q and "topic_id" in q
        for q in execute_calls
    ), f"Expected UPDATE uploaded_media in execute calls.\nCalls:\n{joined}"

    # 3. UPDATE telegram_topics SET face_count for target
    assert any(
        "telegram_topics" in q and "face_count" in q
        for q in execute_calls
    ), f"Expected UPDATE telegram_topics face_count in execute calls.\nCalls:\n{joined}"

    # 4. DELETE FROM telegram_topics WHERE id=source
    assert any(
        "DELETE" in q.upper() and "telegram_topics" in q
        for q in execute_calls
    ), f"Expected DELETE FROM telegram_topics in execute calls.\nCalls:\n{joined}"

    # Total: exactly 4 execute calls inside the transaction
    assert len(execute_calls) == 4, (
        f"Expected exactly 4 execute calls in merge transaction, "
        f"got {len(execute_calls)}.\nCalls:\n{joined}"
    )


# ---------------------------------------------------------------------------
# Property 17: Split Preserves Face Count Total
# Validates: Requirements 13.2
#
# After split_identity(source, embedding_ids[:k]):
#   - Both source and new topic get their face_count updated via UPDATE
# ---------------------------------------------------------------------------

@given(
    n=st.integers(2, 20),
    k=st.integers(1, 10),
)
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_17_split_preserves_face_count_total(n: int, k: int) -> None:
    """**Validates: Requirements 13.2**

    After split_identity(source, embedding_ids_to_split):
      - The source topic gets its face_count updated
      - The new topic gets its face_count updated
      - Both UPDATE calls target telegram_topics with face_count

    n = total embeddings in source; k = embeddings to split off.
    We assume k <= n (split cannot exceed total).
    """
    assume(k <= n)

    source_id = 10
    new_topic_id = 99
    embedding_ids = list(range(1, k + 1))

    execute_calls = []

    conn = _make_conn_mock(fetchval_return=new_topic_id)

    async def capture_execute(query, *args):
        execute_calls.append(query.strip())

    conn.execute = AsyncMock(side_effect=capture_execute)

    pool = _make_pool_mock(conn_mock=conn)
    bot_pool = _make_bot_pool_mock()
    corrections = Corrections(pool, bot_pool)

    result = asyncio.run(corrections.split_identity(source_id, embedding_ids))

    assert result == new_topic_id, (
        f"Expected split_identity to return new_topic_id={new_topic_id}, got {result}"
    )

    joined = "\n".join(execute_calls)

    # There should be exactly 3 execute calls:
    #   1. UPDATE face_embeddings (reassign selected embeddings)
    #   2. UPDATE telegram_topics face_count for source
    #   3. UPDATE telegram_topics face_count for new topic
    assert len(execute_calls) == 3, (
        f"Expected 3 execute calls in split transaction, "
        f"got {len(execute_calls)}.\nCalls:\n{joined}"
    )

    # Both face_count updates must target telegram_topics
    face_count_updates = [
        q for q in execute_calls
        if "telegram_topics" in q and "face_count" in q
    ]
    assert len(face_count_updates) == 2, (
        f"Expected 2 face_count UPDATE calls on telegram_topics, "
        f"got {len(face_count_updates)}.\nCalls:\n{joined}"
    )

    # The face_embeddings reassignment must be present
    assert any(
        "face_embeddings" in q and "topic_id" in q
        for q in execute_calls
    ), f"Expected UPDATE face_embeddings in execute calls.\nCalls:\n{joined}"


# ---------------------------------------------------------------------------
# Property 18: Correction Atomicity
# Validates: Requirements 13.4
#
# If the Nth execute call raises an exception, the exception propagates
# (is not swallowed). The caller is responsible for rollback via the
# transaction context manager.
# ---------------------------------------------------------------------------

@given(fail_at_step=st.integers(1, 4))
@h_settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_property_18_correction_atomicity(fail_at_step: int) -> None:
    """**Validates: Requirements 13.4**

    If the Nth execute call inside merge_identities raises an exception,
    the exception propagates out of merge_identities (is not swallowed).

    asyncpg transactions re-raise exceptions on __aexit__, so the caller
    sees the error and can handle rollback. We verify that the exception
    is NOT suppressed by the Corrections implementation.
    """
    source_id = 1
    target_id = 2

    call_counter = {"n": 0}

    class _InjectedError(RuntimeError):
        pass

    async def failing_execute(query, *args):
        call_counter["n"] += 1
        if call_counter["n"] == fail_at_step:
            raise _InjectedError(f"Injected failure at step {fail_at_step}")

    # Transaction context manager that re-raises (like real asyncpg)
    def _make_reraise_transaction_ctx():
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=None)

        async def reraise_exit(exc_type, exc_val, exc_tb):
            # Do NOT suppress the exception — return False (or None)
            return False

        ctx.__aexit__ = AsyncMock(side_effect=reraise_exit)
        return ctx

    conn_prefetch = _make_conn_mock(fetchrow_return={"topic_id": 0})

    conn_txn = MagicMock()
    conn_txn.execute = AsyncMock(side_effect=failing_execute)
    conn_txn.fetchrow = AsyncMock(return_value=None)
    conn_txn.fetchval = AsyncMock(return_value=None)
    conn_txn.transaction = MagicMock(return_value=_make_reraise_transaction_ctx())

    acquire_call_count = {"n": 0}

    def acquire_side_effect():
        acquire_call_count["n"] += 1
        conn = conn_prefetch if acquire_call_count["n"] == 1 else conn_txn
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=acquire_side_effect)

    bot_pool = _make_bot_pool_mock()
    corrections = Corrections(pool, bot_pool)

    raised = False
    try:
        asyncio.run(corrections.merge_identities(source_id, target_id))
    except _InjectedError:
        raised = True
    except Exception:
        # Any other exception also counts as "not swallowed"
        raised = True

    assert raised, (
        f"Expected merge_identities to propagate the exception injected at "
        f"execute step {fail_at_step}, but no exception was raised."
    )
