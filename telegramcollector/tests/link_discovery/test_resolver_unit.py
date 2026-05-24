"""
Unit tests for services/link_discovery/resolver.py

Requirements: 5.1, 5.2, 5.3, 5.4, 3.2
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.link_discovery.resolver import Resolver, ResolvedMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_resolver(rate_limit: int = 60) -> Resolver:
    """Create a Resolver without a real DB pool."""
    pool = MagicMock()
    return Resolver(
        db_pool=pool,
        tg_api_id=12345,
        tg_api_hash="testhash",
        rate_limit_per_minute=rate_limit,
    )


def _inject_accounts(resolver: Resolver, accounts: list[dict]) -> None:
    """Pre-populate the account pool to skip DB calls."""
    resolver._accounts = accounts


# ---------------------------------------------------------------------------
# ResolvedMetadata dataclass
# ---------------------------------------------------------------------------

def test_resolved_metadata_fields():
    m = ResolvedMetadata(chat_title="Test", member_count=100, link_type="group", is_bot=False)
    assert m.chat_title == "Test"
    assert m.member_count == 100
    assert m.link_type == "group"
    assert m.is_bot is False


def test_resolved_metadata_none_fields():
    m = ResolvedMetadata(chat_title=None, member_count=None, link_type="unknown", is_bot=False)
    assert m.chat_title is None
    assert m.member_count is None


# ---------------------------------------------------------------------------
# _pick_account — round-robin and RuntimeError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pick_account_round_robin():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}, {"id": 2, "phone_number": "+2"}])
    a1 = await resolver._pick_account()
    a2 = await resolver._pick_account()
    a3 = await resolver._pick_account()
    assert a1["id"] == 1
    assert a2["id"] == 2
    assert a3["id"] == 1  # wraps around


@pytest.mark.asyncio
async def test_pick_account_refreshes_when_empty():
    resolver = make_resolver()
    # Pool starts empty; _refresh_accounts should be called
    resolver._pool.fetch = AsyncMock(return_value=[{"id": 5, "phone_number": "+5"}])
    account = await resolver._pick_account()
    assert account["id"] == 5
    resolver._pool.fetch.assert_called_once()


@pytest.mark.asyncio
async def test_pick_account_raises_when_no_active_accounts():
    resolver = make_resolver()
    resolver._pool.fetch = AsyncMock(return_value=[])
    with pytest.raises(RuntimeError, match="No active Telegram accounts"):
        await resolver._pick_account()


# ---------------------------------------------------------------------------
# _enforce_rate_limit — sliding window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_does_not_sleep_when_under_limit():
    resolver = make_resolver(rate_limit=5)
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        for _ in range(5):
            await resolver._enforce_rate_limit()
        mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_sleeps_when_at_limit():
    resolver = make_resolver(rate_limit=3)
    # Pre-fill timestamps so we're at the limit (all within the last 60s)
    now = time.monotonic()
    resolver._call_timestamps = deque([now - 10, now - 5, now - 1])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await resolver._enforce_rate_limit()
        mock_sleep.assert_called_once()
        wait = mock_sleep.call_args[0][0]
        assert wait > 0


@pytest.mark.asyncio
async def test_rate_limit_purges_old_timestamps():
    resolver = make_resolver(rate_limit=2)
    # Add timestamps older than 60 seconds
    now = time.monotonic()
    resolver._call_timestamps = deque([now - 120, now - 90])

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await resolver._enforce_rate_limit()
        # Old entries purged, so no sleep needed
        mock_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_appends_timestamp():
    resolver = make_resolver(rate_limit=10)
    before = len(resolver._call_timestamps)
    await resolver._enforce_rate_limit()
    assert len(resolver._call_timestamps) == before + 1


# ---------------------------------------------------------------------------
# resolve — entity type mapping
# ---------------------------------------------------------------------------

def _make_channel(megagroup: bool, title: str = "Test Channel") -> MagicMock:
    entity = MagicMock(spec=['megagroup', 'title'])
    entity.megagroup = megagroup
    entity.title = title
    return entity


def _make_user(bot: bool, first_name: str = "TestUser") -> MagicMock:
    entity = MagicMock(spec=['bot', 'first_name'])
    entity.bot = bot
    entity.first_name = first_name
    return entity


def _make_participants_result(total: int) -> MagicMock:
    result = MagicMock()
    result.total = total
    return result


@pytest.mark.asyncio
async def test_resolve_megagroup_channel_returns_group_type():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    entity = _make_channel(megagroup=True, title="My Group")
    participants = _make_participants_result(500)

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(return_value=entity)
        client_instance.get_participants = AsyncMock(return_value=participants)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolver.resolve("t.me/mygroup")

    assert result is not None
    assert result.link_type == "group"
    assert result.is_bot is False
    assert result.chat_title == "My Group"
    assert result.member_count == 500


@pytest.mark.asyncio
async def test_resolve_broadcast_channel_returns_channel_type():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    entity = _make_channel(megagroup=False, title="News Channel")
    participants = _make_participants_result(1000)

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(return_value=entity)
        client_instance.get_participants = AsyncMock(return_value=participants)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolver.resolve("t.me/newschannel")

    assert result is not None
    assert result.link_type == "channel"
    assert result.is_bot is False
    assert result.chat_title == "News Channel"


@pytest.mark.asyncio
async def test_resolve_bot_user_returns_bot_type_and_is_bot_true():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    entity = _make_user(bot=True, first_name="MyBot")

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(return_value=entity)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolver.resolve("t.me/mybot")

    assert result is not None
    assert result.link_type == "bot"
    assert result.is_bot is True
    assert result.chat_title == "MyBot"
    assert result.member_count is None


@pytest.mark.asyncio
async def test_resolve_regular_user_returns_user_type():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    entity = _make_user(bot=False, first_name="Alice")

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(return_value=entity)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolver.resolve("t.me/alice")

    assert result is not None
    assert result.link_type == "user"
    assert result.is_bot is False


# ---------------------------------------------------------------------------
# resolve — error handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_flood_wait_error_returns_none_and_logs_warning(caplog):
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    # Create a real exception with a seconds attribute
    class _FakeFloodWait(Exception):
        seconds = 30
    flood_error = _FakeFloodWait()

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(side_effect=flood_error)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        import logging
        with caplog.at_level(logging.WARNING, logger="services.link_discovery.resolver"):
            result = await resolver.resolve("t.me/somegroup")

    assert result is None
    assert any("FloodWaitError" in r.message or "Error resolving" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolve_username_not_occupied_returns_none():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    class _FakeUsernameNotOccupied(Exception):
        pass

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(side_effect=_FakeUsernameNotOccupied())
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolver.resolve("t.me/doesnotexist")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_generic_exception_returns_none_and_logs_warning(caplog):
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(side_effect=ValueError("unexpected"))
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        import logging
        with caplog.at_level(logging.WARNING, logger="services.link_discovery.resolver"):
            result = await resolver.resolve("t.me/somegroup")

    assert result is None
    assert any("Error resolving" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_resolve_member_count_none_when_get_participants_fails():
    resolver = make_resolver()
    _inject_accounts(resolver, [{"id": 1, "phone_number": "+1"}])

    entity = _make_channel(megagroup=True, title="Group")

    with patch("telethon.TelegramClient") as MockClient:
        client_instance = AsyncMock()
        client_instance.get_entity = AsyncMock(return_value=entity)
        client_instance.get_participants = AsyncMock(side_effect=Exception("forbidden"))
        MockClient.return_value.__aenter__ = AsyncMock(return_value=client_instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await resolver.resolve("t.me/group")

    assert result is not None
    assert result.member_count is None
