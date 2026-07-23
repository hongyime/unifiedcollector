from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collectors import instagram as instagram_mod


def _make_pool(fetchval_result=None):
    pool = MagicMock()
    conn = MagicMock()
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
    coll.rate_limiter = object()
    coll.account_pool = MagicMock()
    coll.account_pool._accounts = []
    return coll


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

    result = await coll._fetch_profile_playwright("target_user")

    assert result is None
    assert coll._session_auth_dead is False
    coll._record_rate_limit_event.assert_awaited_once_with(
        scope="profile_fetch_playwright",
        status_code=429,
        reason="Playwright profile rate-limit response",
        metadata={"username": "target_user", "endpoint": "web_profile_info"},
    )
