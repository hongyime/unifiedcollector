"""
Unit tests for MembershipTracker.

Requirements: 3.1, 3.3, 3.4
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.user_intelligence.membership_tracker import MembershipTracker


def make_pool(fetchrow_return=None):
    """Build a minimal asyncpg pool mock."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    return pool


@pytest.mark.asyncio
async def test_null_chat_id_skipped():
    """
    Sighting with seen_in_chat_id=None must return False without touching the DB.
    Validates: Requirement 3.4
    """
    pool = make_pool()
    tracker = MembershipTracker(pool)

    result = await tracker.process_sighting({"user_id": 1, "seen_in_chat_id": None})

    assert result is False
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_first_sighting_returns_true():
    """
    When the DB upsert takes the INSERT path (is_insert=True), process_sighting returns True.
    Validates: Requirement 3.1, 3.3
    """
    pool = make_pool(fetchrow_return={"is_insert": True})
    tracker = MembershipTracker(pool)

    result = await tracker.process_sighting({"user_id": 42, "seen_in_chat_id": 100})

    assert result is True
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_sighting_returns_false():
    """
    When the DB upsert takes the UPDATE path (is_insert=False), process_sighting returns False.
    Validates: Requirement 3.1, 3.3
    """
    pool = make_pool(fetchrow_return={"is_insert": False})
    tracker = MembershipTracker(pool)

    result = await tracker.process_sighting({"user_id": 42, "seen_in_chat_id": 100})

    assert result is False
    pool.fetchrow.assert_awaited_once()


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------
import asyncio
from hypothesis import given, settings
from hypothesis import strategies as st


# Feature: user-intelligence-service, Property 5: membership upsert idempotence
@given(
    user_id=st.integers(min_value=1),
    chat_id=st.integers(min_value=1),
    n=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=200)
def test_membership_upsert_idempotent(user_id, chat_id, n):
    """
    For any (user_id, chat_id) pair, processing N sightings SHALL result in
    fetchrow being called exactly N times: the first call returns is_insert=True
    (INSERT path) and all subsequent calls return is_insert=False (UPDATE path).

    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    async def run():
        # Build side_effect: first call → INSERT, rest → UPDATE
        side_effects = [{"is_insert": True}] + [{"is_insert": False}] * (n - 1)

        pool = MagicMock()
        pool.fetchrow = AsyncMock(side_effect=side_effects)
        tracker = MembershipTracker(pool)

        sighting = {"user_id": user_id, "seen_in_chat_id": chat_id}

        results = []
        for _ in range(n):
            result = await tracker.process_sighting(sighting)
            results.append(result)

        # fetchrow must be called exactly n times — one per sighting
        assert pool.fetchrow.await_count == n

        # Exactly one INSERT (True) and n-1 UPDATEs (False)
        assert results.count(True) == 1
        assert results.count(False) == n - 1

    asyncio.run(run())
