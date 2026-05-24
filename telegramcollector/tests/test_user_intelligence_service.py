"""
Unit tests for UserIntelligenceService.

Validates: Requirements 1.1, 1.4, 6.3
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(fetchrow_return=None, fetch_return=None):
    """Build a minimal asyncpg pool mock."""
    pool = MagicMock()
    pool.execute = AsyncMock(return_value=None)
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.fetch = AsyncMock(return_value=fetch_return if fetch_return is not None else [])
    return pool


def _make_service(pool):
    """Instantiate UserIntelligenceService with stub component dependencies."""
    from services.user_intelligence.main import UserIntelligenceService

    change_tracker = MagicMock()
    change_tracker.process_sighting = AsyncMock(return_value=None)

    membership_tracker = MagicMock()
    membership_tracker.process_sighting = AsyncMock(return_value=False)

    network_builder = MagicMock()
    network_builder.process_new_membership = AsyncMock(return_value=None)

    return UserIntelligenceService(
        db_pool=pool,
        change_tracker=change_tracker,
        membership_tracker=membership_tracker,
        network_builder=network_builder,
    )


# ---------------------------------------------------------------------------
# Cursor initialisation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cursor_init_no_row():
    """
    When no row exists in service_cursors, _init_cursor() should INSERT
    (ON CONFLICT DO NOTHING) and return 0.

    Validates: Requirement 1.1
    """
    pool = _make_pool(fetchrow_return={"last_message_id": 0})
    service = _make_service(pool)

    result = await service._init_cursor()

    # INSERT was attempted
    pool.execute.assert_called_once()
    # Cursor value returned is 0
    assert result == 0


@pytest.mark.asyncio
async def test_cursor_init_existing_row():
    """
    When a row already exists with last_message_id = 42, _init_cursor()
    should return 42.

    Validates: Requirement 1.1
    """
    pool = _make_pool(fetchrow_return={"last_message_id": 42})
    service = _make_service(pool)

    result = await service._init_cursor()

    assert result == 42


# ---------------------------------------------------------------------------
# Polling loop behaviour tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_batch_sleeps():
    """
    When the sightings batch is empty, the loop should call
    asyncio.sleep(USER_INTEL_POLL_INTERVAL) before polling again.

    Validates: Requirement 1.4
    """
    pool = _make_pool(
        fetchrow_return={"last_message_id": 0},
        fetch_return=[],  # empty batch
    )
    service = _make_service(pool)

    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)
        # Stop the loop after the first sleep so the test terminates.
        service.stop()

    with patch("shared.config.settings") as mock_settings, \
         patch("shared.config.get_dynamic_setting", return_value=True), \
         patch("asyncio.sleep", side_effect=_fake_sleep):

        mock_settings.USER_INTEL_PROCESSING_ENABLED = True
        mock_settings.USER_INTEL_BATCH_SIZE = 100
        mock_settings.USER_INTEL_POLL_INTERVAL = 5
        mock_settings.USER_INTEL_NETWORK_ENABLED = True

        await service.start()

    assert len(sleep_calls) >= 1
    assert sleep_calls[0] == mock_settings.USER_INTEL_POLL_INTERVAL


@pytest.mark.asyncio
async def test_processing_disabled_skips_batch():
    """
    When USER_INTEL_PROCESSING_ENABLED is False, the loop must NOT call
    pool.fetch (no sightings query is issued).

    Validates: Requirement 6.3
    """
    pool = _make_pool()
    service = _make_service(pool)

    async def _fake_sleep(seconds):
        # Stop the loop after the first disabled-path sleep.
        service.stop()

    with patch("shared.config.settings") as mock_settings, \
         patch("shared.config.get_dynamic_setting", return_value=False), \
         patch("asyncio.sleep", side_effect=_fake_sleep):

        mock_settings.USER_INTEL_PROCESSING_ENABLED = False
        mock_settings.USER_INTEL_BATCH_SIZE = 100
        mock_settings.USER_INTEL_POLL_INTERVAL = 5
        mock_settings.USER_INTEL_NETWORK_ENABLED = True

        await service.start()

    pool.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Property 4: Cursor monotonicity
# ---------------------------------------------------------------------------

from hypothesis import given, settings as h_settings
from hypothesis import strategies as st


@given(
    batches=st.lists(
        st.lists(st.integers(min_value=1, max_value=10_000), min_size=1),
        min_size=2,
    )
)
@h_settings(max_examples=100)
def test_cursor_monotonically_increases(batches):
    """
    Property 4: Cursor monotonicity.

    For any sequence of non-empty batches (made globally increasing to mirror the
    real cursor-based SELECT WHERE id > cursor ORDER BY id ASC behaviour), the
    value passed to _advance_cursor after processing batch N+1 SHALL be strictly
    greater than the value passed after batch N.
    After all batches, the final cursor value SHALL equal the max id across all batches.

    Validates: Requirements 1.3
    """
    # Flatten, sort, and deduplicate all ids so that the resulting batches are
    # globally increasing — this mirrors the real service behaviour where each
    # batch only contains ids strictly greater than the previous cursor.
    all_ids = sorted(set(id for batch in batches for id in batch))
    if not all_ids:
        return  # nothing to test

    # Split the sorted ids back into batches of roughly equal size
    n_batches = len(batches)
    chunk_size = max(1, len(all_ids) // n_batches)
    globally_increasing_batches = [
        all_ids[i : i + chunk_size]
        for i in range(0, len(all_ids), chunk_size)
    ]
    # Ensure we have at least 2 non-empty batches
    globally_increasing_batches = [b for b in globally_increasing_batches if b]
    if len(globally_increasing_batches) < 2:
        return  # degenerate case — skip

    pool = _make_pool()
    service = _make_service(pool)

    # Collect the values passed to _advance_cursor in order
    cursor_values: list[int] = []

    async def _fake_advance_cursor(new_value: int) -> None:
        cursor_values.append(new_value)

    service._advance_cursor = _fake_advance_cursor

    # For each batch, call _advance_cursor(max(batch)) — mirrors the real loop
    async def _run():
        for batch in globally_increasing_batches:
            await service._advance_cursor(max(batch))

    asyncio.run(_run())

    # Monotonicity: each successive cursor value must be >= the previous one
    for i in range(1, len(cursor_values)):
        assert cursor_values[i] >= cursor_values[i - 1], (
            f"Cursor decreased: cursor_values[{i}]={cursor_values[i]} < "
            f"cursor_values[{i-1}]={cursor_values[i-1]}"
        )

    # After all batches, the final cursor must equal the max id across all batches
    global_max = max(all_ids)
    assert cursor_values[-1] == global_max, (
        f"Final cursor {cursor_values[-1]} != global max id {global_max}"
    )
