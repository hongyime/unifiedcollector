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

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collectors import search as search_mod
from src.collectors.search.bucket_crawler import expand_dorks
from src.collectors.search import SearchCollector


# ── helpers ────────────────────────────────────────────────────────────────


def _set_clean_env(monkeypatch):
    """Reset all SEARCH_* / SERPER_* env so each test starts deterministic."""
    for var in (
        "SEARCH_MAX_RESULTS", "SEARCH_MIN_DIMENSION", "SEARCH_MIN_FILE_SIZE",
        "SEARCH_DOWNLOAD_IMAGES", "SEARCH_SPIDER_PAGES", "SEARCH_BING_PAGES",
        "SEARCH_SERPER_THRESHOLD", "SEARCH_CONCURRENT_DOWNLOADS",
        "SEARCH_CACHE_TTL_HOURS", "SEARCH_MAX_PDF_PAGES",
        "SEARCH_DOMAIN_DELAY_SECONDS", "SEARCH_DOMAIN_JITTER_SECONDS",
        "SEARCH_RATE_LIMIT_COOLDOWN_SECONDS", "SEARCH_RATE_LIMIT_MAX_COOLDOWN_SECONDS",
        "SEARCH_DYNAMIC_TERMS_ENABLED", "SEARCH_DYNAMIC_TERM_CAP",
        "SEARCH_DYNAMIC_TERM_TTL_DAYS", "SEARCH_DYNAMIC_REFRESH_HOURS",
        "SEARCH_DYNAMIC_QUERY_INTERVAL_DAYS", "SEARCH_DOCUMENT_INDEX_ENABLED",
        "SEARCH_DYNAMIC_SOURCE_ROWS", "SEARCH_MAX_DOCUMENT_TEXT_CHARS",
        "SEARCH_MAX_DOCUMENT_ARCHIVE_BYTES",
        "SERPER_API_KEY", "SERPER_DAILY_QUOTA",
    ):
        monkeypatch.delenv(var, raising=False)
    # Disable downloads-by-default so finalise_query doesn't go fetch URLs.
    monkeypatch.setenv("SEARCH_DOWNLOAD_IMAGES", "0")
    monkeypatch.setenv("SEARCH_SPIDER_PAGES", "0")
    monkeypatch.setenv("SEARCH_DOMAIN_DELAY_SECONDS", "0")
    monkeypatch.setenv("SEARCH_DOMAIN_JITTER_SECONDS", "0")
    monkeypatch.setenv("SEARCH_DYNAMIC_TERMS_ENABLED", "0")


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


@pytest.mark.asyncio
async def test_search_scope_cooldown_is_exact(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()

    coll._set_search_scope_cooldown("bing", "bing.com", 60)

    assert coll._search_scope_cooling_down("bing", "bing.com") is True
    assert coll._search_scope_cooling_down("spider", "bing.com") is False
    assert coll._search_scope_cooling_down("bing", "example.com") is False


@pytest.mark.asyncio
async def test_record_search_rate_limit_defaults_cooldown(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_RATE_LIMIT_COOLDOWN_SECONDS", "123")
    coll = SearchCollector()
    record_event = AsyncMock()
    monkeypatch.setattr(search_mod, "record_rate_limit_event", record_event)

    await coll._record_search_rate_limit(
        engine="bing",
        scope="bing.com",
        status_code=429,
        reason="bing search returned 429",
    )

    record_event.assert_awaited_once()
    assert record_event.await_args.kwargs["cooldown_seconds"] == 123
    assert coll._search_scope_cooldown_remaining("bing", "bing.com") > 100
    await coll._record_search_rate_limit(
        engine="bing",
        scope="bing.com",
        status_code=429,
        reason="bing search returned 429",
    )
    assert record_event.await_count == 2
    assert record_event.await_args.kwargs["cooldown_seconds"] == 246


@pytest.mark.asyncio
async def test_retry_search_after_429_skips_second_request(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    fetch = AsyncMock()

    out = await coll._retry_search_after_429(
        fetch,
        engine="bing",
        scope="bing.com",
        reason="bing search returned 429",
        metadata={"query": "q"},
    )

    assert out is None
    fetch.assert_not_awaited()


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
async def test_search_query_records_serper_quota_exhaustion(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SERPER_API_KEY", "abcd1234efgh")
    coll = SearchCollector()
    coll._DDGS = None
    coll._BS = None
    coll._serper_has_quota = AsyncMock(return_value=False)
    coll._search_serper = AsyncMock(return_value=[])
    coll._record_search_rate_limit = AsyncMock()

    out = await coll.search_query("q", engines=("serper",))

    assert out == []
    coll._search_serper.assert_not_called()
    coll._record_search_rate_limit.assert_awaited_once_with(
        engine="serper",
        account="abcd1234",
        scope="serper_quota",
        status_code=None,
        reason="serper account quota exhausted",
        metadata={
            "query": "q",
            "daily_quota": 2500,
        },
    )


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


@pytest.mark.asyncio
async def test_search_ddg_uses_one_backend_per_request(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    seen = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def text(self, query, **kwargs):
            seen.append(("text", kwargs["backend"]))
            return []

        def images(self, query, **kwargs):
            seen.append(("images", kwargs["backend"]))
            return []

    coll._DDGS = FakeDDGS
    coll.wait_rate_limit = AsyncMock()

    await coll._search_ddg("q")

    assert seen == [("text", "yahoo"), ("images", "bing")]


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


@pytest.mark.asyncio
async def test_search_bing_does_not_retry_429_and_respects_retry_after(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    coll._cache.put = MagicMock()
    coll.wait_rate_limit = AsyncMock()
    coll._BS = MagicMock(return_value=MagicMock(select=MagicMock(return_value=[])))
    record_event = AsyncMock()
    monkeypatch.setattr(search_mod, "record_rate_limit_event", record_event)
    first = MagicMock(status_code=429, headers={"retry-after": "1200"})
    client = MagicMock()
    client.get = AsyncMock(return_value=first)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(coll, "_make_client", return_value=client):
        out = await coll._search_bing("q", num_pages=1)

    assert out == []
    assert client.get.await_count == 1
    record_event.assert_awaited_once()
    assert record_event.await_args.kwargs["cooldown_seconds"] == 1200
    assert coll._search_scope_cooldown_remaining("bing", "bing.com") > 1100
    assert coll._last_search_query_rate_limited is True


@pytest.mark.asyncio
async def test_search_bing_sets_exponential_cooldown_after_429(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_RATE_LIMIT_COOLDOWN_SECONDS", "91")
    coll = SearchCollector()
    coll._cache.get = MagicMock(return_value=None)
    coll.wait_rate_limit = AsyncMock()
    coll._BS = MagicMock()
    record_event = AsyncMock()
    monkeypatch.setattr(search_mod, "record_rate_limit_event", record_event)
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=429, headers={}))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch.object(coll, "_make_client", return_value=client):
        out = await coll._search_bing("q", num_pages=1)

    assert out == []
    assert client.get.await_count == 1
    record_event.assert_awaited_once()
    assert record_event.await_args.kwargs["source"] == "search"
    assert record_event.await_args.kwargs["scope"] == "bing.com"
    assert record_event.await_args.kwargs["cooldown_seconds"] == 91
    assert coll._search_scope_cooldown_remaining("bing", "bing.com") > 80


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
    monkeypatch.setenv("SEARCH_SPIDER_DEPTH", "0")
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
    # One execute call for the query upsert. Results use INSERT .. RETURNING
    # so duplicate URL conflicts can be observed as no-progress.
    assert pool._conn.execute.await_count == 1
    # Four fetchrow calls: query lookup + insert-returning for each result row.
    assert pool._conn.fetchrow.await_count == 4
    assert coll.progress_count == 2


@pytest.mark.asyncio
async def test_finalise_query_does_not_count_duplicate_results_as_progress(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    # _upsert_result calls fetchrow twice per hit: query lookup then
    # INSERT ... ON CONFLICT DO NOTHING RETURNING id. The second hit conflicts.
    pool._conn.fetchrow.side_effect = [
        {"id": "query-uuid"}, {"id": "result-1"},
        {"id": "query-uuid"}, None,
    ]

    hits = [
        {"url": "https://a.com/1", "title": "A", "snippet": "s", "rank": 1},
        {"url": "https://a.com/1", "title": "A again", "snippet": "s", "rank": 2},
    ]
    out = await coll._finalise_query("q", hits)

    assert out == hits
    assert pool._conn.fetchrow.await_count == 4
    assert coll.progress_count == 1


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


class _FakeImage:
    size = (320, 240)
    mode = "RGB"

    def save(self, out, **_kwargs):
        out.write(b"encoded search image")


class _FakePIL:
    @staticmethod
    def open(_data):
        return _FakeImage()


@pytest.mark.asyncio
async def test_save_image_writes_vault_blob(monkeypatch, tmp_path):
    _set_clean_env(monkeypatch)
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(search_mod, "VAULT_ROOT", vault_root)
    coll = SearchCollector()
    coll._PIL = _FakePIL()
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()
    digest = hashlib.sha256(b"encoded search image").hexdigest()

    ok = await coll._save_image(
        "query-id",
        "query name",
        "imgcid",
        b"raw image bytes",
        "https://example.com/page",
    )

    assert ok is True
    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == b"encoded search image"
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(b"encoded search image")
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    coll.send_to_dlq.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_document_writes_vault_blob(monkeypatch, tmp_path):
    _set_clean_env(monkeypatch)
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(search_mod, "VAULT_ROOT", vault_root)
    coll = SearchCollector()
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()
    data = b"search document bytes"
    digest = hashlib.sha256(data).hexdigest()

    ok = await coll._save_document(
        "query-id",
        "query name",
        "doccid",
        data,
        "https://example.com/doc.txt",
        ".txt",
    )

    assert ok is True
    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.txt"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://example.com/doc.txt"
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    coll.send_to_dlq.assert_not_awaited()


def test_dynamic_term_extraction_is_balanced_and_capped():
    rows = [
        {
            "platform": "instagram",
            "source_reference": "profile-1",
            "username": "Alice.Photo",
            "full_name": "Alice Example",
            "text_blob": "Weekend photography and wildlife photography in Singapore",
            "hashtags": ["#wildlifephoto", "#singaporetrip"],
        },
        {
            "platform": "tiktok",
            "source_reference": "post-2",
            "username": "bob_travels",
            "text_blob": "Rare orchids growing beside the trail",
            "hashtags": ["orchidwatch"],
        },
    ]

    terms = SearchCollector._extract_dynamic_term_candidates(rows, limit=10)
    by_kind = {kind: [row["term"] for row in terms if row["term_kind"] == kind]
               for kind in ("username", "full_name", "hashtag", "keyword")}

    assert len(terms) <= 10
    assert "alice.photo" in by_kind["username"]
    assert "alice example" in by_kind["full_name"]
    assert "wildlifephoto" in by_kind["hashtag"]
    assert "photography" in by_kind["keyword"]
    assert "orchids" in by_kind["keyword"]


def test_dynamic_term_sources_cover_additional_social_profiles_and_posts():
    source_names = {name for name, _query in search_mod._DYNAMIC_TERM_SOURCE_QUERIES}

    assert {
        "facebook_profiles",
        "x_profiles",
        "bluesky_profiles",
        "x_posts",
        "youtube_community_posts",
        "pinterest_boards",
        "github_users",
        "github_repos",
        "strava_athletes",
        "strava_activities",
        "airbnb_hosts",
        "airbnb_listings",
        "airbnb_reviews",
        "snapchat_stories",
        "snapchat_spotlights",
        "snapchat_highlights",
        "telegram_chats",
        "paypal_profiles",
    } <= source_names


def test_dynamic_dorks_include_approved_person_and_document_queries():
    queries = search_mod._expand_dynamic_dorks("alice example")

    assert queries[:2] == [
        '"alice example" filetype:pdf',
        'filetype:pdf "alice example"',
    ]
    assert '"alice example" filetype:pdf' in queries
    assert '"alice example" (site:github.com OR site:linkedin.com OR site:twitter.com)' in queries
    assert 'filetype:pdf "alice example"' in queries
    assert '"alice example" filetype:(pdf OR docx OR xlsx OR pptx)' in queries
    assert '"alice example" filetype:pdf resume' in queries
    assert 'site:target.com filetype:pdf "alice example"' in queries


@pytest.mark.asyncio
async def test_collect_runs_dynamic_dork_queries_before_static_targets(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    coll.search_query = AsyncMock(return_value=[])
    coll.checkpoint.save_progress = AsyncMock()
    coll._prepare_dynamic_search_queries = AsyncMock(return_value=[{
        "term": "alice",
        "query": 'site:s3.amazonaws.com alice',
        "next_template_cursor": 1,
    }])
    coll._mark_dynamic_search_query = AsyncMock()

    await coll.collect(["static query"])

    assert [call.args[0] for call in coll.search_query.await_args_list] == [
        'site:s3.amazonaws.com alice', "static query",
    ]
    coll._mark_dynamic_search_query.assert_awaited_once_with(
        coll._prepare_dynamic_search_queries.return_value[0],
        result_count=0,
        completed=True,
    )


@pytest.mark.asyncio
async def test_collect_advances_dynamic_terms_before_result_postprocessing(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_QUERY_DELAY_SECONDS", "0")
    coll = SearchCollector()
    events = []
    item = {
        "term": "alice",
        "query": 'site:s3.amazonaws.com alice',
        "next_template_cursor": 1,
    }
    item2 = {
        "term": "bob",
        "query": 'site:s3.amazonaws.com bob',
        "next_template_cursor": 1,
    }
    hits = [{"url": "https://example.org/"}]

    async def search(_query, *, postprocess=True):
        events.append(("search", postprocess))
        return hits

    async def mark(_item, *, result_count, completed):
        events.append(("mark", result_count, completed))

    async def postprocess(_query, _hits):
        events.append(("postprocess",))

    coll.search_query = AsyncMock(side_effect=search)
    coll._prepare_dynamic_search_queries = AsyncMock(return_value=[item, item2])
    coll._mark_dynamic_search_query = AsyncMock(side_effect=mark)
    coll._postprocess_query_results = AsyncMock(side_effect=postprocess)
    coll.checkpoint.save_progress = AsyncMock()
    coll.heartbeat_source_health = AsyncMock()

    await coll.collect([])

    assert events == [
        ("search", False),
        ("mark", 1, True),
        ("search", False),
        ("mark", 1, True),
        ("postprocess",),
        ("postprocess",),
    ]


@pytest.mark.asyncio
async def test_collect_retries_dynamic_query_after_rate_limit(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    async def rate_limited(_query, *, postprocess=True):
        coll._last_search_query_rate_limited = True
        return []

    coll.search_query = AsyncMock(side_effect=rate_limited)
    coll.checkpoint.save_progress = AsyncMock()
    item = {
        "term": "alice",
        "query": 'site:s3.amazonaws.com alice',
        "next_template_cursor": 1,
    }
    coll._prepare_dynamic_search_queries = AsyncMock(return_value=[item])
    coll._mark_dynamic_search_query = AsyncMock()

    await coll.collect([])

    coll._mark_dynamic_search_query.assert_awaited_once_with(
        item, result_count=0, completed=False,
    )


@pytest.mark.asyncio
async def test_dynamic_search_query_uses_rotating_approved_dork(monkeypatch):
    _set_clean_env(monkeypatch)
    monkeypatch.setenv("SEARCH_DYNAMIC_TERMS_ENABLED", "1")
    monkeypatch.setenv("SEARCH_DYNAMIC_TERM_CAP", "1")
    coll = SearchCollector()
    pool = _make_pool()
    # The verified-term cleanup marker already exists on this recurring cycle.
    pool._conn.fetchval = AsyncMock(return_value=None)

    async def fetch(query, *args, **kwargs):
        if "FROM search_dynamic_terms" in query:
            return [{
                "term": "alice",
                "term_kind": "username",
                "source_platform": "instagram",
                "source_reference": "profile-1",
                "template_cursor": 2,
            }]
        return []

    pool._conn.fetch = AsyncMock(side_effect=fetch)
    coll.set_pool(pool)

    items = await coll._prepare_dynamic_search_queries()

    assert len(items) == 1
    assert items[0]["query"] == search_mod._expand_dynamic_dorks("alice")[2]
    assert items[0]["next_template_cursor"] == 3


@pytest.mark.asyncio
async def test_dynamic_term_refresh_runs_each_collection_cycle_by_default(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    pool._conn.fetch = AsyncMock(return_value=[])
    pool._conn.fetchval = AsyncMock(return_value=None)
    coll.set_pool(pool)

    await coll._refresh_dynamic_search_terms(pool._conn)

    assert pool._conn.fetchval.await_count == 1
    assert pool._conn.fetchval.await_args.args[1] == search_mod._DYNAMIC_TERM_BADGE_SCRUB_CURSOR
    assert pool._conn.fetch.await_count == len(search_mod._DYNAMIC_TERM_SOURCE_QUERIES)
    assert any("FROM airbnb_listings" in call.args[0] for call in pool._conn.fetch.await_args_list)


@pytest.mark.asyncio
async def test_dynamic_query_result_update_tracks_zero_results(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    item = {"term": "alice", "next_template_cursor": 4}

    await coll._mark_dynamic_search_query(item, result_count=0, completed=True)

    sql, *args = pool._conn.execute.await_args.args
    assert "zero_result_since = CASE" in sql
    assert "total_result_count = total_result_count" in sql
    assert args[0] == "alice"
    assert args[1] is True
    assert args[3] == 4
    assert args[4] == 0


@pytest.mark.asyncio
async def test_search_query_releases_shared_mutex_before_finalisation(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    events = []
    lock_held = False

    async def acquire(source):
        nonlocal lock_held
        lock_held = True
        events.append(("acquire", source))
        return "lock-handle"

    async def release(handle):
        nonlocal lock_held
        lock_held = False
        events.append(("release", handle))

    async def search(_query, **_kwargs):
        events.append(("search", lock_held))
        return [{"url": "https://example.org/"}]

    async def finalise(_query, hits, *, postprocess=True):
        events.append(("finalise", lock_held))
        return hits

    coll._collect_mutex_acquire = acquire
    coll._collect_mutex_release = release
    coll._DDGS = None
    coll._BS = None
    coll._serper_api_key = ""
    coll._finalise_query = AsyncMock(side_effect=finalise)
    coll._search_query_unlocked = AsyncMock(side_effect=search)

    result = await coll.search_query("public term")

    assert result == [{"url": "https://example.org/"}]
    assert events == [
        ("acquire", "search"),
        ("search", True),
        ("release", "lock-handle"),
        ("finalise", False),
    ]
    coll._search_query_unlocked.assert_awaited_once()
    coll._finalise_query.assert_awaited_once_with(
        "public term", [{"url": "https://example.org/"}], postprocess=True,
    )


def test_document_text_extraction_handles_text_rtf_and_docx():
    import io
    import zipfile

    text, method, page_count = SearchCollector._extract_document_text(
        b"Searchable plain text", ".txt"
    )
    assert text == "Searchable plain text"
    assert method == "text" and page_count is None

    rtf, method, _ = SearchCollector._extract_document_text(
        b"{\\rtf1\nPublic \\b document\\b0 text\\par}", ".rtf"
    )
    assert "Public" in rtf and "document" in rtf and "\\rtf" not in rtf
    assert method == "rtf"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:word"><w:t>Public report text</w:t></w:document>',
        )
    docx_text, method, _ = SearchCollector._extract_document_text(
        buffer.getvalue(), ".docx"
    )
    assert "Public report text" in docx_text
    assert method == "openxml"


def test_document_text_extraction_bounds_actual_zip_member_reads(monkeypatch):
    import io
    import zipfile

    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:word"><w:t>small</w:t></w:document>',
        )

    oversized_xml = (
        b'<w:document xmlns:w="urn:word"><w:t>'
        + b"x" * 2_000_000
        + b"</w:t></w:document>"
    )
    bytes_read = 0
    original_open = zipfile.ZipFile.open

    class CountingReader(io.BytesIO):
        def read(self, size=-1):
            nonlocal bytes_read
            chunk = super().read(size)
            bytes_read += len(chunk)
            return chunk

    def open_member(self, name, mode="r", pwd=None, *, force_zip64=False):
        if getattr(name, "filename", name) == "word/document.xml":
            return CountingReader(oversized_xml)
        return original_open(self, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", open_member)
    _text, _method, _pages = SearchCollector._extract_document_text(
        archive_data.getvalue(), ".docx", max_archive_bytes=1_000_000
    )

    assert bytes_read <= 1_000_000


def test_document_extension_can_be_inferred_from_content_type():
    assert search_mod._document_extension_from_content_type(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document; charset=binary"
    ) == ".docx"


def test_document_text_extraction_keeps_metadata_path_for_corrupt_zip(monkeypatch):
    import io
    import zipfile
    import zlib

    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
    original_open = zipfile.ZipFile.open

    def open_member(self, name, mode="r", pwd=None, *, force_zip64=False):
        if getattr(name, "filename", name) == "word/document.xml":
            raise zlib.error("invalid compressed data")
        return original_open(self, name, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", open_member)
    text, method, page_count = SearchCollector._extract_document_text(
        archive_data.getvalue(), ".docx"
    )

    assert text == ""
    assert method == "metadata_only"
    assert page_count is None


@pytest.mark.asyncio
async def test_document_index_writes_extracted_text_and_metadata(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    pool._conn.fetchval = AsyncMock(return_value="media-id")
    coll.set_pool(pool)

    await coll._index_downloaded_document(
        content_id="doc-1",
        data=b"Document body for text index",
        extension=".txt",
        source_url="https://example.com/report.txt",
        title="Annual report",
        snippet="Public report",
        search_query="annual report",
    )

    sql, *args = pool._conn.execute.await_args.args
    assert "INSERT INTO search_documents" in sql
    assert args[4] == hashlib.sha256(b"Document body for text index").hexdigest()
    assert args[5] == "Document body for text index"
    assert args[6] == "text"
    assert '"search_query": "annual report"' in args[8]


@pytest.mark.asyncio
async def test_image_metadata_is_written_to_search_documents(monkeypatch):
    _set_clean_env(monkeypatch)
    coll = SearchCollector()
    pool = _make_pool()
    pool._conn.fetchval = AsyncMock(return_value="media-id")
    coll.set_pool(pool)

    await coll._index_search_document(
        content_id="image-1",
        data=b"jpeg bytes",
        extension=".jpg",
        source_url="https://example.com/photo.jpg",
        title="photo",
        content_text="",
        extraction_method="pillow_image_metadata",
        page_count=None,
        metadata={"image": {"width": 640, "height": 480}},
    )

    sql, *args = pool._conn.execute.await_args.args
    assert "INSERT INTO search_documents" in sql
    assert args[6] == "pillow_image_metadata"
    assert '"width": 640' in args[8]


class _SearchVideoResponse:
    status_code = 200
    headers = {"content-type": "video/mp4"}

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def aiter_bytes(self, _chunk_size):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_stream_video_writes_vault_blob(monkeypatch, tmp_path):
    _set_clean_env(monkeypatch)
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(search_mod, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(search_mod, "assert_media_write_allowed", lambda *_a, **_kw: None)
    coll = SearchCollector()
    coll._min_file_size = 1
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.send_to_dlq = AsyncMock()

    data = b"search-video-1search-video-2"
    client = MagicMock()
    client.stream = MagicMock(return_value=_SearchVideoResponse([data[:14], data[14:]]))
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(coll, "_make_client", MagicMock(return_value=client_cm))

    ok = await coll._stream_video(
        "query name",
        "videocid",
        "https://example.com/video.mp4",
        "https://example.com/page",
    )

    assert ok is True
    digest = hashlib.sha256(data).hexdigest()
    kwargs = coll.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.mp4"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://example.com/page"
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    coll.send_to_dlq.assert_not_awaited()


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
