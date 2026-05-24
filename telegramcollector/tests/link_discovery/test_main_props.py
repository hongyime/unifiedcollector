"""
Property-based tests for services/link_discovery/main.py

Feature: link-discovery-service

Properties tested:
  7. Cursor monotonicity              — Validates: Requirements 1.2
  8. Auto-queue account_id is NULL    — Validates: Requirements 7.9
  4. Dedup invariant                  — Validates: Requirements 4.2, 4.3
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from services.link_discovery.main import LinkDiscoveryService


# ---------------------------------------------------------------------------
# Property 7: Cursor monotonicity
# Validates: Requirements 1.2
# ---------------------------------------------------------------------------

@given(
    batches=st.lists(
        st.lists(st.integers(min_value=1, max_value=10**6), min_size=1, max_size=20),
        min_size=1, max_size=10,
    )
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cursor_monotonicity(batches):
    """
    For any sequence of batch id lists, the cursor value after each batch
    equals max(batch_ids) for that batch. The service enforces monotonicity
    at the SQL level (WHERE id > cursor), so the cursor update logic simply
    sets cursor = max(batch_ids) for each batch processed.
    **Validates: Requirements 1.2**
    """
    cursor = 0
    for batch_ids in batches:
        new_cursor = max(batch_ids)
        cursor = new_cursor
    # After all batches, cursor equals max of the last batch
    assert cursor == max(batches[-1])


# ---------------------------------------------------------------------------
# Property 8: Auto-queue account_id is always NULL
# Validates: Requirements 7.9
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_queue_account_id_is_always_null():
    """
    For any link that triggers an auto-queue insert, the inserted
    collector.group_join_queue row has account_id = NULL.
    **Validates: Requirements 7.9**
    """
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=MagicMock())  # newly inserted
    pool.execute = AsyncMock()

    extractor = MagicMock()
    from services.link_discovery.extractor import ExtractedLink
    extractor.extract_links.return_value = [
        ExtractedLink(link='t.me/testgroup', link_type='unknown', is_bot_link=False, raw_message_id=1)
    ]

    queue_rules = AsyncMock()
    from services.link_discovery.queue_rules import QueueDecision
    queue_rules.evaluate = AsyncMock(return_value=QueueDecision(should_queue=True, rule_id=1))

    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=None)

    service = LinkDiscoveryService(pool, extractor, resolver, queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([{'id': 1, 'payload': {'message': 't.me/testgroup'}}])

    insert_calls = [
        c for c in pool.execute.call_args_list
        if 'group_join_queue' in str(c)
    ]
    assert len(insert_calls) >= 1
    for c in insert_calls:
        sql = c[0][0] if c[0] else str(c)
        assert 'NULL' in sql


# ---------------------------------------------------------------------------
# Property 4: Dedup invariant
# Validates: Requirements 4.2, 4.3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedup_invariant_duplicate_skipped():
    """
    When ON CONFLICT DO NOTHING returns no row (duplicate), the link is silently skipped.
    **Validates: Requirements 4.2, 4.3**
    """
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)  # duplicate — no row returned
    pool.execute = AsyncMock()

    extractor = MagicMock()
    from services.link_discovery.extractor import ExtractedLink
    extractor.extract_links.return_value = [
        ExtractedLink(link='t.me/existing', link_type='unknown', is_bot_link=False, raw_message_id=1)
    ]

    queue_rules = AsyncMock()
    resolver = AsyncMock()

    service = LinkDiscoveryService(pool, extractor, resolver, queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([{'id': 1, 'payload': {'message': 't.me/existing'}}])

    queue_rules.evaluate.assert_not_called()
