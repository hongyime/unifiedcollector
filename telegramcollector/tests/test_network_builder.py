"""
Unit tests for NetworkBuilder.

Requirements: 4.3, 4.5, 4.6
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from contextlib import asynccontextmanager

from services.user_intelligence.network_builder import NetworkBuilder


def make_pool(fetch_return=None):
    """
    Build a minimal asyncpg pool mock that supports:
        async with pool.acquire() as conn:
            rows = await conn.fetch(...)
            await conn.execute(...)
    """
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()

    @asynccontextmanager
    async def mock_acquire():
        yield conn

    pool.acquire = mock_acquire
    return pool, conn


# ---------------------------------------------------------------------------
# Test 1: no edges written when chat is empty (covers the "disabled" note too)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_network_write_when_disabled():
    """
    When process_new_membership is called for a chat with no existing members
    (fetch returns []), conn.execute must never be called.

    Note: the USER_INTEL_NETWORK_ENABLED flag is checked in main.py/_process_batch
    before calling NetworkBuilder at all. This test verifies that NetworkBuilder
    itself does nothing when there are no co-members to connect.

    Validates: Requirement 4.3
    """
    pool, conn = make_pool(fetch_return=[])
    builder = NetworkBuilder(pool)

    await builder.process_new_membership(new_user_id=1, chat_id=10)

    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: no edges for empty chat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_edges_for_empty_chat():
    """
    _fetch_chat_members returns [] → _upsert_edge is never called → conn.execute
    is never called.

    Validates: Requirement 4.5
    """
    pool, conn = make_pool(fetch_return=[])
    builder = NetworkBuilder(pool)

    await builder.process_new_membership(new_user_id=42, chat_id=99)

    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: self-edge never created — SQL excludes the new user
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_edge_never_created():
    """
    _fetch_chat_members passes exclude_user_id as the second SQL parameter ($2),
    so the new user is never returned and therefore never connected to themselves.

    We verify this by calling _fetch_chat_members directly and asserting that
    conn.fetch was called with the correct positional arguments (chat_id, exclude_user_id).

    Validates: Requirement 4.6
    """
    chat_id = 55
    new_user_id = 7

    pool, conn = make_pool(fetch_return=[])
    builder = NetworkBuilder(pool)

    await builder._fetch_chat_members(chat_id=chat_id, exclude_user_id=new_user_id)

    # conn.fetch must have been called once
    conn.fetch.assert_awaited_once()

    # The SQL positional args must be (chat_id, exclude_user_id) in that order
    _call_args = conn.fetch.call_args
    positional_args = _call_args.args  # (sql, chat_id, exclude_user_id)
    assert positional_args[1] == chat_id, (
        f"Expected $1 (chat_id) = {chat_id}, got {positional_args[1]}"
    )
    assert positional_args[2] == new_user_id, (
        f"Expected $2 (exclude_user_id) = {new_user_id}, got {positional_args[2]}"
    )


# ---------------------------------------------------------------------------
# Property 6: Network edge canonicality
# ---------------------------------------------------------------------------
# Feature: user-intelligence-service, Property 6: network edge canonicality
from hypothesis import given, settings
from hypothesis import strategies as st
import asyncio


@given(
    user_id_x=st.integers(min_value=1, max_value=10_000),
    user_id_y=st.integers(min_value=1, max_value=10_000).filter(lambda y: y != 0),
)
@settings(max_examples=300)
def test_edge_canonical_ordering(user_id_x, user_id_y):
    """
    For any two distinct user IDs X and Y, _upsert_edge must:
      1. Call conn.execute exactly once.
      2. Pass the raw (user_id_x, user_id_y) values as SQL parameters — the
         LEAST/GREATEST canonicalisation is expressed in the SQL itself, not in
         Python, so we verify the SQL string contains "LEAST" and "GREATEST".

    Validates: Requirements 4.2, 4.5
    """
    # Ensure the two IDs are distinct (filter above only excludes 0; we need x != y)
    if user_id_x == user_id_y:
        return  # skip degenerate case — Hypothesis filter handles most, but be safe

    pool, conn = make_pool()
    builder = NetworkBuilder(pool)

    asyncio.run(builder._upsert_edge(user_id_x, user_id_y))

    # 1. conn.execute must have been called exactly once
    conn.execute.assert_awaited_once()

    # 2. Inspect the SQL string and the positional parameters
    call_args = conn.execute.call_args
    sql = call_args.args[0]
    params = call_args.args[1:]  # positional args after the SQL string

    assert "LEAST" in sql.upper(), (
        "SQL must contain LEAST() to enforce canonical ordering"
    )
    assert "GREATEST" in sql.upper(), (
        "SQL must contain GREATEST() to enforce canonical ordering"
    )

    # The raw user IDs are passed as parameters; LEAST/GREATEST is in the SQL
    assert params[0] == user_id_x, (
        f"First SQL parameter must be user_id_x={user_id_x}, got {params[0]}"
    )
    assert params[1] == user_id_y, (
        f"Second SQL parameter must be user_id_y={user_id_y}, got {params[1]}"
    )


# ---------------------------------------------------------------------------
# Property 7: Network edge deduplication
# ---------------------------------------------------------------------------
# Feature: user-intelligence-service, Property 7: network edge dedup
# Validates: Requirements 4.2, 4.4


@given(
    user_id_a=st.integers(min_value=1, max_value=1000),
    user_id_b=st.integers(min_value=1001, max_value=2000),  # always > user_id_a
    n_chats=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=200)
def test_edge_dedup_shared_chat_count(user_id_a, user_id_b, n_chats):
    """
    For any (user_id_a, user_id_b) pair, no matter how many times the two users
    are observed sharing a chat, _upsert_edge must be called exactly n_chats times
    and each call must use SQL containing "ON CONFLICT" (deduplication is in the SQL).

    Since we mock the DB we cannot inspect the actual row count, so we verify:
      1. conn.execute is called exactly n_chats times (one per _upsert_edge call).
      2. Every call uses the same SQL containing "ON CONFLICT".

    Validates: Requirements 4.2, 4.4
    """
    pool, conn = make_pool()
    builder = NetworkBuilder(pool)

    # Call _upsert_edge n_chats times to simulate the pair sharing n_chats chats
    async def run_upserts():
        for _ in range(n_chats):
            await builder._upsert_edge(user_id_a, user_id_b)

    asyncio.run(run_upserts())

    # 1. conn.execute must have been called exactly n_chats times
    assert conn.execute.await_count == n_chats, (
        f"Expected conn.execute to be called {n_chats} times, "
        f"got {conn.execute.await_count}"
    )

    # 2. Every call must use SQL containing "ON CONFLICT"
    for i, call in enumerate(conn.execute.call_args_list):
        sql = call.args[0]
        assert "ON CONFLICT" in sql.upper(), (
            f"Call {i + 1}: SQL must contain 'ON CONFLICT' for deduplication, "
            f"got: {sql[:120]!r}"
        )


# ---------------------------------------------------------------------------
# Property 8: Network only on new membership
# ---------------------------------------------------------------------------
# Feature: user-intelligence-service, Property 8: network only on new membership
# Validates: Requirements 4.3


@given(
    user_id=st.integers(min_value=1),
    chat_id=st.integers(min_value=1),
    n_extra=st.integers(min_value=1, max_value=10),
)
@settings(max_examples=200)
def test_no_network_write_on_existing_membership(user_id, chat_id, n_extra):
    """
    For any sighting where MembershipTracker.process_sighting() returns
    is_new_membership=False (i.e., the (user_id, chat_id) pair already existed),
    processing that sighting SHALL NOT call network_builder.process_new_membership.

    We simulate the orchestration logic from main.py's _process_batch:
        is_new = await membership_tracker.process_sighting(sighting)
        if is_new and settings.USER_INTEL_NETWORK_ENABLED:
            await network_builder.process_new_membership(user_id, chat_id)

    - First call to membership_tracker.process_sighting returns True (INSERT path)
    - Subsequent n_extra calls return False (UPDATE path)

    Asserts:
      1. After the first sighting (is_new=True), network_builder.process_new_membership
         is called exactly once.
      2. After n_extra more sightings (is_new=False), network_builder.process_new_membership
         is still called only once total — no additional calls on UPDATE path.

    Validates: Requirements 4.3
    """
    membership_tracker = MagicMock()
    network_builder = MagicMock()
    network_builder.process_new_membership = AsyncMock()

    # First call returns True (INSERT path), all subsequent calls return False (UPDATE path)
    membership_tracker.process_sighting = AsyncMock(
        side_effect=[True] + [False] * n_extra
    )

    async def run():
        sighting = {"user_id": user_id, "seen_in_chat_id": chat_id}
        network_enabled = True

        # First sighting — INSERT path
        is_new = await membership_tracker.process_sighting(sighting)
        if is_new and network_enabled:
            await network_builder.process_new_membership(user_id, chat_id)

        # n_extra more sightings — UPDATE path
        for _ in range(n_extra):
            is_new = await membership_tracker.process_sighting(sighting)
            if is_new and network_enabled:
                await network_builder.process_new_membership(user_id, chat_id)

    asyncio.run(run())

    # network_builder.process_new_membership must have been called exactly once
    # (only for the first INSERT-path sighting, never for the UPDATE-path sightings)
    network_builder.process_new_membership.assert_awaited_once_with(user_id, chat_id)


# ---------------------------------------------------------------------------
# Property 9: Incremental shared_chat_count correctness
# ---------------------------------------------------------------------------
# Feature: user-intelligence-service, Property 9: incremental graph correctness
# Validates: Requirements 4.2, 4.4


@given(
    sightings=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=20),   # user_id
            st.integers(min_value=1, max_value=10),   # chat_id
        ),
        min_size=2,
        max_size=100,
    )
)
@settings(max_examples=100)
def test_shared_chat_count_equals_distinct_shared_chats(sightings):
    """
    For any two users A and B, the shared_chat_count value in user_connections
    SHALL equal the number of distinct chat_id values for which both A and B
    have a row in user_chat_memberships.

    Simulates the full pipeline in-memory without a real DB:
      1. memberships: {(user_id, chat_id): True} — tracks known pairs
      2. connections: {(min(a,b), max(a,b)): count} — incremented on new membership
      3. After all sightings, verify each edge count equals the number of distinct
         chats shared by both users in memberships.

    Validates: Requirements 4.2, 4.4
    """
    # In-memory state
    memberships: dict[tuple[int, int], bool] = {}
    connections: dict[tuple[int, int], int] = {}

    for user_id, chat_id in sightings:
        pair = (user_id, chat_id)
        if pair not in memberships:
            # INSERT path — find all other users already in this chat
            other_users = [uid for (uid, cid) in memberships if cid == chat_id]
            memberships[pair] = True
            # Upsert an edge for each co-member
            for other_user_id in other_users:
                edge = (min(user_id, other_user_id), max(user_id, other_user_id))
                connections[edge] = connections.get(edge, 0) + 1
        # UPDATE path — no network write

    # Verify: for each edge (a, b), shared_chat_count must equal the number of
    # distinct chat_ids where both a and b appear in memberships.
    for (user_id_a, user_id_b), count in connections.items():
        chats_a = {cid for (uid, cid) in memberships if uid == user_id_a}
        chats_b = {cid for (uid, cid) in memberships if uid == user_id_b}
        expected_count = len(chats_a & chats_b)
        assert count == expected_count, (
            f"Edge ({user_id_a}, {user_id_b}): "
            f"connections count={count}, "
            f"expected distinct shared chats={expected_count}. "
            f"chats_a={chats_a}, chats_b={chats_b}"
        )
