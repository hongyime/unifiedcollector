"""Unit tests for src/core/tiktok_browser.py.

Pure-unit. Mocks Playwright entirely — no real browser ever launches.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.core import tiktok_browser as tb_mod
from src.core.tiktok_browser import TikTokBrowserDownloader


@pytest.fixture(autouse=True)
def _disable_headless_dwell(monkeypatch):
    monkeypatch.setenv("HEADLESS_NAV_DWELL_ENABLED", "0")


# ── Fakes for the Playwright async API ──────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeRequestContext:
    def __init__(self, status: int = 200, body: bytes = b""):
        self._status = status
        self._body = body
        self.calls: list[str] = []

    async def get(self, url: str, **kw) -> _FakeResponse:
        self.calls.append(url)
        return _FakeResponse(self._status, self._body)


class _FakeElement:
    def __init__(self, attrs: dict[str, str] | None = None):
        self._attrs = attrs or {}

    async def get_attribute(self, name: str):
        return self._attrs.get(name)


class FakePage:
    """A minimal-but-controllable fake of playwright.async_api.Page."""

    def __init__(
        self,
        *,
        video_links: list[str] | None = None,
        goto_status: int = 200,
        goto_raises: bool = False,
        wait_for_selector_raises: bool = False,
        cdn_body: bytes = b"x" * 20_000,
        cdn_status: int = 200,
        request_url_to_capture: str | None = (
            "https://v19-webapp.tiktok.com/abc.mp4?mime_type=video"
        ),
    ):
        self._video_links = video_links or []
        self._goto_status = goto_status
        self._goto_raises = goto_raises
        self._wait_for_selector_raises = wait_for_selector_raises
        self.request = _FakeRequestContext(cdn_status, cdn_body)
        self._listeners: dict[str, list] = {}
        self._request_url_to_capture = request_url_to_capture
        self.evaluations: list[str] = []
        self.closed = False

    async def goto(self, url, **kw):
        if self._goto_raises:
            raise RuntimeError("goto exploded")
        # Fire any registered request listener with our captured CDN URL so
        # _download_video_via_page sees a hit during its 10s wait loop.
        if self._request_url_to_capture:
            for cb in self._listeners.get("request", []):
                cb(type("R", (), {"url": self._request_url_to_capture})())

        class _Resp:
            status = self._goto_status

        return _Resp()

    async def wait_for_selector(self, selector, **kw):
        if self._wait_for_selector_raises:
            raise RuntimeError("selector timeout")
        return _FakeElement()

    async def query_selector(self, selector):
        return _FakeElement({"src": "https://example.com/fallback.mp4"})

    async def query_selector_all(self, selector):
        if 'a[href*="/video/"]' in selector:
            return [_FakeElement({"href": h}) for h in self._video_links]
        return []

    async def evaluate(self, script, *args, **kw):
        self.evaluations.append(script)
        return None

    def on(self, event, cb):
        self._listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        try:
            self._listeners.get(event, []).remove(cb)
        except ValueError:
            pass

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page
        self.cookies_added: list[Any] = []
        self.init_scripts: list[str] = []
        self.closed = False

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def add_cookies(self, cookies):
        self.cookies_added.extend(cookies)

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, ctx: FakeContext):
        self._ctx = ctx
        self.closed = False
        self.launch_args: dict[str, Any] = {}

    async def new_context(self, **kw) -> FakeContext:
        return self._ctx

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser

    async def launch(self, **kw):
        self._browser.launch_args = kw
        return self._browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium):
        self.chromium = chromium


class FakePlaywrightCM:
    """Mimics ``async_playwright()`` — supports start()/__aexit__."""

    def __init__(self, pw: FakePlaywright):
        self._pw = pw
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True
        return self._pw

    async def __aexit__(self, exc_type, exc, tb):
        self.stopped = True


def _wire_fake_playwright(monkeypatch, page: FakePage):
    """Patch ``src.core.tiktok_browser`` so its lazy import returns our fakes."""
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    chromium = FakeChromium(browser)
    pw = FakePlaywright(chromium)
    cm = FakePlaywrightCM(pw)

    def _fake_async_playwright():
        return cm

    # Build a fake module exposing ``async_playwright``.
    import types
    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = _fake_async_playwright
    fake_root = types.ModuleType("playwright")
    fake_root.async_api = fake_async_api

    import sys
    monkeypatch.setitem(sys.modules, "playwright", fake_root)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    return cm, pw, browser, ctx


# ── Tests ───────────────────────────────────────────────────────────────────


def test_constructor_wires_cookies(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    dl = TikTokBrowserDownloader(
        cookies_file=cookies, headless=True, timeout_ms=5_000
    )
    assert dl.cookies_file == cookies
    assert dl.headless is True
    assert dl.timeout_ms == 5_000
    # Default output_dir is a vault temp folder; the collector re-ingests and
    # removes these files after canonical artifact storage.
    assert dl.output_dir == tb_mod.VAULT_ROOT / "tmp" / "tiktok_browser"
    # No browser brought up yet — close should be a no-op
    asyncio.run(dl.close())
    assert dl._browser is None


def test_download_user_yields_items_via_mock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    monkeypatch.setattr(tb_mod, "assert_media_write_allowed", lambda *_a, **_kw: None)
    page = FakePage(
        video_links=[
            "/@bob/video/7000000000000000001",
            "/@bob/video/7000000000000000002",
            "/@bob/video/7000000000000000003",
        ],
    )
    _wire_fake_playwright(monkeypatch, page)

    dl = TikTokBrowserDownloader(output_dir=tmp_path / "out")

    async def _run():
        items = await dl.download_user("bob", max_videos=3)
        await dl.close()
        return items

    items = asyncio.run(_run())
    assert isinstance(items, list)
    assert len(items) == 3
    for it in items:
        assert "video_id" in it
        assert "file_path" in it
        assert "metadata" in it
        # File should have been written by atomic_write helper
        assert Path(it["file_path"]).exists()
        assert Path(it["file_path"]).read_bytes().startswith(b"x")


def test_download_video_returns_none_on_404(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    page = FakePage(goto_status=404, request_url_to_capture=None)
    _wire_fake_playwright(monkeypatch, page)

    dl = TikTokBrowserDownloader(output_dir=tmp_path / "out")

    async def _run():
        out = await dl.download_video("7000000000000000001")
        await dl.close()
        return out

    assert asyncio.run(_run()) is None


def test_close_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    page = FakePage(video_links=[])
    _wire_fake_playwright(monkeypatch, page)
    dl = TikTokBrowserDownloader(output_dir=tmp_path / "out")

    async def _run():
        # Bring the browser up so close() actually has work to do
        await dl.download_user("alice", max_videos=1)
        await dl.close()
        # Second close must not raise
        await dl.close()
        # And a third for good measure
        await dl.close()

    asyncio.run(_run())
    assert dl._closed is True
    assert dl._browser is None
