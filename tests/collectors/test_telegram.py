"""Tests for src/collectors/telegram.py — Wave 2 Batch E.

Pure-unit. Telethon is never imported live; every TelegramClient is a
MagicMock/AsyncMock stand-in. Exercises:

  • module helpers (_tg_json, _ext_from_mime, _is_flood_wait, SessionState)
  • TelegramCollector construction + feature gates
  • _dispatch hashing (deterministic + balanced)
  • collect() bail paths (missing api creds, no workers)
  • _process_spider_queue claim-and-mark loop
  • _handle_flood_wait state transitions
  • backfill_chat happy path + FloodWait path + bail path
  • collect_dialogs aggregation/dedup
  • collect_chat_members role classification
  • collect_user_profile w/ broken UserChangeTracker (must not crash)
  • download_message_media routing for photo/document/generic
  • cleanup() teardown sequence
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import telegram as tg_mod
from src.collectors.telegram import (
    EntityResolveDeferred,
    EntityUnresolvable,
    SessionState,
    TelegramCollector,
    TelegramWorker,
    _ext_from_mime,
    _format_exception,
    _is_flood_wait,
    _telegram_message_content_id,
    _tg_json,
)


# ── shared helpers ────────────────────────────────────────────────────────


class _FakeFloodWait(Exception):
    """Mimics telethon.errors.FloodWaitError without importing telethon."""

    def __init__(self, seconds: int = 5):
        super().__init__(f"flood wait {seconds}s")
        self.seconds = seconds

    # __name__ lookup uses type(exc).__name__ — name the class deliberately
    # so _is_flood_wait recognises it.

# Rename the class so type(exc).__name__ == "FloodWaitError".
_FakeFloodWait.__name__ = "FloodWaitError"


class _FakeFileReferenceExpired(Exception):
    """Mimics telethon.errors.FileReferenceExpiredError without importing telethon."""


_FakeFileReferenceExpired.__name__ = "FileReferenceExpiredError"


class _FakePool:
    """Minimal asyncpg-pool stand-in: pool.acquire() returns an async ctx
    manager whose __aenter__ yields a connection mock with execute/fetchrow.

    Smart fetchrow: returns a fake row with an 'id' UUID for platform_chat_id /
    platform_user_id / platform_message_id lookups (so the new UUID-resolution
    code paths in collect_chat_members / capture_reactions / etc. work in
    isolated tests). Tests that need fetchrow=None can overwrite ``conn.fetchrow``
    after construction.
    """

    def __init__(self):
        import uuid as _uuid
        self.conn = MagicMock()
        self.conn.execute = AsyncMock()
        self.conn.fetchval = AsyncMock(return_value=True)

        async def _smart_fetchrow(sql, *args, **kwargs):
            # Default behaviour: return a fake-id row for the common lookup
            # patterns the collector uses post-Phase-1.
            sql_lc = sql.lower() if isinstance(sql, str) else ""
            if (
                "from telegram_chats where platform_chat_id" in sql_lc
                or "from telegram_users where platform_user_id" in sql_lc
                or "from telegram_messages where platform_message_id" in sql_lc
            ):
                return {"id": _uuid.uuid4()}
            # INSERT … RETURNING id (used by _upsert_message)
            if "returning id" in sql_lc:
                return {"id": _uuid.uuid4()}
            return None

        self.conn.fetchrow = AsyncMock(side_effect=_smart_fetchrow)
        self.conn.fetch = AsyncMock(return_value=[])
        self.conn.executemany = AsyncMock()

    def acquire(self):
        conn = self.conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


def _aiter(seq):
    """Build an async iterator over `seq`."""

    async def _gen():
        for x in seq:
            yield x

    return _gen()


def _make_collector(monkeypatch, *, accounts=None, api_id="123", api_hash="x"):
    """Build a TelegramCollector with a fake pool + zero-or-more dummy
    accounts pre-installed, bypassing env-driven account loading."""
    monkeypatch.setenv("TELEGRAM_API_ID", api_id)
    monkeypatch.setenv("TELEGRAM_API_HASH", api_hash)
    monkeypatch.setenv("PROXIMITY_CACHE_ENABLED", "false")
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "0")
    # Avoid noisy disk scans
    monkeypatch.setattr(tg_mod, "logger", tg_mod.logger)  # no-op, kept for symmetry

    coll = TelegramCollector()
    pool = _FakePool()
    # Use set_pool so UserChangeTracker is wired (parity with runtime behaviour).
    coll.set_pool(pool)
    # Plug in the stable account list — bypass env loader
    if accounts is None:
        accounts = []
    coll.account_pool._accounts = list(accounts)
    return coll


def _make_worker(coll, *, name="acct1"):
    acct = SimpleNamespace(
        name=name,
        credentials={"api_id": "1", "api_hash": "h", "session": "s", "phone": "+1"},
    )
    w = TelegramWorker(coll, acct, worker_id=0)
    w.client = MagicMock()
    w.state = SessionState.CONNECTED
    return w


# ── module-level helpers ──────────────────────────────────────────────────


def test_tg_json_handles_bytes_and_datetime():
    assert _tg_json(b"\xde\xad\xbe\xef") == "deadbeef"
    dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert _tg_json(dt) == dt.isoformat()
    # Anything else falls through to str()
    assert _tg_json(SessionState.INIT) == "SessionState.INIT"


def test_ext_from_mime_known_and_unknown():
    assert _ext_from_mime("image/jpeg") == "jpg"
    assert _ext_from_mime("VIDEO/MP4") == "mp4"
    assert _ext_from_mime("application/octet-stream") is None
    assert _ext_from_mime(None) is None
    assert _ext_from_mime("") is None


def test_is_flood_wait_detection():
    assert _is_flood_wait(_FakeFloodWait(10)) is True

    class _Other(Exception):
        seconds = 5

    # name not flood-related and seconds attr alone shouldn't match unless
    # the class name *contains* flood — guard against false positives.
    assert _is_flood_wait(_Other()) is False
    assert _is_flood_wait(RuntimeError("boom")) is False


def test_format_exception_keeps_blank_timeouts_readable():
    assert _format_exception(TimeoutError()) == "TimeoutError"
    assert _format_exception(RuntimeError("boom")) == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_upsert_message_extracts_links(monkeypatch):
    coll = _make_collector(monkeypatch)
    message = SimpleNamespace(
        id=12,
        sender_id=77,
        message="join https://t.me/example and read https://example.com/page.",
        caption=None,
        photo=False,
        video=False,
        voice=False,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        pinned=False,
        reactions=None,
        media=None,
        to_dict=lambda: {"id": 12, "message": "with links"},
    )

    await coll._upsert_message(message, chat_id=55, sender_uuid="sender-uuid")

    link_calls = [
        c for c in coll.pool.conn.execute.await_args_list
        if "INSERT INTO discovered_links" in c.args[0]
    ]
    assert len(link_calls) == 2
    assert {c.args[6] for c in link_calls} == {
        "https://t.me/example",
        "https://example.com/page",
    }
    assert all(c.args[1] == "telegram" for c in link_calls)
    assert all(c.args[3] == "55:12" for c in link_calls)


@pytest.mark.asyncio
async def test_upsert_message_records_mentions(monkeypatch):
    coll = _make_collector(monkeypatch)
    message = SimpleNamespace(
        id=13,
        sender_id=77,
        message="hi @AliceDemo see https://t.me/BobDemo",
        caption=None,
        photo=False,
        video=False,
        voice=False,
        date=datetime(2024, 1, 1, tzinfo=timezone.utc),
        pinned=False,
        reactions=None,
        media=None,
        action=None,
        entities=[],
        caption_entities=[],
        to_dict=lambda: {"id": 13, "message": "with mentions"},
    )

    await coll._upsert_message(message, chat_id=55, sender_uuid="sender-uuid")

    mention_calls = [
        c for c in coll.pool.conn.execute.await_args_list
        if "INSERT INTO telegram_message_mentions" in c.args[0]
    ]
    assert len(mention_calls) == 2
    assert {c.args[3] for c in mention_calls} == {"alicedemo", "bobdemo"}

    queue_calls = [
        c for c in coll.pool.conn.execute.await_args_list
        if "INSERT INTO telegram_spider_queue" in c.args[0]
        and "'mention'" in c.args[0]
    ]
    assert {c.args[1] for c in queue_calls} == {"alicedemo", "bobdemo"}

    edge_calls = [
        c for c in coll.pool.conn.execute.await_args_list
        if "INSERT INTO graph_edges" in c.args[0]
    ]
    assert {c.args[2] for c in edge_calls} == {"alicedemo", "bobdemo"}


def test_extract_message_mentions_from_dict_entities(monkeypatch):
    coll = _make_collector(monkeypatch)
    message = SimpleNamespace(
        message="read this",
        caption=None,
        entities=[
            {
                "_": "MessageEntityTextUrl",
                "offset": 0,
                "length": 4,
                "url": "https://t.me/HiddenDemo",
            },
            {
                "_": "MessageEntityMentionName",
                "offset": 5,
                "length": 4,
                "user_id": 123456,
            },
        ],
        caption_entities=[],
    )

    mentions = coll._extract_message_mentions(message)
    assert any(m["username"] == "hiddendemo" for m in mentions)
    assert any(m["user_id"] == "123456" for m in mentions)


def test_session_state_enum_complete():
    # The enum is the worker FSM contract — nail down its members so
    # downstream callers (state == CONNECTED checks) don't silently break.
    assert SessionState.INIT.value == "init"
    assert SessionState.CONNECTING.value == "connecting"
    assert SessionState.CONNECTED.value == "connected"


@pytest.mark.asyncio
async def test_mark_runtime_healthy_clears_stale_source_health(monkeypatch):
    coll = _make_collector(monkeypatch)

    await coll._mark_runtime_healthy("connected")

    sql = coll.pool.conn.execute.await_args.args[0]
    assert "INSERT INTO source_health" in sql
    assert "last_error=NULL" in sql
    assert "status='running'" in sql


# ── construction + feature gate ───────────────────────────────────────────


def test_constructor_picks_up_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "999")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_BATCH_SIZE", "42")
    monkeypatch.setenv("TELEGRAM_MAX_MEDIA_SIZE_MB", "7")
    monkeypatch.setenv("TELEGRAM_BACKFILL_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_STORY_SCAN_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_GROUP_JOIN_ENABLED", "false")

    coll = TelegramCollector()
    assert coll._api_id == "999"
    assert coll._api_hash == "abc"
    assert coll._batch_size == 42
    assert coll._max_media_size == 7 * 1024 * 1024
    assert coll._backfill_enabled is False
    assert coll._story_enabled is False
    assert coll._group_join_enabled is False
    assert coll.SOURCE_NAME == "telegram"
    assert coll._workers == []
    assert coll._primary_client is None
    assert coll._realtime_running is False


def test_telegram_message_content_id_is_chat_scoped():
    assert _telegram_message_content_id("12345", 99) == "12345_99"


def test_parse_telegram_link_target():
    assert TelegramCollector._parse_telegram_link_target(
        "https://t.me/+AbCdEf123"
    ) == {"kind": "invite", "invite_hash": "AbCdEf123"}
    assert TelegramCollector._parse_telegram_link_target(
        "https://t.me/joinchat/InviteHash"
    ) == {"kind": "invite", "invite_hash": "InviteHash"}
    assert TelegramCollector._parse_telegram_link_target(
        "https://t.me/s/SomeChannel/123"
    ) == {"kind": "public", "username": "somechannel"}
    assert TelegramCollector._parse_telegram_link_target(
        "https://t.me/c/123/456"
    ) is None


@pytest.mark.asyncio
async def test_process_join_queue_times_out_hanging_link(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    coll._workers = [_make_worker(coll)]
    coll._join_links_max_per_cycle = 1
    coll._join_link_timeout = 0.01
    coll.pool.conn.fetch = AsyncMock(
        return_value=[{"id": 1, "url": "https://t.me/+InviteHash"}]
    )

    async def _hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(coll, "_visit_telegram_link", _hang)
    with caplog.at_level("WARNING", logger="src.collectors.telegram"):
        processed = await coll._process_join_queue()

    assert processed == 1
    assert any("telegram link visit timed out" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_process_join_queue_skips_cooling_spider_accounts(monkeypatch):
    coll = _make_collector(monkeypatch)
    coll._workers = [_make_worker(coll)]
    coll._worker_can_resolve = MagicMock(return_value=False)

    processed = await coll._process_join_queue()

    assert processed == 0
    coll.pool.conn.fetch.assert_not_awaited()


def test_spider_allowlist_matches_worker_account(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SPIDER_ACCOUNTS", "acct2, acct3")
    coll = _make_collector(monkeypatch)
    assert coll._is_spider_allowed(_make_worker(coll, name="acct2")) is True
    assert coll._is_spider_allowed(_make_worker(coll, name="acct1")) is False


def test_account_media_dir_isolated_per_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_SESSION", "alice/?weird")
    coll = _make_collector(monkeypatch)
    # Force media_dir into a tmp path so the test never writes into the
    # production drive root.
    monkeypatch.setattr(
        type(coll), "media_dir",
        property(lambda self: tmp_path),
    )
    p = coll.account_media_dir
    assert p.parent == tmp_path
    # sanitized: '/' and '?' shouldn't survive into the directory name
    assert "/" not in p.name
    assert "?" not in p.name
    assert p.is_dir()


# ── _dispatch ─────────────────────────────────────────────────────────────


def test_dispatch_partitions_targets_deterministically(monkeypatch):
    coll = _make_collector(monkeypatch)
    targets = [f"chat-{i}" for i in range(20)]
    a = coll._dispatch(targets, 4)
    b = coll._dispatch(targets, 4)
    assert a == b  # deterministic
    flat = [t for bucket in a for t in bucket]
    assert sorted(flat) == sorted(targets)
    assert len(a) == 4
    # Each target ends up in EXACTLY one bucket — no duplication
    assert len({t for bucket in a for t in bucket}) == len(targets)


def test_dispatch_with_one_worker_keeps_all_targets():
    coll = TelegramCollector.__new__(TelegramCollector)
    out = coll._dispatch(["a", "b", "c"], 1)
    assert out == [["a", "b", "c"]]


# ── collect() bail paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_bails_without_api_credentials(monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    monkeypatch.setenv("TELEGRAM_API_HASH", "")
    coll = TelegramCollector()
    coll.pool = _FakePool()

    with caplog.at_level("ERROR", logger="src.collectors.telegram"):
        out = await coll.collect(["@somechan"])
    assert out is None  # no return value, just an early-out
    assert any("API_ID" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_bails_when_no_workers_connect(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    coll._spawn_workers = AsyncMock(return_value=[])
    with caplog.at_level("ERROR", logger="src.collectors.telegram"):
        await coll.collect(["@a"])
    assert any("No Telegram workers" in r.getMessage() for r in caplog.records)


# ── _handle_flood_wait ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_flood_wait_records_and_sleeps(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll.account_pool.record_flood_wait = MagicMock()
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(tg_mod.asyncio, "sleep", _fake_sleep)

    err = _FakeFloodWait(seconds=12)
    await coll._handle_flood_wait(worker, err)

    coll.account_pool.record_flood_wait.assert_called_once_with(worker.account.name, 12.0)
    event_call = coll.pool.conn.execute.await_args
    assert "INSERT INTO rate_limit_events" in event_call.args[0]
    assert event_call.args[1:7] == (
        "telegram",
        worker.account.name,
        "flood_wait",
        429,
        12,
        "Telegram FloodWaitError",
    )
    metadata = json.loads(event_call.args[7])
    assert metadata["exception"] == "FloodWaitError"
    assert metadata["wait_seconds"] == 12
    assert sleeps == [12]  # min(12, 300) == 12
    assert worker.state == SessionState.CONNECTED


@pytest.mark.asyncio
async def test_handle_flood_wait_caps_sleep_at_300(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll.account_pool.record_flood_wait = MagicMock()
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(tg_mod.asyncio, "sleep", _fake_sleep)
    await coll._handle_flood_wait(worker, _FakeFloodWait(seconds=9_999))
    assert sleeps == [300]
    coll.account_pool.record_flood_wait.assert_called_once_with(worker.account.name, 9999.0)
    event_call = coll.pool.conn.execute.await_args
    assert event_call.args[5] == 9999


@pytest.mark.asyncio
async def test_handle_flood_wait_can_pin_without_sleep(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll.account_pool.record_flood_wait = MagicMock()
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(tg_mod.asyncio, "sleep", _fake_sleep)
    await coll._handle_flood_wait(worker, _FakeFloodWait(seconds=77), sleep=False)

    assert sleeps == []
    assert worker.state == SessionState.FLOOD_WAIT
    coll.account_pool.record_flood_wait.assert_called_once_with(worker.account.name, 77.0)
    event_call = coll.pool.conn.execute.await_args
    assert event_call.args[5] == 77


@pytest.mark.asyncio
async def test_worker_unresolvable_target_does_not_penalize_account(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._collect_chat = AsyncMock(side_effect=EntityUnresolvable("no connected account owns entity 'bad'"))
    coll._mark_collection_target_error = AsyncMock()
    coll.send_to_dlq = AsyncMock()
    coll.account_pool.record_error_classified = MagicMock()
    coll.account_pool.record_success = MagicMock()

    await worker.run_targets(["bad"])

    coll.account_pool.record_error_classified.assert_not_called()
    coll.account_pool.record_success.assert_not_called()
    coll._mark_collection_target_error.assert_awaited_once()
    args = coll._mark_collection_target_error.await_args.args
    assert args[0] == "bad"
    assert args[1] == "unresolvable"
    coll.send_to_dlq.assert_awaited_once()
    assert coll._target_unresolvable_until("bad") > 0


@pytest.mark.asyncio
async def test_resolve_rejects_account_name_target_without_network(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll, name="shotsbyseah234")
    worker.client.get_entity = AsyncMock()
    coll._workers = [worker]

    with pytest.raises(EntityUnresolvable, match="connected Telegram account name"):
        await coll._resolve_entity_any_worker(worker, "shotsbyseah234")

    worker.client.get_entity.assert_not_called()


@pytest.mark.asyncio
async def test_worker_deferred_target_does_not_penalize_account(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._collect_chat = AsyncMock(side_effect=EntityResolveDeferred("account unavailable or in cooldown"))
    coll._mark_collection_target_error = AsyncMock()
    coll.send_to_dlq = AsyncMock()
    coll.account_pool.record_error_classified = MagicMock()
    coll.account_pool.record_success = MagicMock()

    await worker.run_targets(["chat"])

    coll.account_pool.record_error_classified.assert_not_called()
    coll.account_pool.record_success.assert_not_called()
    coll.send_to_dlq.assert_not_called()
    coll._mark_collection_target_error.assert_awaited_once()
    args = coll._mark_collection_target_error.await_args.args
    assert args[0] == "chat"
    assert args[1] == "pending"


@pytest.mark.asyncio
async def test_resolve_sweep_uses_local_session_cache_only_by_default(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    worker.client.session = SimpleNamespace(get_input_entity=MagicMock(side_effect=ValueError("cold cache")))
    worker.client.get_input_entity = AsyncMock()
    coll._workers = [worker]

    verdict = await coll._resolves_on_any_worker("123")

    assert verdict == "transient"
    worker.client.get_input_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_sweep_network_flood_aborts_without_sleep(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RESOLVE_SWEEP_ALLOW_NETWORK", "true")
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    worker.client.session = SimpleNamespace(get_input_entity=MagicMock(side_effect=ValueError("cold cache")))
    worker.client.get_input_entity = AsyncMock(side_effect=_FakeFloodWait(seconds=44))
    coll._handle_flood_wait = AsyncMock()
    coll._workers = [worker]

    verdict = await coll._resolves_on_any_worker("123")

    assert verdict == "rate_limited"
    coll._handle_flood_wait.assert_awaited_once()
    assert coll._handle_flood_wait.await_args.kwargs == {"sleep": False}


@pytest.mark.asyncio
async def test_sweep_resolve_pending_pauses_after_rate_limit(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RESOLVE_SWEEP_ENABLED", "1")
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._workers = [worker]
    coll.pool.conn.fetch = AsyncMock(
        return_value=[
            {"platform_chat_id": "1"},
            {"platform_chat_id": "2"},
        ]
    )
    coll._resolves_on_any_worker = AsyncMock(return_value="rate_limited")

    out = await coll._sweep_resolve_pending()

    assert out == {"checked": 1, "resolvable": 0, "unresolvable": 0, "transient": 1}
    coll._resolves_on_any_worker.assert_awaited_once_with("1")


@pytest.mark.asyncio
async def test_restore_flood_waits_from_events_locks_known_account(monkeypatch):
    coll = _make_collector(monkeypatch)
    coll.account_pool.add_account("acct1", credentials={})
    coll.pool.conn.fetch = AsyncMock(
        return_value=[
            {
                "account": "acct1",
                "cooldown_until": datetime.now(timezone.utc) + timedelta(seconds=60),
            }
        ]
    )

    restored = await coll._restore_flood_waits_from_events()

    assert restored == 1
    assert coll.account_pool.is_available("acct1") is False


# ── _process_spider_queue ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_spider_queue_claims_and_marks_completed(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)

    # First fetchrow returns a job, second returns None to terminate.
    coll.pool.conn.fetchrow.side_effect = [
        {"platform_chat_id": "123", "title": "Sample"},
        None,
    ]
    coll._collect_chat = AsyncMock()

    await coll._process_spider_queue(worker)

    coll._collect_chat.assert_awaited_once_with(worker, "123")
    # 'completed' UPDATE must fire exactly once; failed path NOT taken
    completed_calls = [
        c for c in coll.pool.conn.execute.await_args_list
        if "completed" in c.args[0]
    ]
    assert len(completed_calls) == 1


@pytest.mark.asyncio
async def test_process_spider_queue_skips_when_worker_account_cooling_down(monkeypatch):
    coll = _make_collector(monkeypatch)
    coll.account_pool.add_account("acct1", credentials={})
    coll.account_pool.record_flood_wait("acct1", 60)
    worker = _make_worker(coll, name="acct1")
    coll._collect_chat = AsyncMock()

    processed = await coll._process_spider_queue(worker)

    assert processed == 0
    coll.pool.conn.fetchrow.assert_not_awaited()
    coll._collect_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_spider_queue_marks_failed_on_exception(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll.pool.conn.fetchrow.side_effect = [
        {"platform_chat_id": "999", "title": "x"},
        {"attempts": 5, "status": "failed"},
        None,
    ]
    coll._collect_chat = AsyncMock(side_effect=RuntimeError("boom"))

    await coll._process_spider_queue(worker)

    retry_or_fail_calls = [
        c for c in coll.pool.conn.fetchrow.await_args_list
        if "failed" in c.args[0]
    ]
    assert len(retry_or_fail_calls) == 1


@pytest.mark.asyncio
async def test_process_spider_queue_terminates_on_empty(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll.pool.conn.fetchrow.return_value = None  # empty from the start
    coll._collect_chat = AsyncMock()

    await coll._process_spider_queue(worker)
    coll._collect_chat.assert_not_awaited()


# ── backfill_chat ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_chat_persists_messages_across_pages(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._workers = [worker]
    coll._batch_size = 2

    # Entity for this chat
    entity = SimpleNamespace(id=42, title="Demo", username="demo", broadcast=True)
    worker.client.get_entity = AsyncMock(return_value=entity)

    coll._upsert_chat = AsyncMock()
    coll._upsert_sender = AsyncMock(return_value="sender-uuid")
    coll._upsert_message = AsyncMock()

    # Pages: 2 messages, 2 messages, then 1 (partial → end-of-channel).
    page1 = [SimpleNamespace(id=10, sender_id=1), SimpleNamespace(id=9, sender_id=2)]
    page2 = [SimpleNamespace(id=8, sender_id=3), SimpleNamespace(id=7, sender_id=4)]
    page3 = [SimpleNamespace(id=6, sender_id=5)]
    pages = iter([page1, page2, page3])

    def _iter_messages(*args, **kwargs):
        try:
            batch = next(pages)
        except StopIteration:
            batch = []
        return _aiter(batch)

    worker.client.iter_messages = _iter_messages

    written = await coll.backfill_chat("42", target_depth=None, max_iterations=10, worker=worker)

    assert written == 5
    assert coll._upsert_message.await_count == 5
    coll._upsert_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_chat_respects_target_depth(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._batch_size = 5

    entity = SimpleNamespace(id=7, title="t", username=None, broadcast=False)
    worker.client.get_entity = AsyncMock(return_value=entity)
    coll._upsert_chat = AsyncMock()
    coll._upsert_sender = AsyncMock(return_value=None)
    coll._upsert_message = AsyncMock()

    msgs = [SimpleNamespace(id=100 - i, sender_id=None) for i in range(5)]
    worker.client.iter_messages = lambda *a, **kw: _aiter(msgs)

    written = await coll.backfill_chat("7", target_depth=3, max_iterations=10, worker=worker)
    # target_depth=3 → after the first batch of 5 we have written>=3; loop breaks.
    assert written == 5
    # Only one fetch round before breaking
    assert coll._upsert_message.await_count == 5


@pytest.mark.asyncio
async def test_backfill_chat_handles_flood_wait_then_continues(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._batch_size = 2

    entity = SimpleNamespace(id=1, title=None, username="u", broadcast=False)
    worker.client.get_entity = AsyncMock(return_value=entity)
    coll._upsert_chat = AsyncMock()
    coll._upsert_sender = AsyncMock(return_value=None)
    coll._upsert_message = AsyncMock()
    coll._handle_flood_wait = AsyncMock()

    calls = {"n": 0}

    def _iter_messages(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            async def _boom():
                raise _FakeFloodWait(seconds=2)
                yield  # pragma: no cover  (make it an async generator)

            return _boom()
        # Second call: empty → end-of-channel
        return _aiter([])

    worker.client.iter_messages = _iter_messages
    written = await coll.backfill_chat("1", max_iterations=5, worker=worker)
    assert written == 0
    coll._handle_flood_wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_chat_bails_when_no_workers(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    coll._spawn_workers = AsyncMock(return_value=[])
    with caplog.at_level("ERROR", logger="src.collectors.telegram"):
        out = await coll.backfill_chat("1")
    assert out == 0
    assert any("no Telegram workers" in r.getMessage() for r in caplog.records)


# ── collect_dialogs ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_dialogs_dedupes_across_workers(monkeypatch):
    coll = _make_collector(monkeypatch)
    w1 = _make_worker(coll, name="a")
    w2 = _make_worker(coll, name="b")
    coll._workers = [w1, w2]
    coll._upsert_chat = AsyncMock()

    e1 = SimpleNamespace(id=1, title="One", username=None, broadcast=True, megagroup=False)
    e2 = SimpleNamespace(id=2, title="Two", username=None, broadcast=False, megagroup=True)
    e3 = SimpleNamespace(id=1, title="OneDup", username=None, broadcast=True, megagroup=False)

    w1.client.iter_dialogs = lambda: _aiter([SimpleNamespace(entity=e1), SimpleNamespace(entity=e2)])
    w2.client.iter_dialogs = lambda: _aiter([SimpleNamespace(entity=e3)])

    out = await coll.collect_dialogs()
    cids = {d["platform_chat_id"] for d in out}
    assert cids == {"1", "2"}
    types = {d["platform_chat_id"]: d["type"] for d in out}
    assert types["1"] == "channel"
    assert types["2"] == "supergroup"


@pytest.mark.asyncio
async def test_collect_dialogs_continues_when_one_worker_raises(monkeypatch):
    coll = _make_collector(monkeypatch)
    w1 = _make_worker(coll, name="a")
    w2 = _make_worker(coll, name="b")
    coll._workers = [w1, w2]
    coll._upsert_chat = AsyncMock()

    def _boom():
        async def _g():
            raise RuntimeError("api crash")
            yield  # pragma: no cover

        return _g()

    w1.client.iter_dialogs = _boom
    e2 = SimpleNamespace(id=99, title="GroupOnly", username=None, broadcast=False, megagroup=False)
    w2.client.iter_dialogs = lambda: _aiter([SimpleNamespace(entity=e2)])

    out = await coll.collect_dialogs()
    # Even though w1 blew up, w2's dialog still surfaces.
    assert any(d["platform_chat_id"] == "99" for d in out)


# ── collect_chat_members ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_chat_members_classifies_roles(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._workers = [worker]
    coll._upsert_chat = AsyncMock()
    coll._upsert_user_full = AsyncMock()

    entity = SimpleNamespace(id=55)
    worker.client.get_entity = AsyncMock(return_value=entity)

    # Build participants exposing different `participant` subtype names.
    def _p(uid, type_name):
        sub = type(type_name, (), {"date": None})()
        return SimpleNamespace(id=uid, participant=sub)

    parts = [_p(1, "ChannelParticipantCreator"),
             _p(2, "ChannelParticipantAdmin"),
             _p(3, "ChannelParticipantBanned"),
             _p(4, "ChannelParticipantLeft"),
             _p(5, "ChannelParticipant")]

    worker.client.iter_participants = lambda e: _aiter(parts)
    n = await coll.collect_chat_members("55", worker=worker)
    assert n == 5

    # Roles fed into the upsert SQL — pick out the executed roles.
    roles = [c.args[3] for c in coll.pool.conn.execute.await_args_list
             if c.args and "telegram_chat_members" in c.args[0]]
    assert roles == ["creator", "admin", "banned", "left", "member"]


@pytest.mark.asyncio
async def test_collect_chat_members_handles_flood_wait(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._workers = [worker]
    coll._upsert_chat = AsyncMock()
    coll._handle_flood_wait = AsyncMock()

    entity = SimpleNamespace(id=10)
    worker.client.get_entity = AsyncMock(return_value=entity)

    def _explode(e):
        async def _g():
            raise _FakeFloodWait(seconds=3)
            yield  # pragma: no cover

        return _g()

    worker.client.iter_participants = _explode
    out = await coll.collect_chat_members("10", worker=worker)
    assert out == 0
    coll._handle_flood_wait.assert_awaited_once()


# ── collect_user_profile (UserChangeTracker hook safety) ──────────────────


@pytest.mark.asyncio
async def test_collect_user_profile_survives_tracker_failure(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._workers = [worker]
    coll._upsert_user_full = AsyncMock()

    user = SimpleNamespace(
        id=77, username="bob", first_name="Bob", last_name="X",
        about="hi", premium=False, verified=True, phone="+1",
        photo=SimpleNamespace(photo_id=42), bot=False,
    )
    worker.client.get_entity = AsyncMock(return_value=user)
    worker.client.download_profile_photo = AsyncMock(return_value=None)
    worker.client.get_profile_photos = AsyncMock(return_value=[])

    # Patch UserChangeTracker so detect_and_log raises — the hook MUST NOT
    # be allowed to abort profile ingestion.
    bogus_tracker = MagicMock()
    bogus_tracker.detect_and_log = AsyncMock(side_effect=RuntimeError("schema drift"))
    monkeypatch.setattr(tg_mod, "UserChangeTracker", lambda _pool: bogus_tracker)

    out = await coll.collect_user_profile(77, worker=worker)
    assert out is not None
    assert out["platform_user_id"] == "77"
    assert out["username"] == "bob"
    coll._upsert_user_full.assert_awaited_once()  # primary write still happens
    bogus_tracker.detect_and_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_user_profile_returns_none_on_resolve_failure(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    coll._workers = [worker]

    async def _bad(arg):
        # int(arg) succeeds for both branches → both raise generic Exception
        raise RuntimeError("not found")

    worker.client.get_entity = _bad
    with caplog.at_level("WARNING", logger="src.collectors.telegram"):
        out = await coll.collect_user_profile("abc", worker=worker)
    assert out is None


# ── download_message_media ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_media_writes_vault_blob(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(tg_mod, "VAULT_ROOT", vault_root)
    coll = _make_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    data = b"telegram image bytes"
    digest = hashlib.sha256(data).hexdigest()

    await coll.download_media({
        "entity_id": "12345",
        "entity_name": "Test Chat",
        "content_type": "photo",
        "content_id": "99",
        "data": data,
        "extension": "jpg",
        "raw": {"message": "hello"},
        "chat_username": "testchat",
        "message_id": 99,
    })

    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://t.me/testchat/99"
    assert kwargs["metadata"]["raw"] == {"message": "hello"}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")
    coll.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_dlq_records_exception_type_for_blank_timeout(monkeypatch):
    coll = _make_collector(monkeypatch)
    coll.insert_media_item = AsyncMock()
    coll.send_to_dlq = AsyncMock()
    worker = _make_worker(coll)
    worker.client.download_media = AsyncMock(side_effect=TimeoutError())

    await coll.download_media({
        "entity_id": "12345",
        "entity_name": "Test Chat",
        "content_type": "photo",
        "content_id": "blank-timeout",
        "media": object(),
        "extension": "jpg",
    }, worker=worker)

    coll.insert_media_item.assert_not_awaited()
    coll.send_to_dlq.assert_awaited_once()
    assert coll.send_to_dlq.await_args.args == ("12345", "blank-timeout", "TimeoutError")


@pytest.mark.asyncio
async def test_download_media_refreshes_expired_file_reference(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(tg_mod, "VAULT_ROOT", vault_root)
    coll = _make_collector(monkeypatch)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()
    worker = _make_worker(coll)

    stale_media = object()
    fresh_media = object()
    fresh_message = SimpleNamespace(
        id=99,
        media=SimpleNamespace(photo=fresh_media),
        to_dict=lambda: {"id": 99, "refreshed": True},
    )
    worker.client.get_messages = AsyncMock(return_value=fresh_message)
    worker.client.download_media = AsyncMock(
        side_effect=[_FakeFileReferenceExpired("file reference has expired"), b"fresh bytes"]
    )

    await coll.download_media({
        "entity_id": "12345",
        "entity_name": "Test Chat",
        "content_type": "photo",
        "content_id": "12345_99",
        "media": stale_media,
        "extension": "jpg",
        "message_id": 99,
    }, worker=worker)

    worker.client.get_messages.assert_awaited_once_with(12345, ids=99)
    assert worker.client.download_media.await_args_list[0].args == (stale_media, bytes)
    assert worker.client.download_media.await_args_list[1].args == (fresh_media, bytes)
    kwargs = coll.insert_media_item.await_args.kwargs
    assert Path(kwargs["file_path"]).read_bytes() == b"fresh bytes"
    assert kwargs["metadata"]["raw"] == {"id": 99, "refreshed": True}
    coll.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_deleted_retries_transient_db_timeout(monkeypatch):
    coll = _make_collector(monkeypatch)
    coll._realtime_write_retry_delay = 0
    worker = _make_worker(coll)
    coll.pool.conn.execute.side_effect = [TimeoutError(), "UPDATE 1"]
    event = SimpleNamespace(chat_id=12345, deleted_ids=[99])

    await coll._on_message_deleted(worker, event)

    assert coll.pool.conn.execute.await_count == 2
    assert coll.pool.conn.execute.await_args.args[1] == "12345:99"


@pytest.mark.asyncio
async def test_upsert_message_archives_raw_payload(monkeypatch):
    coll = _make_collector(monkeypatch)
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, path=None, relative_path=None, sidecar=None, error=None)

    monkeypatch.setattr(tg_mod, "write_raw_payload", fake_write_raw_payload)
    message = SimpleNamespace(
        id=99,
        sender_id=123,
        message="hello",
        caption=None,
        date=datetime(2026, 7, 24, tzinfo=timezone.utc),
        photo=None,
        video=None,
        voice=None,
        pinned=False,
        reactions=None,
        media=None,
        action=None,
        to_dict=lambda: {"id": 99, "message": "hello", "sender_id": 123},
    )

    await coll._upsert_message(message, 42, "sender-uuid")

    assert coll.progress_count == 1
    assert calls
    assert calls[0]["source"] == "telegram"
    assert calls[0]["artifact_id"] == "messages/42:99"
    assert calls[0]["target_tables"] == ["telegram_messages"]
    assert calls[0]["payload"]["message"] == "hello"
    assert calls[0]["metadata"]["platform_chat_id"] == "42"


@pytest.mark.asyncio
async def test_upsert_user_full_archives_raw_payload(monkeypatch):
    coll = _make_collector(monkeypatch)
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, path=None, relative_path=None, sidecar=None, error=None)

    monkeypatch.setattr(tg_mod, "write_raw_payload", fake_write_raw_payload)
    user = SimpleNamespace(
        id=77,
        username="alice",
        first_name="Alice",
        last_name="Example",
        phone="+1",
        bio="bio",
        bot=False,
        to_dict=lambda: {"id": 77, "username": "alice"},
    )

    await coll._upsert_user_full(user)

    assert calls
    assert calls[0]["artifact_id"] == "users/77"
    assert calls[0]["target_tables"] == ["telegram_users"]
    assert calls[0]["payload"]["username"] == "alice"


@pytest.mark.asyncio
async def test_download_message_media_routes_photo(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)

    # Make the isinstance() checks succeed by patching the imported names
    # inside the function. The function imports them locally each call,
    # so monkey-patching the module-level telethon classes is unreliable;
    # instead, bypass the routing by stubbing the helpers + faking
    # MessageMedia*.
    import sys
    import types
    fake_types = types.ModuleType("telethon.tl.types")

    class _MMP:  # MessageMediaPhoto
        pass

    class _MMD:  # MessageMediaDocument
        pass

    fake_types.MessageMediaPhoto = _MMP
    fake_types.MessageMediaDocument = _MMD
    fake_tl = types.ModuleType("telethon.tl")
    fake_tl.types = fake_types
    fake_telethon = sys.modules.get("telethon") or types.ModuleType("telethon")
    fake_telethon.tl = fake_tl
    monkeypatch.setitem(sys.modules, "telethon", fake_telethon)
    monkeypatch.setitem(sys.modules, "telethon.tl", fake_tl)
    monkeypatch.setitem(sys.modules, "telethon.tl.types", fake_types)

    coll._handle_photo = AsyncMock()
    coll._handle_document = AsyncMock()

    msg = SimpleNamespace(id=99, media=_MMP(), chat_id=5)
    worker.client.get_entity = AsyncMock(
        return_value=SimpleNamespace(id=5, title="C", username="c"),
    )
    out = await coll.download_message_media(msg, worker=worker, chat_id=5)
    assert out is True
    coll._handle_photo.assert_awaited_once()
    coll._handle_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_message_media_no_media_returns_none(monkeypatch):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    msg = SimpleNamespace(id=1, media=None, chat_id=2)
    out = await coll.download_message_media(msg, worker=worker, chat_id=2)
    assert out is None


@pytest.mark.asyncio
async def test_realtime_write_retries_transient_timeout(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REALTIME_WRITE_ATTEMPTS", "2")
    monkeypatch.setenv("TELEGRAM_REALTIME_WRITE_RETRY_DELAY", "0")
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    message = SimpleNamespace(id=99)
    write = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
    coll._write_realtime_message = write

    await coll._write_realtime_message_with_retry(worker, message, 42)

    assert write.await_count == 2


@pytest.mark.asyncio
async def test_realtime_write_does_not_retry_non_transient_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REALTIME_WRITE_ATTEMPTS", "3")
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    message = SimpleNamespace(id=99)
    write = AsyncMock(side_effect=ValueError("bad row"))
    coll._write_realtime_message = write

    with pytest.raises(ValueError):
        await coll._write_realtime_message_with_retry(worker, message, 42)

    assert write.await_count == 1


@pytest.mark.asyncio
async def test_download_message_media_bails_without_workers(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    coll._spawn_workers = AsyncMock(return_value=[])
    with caplog.at_level("ERROR", logger="src.collectors.telegram"):
        out = await coll.download_message_media(SimpleNamespace(media=None), chat_id=1)
    assert out is None


@pytest.mark.asyncio
async def test_download_message_media_id_resolution_get_messages_failure(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    worker.client.get_messages = AsyncMock(side_effect=RuntimeError("api"))
    with caplog.at_level("ERROR", logger="src.collectors.telegram"):
        out = await coll.download_message_media(123, worker=worker, chat_id=4)
    assert out is None


@pytest.mark.asyncio
async def test_download_message_media_id_without_chat_id_returns_none(monkeypatch, caplog):
    coll = _make_collector(monkeypatch)
    worker = _make_worker(coll)
    with caplog.at_level("ERROR", logger="src.collectors.telegram"):
        out = await coll.download_message_media(123, worker=worker, chat_id=None)
    assert out is None


# ── cleanup ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_disconnects_workers_and_stops_pools(monkeypatch):
    coll = _make_collector(monkeypatch)
    w1 = _make_worker(coll, name="a")
    w2 = _make_worker(coll, name="b")
    w1.disconnect = AsyncMock()
    w2.disconnect = AsyncMock()
    coll._workers = [w1, w2]
    coll._realtime_running = True
    coll._hub_notifier.stop = AsyncMock()
    coll._bot_pool.stop_health_monitor = AsyncMock()
    coll._primary_client = MagicMock()

    await coll.cleanup()

    assert coll._realtime_running is False
    coll._hub_notifier.stop.assert_awaited_once()
    coll._bot_pool.stop_health_monitor.assert_awaited_once()
    w1.disconnect.assert_awaited_once()
    w2.disconnect.assert_awaited_once()
    assert coll._workers == []
    assert coll._primary_client is None


@pytest.mark.asyncio
async def test_cleanup_swallows_hub_and_bot_errors(monkeypatch):
    coll = _make_collector(monkeypatch)
    coll._hub_notifier.stop = AsyncMock(side_effect=RuntimeError("nope"))
    coll._bot_pool.stop_health_monitor = AsyncMock(side_effect=RuntimeError("also nope"))
    coll._workers = []
    # Must not propagate.
    await coll.cleanup()
