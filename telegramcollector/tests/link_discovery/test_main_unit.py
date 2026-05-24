"""
Unit tests for services/link_discovery/main.py

Feature: link-discovery-service
Requirements: 1.1–1.6, 3.3, 4.1–4.4, 5.5, 5.6, 7.9, 7.10
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services.link_discovery.main import LinkDiscoveryService
from services.link_discovery.extractor import ExtractedLink
from services.link_discovery.queue_rules import QueueDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service(pool=None, extractor=None, resolver=None, queue_rules=None):
    pool = pool or AsyncMock()
    extractor = extractor or MagicMock()
    resolver = resolver or AsyncMock()
    queue_rules = queue_rules or AsyncMock()
    return LinkDiscoveryService(pool, extractor, resolver, queue_rules)


# ---------------------------------------------------------------------------
# Cursor tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_init_cursor_returns_zero_when_no_row():
    """Cursor initialised to 0 when no existing row (pool returns 0)."""
    pool = AsyncMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={'last_message_id': 0})

    service = make_service(pool=pool)
    result = await service._init_cursor()

    assert result == 0
    assert service._cursor == 0


@pytest.mark.asyncio
async def test_init_cursor_returns_existing_value():
    """Cursor reads existing value from DB."""
    pool = AsyncMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={'last_message_id': 42})

    service = make_service(pool=pool)
    result = await service._init_cursor()

    assert result == 42
    assert service._cursor == 42


@pytest.mark.asyncio
async def test_advance_cursor_updates_state():
    """Cursor advances to max(batch_ids) after batch."""
    pool = AsyncMock()
    pool.execute = AsyncMock()

    service = make_service(pool=pool)
    service._cursor = 10

    await service._advance_cursor(99)

    assert service._cursor == 99
    pool.execute.assert_called_once()
    call_args = pool.execute.call_args[0]
    assert 99 in call_args


# ---------------------------------------------------------------------------
# Bot link filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bot_links_not_inserted():
    """Bot links are not inserted into discovered_links."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock()
    pool.execute = AsyncMock()

    extractor = MagicMock()
    extractor.extract_links.return_value = [
        ExtractedLink(link='t.me/mybot', link_type='unknown', is_bot_link=True, raw_message_id=1)
    ]

    queue_rules = AsyncMock()
    service = make_service(pool=pool, extractor=extractor, queue_rules=queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([{'id': 1, 'payload': {'message': 't.me/mybot'}}])

    pool.fetchrow.assert_not_called()
    queue_rules.evaluate.assert_not_called()


# ---------------------------------------------------------------------------
# Duplicate link handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_link_silently_skipped():
    """Duplicate links (ON CONFLICT returns None) are silently skipped — no error raised."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)  # conflict — no row returned
    pool.execute = AsyncMock()

    extractor = MagicMock()
    extractor.extract_links.return_value = [
        ExtractedLink(link='t.me/existing', link_type='unknown', is_bot_link=False, raw_message_id=1)
    ]

    queue_rules = AsyncMock()
    service = make_service(pool=pool, extractor=extractor, queue_rules=queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([{'id': 1, 'payload': {'message': 't.me/existing'}}])

    queue_rules.evaluate.assert_not_called()


# ---------------------------------------------------------------------------
# LINK_DISCOVERY_RESOLVE_METADATA=False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_metadata_false_no_resolver_calls():
    """When LINK_DISCOVERY_RESOLVE_METADATA=False, resolver is never called."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=MagicMock())  # newly inserted
    pool.execute = AsyncMock()

    extractor = MagicMock()
    extractor.extract_links.return_value = [
        ExtractedLink(link='t.me/somegroup', link_type='unknown', is_bot_link=False, raw_message_id=1)
    ]

    resolver = AsyncMock()
    queue_rules = AsyncMock()
    queue_rules.evaluate = AsyncMock(return_value=QueueDecision(should_queue=False, rule_id=None))

    service = make_service(pool=pool, extractor=extractor, resolver=resolver, queue_rules=queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([{'id': 1, 'payload': {'message': 't.me/somegroup'}}])

    resolver.resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Per-link error does not abort batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_per_link_error_does_not_abort_batch():
    """An error on one link does not prevent other links in the batch from being processed."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(side_effect=[Exception("DB error"), MagicMock()])
    pool.execute = AsyncMock()

    extractor = MagicMock()
    extractor.extract_links.side_effect = [
        [ExtractedLink(link='t.me/bad', link_type='unknown', is_bot_link=False, raw_message_id=1)],
        [ExtractedLink(link='t.me/good', link_type='unknown', is_bot_link=False, raw_message_id=2)],
    ]

    queue_rules = AsyncMock()
    queue_rules.evaluate = AsyncMock(return_value=QueueDecision(should_queue=False, rule_id=None))

    service = make_service(pool=pool, extractor=extractor, queue_rules=queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([
            {'id': 1, 'payload': {'message': 't.me/bad'}},
            {'id': 2, 'payload': {'message': 't.me/good'}},
        ])

    assert pool.fetchrow.call_count == 2


# ---------------------------------------------------------------------------
# Queue insert uses account_id=NULL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_insert_uses_null_account_id():
    """Auto-queued rows always have account_id=NULL in the INSERT SQL."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=MagicMock())  # newly inserted
    pool.execute = AsyncMock()

    extractor = MagicMock()
    extractor.extract_links.return_value = [
        ExtractedLink(link='t.me/targetgroup', link_type='unknown', is_bot_link=False, raw_message_id=5)
    ]

    queue_rules = AsyncMock()
    queue_rules.evaluate = AsyncMock(return_value=QueueDecision(should_queue=True, rule_id=1))

    service = make_service(pool=pool, extractor=extractor, queue_rules=queue_rules)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([{'id': 5, 'payload': {'message': 't.me/targetgroup'}}])

    insert_calls = [c for c in pool.execute.call_args_list if 'group_join_queue' in str(c)]
    assert len(insert_calls) >= 1
    for c in insert_calls:
        assert 'NULL' in c[0][0]


# ---------------------------------------------------------------------------
# Payload as JSON string
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payload_as_json_string_parsed():
    """Payload given as a JSON string is parsed correctly."""
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    pool.execute = AsyncMock()

    extractor = MagicMock()
    extractor.extract_links.return_value = []

    service = make_service(pool=pool, extractor=extractor)

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_RESOLVE_METADATA = False
        await service._process_batch([
            {'id': 1, 'payload': json.dumps({'message': 'hello t.me/group1'})}
        ])

    extractor.extract_links.assert_called_once_with('hello t.me/group1')


# ---------------------------------------------------------------------------
# stop() is idempotent
# ---------------------------------------------------------------------------

def test_stop_is_idempotent():
    """stop() can be called multiple times without error."""
    service = make_service()
    service._running = True
    service.stop()
    assert service._running is False
    service.stop()
    assert service._running is False


# ---------------------------------------------------------------------------
# LINK_DISCOVERY_PROCESSING_ENABLED=False skips DB reads
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_processing_disabled_skips_db_reads():
    """When LINK_DISCOVERY_PROCESSING_ENABLED=False, no DB fetch is performed."""
    pool = AsyncMock()
    pool.execute = AsyncMock()
    pool.fetchrow = AsyncMock()
    pool.fetch = AsyncMock()

    service = make_service(pool=pool)
    service._running = True

    call_count = 0

    async def fake_sleep(n):
        nonlocal call_count
        call_count += 1
        service.stop()

    with patch('shared.config.settings') as mock_settings:
        mock_settings.LINK_DISCOVERY_BATCH_SIZE = 100
        mock_settings.LINK_DISCOVERY_POLL_INTERVAL = 5
        with patch('shared.config.get_dynamic_setting', return_value=False):
            with patch('asyncio.sleep', side_effect=fake_sleep):
                await service.start()

    pool.fetch.assert_not_called()
    assert call_count >= 1
