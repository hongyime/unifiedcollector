"""Tests for src/collectors/whatsapp.py — RECEIVE-ONLY bridge collector.

Pure-unit. httpx is patched at the module level — no real network. asyncpg
pool replaced with AsyncMock. aio_pika / redis are never imported live.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collectors import whatsapp as wa_mod
from src.collectors.whatsapp import WhatsappCollector


# ── fixtures ──────────────────────────────────────────────────────────────


def _make_pool():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": "user-uuid-1"})
    conn.fetchval = AsyncMock(return_value="chat-uuid-1")
    conn.fetch = AsyncMock(return_value=[])

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.fixture
def collector(tmp_path, monkeypatch):
    # Reset all relevant env vars to known defaults.
    for k in (
        "WHATSAPP_EXPORT_DIR", "WHATSAPP_SESSION_BRIDGES_JSON",
        "SESSION_BRIDGES_JSON", "WHATSAPP_MEDIA_BRIDGE_SECRET",
        "MEDIA_BRIDGE_SECRET", "WHATSAPP_SESSION_NAMES", "SESSION_NAMES",
        "WHATSAPP_RABBITMQ_URL", "RABBITMQ_URL",
        "WHATSAPP_REDIS_URL", "REDIS_URL",
        "WHATSAPP_SPIDER_SESSIONS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = WhatsappCollector()
    pool, conn = _make_pool()
    c.set_pool(pool)
    c._test_conn = conn  # type: ignore[attr-defined]
    return c


@pytest.fixture
def configured_collector(tmp_path, monkeypatch):
    """Collector configured for real-time bridge mode."""
    monkeypatch.setenv(
        "WHATSAPP_SESSION_BRIDGES_JSON",
        json.dumps({"sess1": "http://bridge.local:8080"}),
    )
    monkeypatch.setenv("WHATSAPP_MEDIA_BRIDGE_SECRET", "topsecret")
    monkeypatch.setenv("WHATSAPP_SESSION_NAMES", "sess1")
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = WhatsappCollector()
    pool, conn = _make_pool()
    c.set_pool(pool)
    c._test_conn = conn  # type: ignore[attr-defined]
    return c


# ── constructor / config ──────────────────────────────────────────────────


def test_constructor_defaults(collector):
    assert collector.SOURCE_NAME == "whatsapp"
    assert collector._session_bridges == {}
    assert collector._bridge_secret == ""
    assert collector._session_names == []
    assert collector._use_realtime is False
    assert collector._use_export is False


def test_constructor_parses_session_bridges_json(configured_collector):
    assert configured_collector._session_bridges == {
        "sess1": "http://bridge.local:8080",
    }
    assert configured_collector._bridge_secret == "topsecret"
    assert configured_collector._session_names == ["sess1"]
    assert configured_collector._use_realtime is True


def test_constructor_handles_invalid_session_bridges_json(
    tmp_path, monkeypatch, caplog,
):
    monkeypatch.setenv("WHATSAPP_SESSION_BRIDGES_JSON", "{not json")
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    with caplog.at_level("WARNING", logger="src.collectors.whatsapp"):
        c = WhatsappCollector()
    assert c._session_bridges == {}
    assert any("Invalid SESSION_BRIDGES_JSON" in r.getMessage()
               for r in caplog.records)


def test_constructor_parses_spider_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_SPIDER_SESSIONS", "session_2, session_3")
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = WhatsappCollector()
    assert c._is_spider_allowed("session_2") is True
    assert c._is_spider_allowed("session_3") is True
    assert c._is_spider_allowed("session_1") is False


def test_constructor_export_mode_when_dir_exists(tmp_path, monkeypatch):
    export = tmp_path / "exports"
    export.mkdir()
    monkeypatch.setenv("WHATSAPP_EXPORT_DIR", str(export))
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = WhatsappCollector()
    assert c._use_export is True


def test_account_media_dir_uses_session_name(configured_collector, tmp_path):
    p = configured_collector.account_media_dir
    assert p.exists()
    assert "session_sess1" in p.name


def test_account_media_dir_default_when_no_session(collector, tmp_path):
    p = collector.account_media_dir
    assert "session_default" in p.name


# ── set_pool ──────────────────────────────────────────────────────────────


def test_set_pool_propagates_to_checkpoint(collector):
    new_pool = MagicMock()
    collector.set_pool(new_pool)
    assert collector.pool is new_pool


# ── collect (entry-point routing) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_warns_when_no_mode_available(collector, caplog):
    with caplog.at_level("WARNING", logger="src.collectors.whatsapp"):
        await collector.collect([])
    assert any("no collection mode" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_routes_to_realtime(configured_collector):
    configured_collector._collect_realtime = AsyncMock()
    configured_collector._collect_from_exports = AsyncMock()
    await configured_collector.collect(["target1"])
    configured_collector._collect_realtime.assert_awaited_once_with(["target1"])
    configured_collector._collect_from_exports.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_routes_to_export_mode(tmp_path, monkeypatch):
    export = tmp_path / "exports"
    export.mkdir()
    # WHATSAPP_EXPORT_DIR must be set BEFORE the constructor, because
    # WhatsappCollector.__init__ snapshots it into self._use_export.
    # Also clear realtime-mode env vars so _use_realtime stays False
    # (otherwise collect() routes to realtime, not exports).
    monkeypatch.delenv("SESSION_BRIDGES_JSON", raising=False)
    monkeypatch.delenv("MEDIA_BRIDGE_SECRET", raising=False)
    monkeypatch.delenv("WHATSAPP_SESSION_BRIDGES_JSON", raising=False)
    monkeypatch.setenv("WHATSAPP_EXPORT_DIR", str(export))
    monkeypatch.setattr(
        "src.core.base_collector.DRIVE_PATH", str(tmp_path),
    )
    c = WhatsappCollector()
    c._collect_from_exports = AsyncMock()
    c._collect_realtime = AsyncMock()
    await c.collect(["t"])
    c._collect_from_exports.assert_awaited_once_with(["t"])
    c._collect_realtime.assert_not_awaited()


# ── _media_type_to_ext / _media_type_to_content_type ─────────────────────


def test_media_type_to_ext_known_types():
    assert WhatsappCollector._media_type_to_ext("imageMessage") == "jpg"
    assert WhatsappCollector._media_type_to_ext("videoMessage") == "mp4"
    assert WhatsappCollector._media_type_to_ext("audioMessage") == "opus"
    assert WhatsappCollector._media_type_to_ext("documentMessage") == "pdf"
    assert WhatsappCollector._media_type_to_ext("stickerMessage") == "webp"
    assert WhatsappCollector._media_type_to_ext("unknown") == "bin"


def test_media_type_to_content_type_known_types():
    assert WhatsappCollector._media_type_to_content_type("imageMessage") == "photo"
    assert WhatsappCollector._media_type_to_content_type("videoMessage") == "video"
    assert WhatsappCollector._media_type_to_content_type("audioMessage") == "audio"
    assert WhatsappCollector._media_type_to_content_type("stickerMessage") == "sticker"
    assert WhatsappCollector._media_type_to_content_type("foo") == "media"


def test_classify_type_routing():
    assert WhatsappCollector._classify_type("mp4") == "video"
    assert WhatsappCollector._classify_type("3gp") == "video"
    assert WhatsappCollector._classify_type("opus") == "audio"
    assert WhatsappCollector._classify_type("m4a") == "audio"
    assert WhatsappCollector._classify_type("pdf") == "document"
    assert WhatsappCollector._classify_type("jpg") == "photo"


# ── _bridge_headers / session health ─────────────────────────────────────


def test_bridge_headers_carries_bearer(configured_collector):
    h = configured_collector._bridge_headers()
    assert h["Authorization"] == "Bearer topsecret"
    assert h["Content-Type"] == "application/json"


def test_session_cooldown_lifecycle(configured_collector):
    c = configured_collector
    assert c._is_session_cooled_down("sess1") is False
    # Trigger cooldown by feeding repeated failures past the risk threshold.
    for _ in range(10):
        c._record_session_failure("sess1")
    assert c._session_health["sess1"]["risk"] >= c._session_risk_threshold
    assert c._is_session_cooled_down("sess1") is True


def test_session_success_recovers_risk(configured_collector):
    c = configured_collector
    c._record_session_failure("sess1")
    risk_after_fail = c._session_health["sess1"]["risk"]
    c._record_session_success("sess1")
    assert c._session_health["sess1"]["risk"] < risk_after_fail


# ── _is_duplicate ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_duplicate_uses_redis_when_available(collector):
    redis_mock = MagicMock()
    # nx=True returns truthy on first set, None on duplicate
    redis_mock.set = AsyncMock(return_value=True)
    collector._redis = redis_mock
    assert await collector._is_duplicate("msg-1", "chat@s.whatsapp.net") is False
    redis_mock.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_duplicate_redis_returns_none_means_dup(collector):
    redis_mock = MagicMock()
    redis_mock.set = AsyncMock(return_value=None)
    collector._redis = redis_mock
    assert await collector._is_duplicate("msg-1", "chat@s.whatsapp.net") is True


@pytest.mark.asyncio
async def test_is_duplicate_falls_back_to_known_set(collector):
    collector._known_ids.add("wa_msg-1")
    assert await collector._is_duplicate("msg-1", "chat@s.whatsapp.net") is True
    assert await collector._is_duplicate("msg-2", "chat@s.whatsapp.net") is False


@pytest.mark.asyncio
async def test_is_duplicate_redis_failure_falls_back(collector):
    redis_mock = MagicMock()
    redis_mock.set = AsyncMock(side_effect=RuntimeError("redis dead"))
    collector._redis = redis_mock
    # falls through to is_known check
    assert await collector._is_duplicate("msg-99", "chat@s.whatsapp.net") is False


# ── _handle_message_event / process_bridge_event ──────────────────────────


def _patch_async_client(monkeypatch, *, response=None, raises=None,
                         post_response=None):
    """Patch httpx.AsyncClient at the whatsapp module level."""
    client = MagicMock()
    if raises is not None:
        client.get = AsyncMock(side_effect=raises)
        client.post = AsyncMock(side_effect=raises)
    else:
        resp = response or MagicMock(
            status_code=200, content=b"", json=MagicMock(return_value={}),
        )
        if hasattr(resp, "raise_for_status"):
            pass
        else:
            resp.raise_for_status = MagicMock()
        client.get = AsyncMock(return_value=resp)
        client.post = AsyncMock(return_value=post_response or resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(wa_mod.httpx, "AsyncClient",
                         MagicMock(return_value=cm))
    return client


@pytest.mark.asyncio
async def test_handle_message_event_skips_when_no_msg_id(collector):
    # No msg_id and no chat_jid → early return, no DB calls
    await collector._handle_message_event({}, [])
    collector._test_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_event_filters_by_target(collector):
    event = {
        "message_id": "m1",
        "chat_jid": "111@s.whatsapp.net",
        "body": "hi",
    }
    # targets non-matching → early return
    await collector._handle_message_event(event, ["nomatch"])
    collector._test_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_event_dedupes(collector):
    event = {
        "message_id": "m1",
        "chat_jid": "111@s.whatsapp.net",
    }
    collector._is_duplicate = AsyncMock(return_value=True)
    await collector._handle_message_event(event, [])
    collector._test_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_message_event_text_message_no_media(collector):
    """Plain text message: upsert chat + user + message, no media path."""
    event = {
        "message_id": "m1",
        "chat_jid": "111@s.whatsapp.net",
        "pushName": "Bob",
        "body": "hello world",
        "key": {"id": "m1", "remoteJid": "111@s.whatsapp.net",
                "participant": "111@s.whatsapp.net", "fromMe": False},
        "timestamp": 1_700_000_000,
    }
    collector._is_duplicate = AsyncMock(return_value=False)
    # PROD BUG: _track_user_profile references self._pool (underscored) but
    # BaseCollector only defines self.pool. Set the attribute so this test
    # can exercise the rest of the pipeline. See
    # ``test_track_user_profile_has_pool_typo_bug`` below for explicit
    # documentation of the bug.
    collector._pool = collector.pool  # type: ignore[attr-defined]
    await collector._handle_message_event(event, [])
    # At least chat upsert + message insert happened.
    assert collector._test_conn.execute.await_count >= 2


@pytest.mark.asyncio
async def test_handle_message_event_text_message_discovers_links_before_media_return(collector):
    event = {
        "message_id": "m1",
        "chat_jid": "111@g.us",
        "body": "join https://chat.whatsapp.com/InviteCode123",
        "session_name": "session_2",
        "key": {"id": "m1", "remoteJid": "111@g.us", "participant": "222@s.whatsapp.net"},
    }
    collector._is_duplicate = AsyncMock(return_value=False)
    collector._upsert_chat = AsyncMock()
    collector._track_user_profile = AsyncMock(return_value="user-uuid")
    collector._upsert_message = AsyncMock()
    collector._extract_wa_location = AsyncMock()
    collector._discover_links = AsyncMock()

    await collector._handle_message_event(event, [])

    collector._discover_links.assert_awaited_once_with(
        event["body"], "111@g.us", session="session_2",
    )


@pytest.mark.asyncio
async def test_track_user_profile_uses_pool_and_returns_uuid(collector):
    """Regression: ``_track_user_profile`` used to check ``self._pool``
    (typo) and always raise AttributeError before any DB work, breaking
    every real-time ingestion. It now correctly uses ``self.pool`` — so with
    a populated pool it completes and returns the upserted user UUID rather
    than raising.
    """
    event = {
        "sender_jid": "111@s.whatsapp.net",
        "pushName": "Bob",
        "key": {"participant": "111@s.whatsapp.net"},
    }
    result = await collector._track_user_profile(event)
    # Fixture's mocked fetchrow returns {"id": "user-uuid-1"}.
    assert result == "user-uuid-1"


@pytest.mark.asyncio
async def test_process_bridge_event_delegates(collector):
    collector._handle_message_event = AsyncMock()
    event = {"message_id": "x", "chat_jid": "y@s.whatsapp.net"}
    await collector.process_bridge_event(event, ["t"])
    collector._handle_message_event.assert_awaited_once_with(event, ["t"])


@pytest.mark.asyncio
async def test_process_bridge_event_default_targets(collector):
    collector._handle_message_event = AsyncMock()
    await collector.process_bridge_event({"a": 1})
    args = collector._handle_message_event.await_args.args
    assert args[1] == []


# ── _upsert_chat / _upsert_message ────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_chat_marks_groups(collector):
    await collector._upsert_chat("111-22@g.us", "Group A", {})
    args = collector._test_conn.execute.await_args.args
    assert "whatsapp_chats" in args[0]
    assert "111-22@g.us" in args
    assert True in args  # is_group


@pytest.mark.asyncio
async def test_upsert_chat_marks_dms(collector):
    await collector._upsert_chat("111@s.whatsapp.net", "Bob", {})
    args = collector._test_conn.execute.await_args.args
    assert False in args  # is_group=False


@pytest.mark.asyncio
async def test_upsert_message_inserts_with_chat_uuid(collector):
    collector._test_conn.fetchrow = AsyncMock(return_value={"id": "chat-uuid"})
    event = {
        "message_id": "m1",
        "body": "hello",
        "timestamp": 1_700_000_000,
        "key": {"id": "m1", "fromMe": False},
        "mimetype": "text/plain",
    }
    await collector._upsert_message(event, "111@s.whatsapp.net", "user-uuid")
    collector._test_conn.fetchrow.assert_awaited()
    collector._test_conn.execute.assert_awaited_once()


# ── link discovery ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_links_stores_url_domain_and_type(collector):
    await collector._discover_links(
        "join https://chat.whatsapp.com/InviteCode123",
        "111@g.us",
        session="session_2",
    )

    args = collector._test_conn.execute.await_args.args
    assert args[1] == "chat-uuid-1"
    assert args[2] == "https://chat.whatsapp.com/InviteCode123"
    assert args[3] == "chat.whatsapp.com"
    assert args[4] == "group_invite"


@pytest.mark.asyncio
async def test_discover_links_restricts_group_invites_from_disallowed_session(collector):
    collector._spider_sessions = {"session_2"}

    await collector._discover_links(
        "join https://chat.whatsapp.com/InviteCode123",
        "111@g.us",
        session="session_1",
    )

    args = collector._test_conn.execute.await_args.args
    assert args[2] == "https://chat.whatsapp.com/InviteCode123"
    assert args[4] == "group_invite_restricted"


# ── backfill_chat ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_chat_returns_none_without_bridge(collector, caplog):
    with caplog.at_level("WARNING", logger="src.collectors.whatsapp"):
        out = await collector.backfill_chat("111@s.whatsapp.net")
    assert out is None
    assert any("no bridge" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_backfill_chat_posts_to_bridge_and_returns_correlation(
    configured_collector, monkeypatch,
):
    # Speed up the rate-limit sleep.
    configured_collector._backfill_rpm = 6000  # → 0.01s sleep
    resp = MagicMock(status_code=200, raise_for_status=MagicMock())
    client = _patch_async_client(monkeypatch, post_response=resp, response=resp)

    out = await configured_collector.backfill_chat(
        "111@s.whatsapp.net", count=50,
    )
    assert isinstance(out, str) and len(out) > 0
    client.post.assert_awaited_once()
    posted_url = client.post.await_args.args[0]
    assert "/backfill-request" in posted_url


@pytest.mark.asyncio
async def test_backfill_chat_returns_none_on_http_error(
    configured_collector, monkeypatch,
):
    _patch_async_client(monkeypatch, raises=RuntimeError("connection refused"))
    out = await configured_collector.backfill_chat("111@s.whatsapp.net")
    assert out is None


@pytest.mark.asyncio
async def test_backfill_chat_falls_back_to_first_bridge(
    configured_collector, monkeypatch,
):
    """If session arg unknown, use the first configured bridge."""
    configured_collector._backfill_rpm = 6000
    resp = MagicMock(status_code=200, raise_for_status=MagicMock())
    _patch_async_client(monkeypatch, post_response=resp, response=resp)
    out = await configured_collector.backfill_chat(
        "111@s.whatsapp.net", session="UNKNOWN",
    )
    # Bridge map contains sess1 → fallback used → success.
    assert out is not None


# ── _download_via_bridge / _download_direct ───────────────────────────────


@pytest.mark.asyncio
async def test_download_via_bridge_returns_none_for_unknown_session(
    configured_collector,
):
    out = await configured_collector._download_via_bridge(
        "no-such-session", "m1", "key", "/path",
    )
    assert out is None


@pytest.mark.asyncio
async def test_download_via_bridge_signs_request_and_returns_bytes(
    configured_collector, monkeypatch,
):
    resp = MagicMock(status_code=200, content=b"binary-blob",
                       raise_for_status=MagicMock())
    client = _patch_async_client(
        monkeypatch, post_response=resp, response=resp,
    )
    out = await configured_collector._download_via_bridge(
        "sess1", "m1", "media-key", "/v/t/abc",
    )
    assert out == b"binary-blob"
    client.post.assert_awaited_once()
    headers = client.post.await_args.kwargs["headers"]
    assert "X-Timestamp" in headers
    assert "X-Signature" in headers
    assert headers["Authorization"] == "Bearer topsecret"


@pytest.mark.asyncio
async def test_download_via_bridge_returns_none_on_non_200(
    configured_collector, monkeypatch,
):
    resp = MagicMock(status_code=403, content=b"",
                       raise_for_status=MagicMock())
    _patch_async_client(monkeypatch, post_response=resp, response=resp)
    out = await configured_collector._download_via_bridge(
        "sess1", "m1", "key", "/path",
    )
    assert out is None


@pytest.mark.asyncio
async def test_download_direct_returns_bytes(collector, monkeypatch):
    resp = MagicMock(status_code=200, content=b"hello",
                       raise_for_status=MagicMock())
    _patch_async_client(monkeypatch, response=resp)
    out = await collector._download_direct("https://cdn/x.jpg")
    assert out == b"hello"


@pytest.mark.asyncio
async def test_download_direct_returns_none_on_error(collector, monkeypatch):
    _patch_async_client(monkeypatch, raises=RuntimeError("dns fail"))
    out = await collector._download_direct("https://cdn/x.jpg")
    assert out is None


# ── download_media ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_media_skips_known(collector):
    collector._known_ids.add("wa_known")
    collector._save_media = AsyncMock()
    await collector.download_media({
        "content_id": "wa_known",
        "data": b"x", "entity_id": "e", "entity_name": "n",
        "content_type": "photo",
    })
    collector._save_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_skips_when_no_data(collector):
    collector._save_media = AsyncMock()
    await collector.download_media({
        "content_id": "wa_new",
        "entity_id": "e", "entity_name": "n",
        "content_type": "photo",
    })
    collector._save_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_media_calls_save(collector):
    collector._save_media = AsyncMock()
    await collector.download_media({
        "content_id": "wa_new",
        "data": b"hello",
        "entity_id": "e1",
        "entity_name": "Bob",
        "content_type": "photo",
        "extension": "jpg",
    })
    collector._save_media.assert_awaited_once()
    args = collector._save_media.await_args.args
    assert args[0] == b"hello"
    assert args[1] == "wa_new"


@pytest.mark.asyncio
async def test_save_media_writes_vault_blob_and_links_message(collector, monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(wa_mod, "VAULT_ROOT", vault_root)
    collector.insert_media_item = AsyncMock(return_value=True)
    collector.send_to_dlq = AsyncMock()

    data = b"whatsapp image bytes"
    digest = hashlib.sha256(data).hexdigest()
    await collector._save_media(
        data,
        "wa_msg123",
        "15551234567@s.whatsapp.net",
        "Alice",
        "image",
        "jpg",
        {"key": {"id": "msg123"}},
    )

    kwargs = collector.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "whatsapp://15551234567@s.whatsapp.net/msg123"
    assert kwargs["metadata"]["raw"] == {"key": {"id": "msg123"}}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")

    update_args = collector._test_conn.execute.await_args.args
    assert "UPDATE whatsapp_messages SET media_url=$1" in update_args[0]
    assert update_args[1] == str(stored_path)
    assert update_args[2] == len(data)
    assert update_args[3] == "msg123"
    collector.send_to_dlq.assert_not_awaited()


# ── _cleanup_connections ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_connections_closes_redis_and_broker(collector):
    collector._redis = MagicMock()
    collector._redis.close = AsyncMock()
    collector._broker_conn = MagicMock()
    collector._broker_conn.close = AsyncMock()
    await collector._cleanup_connections()
    collector._redis.close.assert_awaited_once()
    collector._broker_conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_connections_swallows_close_errors(collector):
    collector._redis = MagicMock()
    collector._redis.close = AsyncMock(side_effect=RuntimeError("eep"))
    collector._broker_conn = MagicMock()
    collector._broker_conn.close = AsyncMock(side_effect=RuntimeError("eep"))
    # Must not raise.
    await collector._cleanup_connections()


@pytest.mark.asyncio
async def test_cleanup_connections_noop_when_nothing_open(collector):
    # Defaults: _redis=None, _broker_conn=None
    await collector._cleanup_connections()  # must not raise
