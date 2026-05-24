"""
Unit tests for ChangeTracker (services/user_intelligence/change_tracker.py).

Tests use unittest.mock to mock the asyncpg pool — no real DB required.

Requirements: 2.2, 2.3, 2.5, 2.6
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.user_intelligence.change_tracker import (
    ChangeTracker,
    _extract_field_values,
    TRACKED_FIELDS,
)


# ---------------------------------------------------------------------------
# Pure-function tests for _extract_field_values
# ---------------------------------------------------------------------------

def test_extract_field_values_all_present():
    """Payload with all five fields populated returns all values."""
    payload = {
        "username": "alice",
        "first_name": "Alice",
        "last_name": "Smith",
        "bio": "Hello world",
        "photo": {"photo_id": 99999},
    }
    result = _extract_field_values(payload)
    assert result["username"] == "alice"
    assert result["first_name"] == "Alice"
    assert result["last_name"] == "Smith"
    assert result["bio"] == "Hello world"
    assert result["profile_photo_id"] == "99999"


def test_extract_field_values_photo_absent():
    """Payload with no 'photo' key → profile_photo_id is None."""
    payload = {
        "username": "bob",
        "first_name": "Bob",
        "last_name": "Jones",
        "bio": "Bio text",
        # no 'photo' key
    }
    result = _extract_field_values(payload)
    assert result["profile_photo_id"] is None
    # Other fields still extracted correctly
    assert result["username"] == "bob"


def test_extract_field_values_empty_string():
    """Payload with username = '' → username is None (empty string treated as absent)."""
    payload = {
        "username": "",
        "first_name": "Carol",
        "last_name": "",
        "bio": None,
        "photo": None,
    }
    result = _extract_field_values(payload)
    assert result["username"] is None
    assert result["last_name"] is None
    assert result["bio"] is None
    assert result["profile_photo_id"] is None
    assert result["first_name"] == "Carol"


# ---------------------------------------------------------------------------
# Async tests for ChangeTracker.process_sighting
# ---------------------------------------------------------------------------

def _make_pool(fetch_return=None):
    """Helper: build a mock asyncpg pool with controllable fetch/execute."""
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    pool.execute = AsyncMock(return_value=None)
    return pool


def _make_sighting(user_id: int, payload: dict) -> dict:
    return {"user_id": user_id, "payload": payload, "seen_at": "2024-01-01T00:00:00"}


@pytest.mark.asyncio
async def test_no_change_record_on_identical_value():
    """
    Same value in payload as in history → no INSERT called.

    Validates: Requirement 2.2
    """
    # Simulate DB returning "alice" as the last known username
    mock_row = MagicMock()
    mock_row.__getitem__ = lambda self, key: "alice" if key == "new_value" else "username"
    mock_row.keys = lambda: ["field_name", "new_value"]

    # Build a proper dict-like row
    db_row = {"field_name": "username", "new_value": "alice"}

    pool = _make_pool(fetch_return=[db_row])

    tracker = ChangeTracker(pool)
    sighting = _make_sighting(
        user_id=1,
        payload={"username": "alice"},  # same as last known
    )

    await tracker.process_sighting(sighting)

    # execute (INSERT) should NOT have been called
    pool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_change_record_on_different_value():
    """
    Different value in payload vs history → exactly one INSERT called.

    Validates: Requirement 2.2
    """
    db_row = {"field_name": "username", "new_value": "alice"}
    pool = _make_pool(fetch_return=[db_row])

    tracker = ChangeTracker(pool)
    sighting = _make_sighting(
        user_id=1,
        payload={"username": "alice_new"},  # different from "alice"
    )

    await tracker.process_sighting(sighting)

    pool.execute.assert_called_once()
    call_args = pool.execute.call_args
    # Verify the SQL and parameters contain the right values
    sql = call_args[0][0]
    params = call_args[0][1:]
    assert "INSERT" in sql.upper()
    assert 1 in params          # user_id
    assert "username" in params
    assert "alice" in params    # old_value
    assert "alice_new" in params  # new_value


@pytest.mark.asyncio
async def test_independent_field_tracking():
    """
    Change in 'username' does not affect 'bio' state.
    When 'bio' is not in the payload, no INSERT for bio is called.

    Validates: Requirement 2.6
    """
    # Only username has prior history; bio has no history (None)
    db_row = {"field_name": "username", "new_value": "alice"}
    pool = _make_pool(fetch_return=[db_row])

    tracker = ChangeTracker(pool)
    # Payload has a changed username but no bio field
    sighting = _make_sighting(
        user_id=1,
        payload={"username": "alice_v2"},  # changed
        # bio absent → partial payload rule → no INSERT for bio
    )

    await tracker.process_sighting(sighting)

    # Only one INSERT — for username change; bio is absent so it's skipped
    pool.execute.assert_called_once()
    call_args = pool.execute.call_args
    params = call_args[0][1:]
    assert "username" in params
    assert "bio" not in params


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

import asyncio
from hypothesis import given, settings, HealthCheck
import hypothesis.strategies as st


# Feature: user-intelligence-service, Property 1: partial payload preservation
@given(
    field=st.sampled_from(list(TRACKED_FIELDS.keys())),
    prior_value=st.text(min_size=1),          # non-empty prior value
    incoming=st.one_of(st.none(), st.just(""), st.just(None)),  # absent/empty
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_partial_payload_no_overwrite(field, prior_value, incoming):
    """**Validates: Requirements 2.3, 2.7**

    Property 1: Partial payload never overwrites a known non-empty value.

    For any user, any tracked field, and any sighting whose payload has an absent
    or empty value for that field, processing that sighting SHALL NOT insert a
    change record for that field.
    """
    # Build a mock pool:
    # - pool.fetch returns a row simulating prior state for this field
    # - pool.execute tracks INSERT calls
    db_row = {"field_name": field, "new_value": prior_value}
    pool = _make_pool(fetch_return=[db_row])

    # Build a payload where the field is either absent (incoming is None sentinel)
    # or set to the incoming value (empty string).
    # For profile_photo_id the payload path is photo.photo_id (nested),
    # so when testing that field as absent, omit the 'photo' key entirely.
    if incoming is None:
        # Absent: omit the field from the payload entirely
        if field == "profile_photo_id":
            payload = {}  # no 'photo' key at all
        else:
            payload = {}  # field key simply absent
    else:
        # Present but empty string
        if field == "profile_photo_id":
            # Empty photo_id — provide photo dict with empty photo_id
            payload = {"photo": {"photo_id": incoming}}
        else:
            payload = {field: incoming}

    sighting = _make_sighting(user_id=42, payload=payload)
    tracker = ChangeTracker(pool)

    async def run():
        await tracker.process_sighting(sighting)

    asyncio.run(run())

    # Assert: pool.execute (INSERT) was NOT called for this field
    pool.execute.assert_not_called()


# Feature: user-intelligence-service, Property 2: change record only on actual change
@given(
    field=st.sampled_from(list(TRACKED_FIELDS.keys())),
    value=st.integers(min_value=1).map(str),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_no_change_record_for_same_value(field, value):
    """**Validates: Requirements 2.2**

    Property 2: Change record only on actual change.

    For any user, any tracked field, and any sighting whose payload contains a
    non-empty value for that field that is identical to the last known value for
    that field, processing that sighting SHALL NOT insert a change record for
    that field.
    """
    # Build mock pool: fetch returns a row with `value` as the last known state
    db_row = {"field_name": field, "new_value": value}
    pool = _make_pool(fetch_return=[db_row])

    # Build payload with the same value (no change)
    if field == "profile_photo_id":
        payload = {"photo": {"photo_id": int(value)}}
    else:
        payload = {field: value}

    sighting = _make_sighting(user_id=42, payload=payload)
    tracker = ChangeTracker(pool)

    asyncio.run(tracker.process_sighting(sighting))

    # Assert: pool.execute (INSERT) was NOT called — same value means no change record
    pool.execute.assert_not_called()


# Feature: user-intelligence-service, Property 3: first observation no-op
@given(
    field=st.sampled_from(list(TRACKED_FIELDS.keys())),
    first_value=st.integers(min_value=1).map(str),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_first_observation_no_change_record(field, first_value):
    """**Validates: Requirements 2.5**

    Property 3: First non-empty observation establishes baseline without a change record.

    For any user and any tracked field with no prior user_history rows, processing a
    sighting with a non-empty value for that field SHALL NOT insert a change record —
    the value is silently established as the baseline state.
    """
    # No prior history: pool.fetch returns [] (no rows for this user+field)
    pool = _make_pool(fetch_return=[])

    # Build payload with a non-empty first_value for the field
    if field == "profile_photo_id":
        payload = {"photo": {"photo_id": int(first_value)}}
    else:
        payload = {field: first_value}

    sighting = _make_sighting(user_id=42, payload=payload)
    tracker = ChangeTracker(pool)

    asyncio.run(tracker.process_sighting(sighting))

    # Assert: no INSERT was called — first observation is a silent baseline establishment
    pool.execute.assert_not_called()
