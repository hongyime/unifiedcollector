from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import instagram as instagram_mod


@pytest.fixture(autouse=True)
def _disable_tier1_raw_archives(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "0")


def _make_pool(fetchval_result=None, fetchrow_result=None):
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.execute = AsyncMock(return_value=None)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _bare_collector():
    coll = instagram_mod.InstagramCollector.__new__(instagram_mod.InstagramCollector)
    coll.pool = _make_pool()
    coll._current_account = SimpleNamespace(name="acct1", fingerprint={})
    coll._consecutive_429s = 0
    coll._daily_views = {}
    coll._daily_actions = {}
    coll.rate_limiter = object()
    coll.account_pool = MagicMock()
    coll.account_pool._accounts = []
    return coll


class _MediaResponse:
    def __init__(self, data: bytes):
        self.content = data

    def raise_for_status(self):
        return None


class _MediaClient:
    def __init__(self, data: bytes):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *_args, **_kwargs):
        return _MediaResponse(self.data)


@pytest.mark.asyncio
async def test_download_media_writes_headless_instagram_to_vault_blob(monkeypatch, tmp_path):
    coll = _bare_collector()
    coll._known_ids = set()
    coll.reconciler = SimpleNamespace(should_recover=lambda _content_id: False)
    coll._get_session_cookies = MagicMock(return_value={})
    coll.rate_limiter = SimpleNamespace(
        async_wait=AsyncMock(),
        record_success=MagicMock(),
        record_failure=MagicMock(),
    )
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()
    data = b"instagram media bytes"
    digest = hashlib.sha256(data).hexdigest()
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    monkeypatch.setattr(instagram_mod, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(instagram_mod, "assert_media_write_allowed", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        instagram_mod.InstagramCollector,
        "account_media_dir",
        property(lambda self: tmp_path / "media" / "account_acct1"),
    )
    monkeypatch.setattr(instagram_mod.httpx, "AsyncClient", lambda *args, **kwargs: _MediaClient(data))

    await coll.download_media({
        "entity_id": "123",
        "entity_name": "alice",
        "content_type": "post",
        "content_id": "post123",
        "extension": "jpg",
        "url": "https://cdn.example.test/p.jpg",
        "source_url": "https://www.instagram.com/p/post123/",
        "raw": {"shortcode": "post123"},
    })

    blob = vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert blob.read_bytes() == data
    kwargs = coll.insert_media_item.await_args.kwargs
    assert Path(kwargs["file_path"]) == blob
    assert kwargs["sha256"] == digest
    assert kwargs["metadata"]["raw"] == {"shortcode": "post123"}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["blob_path"] == f"media/blobs/{digest[:2]}/{digest[2:4]}/{digest}.jpg"
    coll.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_profile_archives_raw_payload(monkeypatch):
    coll = _bare_collector()
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return MagicMock(ok=True)

    monkeypatch.setattr(instagram_mod, "write_raw_payload", fake_write_raw_payload)

    profile = {
        "id": "123",
        "username": "alice",
        "full_name": "Alice",
        "biography": "bio",
        "edge_followed_by": {"count": 10},
        "edge_follow": {"count": 5},
        "edge_owner_to_timeline_media": {"count": 2},
    }

    await coll._upsert_profile(profile)

    assert calls
    assert calls[0]["source"] == "instagram"
    assert calls[0]["artifact_id"].startswith("profiles/123/")
    assert calls[0]["payload"] == profile
    assert calls[0]["target_tables"] == ["instagram_profiles"]
    assert calls[0]["metadata"]["collection_account"] == "acct1"


@pytest.mark.asyncio
async def test_upsert_post_archives_raw_payload(monkeypatch):
    coll = _bare_collector()
    coll.pool = _make_pool(fetchrow_result={"id": "profile-uuid"})
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return MagicMock(ok=True)

    monkeypatch.setattr(instagram_mod, "write_raw_payload", fake_write_raw_payload)
    post = {
        "shortcode": "post123",
        "__typename": "GraphImage",
        "taken_at_timestamp": 1_700_000_000,
        "edge_media_preview_like": {"count": 3},
        "edge_media_to_comment": {"count": 1},
        "edge_media_to_caption": {"edges": [{"node": {"text": "hello"}}]},
    }

    await coll._upsert_post(post, "123")

    assert calls
    assert calls[0]["artifact_id"].startswith("posts/post123/")
    assert calls[0]["payload"] == post
    assert calls[0]["target_tables"] == ["instagram_posts"]
    assert calls[0]["metadata"]["platform_user_id"] == "123"


@pytest.mark.asyncio
async def test_collect_posts_archives_graphql_page(monkeypatch):
    coll = _bare_collector()
    coll._sem = asyncio.Semaphore(1)
    coll._stop = SimpleNamespace(is_set=lambda: False)
    coll.rate_limiter = SimpleNamespace(
        async_wait=AsyncMock(),
        record_success=MagicMock(),
        record_failure=MagicMock(),
    )
    coll.circuit_breaker = SimpleNamespace(record_success=MagicMock(), record_failure=MagicMock())
    coll._process_post = AsyncMock()
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return MagicMock(ok=True)

    monkeypatch.setattr(instagram_mod, "write_raw_payload", fake_write_raw_payload)

    response_payload = {
        "data": {
            "user": {
                "edge_owner_to_timeline_media": {
                    "edges": [{"node": {"shortcode": "post123"}}],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                }
            }
        }
    }
    response = MagicMock(status_code=200)
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=response_payload)
    client = SimpleNamespace(get=AsyncMock(return_value=response))

    ok = await coll._collect_posts(client, "123", "alice")

    assert ok is True
    assert calls
    assert calls[0]["artifact_id"].startswith("graphql/posts/123/page_0/")
    assert calls[0]["payload"] == response_payload
    assert calls[0]["target_tables"] == ["instagram_posts"]
    assert calls[0]["metadata"]["ingest_path"] == "httpx_graphql"
    coll._process_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_rate_limit_records_scoped_event_metadata(monkeypatch):
    coll = _bare_collector()
    record_event = AsyncMock()
    monkeypatch.setattr(instagram_mod, "record_rate_limit_event", record_event)
    monkeypatch.setattr(instagram_mod, "TLSFingerprintRotator", None)

    await coll._handle_rate_limit(
        Exception("429"),
        scope="graphql_posts",
        metadata={
            "username": "target_user",
            "uid": "123",
            "endpoint": "graphql/query",
        },
    )

    record_event.assert_awaited_once()
    kwargs = record_event.await_args.kwargs
    assert kwargs["source"] == "instagram"
    assert kwargs["account"] == "acct1"
    assert kwargs["scope"] == "graphql_posts"
    assert kwargs["status_code"] == 429
    assert kwargs["metadata"]["username"] == "target_user"
    assert kwargs["metadata"]["uid"] == "123"
    assert kwargs["metadata"]["endpoint"] == "graphql/query"
    assert kwargs["metadata"]["streak"] == 1


@pytest.mark.asyncio
async def test_process_target_respects_cooldown_with_playwright_primary(monkeypatch):
    coll = _bare_collector()
    monkeypatch.setenv("INSTA_PLAYWRIGHT_PRIMARY", "true")
    limiter = instagram_mod.HumanLikeRateLimiter()
    limiter._in_emergency["instagram.com:acct1"] = time.monotonic() + 120
    coll.rate_limiter = limiter
    coll._collect_user = AsyncMock()

    await coll._process_target(MagicMock(), "target_user")

    coll._collect_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_profile_playwright_records_429_without_marking_session_dead(monkeypatch):
    coll = _bare_collector()
    coll._session_auth_dead = False
    coll._build_playwright_storage_state = MagicMock(return_value=None)
    coll.user_agents = MagicMock()
    coll.user_agents.get_for_domain = MagicMock(return_value="ua")
    coll._record_rate_limit_event = AsyncMock()

    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value={"status": 429, "body": ""})

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock(return_value=None)

    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)

    playwright_ctx = MagicMock()
    playwright_ctx.chromium = chromium
    playwright_ctx.stop = AsyncMock(return_value=None)

    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright_ctx)

    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = MagicMock(return_value=starter)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    monkeypatch.setattr(instagram_mod, "headless_dwell", AsyncMock(return_value=None))
    monkeypatch.setattr(
        instagram_mod,
        "sleep_before_pre_cooldown_retry",
        AsyncMock(return_value=0.0),
    )

    result = await coll._fetch_profile_playwright("target_user")

    assert result is None
    assert coll._session_auth_dead is False
    assert page.evaluate.await_count == 2
    coll._record_rate_limit_event.assert_awaited_once_with(
        scope="profile_fetch_playwright",
        status_code=429,
        reason="Playwright profile rate-limit response",
        metadata={"username": "target_user", "endpoint": "web_profile_info"},
    )


@pytest.mark.asyncio
async def test_fetch_profile_playwright_retries_transient_429(monkeypatch):
    coll = _bare_collector()
    coll._session_auth_dead = False
    coll._build_playwright_storage_state = MagicMock(return_value=None)
    coll.user_agents = MagicMock()
    coll.user_agents.get_for_domain = MagicMock(return_value="ua")
    coll._record_rate_limit_event = AsyncMock()

    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(side_effect=[
        {"status": 429, "body": ""},
        {"status": 200, "body": json.dumps({"data": {"user": {"id": "123", "username": "target_user"}}})},
    ])

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock(return_value=None)

    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)

    playwright_ctx = MagicMock()
    playwright_ctx.chromium = chromium
    playwright_ctx.stop = AsyncMock(return_value=None)

    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright_ctx)

    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = MagicMock(return_value=starter)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    monkeypatch.setattr(instagram_mod, "headless_dwell", AsyncMock(return_value=None))
    monkeypatch.setattr(
        instagram_mod,
        "sleep_before_pre_cooldown_retry",
        AsyncMock(return_value=0.0),
    )

    result = await coll._fetch_profile_playwright("target_user")

    assert result == {"id": "123", "username": "target_user"}
    assert coll._session_auth_dead is False
    assert page.evaluate.await_count == 2
    coll._record_rate_limit_event.assert_awaited_once()
    kwargs = coll._record_rate_limit_event.await_args.kwargs
    assert kwargs["scope"] == "profile_fetch_playwright"
    assert kwargs["status_code"] == 429
    assert kwargs["reason"] == "Playwright profile transient rate-limit retried"
    assert kwargs["metadata"]["pre_cooldown_retry"] is True
    assert kwargs["metadata"]["retry_status_code"] == 200


@pytest.mark.asyncio
async def test_fetch_profile_playwright_archives_raw_response(monkeypatch):
    coll = _bare_collector()
    coll._session_auth_dead = False
    coll._build_playwright_storage_state = MagicMock(return_value=None)
    coll.user_agents = MagicMock()
    coll.user_agents.get_for_domain = MagicMock(return_value="ua")
    coll._record_rate_limit_event = AsyncMock()
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return MagicMock(ok=True)

    monkeypatch.setattr(instagram_mod, "write_raw_payload", fake_write_raw_payload)

    payload = {"data": {"user": {"id": "123", "username": "target_user"}}}
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.evaluate = AsyncMock(return_value={"status": 200, "body": json.dumps(payload)})

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock(return_value=None)

    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser)

    playwright_ctx = MagicMock()
    playwright_ctx.chromium = chromium
    playwright_ctx.stop = AsyncMock(return_value=None)

    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright_ctx)

    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = MagicMock(return_value=starter)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    monkeypatch.setattr(instagram_mod, "headless_dwell", AsyncMock(return_value=None))

    result = await coll._fetch_profile_playwright("target_user")

    assert result == payload["data"]["user"]
    assert calls
    assert calls[0]["artifact_id"].startswith("playwright/profiles/target_user/")
    assert calls[0]["payload"] == payload
    assert calls[0]["target_tables"] == ["instagram_profiles"]
    assert calls[0]["metadata"]["ingest_path"] == "playwright_profile_fetch"
