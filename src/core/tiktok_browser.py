"""Async Playwright-based TikTok browser downloader.

Ported from ``tiktoktoolkit/src/browser_downloader.py`` (sync) to an
async-first surface compatible with the unified collector's asyncio runtime.

Anti-bot tricks preserved from the toolkit:
  * Realistic Chromium user-agent + viewport + locale + timezone
  * sec-ch-ua client-hint headers
  * ``--disable-blink-features=AutomationControlled`` launch flag
  * Stealth init script overriding ``navigator.webdriver`` / ``plugins`` /
    ``languages``
  * Netscape-format cookie jar injection (sessionid / ttwid / msToken / etc.)

Public surface
--------------
class TikTokBrowserDownloader:
    async def download_user(username, max_videos=50) -> list[dict]
    async def download_video(video_id) -> dict | None
    async def close() -> None

Each returned item is a dict with at least:
    {"video_id": str, "file_path": str | None, "metadata": dict}

The class is *lazy*: ``async_playwright`` is imported on first use so plain
import of this module stays cheap and doesn't drag the Playwright runtime
into every collector start-up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Anti-bot constants (mirrors tiktoktoolkit/src/browser_downloader.py) ────

_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]

_CONTEXT_OPTIONS: dict[str, Any] = {
    "viewport": {"width": 1920, "height": 1080},
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "extra_http_headers": {
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
}

_STEALTH_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });"
    "Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });"
)

_VIDEO_ID_PATTERN = re.compile(r"/video/(\d{15,25})")
_LOGIN_COOKIE_NAMES = {"sessionid", "sid_tt", "uid_tt", "ttwid", "msToken"}


# ── Cookie helpers ──────────────────────────────────────────────────────────


def _parse_netscape_cookies(cookies_file: Path) -> list[dict[str, Any]]:
    """Parse a Netscape-format cookies.txt into Playwright-compatible dicts."""
    cookies: list[dict[str, Any]] = []
    try:
        with cookies_file.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain = parts[0].strip()
                if not domain:
                    continue
                if domain.startswith(("http://", "https://")):
                    domain = domain.split("://", 1)[1]
                try:
                    expires = int(parts[4]) if parts[4] not in ("0", "") else -1
                except ValueError:
                    expires = -1
                cookie: dict[str, Any] = {
                    "name": parts[5],
                    "value": parts[6],
                    "domain": domain,
                    "path": parts[2] or "/",
                    "secure": parts[3].lower() == "true",
                }
                if expires > 0:
                    cookie["expires"] = expires
                cookies.append(cookie)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to parse cookies file %s: %s", cookies_file, exc)
    return cookies


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` via .tmp + os.replace (best-effort fsync)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # Best-effort: replace itself is atomic on POSIX/NTFS.
            if os.name != "nt":
                logger.debug("fsync failed for %s", tmp, exc_info=True)
    os.replace(str(tmp), str(dest))


def _extract_video_id(url: str) -> Optional[str]:
    m = _VIDEO_ID_PATTERN.search(url or "")
    return m.group(1) if m else None


# ── Public class ────────────────────────────────────────────────────────────


class TikTokBrowserDownloader:
    """Async Playwright fallback downloader for TikTok.

    Use as:

        dl = TikTokBrowserDownloader(cookies_file=Path("cookies.txt"))
        try:
            items = await dl.download_user("someone", max_videos=10)
        finally:
            await dl.close()
    """

    def __init__(
        self,
        cookies_file: Path | None = None,
        headless: bool = True,
        output_dir: Path | None = None,
        timeout_ms: int = 30_000,
    ) -> None:
        self.cookies_file: Optional[Path] = Path(cookies_file) if cookies_file else None
        self.headless = bool(headless)
        if output_dir is None:
            output_dir = Path(os.getenv("MEDIA_ROOT", "/data/media")) / "tiktok"
        self.output_dir: Path = Path(output_dir)
        self.timeout_ms = int(timeout_ms)

        # Lazy-initialised playwright handles. ``close()`` is idempotent so a
        # caller that never spins anything up can still ``await close()``.
        self._pw_ctx = None  # async_playwright() context
        self._pw = None  # entered playwright instance
        self._browser = None
        self._context = None
        self._closed = False

    # ── lazy bring-up ──────────────────────────────────────────────────────

    async def _ensure_browser(self) -> None:
        if self._browser is not None or self._closed:
            return
        try:
            from playwright.async_api import async_playwright  # lazy import
        except Exception as exc:  # pragma: no cover - environmental
            raise RuntimeError(
                "Playwright async API unavailable. Install with "
                "`pip install playwright && playwright install chromium`."
            ) from exc

        self._pw_ctx = async_playwright()
        self._pw = await self._pw_ctx.start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, args=_CHROMIUM_ARGS
        )
        self._context = await self._browser.new_context(**_CONTEXT_OPTIONS)
        try:
            await self._context.add_init_script(_STEALTH_SCRIPT)
        except Exception:
            logger.debug("add_init_script failed", exc_info=True)
        if self.cookies_file and self.cookies_file.exists():
            parsed = _parse_netscape_cookies(self.cookies_file)
            if parsed:
                try:
                    await self._context.add_cookies(parsed)
                    logger.info(
                        "TikTok browser: loaded %d cookies from %s",
                        len(parsed), self.cookies_file,
                    )
                except Exception as exc:
                    logger.warning("add_cookies failed: %s", exc)

    # ── public surface ─────────────────────────────────────────────────────

    async def download_user(
        self, username: str, max_videos: int = 50
    ) -> list[dict]:
        """Scrape a user's profile page and download up to ``max_videos`` videos.

        Returns a list of dicts: ``[{"video_id", "file_path", "metadata"}, ...]``.
        Soft-fails: returns ``[]`` on private/404/anti-bot blocks rather than
        raising, so the calling collector can fall through to its own
        error-handling without try/except sprawl.
        """
        username = (username or "").lstrip("@").strip()
        if not username:
            return []
        await self._ensure_browser()
        if self._context is None:  # bring-up failed silently
            return []
        profile_url = f"https://www.tiktok.com/@{username}"
        logger.info("TikTok browser: download_user @%s (limit=%d)", username, max_videos)

        items: list[dict] = []
        page = await self._context.new_page()
        try:
            try:
                await page.goto(
                    profile_url, timeout=self.timeout_ms, wait_until="domcontentloaded"
                )
            except Exception as exc:
                logger.warning("download_user @%s: goto failed: %s", username, exc)
                return []

            # Best-effort hydrate wait
            try:
                await page.wait_for_selector(
                    'a[href*="/video/"]', timeout=min(self.timeout_ms, 20_000)
                )
            except Exception:
                # No grid → likely private / 404 / login-walled. Give the
                # caller an empty list; logs above carry the diagnostic.
                logger.info(
                    "download_user @%s: no /video/ links surfaced (private/404/login?)",
                    username,
                )
                return []

            video_urls = await self._extract_video_urls(page, max_videos)
            if not video_urls:
                return []

            for url in video_urls[:max_videos]:
                vid = _extract_video_id(url)
                if not vid:
                    continue
                rec = await self._download_video_via_page(page, url, username)
                if rec is not None:
                    items.append(rec)
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return items

    async def download_video(self, video_id: str) -> dict | None:
        """Download a single video by its 19-digit ID. Returns None on 404/error."""
        vid = (video_id or "").strip()
        if not vid:
            return None
        await self._ensure_browser()
        if self._context is None:
            return None
        # We don't know the username here; TikTok's /@/video/<id> path requires
        # the username. Fall back to the canonical /video/<id> redirect.
        url = f"https://www.tiktok.com/video/{vid}"
        page = await self._context.new_page()
        try:
            try:
                resp = await page.goto(
                    url, timeout=self.timeout_ms, wait_until="domcontentloaded"
                )
            except Exception as exc:
                logger.warning("download_video %s: goto failed: %s", vid, exc)
                return None
            # Treat 404 / 4xx as "not found"
            status = getattr(resp, "status", None) if resp is not None else None
            if status is not None and 400 <= int(status) < 500:
                logger.info("download_video %s: HTTP %s — returning None", vid, status)
                return None
            return await self._download_video_via_page(page, url, username=None)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def close(self) -> None:
        """Tear down browser/context/playwright. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for closer, name in (
            (self._context, "context"),
            (self._browser, "browser"),
        ):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception:
                logger.debug("TikTok browser: %s close failed", name, exc_info=True)
        if self._pw_ctx is not None:
            try:
                await self._pw_ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("TikTok browser: playwright stop failed", exc_info=True)
        self._pw_ctx = None
        self._pw = None
        self._browser = None
        self._context = None

    # ── internals ──────────────────────────────────────────────────────────

    async def _extract_video_urls(self, page, limit: int) -> list[str]:
        """Mirror the toolkit's /video/ link scan, async edition."""
        out: list[str] = []
        try:
            links = await page.query_selector_all('a[href*="/video/"]')
        except Exception as exc:
            logger.debug("query_selector_all failed: %s", exc)
            return out
        for link in links or []:
            try:
                href = await link.get_attribute("href")
            except Exception:
                href = None
            if not href or "/video/" not in href:
                continue
            if href.startswith("/"):
                href = f"https://www.tiktok.com{href}"
            if href not in out:
                out.append(href)
            if len(out) >= limit:
                break
        return out

    async def _download_video_via_page(
        self, page, video_url: str, username: str | None
    ) -> dict | None:
        """Capture the CDN URL via request interception, then write to disk."""
        vid = _extract_video_id(video_url)
        if not vid:
            return None
        dest = self.output_dir / f"{vid}.mp4"
        if dest.exists():
            return {
                "video_id": vid,
                "file_path": str(dest),
                "metadata": {"username": username, "skipped": True, "url": video_url},
            }

        captured: list[str] = []

        def _on_request(request) -> None:
            url = getattr(request, "url", "") or ""
            if (
                not captured
                and ("mime_type=video" in url or url.endswith(".mp4"))
                and ("tiktok.com" in url or "tiktokcdn.com" in url or "tiktokv.com" in url)
                and not url.startswith("blob:")
            ):
                captured.append(url)

        try:
            page.on("request", _on_request)
        except Exception:
            logger.debug("page.on('request') wiring failed", exc_info=True)

        try:
            try:
                await page.goto(
                    video_url, timeout=self.timeout_ms, wait_until="domcontentloaded"
                )
            except Exception as exc:
                logger.warning("download_video %s: goto failed: %s", vid, exc)
                return None
            try:
                await page.wait_for_selector("video", timeout=self.timeout_ms)
            except Exception:
                logger.info("download_video %s: <video> not found", vid)
                return None
            # Trigger CDN fetch by playing muted
            try:
                await page.evaluate(
                    "() => { const v = document.querySelector('video'); "
                    "if (v) { v.muted = true; v.play(); } }"
                )
            except Exception:
                pass
            # Wait up to 10s for the CDN URL to fly by
            for _ in range(50):
                if captured:
                    break
                await asyncio.sleep(0.2)
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:
                pass

        if not captured:
            # Fallback: pull <video src> if it isn't a blob:
            try:
                el = await page.query_selector("video")
                src = await el.get_attribute("src") if el is not None else None
            except Exception:
                src = None
            if src and not src.startswith("blob:"):
                captured.append(src)
            else:
                logger.info("download_video %s: no CDN URL captured", vid)
                return None

        cdn = captured[0]
        # Pull bytes through the page's request context (carries session)
        try:
            response = await page.request.get(
                cdn,
                headers={
                    "Referer": "https://www.tiktok.com/",
                    "Origin": "https://www.tiktok.com",
                },
                timeout=120_000,
            )
            status = getattr(response, "status", 0)
            if status not in (200, 206):
                logger.info("download_video %s: CDN HTTP %s", vid, status)
                return None
            data = await response.body()
        except Exception as exc:
            logger.warning("download_video %s: CDN fetch failed: %s", vid, exc)
            return None

        if not data or len(data) < 10_000:
            logger.info("download_video %s: CDN returned only %d bytes", vid, len(data or b""))
            return None

        try:
            _atomic_write_bytes(dest, data)
        except Exception as exc:
            logger.warning("download_video %s: atomic write failed: %s", vid, exc)
            return None

        return {
            "video_id": vid,
            "file_path": str(dest),
            "metadata": {
                "username": username,
                "url": video_url,
                "cdn_url": cdn,
                "size": len(data),
            },
        }


__all__ = ["TikTokBrowserDownloader"]
