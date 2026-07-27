"""Tests for src/collectors/website.py — Wave 2 generic site crawler.

Pure-unit. No httpx network, no playwright launch, no PIL gating that would
require real image bytes. Every external call (HTTP GET, robots.txt fetch,
DB pool acquire, render) is faked.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Force a writable drive path BEFORE importing the collector so DRIVE_PATH
# resolves under tmp.
os.environ.setdefault("COLLECTOR_DRIVE_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "uc_test_media"))

from src.collectors import website as website_mod  # noqa: E402
from src.collectors.website import (  # noqa: E402
    WebsiteCollector,
    _RobotsCache,
    _categorize_link,
    _is_image_url,
    _is_pdf_url,
    _is_social_media,
    _normalize_url,
    _registrable_domain,
    _same_domain,
)


# ── fake pool helpers (mirrors test_matrix.py shape) ──────────────────────


def _make_pool(*, target_id: int | None = 99, executes_raise: bool = False):
    """Return an AsyncMock-backed asyncpg-shaped pool.

    `pool.acquire()` is an async context manager that yields a connection.
    The connection's .execute / .fetchrow are AsyncMocks the tests can
    inspect via `pool._conn`.
    """
    conn = MagicMock()
    if executes_raise:
        conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        conn.execute = AsyncMock(return_value=None)
    if target_id is None:
        conn.fetchrow = AsyncMock(return_value=None)
    else:
        conn.fetchrow = AsyncMock(return_value={"id": target_id})

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn
    return pool


def _make_collector(monkeypatch=None, **env) -> WebsiteCollector:
    """Construct a WebsiteCollector with default-friendly env."""
    if monkeypatch is not None:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    return WebsiteCollector()


@pytest.mark.asyncio
async def test_collect_crawls_targets_before_broad_discovery(monkeypatch):
    c = WebsiteCollector()
    c.checkpoint.save_progress = AsyncMock()
    events: list[tuple[str, str]] = []

    async def _spider(seed, **_kwargs):
        events.append(("target", seed))

    async def _discover(**_kwargs):
        events.append(("discovery", "sg"))

    monkeypatch.setattr(c, "spider_domain", _spider)
    monkeypatch.setattr(c, "_promote_discovered_sg_domains", _discover)

    await c.collect(["example.com"])

    assert events == [("target", "https://example.com"), ("discovery", "sg")]


@pytest.mark.asyncio
async def test_collect_marks_successful_target_completed(monkeypatch):
    c = WebsiteCollector()
    c.pool = _make_pool()
    c.checkpoint.save_progress = AsyncMock()

    async def _spider(seed, **_kwargs):
        return {
            "pages": 2,
            "images": 3,
            "pdfs": 1,
            "docs": 0,
            "videos": 0,
            "errors": 0,
            "skipped": 0,
        }

    monkeypatch.setattr(c, "spider_domain", _spider)
    monkeypatch.setattr(c, "_promote_discovered_sg_domains", AsyncMock())

    await c.collect(["https://example.com"])

    sql_calls = [call.args[0] for call in c.pool._conn.execute.await_args_list]
    assert any("status = 'completed'" in sql for sql in sql_calls)
    c.checkpoint.save_progress.assert_awaited_once_with("https://example.com")


@pytest.mark.asyncio
async def test_collect_demotes_timed_out_target(monkeypatch):
    monkeypatch.setenv("WEBSITE_TARGET_TIMEOUT_SECONDS", "0.01")
    c = WebsiteCollector()
    c.pool = _make_pool()
    c.checkpoint.save_progress = AsyncMock()
    c.send_to_dlq = AsyncMock()

    async def _spider(seed, **_kwargs):
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(c, "spider_domain", _spider)
    monkeypatch.setattr(c, "_promote_discovered_sg_domains", AsyncMock())

    await c.collect(["https://example.com"])

    sql_calls = [call.args[0] for call in c.pool._conn.execute.await_args_list]
    assert any("status = 'error'" in sql and "priority = LEAST" in sql for sql in sql_calls)
    c.checkpoint.save_progress.assert_awaited_once_with("https://example.com")
    c.send_to_dlq.assert_not_called()


@pytest.mark.asyncio
async def test_collect_checkpoints_failed_target_and_dlqs(monkeypatch):
    c = WebsiteCollector()
    c.pool = _make_pool()
    c.checkpoint.save_progress = AsyncMock()
    c.send_to_dlq = AsyncMock()

    async def _spider(seed, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(c, "spider_domain", _spider)
    monkeypatch.setattr(c, "_promote_discovered_sg_domains", AsyncMock())

    await c.collect(["https://example.com"])

    c.checkpoint.save_progress.assert_awaited_once_with("https://example.com")
    c.send_to_dlq.assert_awaited_once()


# ── module helpers (small pure functions) ─────────────────────────────────


def test_normalize_url_strips_fragment_and_default_port():
    assert _normalize_url("HTTP://Example.COM:80/path/?b=1#frag") == "http://example.com/path/?b=1"
    assert _normalize_url("https://Example.com:443/x") == "https://example.com/x"


def test_normalize_url_with_base_resolves_relative():
    out = _normalize_url("/foo", base="https://example.com/bar/")
    assert out == "https://example.com/foo"


def test_normalize_url_handles_garbage_input():
    # The helper swallows exceptions and returns the original — never raises.
    assert _normalize_url("") == ""


def test_registrable_domain_strips_www():
    assert _registrable_domain("www.example.com") == "example.com"
    assert _registrable_domain("example.com") == "example.com"


def test_same_domain_true_and_false():
    # www. is normalised away, but deeper subdomains stay distinct under
    # the simple eTLD+1 approximation in _registrable_domain.
    assert _same_domain("https://www.example.com/a", "https://example.com/b") is True
    assert _same_domain("https://example.com/", "https://other.org/") is False
    assert _same_domain("https://example.com/", "https://x.example.com/") is False


def test_is_social_media_known_and_unknown():
    assert _is_social_media("https://www.tiktok.com/@x") is True
    assert _is_social_media("https://t.me/xyz") is True
    assert _is_social_media("https://example.com/") is False


def test_is_image_and_pdf_extension_sniff():
    assert _is_image_url("https://x/y/cat.JPG") is True
    assert _is_image_url("https://x/y/index.html") is False
    assert _is_pdf_url("https://x/y/doc.PDF") is True
    assert _is_pdf_url("https://x/y/page.html") is False


def test_categorize_link_buckets():
    assert _categorize_link("https://example.com/photo.png") == "images"
    assert _categorize_link("https://example.com/file.pdf") == "pdfs"
    assert _categorize_link("https://example.com/about") == "internal_links"
    assert _categorize_link("https://tiktok.com/@x") == "social_media_skip"


# ── _RobotsCache ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_robots_cache_parses_disallow_and_sitemap():
    body = (
        "User-agent: *\n"
        "Disallow: /private\n"
        "Disallow: /admin/\n"
        "Sitemap: https://example.com/sitemap.xml\n"
    )
    resp = MagicMock(status_code=200, text=body)
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    cache = _RobotsCache()
    await cache.load("https://example.com/", client)

    assert cache.is_allowed("https://example.com/public/page") is True
    assert cache.is_allowed("https://example.com/private/x") is False
    assert cache.is_allowed("https://example.com/admin/y") is False
    assert "https://example.com/sitemap.xml" in cache.sitemaps_for("https://example.com/")


@pytest.mark.asyncio
async def test_robots_cache_swallows_404():
    resp = MagicMock(status_code=404, text="")
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    cache = _RobotsCache()
    await cache.load("https://example.com/", client)
    # 404 → empty rule list, everything allowed.
    assert cache.is_allowed("https://example.com/anything") is True


@pytest.mark.asyncio
async def test_robots_cache_load_idempotent_per_host():
    """Second load() for the same host must NOT refetch."""
    resp = MagicMock(status_code=200, text="User-agent: *\nDisallow: /x\n")
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    cache = _RobotsCache()
    await cache.load("https://example.com/", client)
    await cache.load("https://example.com/", client)
    assert client.get.await_count == 1


# ── constructor / env-driven config ───────────────────────────────────────


def test_constructor_defaults(monkeypatch):
    # Strip any leaked env so we're testing the documented defaults.
    for var in (
        "WEBSITE_MAX_DEPTH", "WEBSITE_MAX_PAGES", "WEBSITE_MAX_CONCURRENT_TASKS",
        "WEBSITE_RESPECT_ROBOTS", "WEBSITE_FOLLOW_EXTERNAL",
        "WEBSITE_DOWNLOAD_IMAGES", "WEBSITE_DOWNLOAD_PDFS",
        "WEBSITE_USE_TOR", "WEBSITE_USE_PLAYWRIGHT",
    ):
        monkeypatch.delenv(var, raising=False)

    c = WebsiteCollector()
    assert c.SOURCE_NAME == "website"
    assert c._max_depth == 3
    assert c._max_pages == 500
    assert c._max_concurrent == 5
    assert c._respect_robots is True
    assert c._follow_external is False
    assert c._download_images is True
    assert c._download_pdfs is True
    assert c._use_tor is False
    assert c._use_playwright is False


def test_constructor_honours_env_overrides(monkeypatch):
    monkeypatch.setenv("WEBSITE_MAX_DEPTH", "7")
    monkeypatch.setenv("WEBSITE_MAX_PAGES", "42")
    monkeypatch.setenv("WEBSITE_RESPECT_ROBOTS", "0")
    monkeypatch.setenv("WEBSITE_FOLLOW_EXTERNAL", "1")
    monkeypatch.setenv("WEBSITE_DOWNLOAD_IMAGES", "0")
    monkeypatch.setenv("WEBSITE_DOWNLOAD_PDFS", "0")
    c = WebsiteCollector()
    assert c._max_depth == 7
    assert c._max_pages == 42
    assert c._respect_robots is False
    assert c._follow_external is True
    assert c._download_images is False
    assert c._download_pdfs is False


# ── _extract_metadata / _extract_links / _extract_images ──────────────────


HTML_RICH = """
<!doctype html>
<html><head>
  <title>  My Page Title  </title>
  <meta name="description" content="meta-desc-here"/>
  <meta property="og:image" content="https://cdn.example.com/og.jpg"/>
  <link rel="canonical" href="https://example.com/canon"/>
</head>
<body>
  <a href="/about">About</a>
  <a href="https://other.com/x">External</a>
  <a href="javascript:void(0)">JS</a>
  <a href="https://example.com/file.pdf">PDF link</a>
  <img src="/img/a.jpg" alt="Alpha"/>
  <img data-src="https://example.com/img/b.png" alt="Beta"/>
  <img srcset="https://example.com/img/c-1x.jpg 1x, https://example.com/img/c-2x.jpg 2x" alt="Charlie"/>
  <picture><source srcset="https://example.com/img/d.webp"/></picture>
  <div style='background-image: url("https://example.com/img/e.jpg")'></div>
</body></html>
"""


def test_extract_metadata_basic():
    c = WebsiteCollector()
    meta = c._extract_metadata(HTML_RICH)
    assert meta.get("title") == "My Page Title"
    assert meta.get("description") == "meta-desc-here"
    assert meta.get("canonical") == "https://example.com/canon"


def test_extract_links_skips_js_and_resolves_relatives():
    c = WebsiteCollector()
    links = c._extract_links(HTML_RICH, "https://example.com/")
    # Relative resolved
    assert "https://example.com/about" in links
    # External preserved
    assert "https://other.com/x" in links
    # PDF link surfaces (categorisation happens elsewhere)
    assert "https://example.com/file.pdf" in links
    # javascript: URL filtered out
    assert not any(u.startswith("javascript:") for u in links)


def test_extract_images_multi_source():
    c = WebsiteCollector()
    imgs = c._extract_images(HTML_RICH, "https://example.com/")
    urls = {i["url"] for i in imgs}
    sources = {i["source"] for i in imgs}
    # img tag relative
    assert "https://example.com/img/a.jpg" in urls
    # data-src lazy
    assert "https://example.com/img/b.png" in urls
    # srcset entries
    assert "https://example.com/img/c-1x.jpg" in urls
    assert "https://example.com/img/c-2x.jpg" in urls
    # picture/source
    assert "https://example.com/img/d.webp" in urls
    # inline-style background
    assert "https://example.com/img/e.jpg" in urls
    # og:image
    assert "https://cdn.example.com/og.jpg" in urls
    assert {"img_tag", "srcset", "picture_source", "inline_style", "og_image"} <= sources


# ── _parse_sitemap ────────────────────────────────────────────────────────


def test_parse_sitemap_splits_urls_vs_child_sitemaps():
    xml = """
    <urlset>
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/b</loc></url>
      <sitemap><loc>https://example.com/sitemap-2.xml</loc></sitemap>
    </urlset>
    """
    c = WebsiteCollector()
    urls, sitemaps = c._parse_sitemap(xml)
    assert "https://example.com/a" in urls
    assert "https://example.com/b" in urls
    assert "https://example.com/sitemap-2.xml" in sitemaps


def test_parse_sitemap_handles_garbage():
    c = WebsiteCollector()
    urls, sitemaps = c._parse_sitemap("not<xml")
    assert urls == [] and sitemaps == []


# ── fetch_url ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_url_url_filter_blocks(monkeypatch):
    c = WebsiteCollector()
    # Force the URL filter to reject everything.
    c._url_filter = MagicMock()
    c._url_filter.is_allowed = MagicMock(return_value=(False, "blocked"))

    out = await c.fetch_url("https://example.com/")
    assert out is None


@pytest.mark.asyncio
async def test_fetch_url_robots_disallow_returns_none(monkeypatch):
    c = WebsiteCollector()
    c._respect_robots = True
    # Bypass URL filter
    c._url_filter = MagicMock()
    c._url_filter.is_allowed = MagicMock(return_value=(True, ""))

    fake_client = MagicMock()
    fake_client.get = AsyncMock()
    fake_client.aclose = AsyncMock()
    monkeypatch.setattr(c, "_build_client", lambda domain: fake_client)

    # Robots cache: load is no-op, is_allowed → False
    c._robots = MagicMock()
    c._robots.load = AsyncMock()
    c._robots.is_allowed = MagicMock(return_value=False)

    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())

    out = await c.fetch_url("https://example.com/blocked")
    assert out is None
    # We must not have issued the GET — robots blocked us first.
    fake_client.get.assert_not_called()
    fake_client.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_fetch_url_happy_path_returns_response(monkeypatch):
    c = WebsiteCollector()
    c._respect_robots = False
    c._use_playwright = False
    c._url_filter = MagicMock()
    c._url_filter.is_allowed = MagicMock(return_value=(True, ""))

    resp = MagicMock(status_code=200)
    resp.headers = {"content-type": "text/html"}
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=resp)
    fake_client.aclose = AsyncMock()
    monkeypatch.setattr(c, "_build_client", lambda domain: fake_client)
    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())

    out = await c.fetch_url("https://example.com/")
    assert out is resp
    fake_client.get.assert_awaited_once_with("https://example.com/")
    fake_client.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_fetch_url_swallows_get_exception(monkeypatch):
    c = WebsiteCollector()
    c._respect_robots = False
    c._url_filter = MagicMock()
    c._url_filter.is_allowed = MagicMock(return_value=(True, ""))

    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=RuntimeError("boom"))
    fake_client.aclose = AsyncMock()
    monkeypatch.setattr(c, "_build_client", lambda domain: fake_client)
    monkeypatch.setattr(c, "wait_rate_limit", AsyncMock())

    out = await c.fetch_url("https://example.com/")
    assert out is None  # never raises
    fake_client.aclose.assert_awaited()


# ── extract_media ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_media_returns_images_and_pdfs(monkeypatch):
    c = WebsiteCollector()
    resp = MagicMock(status_code=200, text=HTML_RICH)
    resp.headers = {"content-type": "text/html"}
    monkeypatch.setattr(c, "fetch_url", AsyncMock(return_value=resp))

    out = await c.extract_media("https://example.com/")
    assert isinstance(out, dict)
    assert "images" in out and "pdfs" in out
    img_urls = {i["url"] for i in out["images"]}
    assert "https://example.com/img/a.jpg" in img_urls
    assert "https://example.com/file.pdf" in out["pdfs"]


@pytest.mark.asyncio
async def test_extract_media_returns_empty_on_fetch_failure(monkeypatch):
    c = WebsiteCollector()
    monkeypatch.setattr(c, "fetch_url", AsyncMock(return_value=None))
    out = await c.extract_media("https://example.com/")
    assert out == {"images": [], "pdfs": []}


@pytest.mark.asyncio
async def test_extract_media_returns_empty_for_non_html(monkeypatch):
    c = WebsiteCollector()
    resp = MagicMock(status_code=200, text="binary")
    resp.headers = {"content-type": "application/octet-stream"}
    monkeypatch.setattr(c, "fetch_url", AsyncMock(return_value=resp))
    out = await c.extract_media("https://example.com/x")
    assert out == {"images": [], "pdfs": []}


# ── DB persistence: _upsert_target / _upsert_page ─────────────────────────


@pytest.mark.asyncio
async def test_upsert_target_noop_without_pool():
    c = WebsiteCollector()
    c.pool = None
    # Must not raise.
    await c._upsert_target("example.com", "https://example.com/")


@pytest.mark.asyncio
async def test_upsert_target_executes_sql_when_pool_set():
    c = WebsiteCollector()
    c.pool = _make_pool()
    await c._upsert_target("example.com", "https://example.com/")
    c.pool._conn.execute.assert_awaited_once()
    # Ensure args carried through.
    args = c.pool._conn.execute.await_args.args
    assert "example.com" in args
    assert "https://example.com/" in args


@pytest.mark.asyncio
async def test_upsert_target_swallows_db_error():
    c = WebsiteCollector()
    c.pool = _make_pool(executes_raise=True)
    # Must not propagate.
    await c._upsert_target("example.com", "https://example.com/")


@pytest.mark.asyncio
async def test_upsert_page_skips_when_target_missing():
    c = WebsiteCollector()
    c.pool = _make_pool(target_id=None)
    await c._upsert_page(
        domain="example.com",
        url="https://example.com/p",
        html="<html></html>",
        status=200,
        title="t",
        description="d",
        content_text="body text",
        images=[],
        internal_links=[],
        external_links=[],
    )
    # fetchrow returned None → no INSERT issued.
    c.pool._conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upsert_page_inserts_with_target_id():
    c = WebsiteCollector()
    c.pool = _make_pool(target_id=42)
    await c._upsert_page(
        domain="example.com",
        url="https://example.com/p",
        html="<html>x</html>",
        status=200,
        title="t",
        description="d",
        content_text="body text",
        images=[{"url": "https://example.com/img/x.jpg"}],
        internal_links=["https://example.com/a"],
        external_links=["https://other.com/b"],
    )
    c.pool._conn.execute.assert_awaited_once()


# ── download_media ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_media_writes_vault_blob(monkeypatch, tmp_path):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(website_mod, "VAULT_ROOT", vault_root)
    c = WebsiteCollector()
    c.insert_media_item = AsyncMock(return_value=True)
    c.send_to_dlq = AsyncMock()

    data = b"website image bytes"
    digest = hashlib.sha256(data).hexdigest()

    await c.download_media({
        "entity_id": "example.com",
        "entity_name": "example.com",
        "content_type": "image",
        "content_id": "img1",
        "extension": "jpg",
        "url": "https://example.com/img1.jpg",
        "source_url": "https://example.com/page",
        "alt": "Example",
        "width": 640,
        "height": 480,
        "data": data,
        "raw": {"page": "https://example.com/page"},
    })

    kwargs = c.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == "https://example.com/page"
    assert kwargs["width"] == 640
    assert kwargs["height"] == 480
    assert kwargs["metadata"]["raw"] == {"page": "https://example.com/page"}
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    assert kwargs["metadata"]["vault_artifact"]["blob_path"].startswith("media/blobs/")
    c.send_to_dlq.assert_not_awaited()


class _VideoStreamResponse:
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
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(website_mod, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(website_mod, "assert_media_write_allowed", lambda *_a, **_kw: None)
    c = WebsiteCollector()
    c.insert_media_item = AsyncMock(return_value=True)
    c.send_to_dlq = AsyncMock()

    data = b"video-chunk-1video-chunk-2"
    video_url = "https://example.com/video.mp4"
    client = MagicMock()
    client.stream = MagicMock(return_value=_VideoStreamResponse([data[:13], data[13:]]))

    ok = await c._stream_video(client, video_url, "example.com")

    assert ok is True
    cid = hashlib.sha256(video_url.encode("utf-8")).hexdigest()[:16]
    digest = hashlib.sha256(data).hexdigest()
    kwargs = c.insert_media_item.await_args.kwargs
    stored_path = Path(kwargs["file_path"])
    assert kwargs["content_id"] == cid
    assert stored_path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.mp4"
    assert stored_path.read_bytes() == data
    assert kwargs["sha256"] == digest
    assert kwargs["file_size"] == len(data)
    assert kwargs["source_url"] == video_url
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert kwargs["metadata"]["vault_artifact"]["partial"] is False
    c.send_to_dlq.assert_not_awaited()


# ── cleanup / module exports ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_is_noop():
    c = WebsiteCollector()
    assert await c.cleanup() is None


def test_module_exports():
    assert "WebsiteCollector" in website_mod.__all__
