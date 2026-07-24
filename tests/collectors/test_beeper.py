"""Tests for src/collectors/beeper.py — Wave 4 polymorphic shadow ingest.

Covers:
  - is_enabled() gating (env flag + token presence)
  - BeeperClient HTTP shape (mocked httpx)
  - Helpers (_parse_ts, _opt)
  - BeeperWriter SQL emission (mocked asyncpg pool)
  - BeeperCollector lifecycle (no real DB, mocked client + writer)

No network calls; no docker; no live Beeper Desktop required.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import src.collectors.beeper as beeper_mod
from src.collectors.beeper import (
    BeeperAPIError,
    BeeperClient,
    BeeperCollector,
    BeeperTransientError,
    BeeperWriter,
    _is_transient_network_error,
    _opt,
    _parse_ts,
    is_enabled,
)


# ── helpers ───────────────────────────────────────────────────────────────


def test_parse_ts_iso_z():
    out = _parse_ts("2026-05-27T13:55:06.532Z")
    assert out is not None
    assert out.year == 2026 and out.month == 5
    assert out.tzinfo is not None


def test_parse_ts_unix_millis():
    out = _parse_ts(1700000000000)
    assert out is not None
    assert out.tzinfo == timezone.utc


def test_parse_ts_garbage_returns_none():
    assert _parse_ts(None) is None
    assert _parse_ts("") is None
    assert _parse_ts("not a date") is None
    assert _parse_ts({"x": 1}) is None


def test_opt_walks_nested_dicts():
    d = {"a": {"b": {"c": 42}}}
    assert _opt(d, "a", "b", "c") == 42
    assert _opt(d, "a", "x") is None
    assert _opt(d, "z") is None
    assert _opt({}, "a") is None


# ── is_enabled() ──────────────────────────────────────────────────────────


def test_is_enabled_requires_both(monkeypatch):
    monkeypatch.delenv("BEEPER_COLLECTOR_ENABLED", raising=False)
    monkeypatch.delenv("BEEPER_DESKTOP_API_TOKEN", raising=False)
    assert is_enabled() is False

    monkeypatch.setenv("BEEPER_COLLECTOR_ENABLED", "1")
    assert is_enabled() is False  # token still missing

    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "bdapi_x")
    assert is_enabled() is True


def test_is_enabled_truthy_values(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("BEEPER_COLLECTOR_ENABLED", v)
        assert is_enabled() is True
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("BEEPER_COLLECTOR_ENABLED", v)
        assert is_enabled() is False


# ── BeeperClient ──────────────────────────────────────────────────────────


def _make_mock_transport(handlers: dict[str, dict]) -> httpx.MockTransport:
    """Build an httpx MockTransport mapping path -> JSON body / status."""

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        cfg = handlers.get(path) or handlers.get("*")
        if not cfg:
            return httpx.Response(404, json={"message": "no mock for " + path})
        return httpx.Response(
            cfg.get("status", 200),
            json=cfg.get("json"),
            text=cfg.get("text"),
        )

    return httpx.MockTransport(_handler)


@pytest.fixture
def beeper_client(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "bdapi_test")
    monkeypatch.setenv("BEEPER_DESKTOP_API_URL", "http://test.local:23373")
    handlers = {
        "/v1/info": {"json": {"app": "Beeper Desktop", "version": "4.2.860"}},
        "/v1/accounts": {"json": [{"accountID": "telegram", "network": "Telegram", "status": "connected"}]},
        "/v1/chats": {"json": {"items": [{"id": "!a:b", "network": "Telegram", "accountID": "telegram"}], "nextCursor": None}},
    }
    client = BeeperClient()
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=_make_mock_transport(handlers),
        headers={"Authorization": f"Bearer {client.token}"},
    )
    return client


@pytest.mark.asyncio
async def test_client_info(beeper_client):
    info = await beeper_client.info()
    assert info["app"] == "Beeper Desktop"
    await beeper_client.close()


@pytest.mark.asyncio
async def test_client_accounts(beeper_client):
    accounts = await beeper_client.accounts()
    assert len(accounts) == 1
    assert accounts[0]["network"] == "Telegram"
    await beeper_client.close()


@pytest.mark.asyncio
async def test_client_raises_on_404(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    handlers = {"/v1/info": {"status": 404, "json": {"message": "not found"}}}
    client = BeeperClient()
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=_make_mock_transport(handlers),
        headers={"Authorization": "Bearer x"},
    )
    with pytest.raises(BeeperAPIError, match="404"):
        await client.info()
    await client.close()


@pytest.mark.asyncio
async def test_client_missing_token_raises(monkeypatch):
    monkeypatch.delenv("BEEPER_DESKTOP_API_TOKEN", raising=False)
    with pytest.raises(BeeperAPIError, match="not set"):
        BeeperClient()


@pytest.mark.asyncio
async def test_iter_chats_paginates(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    pages = [
        {"items": [{"id": "!1:b"}, {"id": "!2:b"}], "nextCursor": "c1"},
        {"items": [{"id": "!3:b"}], "nextCursor": None},
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[min(call_count["n"], len(pages) - 1)]
        call_count["n"] += 1
        return httpx.Response(200, json=page)

    client = BeeperClient()
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer x"},
    )
    seen = []
    async for chat in client.iter_chats():
        seen.append(chat["id"])
    assert seen == ["!1:b", "!2:b", "!3:b"]
    await client.close()


@pytest.mark.asyncio
async def test_iter_messages_walks_oldest_cursor(monkeypatch):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    pages = [
        {"items": [{"id": "m1"}, {"id": "m2"}], "oldestCursor": "c1", "newestCursor": "n1", "hasMore": True},
        {"items": [{"id": "m3"}], "oldestCursor": "c2", "newestCursor": "n1", "hasMore": False},
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[min(call_count["n"], len(pages) - 1)]
        call_count["n"] += 1
        return httpx.Response(200, json=page)

    client = BeeperClient()
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer x"},
    )
    ids = []
    async for msg, meta in client.iter_messages("!a:b", direction="before"):
        ids.append(msg["id"])
    assert ids == ["m1", "m2", "m3"]
    await client.close()


# ── BeeperWriter ──────────────────────────────────────────────────────────


def _mock_pool() -> tuple[MagicMock, MagicMock]:
    """Return (pool, conn) where pool.acquire() yields conn (async ctx)."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"inserted": True})
    conn.fetch = AsyncMock(return_value=[])

    # Build an async context manager that yields conn
    class _AcquireCtx:
        async def __aenter__(self_):
            return conn

        async def __aexit__(self_, *exc):
            return False

    # Same for transaction()
    class _TxCtx:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *exc):
            return False

    conn.transaction = MagicMock(return_value=_TxCtx())

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCtx())
    return pool, conn


@pytest.mark.asyncio
async def test_writer_upsert_account_emits_sql():
    pool, conn = _mock_pool()
    w = BeeperWriter(pool)
    await w.upsert_account({
        "accountID": "telegram",
        "network": "Telegram",
        "loginID": 123,
        "bridge": {"type": "telegram", "provider": "cloud"},
        "user": {
            "id": "@me:beeper.local", "fullName": "Bryan",
            "username": "bryan", "email": "x@y.z", "phoneNumber": "+65...",
            "imgURL": "mxc://..."
        },
        "status": "connected",
    })
    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO beeper_shadow_accounts" in sql
    args = conn.execute.await_args.args[1:]
    assert args[0] == "telegram"
    assert args[1] == "Telegram"
    assert args[2] == "123"  # loginID coerced to str


@pytest.mark.asyncio
async def test_writer_upsert_chat_includes_participants():
    pool, conn = _mock_pool()
    w = BeeperWriter(pool)
    await w.upsert_chat({
        "id": "!room:beeper.local",
        "localChatID": 99,
        "accountID": "discordgo",
        "network": "Discord",
        "title": "#ducks",
        "type": "group",
        "isReadOnly": False,
        "participants": {
            "items": [
                {"id": "@u1:b", "username": "alice", "fullName": "Alice"},
                {"id": "@u2:b", "username": "bob", "isAdmin": True},
            ]
        },
    })
    # 1 chat upsert + 2 participant upserts
    assert conn.execute.await_count == 3
    chat_sql = conn.execute.await_args_list[0].args[0]
    assert "INSERT INTO beeper_shadow_chats" in chat_sql


@pytest.mark.asyncio
async def test_writer_upsert_message_returns_inserted_flag():
    pool, conn = _mock_pool()
    conn.fetchrow = AsyncMock(return_value={"inserted": True})
    w = BeeperWriter(pool)
    is_new = await w.upsert_message({
        "id": "m1",
        "chatID": "!a:b",
        "accountID": "telegram",
        "network": "Telegram",
        "senderID": "@u:b",
        "senderName": "Alice",
        "timestamp": "2026-05-27T13:55:06.532Z",
        "type": "TEXT",
        "text": "hello",
    })
    assert is_new is True
    sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO beeper_shadow_messages" in sql
    assert "RETURNING (xmax = 0)" in sql


@pytest.mark.asyncio
async def test_writer_skips_message_with_no_timestamp():
    pool, conn = _mock_pool()
    w = BeeperWriter(pool)
    is_new = await w.upsert_message({"id": "m1", "chatID": "!a:b"})
    assert is_new is False
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_writer_update_sync_state_marks_complete():
    pool, conn = _mock_pool()
    w = BeeperWriter(pool)
    await w.update_sync_state(
        "!a:b",
        oldest_cursor="c1",
        newest_cursor="c2",
        backfill_complete=True,
    )
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert args[1] == "!a:b"
    assert args[4] is True  # backfill_complete


# ── BeeperCollector ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collector_collect_requires_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))

    coll = BeeperCollector(client=MagicMock(spec=BeeperClient))
    with pytest.raises(RuntimeError, match="DB pool"):
        await coll.collect([])


@pytest.mark.asyncio
async def test_collector_full_cycle_smoke(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    monkeypatch.setenv("BEEPER_MAX_CHATS_PER_CYCLE", "1")
    monkeypatch.setenv("BEEPER_BACKFILL_PAGES_PER_CYCLE", "1")

    pool, conn = _mock_pool()
    # _sync_messages's SELECT
    conn.fetch = AsyncMock(return_value=[{
        "chat_id": "!a:b",
        "network": "Telegram",
        "oldest_cursor": None,
        "newest_cursor": None,
        "backfill_complete": False,
        "last_message_ts": None,
    }])

    fake_client = MagicMock(spec=BeeperClient)
    fake_client.accounts = AsyncMock(return_value=[
        {"accountID": "tg", "network": "Telegram", "status": "connected"}
    ])

    async def _iter_chats(**_kw):
        yield {"id": "!a:b", "accountID": "tg", "network": "Telegram", "title": "x"}

    async def _iter_messages(chat_id, **_kw):
        yield (
            {
                "id": "m1", "chatID": chat_id, "accountID": "tg",
                "network": "Telegram", "senderID": "@u:b",
                "senderName": "Alice", "timestamp": "2026-05-27T13:55:06.532Z",
                "type": "TEXT", "text": "hi",
            },
            {"oldestCursor": "c1", "newestCursor": "n1", "hasMore": False},
        )

    fake_client.iter_chats = _iter_chats
    fake_client.iter_messages = _iter_messages

    coll = BeeperCollector(client=fake_client)
    coll.set_pool(pool)
    coll.drive_ok = True

    stats = await coll.collect([])
    assert stats["accounts"] == 1
    assert stats["chats"] == 1
    # No assertion on messages_inserted exact count — depends on direction loops
    # but it should at least be 0 with no exceptions.
    assert stats["errors"] == 0


@pytest.mark.asyncio
async def test_collector_download_media_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    coll = BeeperCollector(client=MagicMock(spec=BeeperClient))
    # Should return None without raising
    assert await coll.download_media({"id": "anything"}) is None


@pytest.mark.asyncio
async def test_collector_download_media_writes_vault_blob(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(beeper_mod, "VAULT_ROOT", vault_root)
    data = b"beeper attachment bytes"
    digest = hashlib.sha256(data).hexdigest()
    client = MagicMock(spec=BeeperClient)
    client.serve_asset = AsyncMock(return_value=data)
    coll = BeeperCollector(client=client)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    await coll.download_media({
        "content_id": "msg1_att1",
        "src_url": "mxc://beeper.local/abc?token=secret",
        "extension": "jpg",
        "network": "discord",
        "chat_id": "!room:beeper.local",
        "message_id": "msg1",
        "content_type": "image",
        "original_filename": "photo.jpg",
        "mime_type": "image/jpeg",
        "width": 640,
        "height": 480,
    })

    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "mxc://beeper.local/abc"
    assert kwargs["metadata"]["network"] == "discord"
    assert kwargs["metadata"]["chat_id"] == "!room:beeper.local"
    assert kwargs["metadata"]["message_id"] == "msg1"
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")
    coll.send_to_dlq.assert_not_awaited()


# ── transient DNS / name-resolution handling ───────────────────────────────


def test_is_transient_network_error_classifies():
    assert _is_transient_network_error(httpx.ConnectError("getaddrinfo failed"))
    assert _is_transient_network_error(httpx.ConnectTimeout("timed out"))
    assert _is_transient_network_error(httpx.ReadTimeout("slow"))
    assert _is_transient_network_error(
        Exception("[Errno -3] Temporary failure in name resolution")
    )
    assert _is_transient_network_error(Exception("nodename nor servname provided"))
    # Non-transient
    assert not _is_transient_network_error(Exception("non-JSON body"))
    assert not _is_transient_network_error(None)


def test_transient_error_is_api_error_subclass():
    # Existing handlers that catch BeeperAPIError must still catch transient ones.
    assert issubclass(BeeperTransientError, BeeperAPIError)


def test_beeper_suppresses_transient_run_error_notifications(monkeypatch, tmp_path):
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    coll = BeeperCollector(client=MagicMock(spec=BeeperClient))

    assert coll.should_notify_run_error(BeeperTransientError("temporary")) is False
    assert coll.should_notify_run_error(httpx.ReadTimeout("slow")) is False
    assert coll.should_notify_run_error(TimeoutError()) is False
    assert coll.should_notify_run_error(RuntimeError("schema broke")) is True


@pytest.mark.asyncio
async def test_request_retries_then_raises_transient(monkeypatch):
    """A persistent DNS blip is retried, then surfaces as BeeperTransientError."""
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setattr(beeper_mod, "_BEEPER_TRANSIENT_RETRIES", 2)
    monkeypatch.setattr(beeper_mod, "_BEEPER_TRANSIENT_BACKOFF", 0.0)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("getaddrinfo failed", request=request)

    client = BeeperClient()
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer x"},
    )
    with pytest.raises(BeeperTransientError):
        await client.info()
    assert attempts["n"] == 3  # 1 initial + 2 retries
    await client.close()


@pytest.mark.asyncio
async def test_request_retries_then_succeeds(monkeypatch):
    """A blip that clears mid-retry resolves without error."""
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setattr(beeper_mod, "_BEEPER_TRANSIENT_RETRIES", 3)
    monkeypatch.setattr(beeper_mod, "_BEEPER_TRANSIENT_BACKOFF", 0.0)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("Temporary failure in name resolution", request=request)
        return httpx.Response(200, json={"app": "Beeper Desktop"})

    client = BeeperClient()
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer x"},
    )
    info = await client.info()
    assert info["app"] == "Beeper Desktop"
    assert attempts["n"] == 3
    await client.close()


@pytest.mark.asyncio
async def test_collect_swallows_transient_without_error_count(monkeypatch, tmp_path):
    """A transient blip during a cycle increments `transient`, not `errors`."""
    monkeypatch.setenv("BEEPER_DESKTOP_API_TOKEN", "x")
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))

    pool, _conn = _mock_pool()
    fake_client = MagicMock(spec=BeeperClient)
    fake_client.accounts = AsyncMock(
        side_effect=BeeperTransientError("GET /v1/accounts transient transport error")
    )

    coll = BeeperCollector(client=fake_client)
    coll.set_pool(pool)
    stats = await coll.collect([])
    assert stats["transient"] == 1
    assert stats["errors"] == 0
