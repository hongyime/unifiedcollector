"""Self-bot circular-loop filter tests for the Telegram collector.

Two layers, both exercised here:

  * Layer 1 — skip when message.sender_id equals the realtime-feed bot's
    user_id (from UC_NOTIFY_BOT_USER_ID, or resolved via /getMe against
    NOTIFY_TELEGRAM_BOT_TOKEN and cached).
  * Layer 2 — skip when chat_id equals TELEGRAM_LOGS_CHAT_ID only when the bot
    user_id is unknown.

When the bot user_id is known, human messages in the logs chat are allowed and
only bot-authored messages are skipped. If the bot user_id cannot be resolved,
Layer 2 falls back to skipping the whole logs chat to avoid a circular loop.

Pure unit — no Telethon or HTTP is actually invoked; the getMe resolver's
aiohttp session is monkeypatched.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import telegram as tg_mod
from src.collectors.telegram import TelegramCollector, _parse_optional_int_env


# ── shared test helpers ───────────────────────────────────────────────────


def _make_collector(monkeypatch, **env):
    """Build a barebones TelegramCollector with env preset. Bypasses the
    account loader (empty pool is fine for these tests — we don't hit the
    handler dispatch path)."""
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("PROXIMITY_CACHE_ENABLED", "false")
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "0")
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    coll = TelegramCollector()
    coll.account_pool._accounts = []
    return coll


def _fake_event(*, chat_id, sender_id, msg_id=42, via_bot_id=None):
    """Minimal Telethon-shaped event: has chat_id and message with id/sender_id."""
    message = SimpleNamespace(
        id=msg_id, sender_id=sender_id, via_bot_id=via_bot_id, media=None,
    )
    return SimpleNamespace(
        chat_id=chat_id,
        message=message,
        get_sender=AsyncMock(return_value=None),
    )


# ── module helper: _parse_optional_int_env ────────────────────────────────


def test_parse_optional_int_env_reads_env(monkeypatch):
    monkeypatch.setenv("_TEST_UC_INT", "-100123456789")
    assert _parse_optional_int_env("_TEST_UC_INT", None) == -100123456789


def test_parse_optional_int_env_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("_TEST_UC_INT", raising=False)
    assert _parse_optional_int_env("_TEST_UC_INT", "42") == 42
    assert _parse_optional_int_env("_TEST_UC_INT", None) is None


def test_parse_optional_int_env_rejects_garbage(monkeypatch):
    monkeypatch.setenv("_TEST_UC_INT", "not-a-number")
    assert _parse_optional_int_env("_TEST_UC_INT", None) is None


def test_parse_optional_int_env_empty_string_uses_default(monkeypatch):
    monkeypatch.setenv("_TEST_UC_INT", "   ")
    assert _parse_optional_int_env("_TEST_UC_INT", "-1003849817923") == -1003849817923


# ── construction: env → collector fields ──────────────────────────────────


def test_collector_reads_logs_chat_id_from_explicit_env(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID="-1003849817923",
        TELEGRAM_CHAT_ID="-1000",
        NOTIFY_TELEGRAM_CHAT_ID="-999",
    )
    assert coll._logs_chat_id == -1003849817923


def test_collector_falls_back_to_telegram_chat_id(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID="-1003849817923",
    )
    assert coll._logs_chat_id == -1003849817923


def test_collector_falls_back_to_notify_telegram_chat_id(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID="-1003849817923",
    )
    assert coll._logs_chat_id == -1003849817923


def test_collector_logs_chat_id_none_when_all_env_unset(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
    )
    assert coll._logs_chat_id is None


def test_collector_reads_notify_bot_user_id_from_env(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    assert coll._notify_bot_user_id == 8953242118


# ── predicate: _should_skip_self_bot_message ─────────────────────────────


def test_layer2_fallback_skips_logs_chat_when_bot_unknown(monkeypatch):
    coll = _make_collector(monkeypatch, TELEGRAM_LOGS_CHAT_ID="-1003849817923")
    msg = SimpleNamespace(id=99, sender_id=12345)  # unrelated sender
    assert coll._should_skip_self_bot_message(-1003849817923, msg) == "logs_chat_unresolved_bot"


def test_layer2_allows_human_logs_chat_when_bot_known(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID="-1003849817923",
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    msg = SimpleNamespace(id=99, sender_id=12345)  # unrelated sender
    assert coll._should_skip_self_bot_message(-1003849817923, msg) is None


def test_layer2_does_not_skip_unrelated_chat(monkeypatch):
    coll = _make_collector(monkeypatch, TELEGRAM_LOGS_CHAT_ID="-1003849817923")
    msg = SimpleNamespace(id=99, sender_id=12345)
    assert coll._should_skip_self_bot_message(-1000000000001, msg) is None


def test_layer1_skips_by_sender_id_even_in_other_chat(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    assert coll._logs_chat_id is None
    msg = SimpleNamespace(id=99, sender_id=8953242118)
    assert coll._should_skip_self_bot_message(-1000000000001, msg) == "notify_bot"


def test_layer1_ignores_non_bot_sender(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    msg = SimpleNamespace(id=99, sender_id=42)  # a real user
    assert coll._should_skip_self_bot_message(-1000000000001, msg) is None


def test_predicate_no_filter_when_all_unset(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        UC_NOTIFY_BOT_USER_ID=None,
    )
    msg = SimpleNamespace(id=99, sender_id=42)
    assert coll._should_skip_self_bot_message(-42, msg) is None


# ── integration: _on_new_message honours the skip ────────────────────────


@pytest.mark.asyncio
async def test_on_new_message_layer2_fallback_skips_write(monkeypatch):
    coll = _make_collector(monkeypatch, TELEGRAM_LOGS_CHAT_ID="-1003849817923")
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    worker = MagicMock()

    event = _fake_event(chat_id=-1003849817923, sender_id=555)
    await coll._on_new_message(worker, event)

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_new_message_layer1_skips_write(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    worker = MagicMock()

    event = _fake_event(chat_id=-42, sender_id=8953242118)
    await coll._on_new_message(worker, event)

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_new_message_allows_normal_message(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID="-1003849817923",
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    coll._upsert_user_full = AsyncMock()
    worker = MagicMock()

    event = _fake_event(chat_id=-1001234567890, sender_id=99999)  # unrelated
    await coll._on_new_message(worker, event)

    write.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_new_message_allows_human_logs_chat_when_bot_known(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID="-1003849817923",
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    coll._upsert_user_full = AsyncMock()
    worker = MagicMock()

    event = _fake_event(chat_id=-1003849817923, sender_id=555)
    await coll._on_new_message(worker, event)

    write.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_message_edited_layer2_fallback_skips_write(monkeypatch):
    coll = _make_collector(monkeypatch, TELEGRAM_LOGS_CHAT_ID="-1003849817923")
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    worker = MagicMock()

    event = _fake_event(chat_id=-1003849817923, sender_id=555)
    await coll._on_message_edited(worker, event)

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_edited_layer1_skips_write(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    worker = MagicMock()

    event = _fake_event(chat_id=-42, sender_id=8953242118)
    await coll._on_message_edited(worker, event)

    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_edited_allows_human_logs_chat_when_bot_known(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        TELEGRAM_LOGS_CHAT_ID="-1003849817923",
        UC_NOTIFY_BOT_USER_ID="8953242118",
    )
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    coll._upsert_user_full = AsyncMock()
    worker = MagicMock()

    event = _fake_event(chat_id=-1003849817923, sender_id=555)
    await coll._on_message_edited(worker, event)

    write.assert_awaited_once()


# ── /getMe fallback resolves + caches ───────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_notify_bot_user_id_uses_env_when_set(monkeypatch):
    coll = _make_collector(monkeypatch, UC_NOTIFY_BOT_USER_ID="8953242118")
    # No aiohttp import, no HTTP: the env value is enough.
    assert await coll._ensure_notify_bot_user_id() == 8953242118


@pytest.mark.asyncio
async def test_ensure_notify_bot_user_id_falls_back_to_getme(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        NOTIFY_TELEGRAM_BOT_TOKEN="fake:token",
    )

    class _FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def json(self):
            return {
                "ok": True,
                "result": {"id": 8953242118, "is_bot": True, "username": "uctest_bot"},
            }

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url):
            _FakeResponse.captured_url = url
            return _FakeResponse()

    fake_aiohttp = SimpleNamespace(
        ClientSession=_FakeSession,
        ClientTimeout=lambda **kw: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake_aiohttp)

    uid = await coll._ensure_notify_bot_user_id()
    assert uid == 8953242118
    # Cache: second call must not re-fetch.
    coll._notify_bot_resolve_attempted = False  # reset attempt gate to be safe
    uid2 = await coll._ensure_notify_bot_user_id()
    assert uid2 == 8953242118


@pytest.mark.asyncio
async def test_ensure_notify_bot_user_id_returns_none_without_token(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        NOTIFY_TELEGRAM_BOT_TOKEN=None,
    )
    assert await coll._ensure_notify_bot_user_id() is None


@pytest.mark.asyncio
async def test_ensure_notify_bot_user_id_survives_getme_failure(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        NOTIFY_TELEGRAM_BOT_TOKEN="fake:token",
    )

    class _BoomSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def get(self, url):
            raise RuntimeError("network down")

    fake_aiohttp = SimpleNamespace(
        ClientSession=_BoomSession,
        ClientTimeout=lambda **kw: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "aiohttp", fake_aiohttp)

    # Must not raise — degrades to Layer 2 only.
    assert await coll._ensure_notify_bot_user_id() is None
    # Second call must not re-attempt (gate flipped).
    assert coll._notify_bot_resolve_attempted is True
    assert await coll._ensure_notify_bot_user_id() is None



# ── SHIP #2: COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS + via_bot_id checks ────


def test_ignore_sender_ids_default_seeds_notify_bot(monkeypatch):
    """Default env value must include the known notify bot id 8953242118 so a
    fresh deploy is safe even before /getMe resolution completes."""
    monkeypatch.delenv("COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS", raising=False)
    coll = _make_collector(monkeypatch, UC_NOTIFY_BOT_USER_ID=None)
    assert 8953242118 in coll._ignore_sender_ids


def test_ignore_sender_ids_env_override_parses_list(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="111, 222; 333 444",
    )
    assert coll._ignore_sender_ids == frozenset({111, 222, 333, 444})


def test_ignore_sender_ids_env_override_replaces_default(monkeypatch):
    """Explicit override wins over the default — no implicit merge with 8953242118."""
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="12345",
    )
    assert coll._ignore_sender_ids == frozenset({12345})


def test_ignore_sender_ids_ignores_garbage_tokens(monkeypatch):
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="111,not-int,222",
    )
    assert coll._ignore_sender_ids == frozenset({111, 222})


def test_skip_by_ignore_sender_id_default_notify_bot(monkeypatch):
    """Default seeded id 8953242118 skips even when UC_NOTIFY_BOT_USER_ID is unset
    (Layer 1 bot_uid resolution not yet done — defence-in-depth)."""
    monkeypatch.delenv("COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS", raising=False)
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
    )
    msg = SimpleNamespace(id=1, sender_id=8953242118, via_bot_id=None)
    assert coll._should_skip_self_bot_message(-42, msg) == "ignore_sender_id"


def test_skip_by_via_bot_id(monkeypatch):
    """Message routed via_bot through our notify bot must also be skipped."""
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="8953242118",
    )
    msg = SimpleNamespace(id=1, sender_id=99, via_bot_id=8953242118)
    assert coll._should_skip_self_bot_message(-42, msg) == "ignore_via_bot_id"


def test_no_skip_for_normal_sender_when_ignore_set(monkeypatch):
    """Normal user messages must still be ingested even when the ignore set is populated."""
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="8953242118",
    )
    msg = SimpleNamespace(id=1, sender_id=424242, via_bot_id=None)
    assert coll._should_skip_self_bot_message(-42, msg) is None


def test_ignore_sender_id_tolerates_bad_message_fields(monkeypatch):
    """A message missing via_bot_id/sender_id must not raise (Telethon may omit them)."""
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="8953242118",
    )
    msg = SimpleNamespace(id=1)  # no sender_id, no via_bot_id
    assert coll._should_skip_self_bot_message(-42, msg) is None


@pytest.mark.asyncio
async def test_on_new_message_skips_notify_bot_via_ignore_set_only(monkeypatch):
    """End-to-end: with only COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS set (bot_uid
    unresolved, no logs chat), a message from the notify bot must NOT be
    persisted while a normal-sender message IS persisted."""
    coll = _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        COLLECTOR_TELEGRAM_IGNORE_SENDER_IDS="8953242118",
    )
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    coll._upsert_user_full = AsyncMock()
    worker = MagicMock()

    # Notify bot: MUST be skipped.
    bot_event = _fake_event(chat_id=-1003849817923, sender_id=8953242118)
    await coll._on_new_message(worker, bot_event)
    write.assert_not_awaited()

    # Normal user: MUST be ingested.
    human_event = _fake_event(chat_id=-1003849817923, sender_id=424242)
    await coll._on_new_message(worker, human_event)
    write.assert_awaited_once()


# ── hub-group exclusion (TELEGRAM_HUB_GROUP_ID) ─────────────────────
# Our own collector accounts sit in "The Prawn Collector" (bare id 5233855517).
# Ingesting it loops our own data back in, so it must be discarded regardless of
# sender, in both the new-message and edited-message paths.


def _hub_collector(monkeypatch, hub_id):
    return _make_collector(
        monkeypatch,
        UC_NOTIFY_BOT_USER_ID=None,
        TELEGRAM_LOGS_CHAT_ID=None,
        TELEGRAM_CHAT_ID=None,
        NOTIFY_TELEGRAM_CHAT_ID=None,
        TELEGRAM_HUB_GROUP_ID=hub_id,
    )


def test_hub_group_skipped_basic_group_form(monkeypatch):
    coll = _hub_collector(monkeypatch, "5233855517")
    msg = SimpleNamespace(id=1, sender_id=424242, via_bot_id=None)
    # basic group: event.chat_id == -bare
    assert coll._should_skip_self_bot_message(-5233855517, msg) == "hub_group"


def test_hub_group_skipped_supergroup_form(monkeypatch):
    coll = _hub_collector(monkeypatch, "5233855517")
    msg = SimpleNamespace(id=1, sender_id=424242, via_bot_id=None)
    # if migrated to a supergroup: event.chat_id == -100<bare>
    assert coll._should_skip_self_bot_message(-1005233855517, msg) == "hub_group"


def test_hub_group_accepts_marked_env_value(monkeypatch):
    coll = _hub_collector(monkeypatch, "-1005233855517")
    msg = SimpleNamespace(id=1, sender_id=424242, via_bot_id=None)
    assert coll._should_skip_self_bot_message(-5233855517, msg) == "hub_group"
    assert coll._should_skip_self_bot_message(-1005233855517, msg) == "hub_group"


def test_hub_group_other_chat_not_skipped(monkeypatch):
    coll = _hub_collector(monkeypatch, "5233855517")
    msg = SimpleNamespace(id=1, sender_id=424242, via_bot_id=None)
    assert coll._should_skip_self_bot_message(-999, msg) is None


def test_hub_group_unset_no_skip(monkeypatch):
    coll = _hub_collector(monkeypatch, None)
    msg = SimpleNamespace(id=1, sender_id=424242, via_bot_id=None)
    assert coll._should_skip_self_bot_message(-5233855517, msg) is None


@pytest.mark.asyncio
async def test_on_new_message_skips_hub_group(monkeypatch):
    coll = _hub_collector(monkeypatch, "5233855517")
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    coll._upsert_user_full = AsyncMock()
    worker = MagicMock()
    event = _fake_event(chat_id=-5233855517, sender_id=424242)
    await coll._on_new_message(worker, event)
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_edited_skips_hub_group(monkeypatch):
    coll = _hub_collector(monkeypatch, "5233855517")
    write = AsyncMock()
    coll._write_realtime_message_with_retry = write
    coll._upsert_user_full = AsyncMock()
    worker = MagicMock()
    event = _fake_event(chat_id=-5233855517, sender_id=424242)
    await coll._on_message_edited(worker, event)
    write.assert_not_awaited()