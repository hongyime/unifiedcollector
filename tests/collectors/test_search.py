"""Tests for src/collectors/search.py — Wave 2 multi-engine collector.

Pure-unit. ddgs / bs4 / PIL / fitz are stubbed via monkeypatching the
optional-import slots on the instance. httpx and the DB pool are mocked,
so no network or database I/O happens. We exercise:

  * constructor + env-driven knobs
  * account_media_dir shape (creates default account dir under media_dir)
  * `_is_content_url` static filter (icon / extension rules)
  * `search_query` happy path (DDG hits → finalise persists → no engines
    promoted past max_results threshold)
  * `search_query` waterfall promotion to Bing / Serper when DDG short
  * `_search_ddg` cache hit short-circuits driver
  * `_search_bing` parses sample HTML via injected stub BS factory
  * `_search_serper` honors quota gate (skipped when key missing)
  * `expand_paste_sites` returns empty when bs4 unavailable
  * `_finalise_query` writes search_queries / search_results rows
  * `download_media` delegates to `_download_asset`
  * `_download_asset` size-gate rejection
  * `cleanup` is a no-op

We tolerate the fact that BaseCollector calls check_drive() in its
constructor — its return value just gates `run()`, not `collect()`,
so direct construction is safe.
"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collectors import search as search_mod
from src.collectors.search import SearchCollector


# ── helpers ────────────────────────────────────────────────────────────────


def _set_clean_env(monkeypatch):
    """Reset all SEARCH_* / SERPER_* env so each test starts deterministic."""
    for var in (
        "SEARCH_MAX_RESULTS", "SEARCH_MIN_DIMENSION", "SEARCH_MIN_FILE_SIZE",
        "SEARCH_DOWNLOAD_IMAGES", "SEARCH_SPIDER_PAGES", "SEARCH_BING_PAGES",
        "SEARCH_SERPER_THRESHOLD", "SEARCH_CONCURRENT_DOWNLOADS",
        "SEARCH_CACHE_TTL_HOURS", "SEARCH_MAX_PDF_PAGES",
        "SERPER_API_KEY", "SERPER_DAILY_QUOTA",
    ):
        monkeypatch.delenv(var, raising=False)
    # Disable downloads-by-default so finalise_query doesn't go fetch URLs.
    monkeypatch.setenv("SEARCH_DOWNLOAD_IMAGES", "0")
    monkeypatch.setenv("SEARCH_SPIDER_PAGES", "0")


def _make_pool():
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value={"id": "query-uuid"})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    pool._conn = conn
    return pool


# ── constructor / feature gate ─────────────────────────────────────────────


def test_constructor_defaults(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    assert coll.SOURCE_NAME == "search"
    assert coll._max_results == 50
    assert coll._download_images is False
    assert coll._spider_pages is False
    assert coll._bing_pages == 3
    assert coll._serper_threshold == 5
    assert coll._serper_api_key == ""
    assert coll.DEFAULT_ENGINES == ("ddg", "bing", "serper")


def test_constructor_env_overrides(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "10")
    monkeypatch.setenv("SEARCH_BING_PAGES", "1")
    monkeypatch.setenv("SEARCH_SERPER_THRESHOLD", "3")
    monkeypatch.setenv("SERPER_API_KEY", "abcd1234efgh")
    monkeypatch.setenv("SERPER_DAILY_QUOTA", "100")

    coll = SearchCollector()
    assert coll._max_results == 10
    assert coll._bing_pages == 1
    assert coll._serper_threshold == 3
    assert coll._serper_api_key == "abcd1234efgh"
    assert coll._serper_quota == 100


# ── account_media_dir ──────────────────────────────────────────────────────


def test_account_media_dir_creates_default(monkeypatch, tmp_path):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    import importlib
    import src.core.drive_check as dc
    importlib.reload(dc)
    import src.core.base_collector as bc
    importlib.reload(bc)
    importlib.reload(search_mod)

    coll = search_mod.SearchCollector()
    p = coll.account_media_dir
    assert p.exists()
    assert p.name == "default"
    assert "search" in str(p)


# ── _is_content_url ────────────────────────────────────────────────────────


def test_is_content_url_accepts_image_pdf():
    assert SearchCollector._is_content_url("https://e.com/x.jpg") is True
    assert SearchCollector._is_content_url("https://e.com/x.PDF") is True
    assert SearchCollector._is_content_url("https://e.com/foo/bar.jpeg") is True


def test_is_content_url_rejects_unsupported_ext():
    assert SearchCollector._is_content_url("https://e.com/x.html") is False
    assert SearchCollector._is_content_url("https://e.com/x.svg") is False  # not in CONTENT_EXTENSIONS


def test_is_content_url_rejects_icons():
    assert SearchCollector._is_content_url("https://e.com/icons/sprite.png") is False
    assert SearchCollector._is_content_url("https://e.com/favicon.ico") is False
    assert SearchCollector._is_content_url("https://e.com/static/logo-small.jpg") is False


# ── search_query waterfall ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_query_uses_ddg_then_short_circuits(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "2")
    coll = SearchCollector()
    coll._DDGS = MagicMock()  # presence is enough; we replace _search_ddg
    coll._BS = MagicMock()
    pool = _make_pool()
    coll.set_pool(pool)

    coll._search_ddg = AsyncMock(return_value=[
        {"url": "https://a.com/1", "title": "A", "snippet": "s", "engine": "ddg", "domain": "a.com"},
        {"url": "https://a.com/2", "title": "B", "snippet": "s", "engine": "ddg", "domain": "a.com"},
    ])
    coll._search_bing = AsyncMock(return_value=[])
    coll._search_serper = AsyncMock(return_value=[])

    out = await coll.search_query("hello")

    assert len(out) == 2
    coll._search_ddg.assert_awaited_once_with("hello")
    # Bing and Serper not consulted (DDG met max_results)
    coll._search_bing.assert_not_called()
    coll._search_serper.assert_not_called()
    # rank assigned in order
    assert [h["rank"] for h in out] == [1, 2]


@pytest.mark.asyncio
async def test_search_query_promotes_to_bing_when_ddg_short(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "50")
    coll = SearchCollector()
    coll._DDGS = MagicMock()
    coll._BS = MagicMock()
    pool = _make_pool()
    coll.set_pool(pool)

    coll._search_ddg = AsyncMock(return_value=[
        {"url": "https://a.com/1", "engine": "ddg", "domain": "a.com"},
    ])
    coll._search_bing = AsyncMock(return_value=[
        {"url": "https://b.com/1", "engine": "bing", "domain": "b.com"},
    ])
    coll._search_serper = AsyncMock(return_value=[])

    out = await coll.search_query("q")
    assert len(out) == 2
    coll._search_bing.assert_awaited_once()
    # Serper not used (no API key)
    coll._search_serper.assert_not_called()


@pytest.mark.asyncio
async def test_search_query_consults_serper_when_under_threshold(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "abcd1234efgh")
    monkeypatch.setenv("SEARCH_SERPER_THRESHOLD", "5")
    coll = SearchCollector()
    coll._DDGS = MagicMock()
    coll._BS = MagicMock()
    pool = _make_pool()
    coll.set_pool(pool)

    coll._search_ddg = AsyncMock(return_value=[
        {"url": "https://a.com/1", "engine": "ddg", "domain": "a.com"},
    ])
    coll._search_bing = AsyncMock(return_value=[])
    coll._search_serper = AsyncMock(return_value=[
        {"url": "https://g.com/1", "engine": "serper", "domain": "g.com"},
    ])
    coll._serper_has_quota = AsyncMock(return_value=True)
    coll._serper_consume = AsyncMock()

    out = await coll.search_query("q")
    assert len(out) == 2
    coll._search_serper.assert_awaited_once()
    coll._serper_consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_query_dedupes_across_engines(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._DDGS = MagicMock()
    coll._BS = MagicMock()
    pool = _make_pool()
    coll.set_pool(pool)

    same = "https://dup.com/page"
    coll._search_ddg = AsyncMock(return_value=[
        {"url": same, "engine": "ddg", "domain": "dup.com"},
    ])
    coll._search_bing = AsyncMock(return_value=[
        {"url": same, "engine": "bing", "domain": "dup.com"},
    ])
    coll._search_serper = AsyncMock(return_value=[])

    out = await coll.search_query("q")
    assert len(out) == 1
    assert out[0]["engine"] == "ddg"  # winner is the first to claim the URL


# ── _search_ddg cache hit ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_ddg_cache_hit_skips_driver(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    cached = [{"url": "https://x.com/", "engine": "ddg", "domain": "x.com"}]
    coll._cache.get = MagicMock(return_value=cached)
    coll._DDGS = MagicMock()  # would explode if called
    coll._DDGS.side_effect = AssertionError("driver should not be called on cache hit")

    out = await coll._search_ddg("q")
    assert out == cached


@pytest.mark.asyncio
async def test_search_ddg_no_driver_returns_empty(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    coll._DDGS = None
    out = await coll._search_ddg("q")
    assert out == []


# ── _search_bing ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_bing_no_bs_returns_empty(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    coll._BS = None
    out = await coll._search_bing("q", num_pages=1)
    assert out == []


@pytest.mark.asyncio
async def test_search_bing_cache_hit(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    cached = [{"url": "https://b.com/", "engine": "bing", "domain": "b.com"}]
    coll._cache.get = MagicMock(return_value=cached)
    out = await coll._search_bing("q", num_pages=1)
    assert out == cached


# ── _search_serper ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_serper_no_key_returns_empty(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    out = await coll._search_serper("q")
    assert out == []


@pytest.mark.asyncio
async def test_search_serper_happy_path(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "abcd1234efgh")
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    coll._cache.put = MagicMock()

    # Page 1: two organic hits. Page 2: empty (loop breaks).
    page1 = MagicMock(status_code=200)
    page1.json = MagicMock(return_value={
        "organic": [
            {"link": "https://x.com/a", "title": "A", "snippet": "s1"},
            {"link": "https://x.com/b", "title": "B", "snippet": "s2"},
        ]
    })
    page2 = MagicMock(status_code=200)
    page2.json = MagicMock(return_value={"organic": []})

    client = MagicMock()
    client.post = AsyncMock(side_effect=[page1, page2, page2])
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(coll, "_make_client", return_value=client):
        out = await coll._search_serper("q")

    assert len(out) == 2
    assert out[0]["engine"] == "serper"
    coll._cache.put.assert_called_once()


# ── expand_paste_sites ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expand_paste_sites_no_bs_returns_empty(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._BS = None
    out = await coll.expand_paste_sites(["https://x.com/seed"])
    assert out == []


@pytest.mark.asyncio
async def test_expand_paste_sites_returns_discovered(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._BS = MagicMock()  # presence-only

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(coll, "_make_client", return_value=client):
        coll._spider_page = AsyncMock(return_value={
            "https://x.com/a.jpg", "https://x.com/b.pdf",
        })
        out = await coll.expand_paste_sites(["https://x.com/seed"])

    assert len(out) == 2
    assert all(h["source_url"] == "https://x.com/seed" for h in out)
    assert all(h["engine"] == "spider" for h in out)


# ── _finalise_query (DB persistence shape) ─────────────────────────────────


@pytest.mark.asyncio
async def test_finalise_query_persists_query_and_results(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    coll.set_pool(pool)

    hits = [
        {"url": "https://a.com/1", "title": "A", "snippet": "s",
         "rank": 1, "engine": "ddg", "domain": "a.com"},
        {"url": "https://a.com/2", "title": "B", "snippet": "s",
         "rank": 2, "engine": "bing", "domain": "a.com"},
    ]
    out = await coll._finalise_query("q", hits)

    assert out == hits
    # Three execute calls: 1 query upsert + 2 result inserts.
    assert pool._conn.execute.await_count == 3
    # Two fetchrow calls — one for each result row to look up query_id.
    assert pool._conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_finalise_query_empty_short_circuits(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    out = await coll._finalise_query("q", [])
    assert out == []
    pool._conn.execute.assert_not_called()


# ── download_media ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_media_delegates_to_download_asset(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._download_asset = AsyncMock(return_value=True)
    item = {
        "entity_name": "myquery",
        "url": "https://e.com/x.jpg",
        "rank": 3,
        "source_url": "https://e.com/page",
    }
    await coll.download_media(item)

    coll._download_asset.assert_awaited_once()
    kwargs = coll._download_asset.await_args.kwargs
    assert kwargs["query"] == "myquery"
    assert kwargs["hit"]["url"] == "https://e.com/x.jpg"
    assert kwargs["source_url"] == "https://e.com/page"


# ── _download_asset gates ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_asset_size_gate_rejects_tiny(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_MIN_FILE_SIZE", "10000")
    coll = SearchCollector()

    resp = MagicMock(status_code=200, content=b"x" * 50, headers={"content-type": "image/jpeg"})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(coll, "_make_client", return_value=client):
        ok = await coll._download_asset("q", {"url": "https://e.com/tiny.jpg"})

    assert ok is False


@pytest.mark.asyncio
async def test_download_asset_skips_known_cid(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    url = "https://e.com/x.jpg"
    cid = hashlib.sha256(url.encode()).hexdigest()[:16]
    coll._known_ids.add(cid)

    with patch.object(coll, "_make_client") as mc:
        ok = await coll._download_asset("q", {"url": url})
        mc.assert_not_called()
    assert ok is False


@pytest.mark.asyncio
async def test_download_asset_no_url_returns_false(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    assert await coll._download_asset("q", {}) is False


@pytest.mark.asyncio
async def test_download_asset_http_failure_returns_false(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(coll, "_make_client", return_value=client):
        ok = await coll._download_asset("q", {"url": "https://e.com/x.jpg"})
    assert ok is False


# ── collect (top-level entry) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_invokes_search_query_and_checkpoints(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.search_query = AsyncMock(return_value=[])
    coll.checkpoint.save_progress = AsyncMock()

    await coll.collect(["alpha", "beta"])

    assert coll.search_query.await_count == 2
    assert coll.checkpoint.save_progress.await_count == 2


@pytest.mark.asyncio
async def test_collect_routes_failures_to_dlq(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.search_query = AsyncMock(side_effect=RuntimeError("kaboom"))
    coll.send_to_dlq = AsyncMock()

    await coll.collect(["q1"])
    coll.send_to_dlq.assert_awaited_once()
    args = coll.send_to_dlq.await_args.args
    assert args[0] == "q1" and "kaboom" in args[2]


# ── cleanup ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_is_a_noop(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    assert await coll.cleanup() is None
