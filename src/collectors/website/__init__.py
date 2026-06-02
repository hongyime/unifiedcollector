"""Website collector — generic site crawler / image+PDF harvester.

Subsumes ``websitetoolkit/`` (~7,888 LOC of standalone toolkit) into a single
``BaseCollector`` subclass that delegates cross-cutting concerns (Tor routing,
adaptive rate-limiting, BFS spidering, media download, dedupe) to Wave 0
cores. Read-only ingest: fetch HTML, extract links + images + PDFs, persist
into ``website_targets`` / ``website_pages`` / ``media_items``.

ABSORBED FROM ``websitetoolkit/``
---------------------------------
* ``main.py``                              — interactive CLI menu / Flask UI.
                                              Replaced by ``run(targets)`` and
                                              the unified scheduler.
* ``src/link_spider.py::LinkSpider``        — BFS HTML crawler →
                                              ``_crawl_site`` + ``spider_domain``.
* ``src/link_spider.py::extract_all_links`` — link/iframe/script/anchor
                                              extraction → ``_extract_links``.
* ``src/link_spider.py::is_social_media_url`` / ``categorize_link`` →
                                              ``_is_social_media`` /
                                              ``_categorize_link``.
* ``src/photo_scraper.py::PhotoScraper``    — multi-source image extraction
                                              (img / srcset / picture /
                                              CSS background / link-to-image)
                                              → ``extract_media`` +
                                              ``_extract_images``.
* ``src/photo_scraper.py::download_image``  — size/dimension-gated download
                                              → ``_download_image``.
* ``src/sitemap_parser.py::SitemapParser``  — sitemap.xml + robots.txt
                                              discovery → ``_discover_sitemaps``
                                              + ``_parse_sitemap``.
* ``src/pdf_processor.py::PDFProcessor``    — PDF download + page rasterise.
                                              Replaced by the canonical
                                              ``src.core.pdf_processor`` core
                                              wrapped via ``_handle_pdf``.
* ``src/url_filter.py``                     — replaced by
                                              ``src.core.url_filter.URLFilter``.
* ``src/tor_manager.py``                    — subprocess Tor daemon. Replaced
                                              by the ``src.core.tor_proxy``
                                              factory (sidecar SOCKS5).
* ``src/rate_limiter.py``                   — replaced by the
                                              ``AdaptiveRateLimiter`` baked
                                              into ``BaseCollector``.
* ``src/db_manager.py``                     — SQLite hash store. Replaced by
                                              Postgres ``content_hashes`` via
                                              ``src.core.dedupe_hash``.
* ``src/data_manager.py``                   — file-tree state. Replaced by
                                              the unified ``checkpoint``
                                              manager + ``website_*`` tables.
* ``src/utils.py::extract_links_from_text`` — text-link regex extractor →
                                              ``_extract_text_links``.
* ``src/utils.py::normalize_url``           — URL canonicalisation →
                                              ``_normalize_url``.
* ``src/utils.py::is_valid_image_url``      — extension sniff → ``_is_image_url``.
* ``src/utils.py::is_same_domain``          — eTLD+1 compare → ``_same_domain``.
* ``src/cycle_manager.py``                  — periodic crawl scheduler.
                                              Replaced by the unified
                                              scheduler in ``src/scheduler``.
* ``src/proxy_manager.py``                  — proxy rotation harness. Tor
                                              path is the only proxy we
                                              support now; HTTP-proxy lists
                                              are deferred.
* ``src/search_cache.py``                   — duplicate of search collector
                                              cache; not re-imported here.
* ``src/resilience.py``                     — replaced by
                                              ``src.core.resilience`` (already
                                              wired through BaseCollector).

DROPPED (NOT PORTED)
--------------------
* Standalone Flask web UI / dashboard. Unified dashboard covers crawl status.
* Scrape-and-republish flows (toolkit's "rehost to S3 / re-post" mode).
  We are READ-ONLY ingest.
* All write/POST endpoints in ``main.py`` (settings save, run-now triggers,
  add-website forms). Targets are scheduler-driven now.
* Bulk website CSV importer (``bulk_website_importer.py``). Targets come from
  the scheduler / config, not from interactive uploads.
* CLI / setup batch scripts (``setup.bat`` / ``run.bat``).
* PDF→Markdown / OCR enrichment (toolkit's optional Tesseract path). Page
  rasterisation is enough for downstream OCR which lives elsewhere.
* Auto-add-discovered-websites-to-config (``_discover_new_websites``).
  We log discoveries but never mutate the target list ourselves.
* Selenium / undetected-chromedriver fallback. Playwright (already in image)
  is the only headless-browser path.

DEFERRED
--------
* JS-rendered crawl via Playwright. Hooks in place (``_render_html``); only
  fired when ``WEBSITE_USE_PLAYWRIGHT=1`` because it 5-10x's per-page cost.
* Tor circuit rotation (NEWNYM) on per-domain 429. Wired via
  ``src.core.tor_proxy.new_circuit()``; firing it is a 1-line follow-up.
* Sitemap-image extension parsing (``<image:image>`` blocks). Listed but not
  yet emitted into media_items.
* HTTP-proxy-list support (toolkit's ``proxy_manager``). Tor sidecar covers
  the same use case for now.

ENVIRONMENT VARS
----------------
WEBSITE_USE_TOR                 '1' to route crawl through the Tor sidecar.
WEBSITE_USE_PLAYWRIGHT          '1' to render pages via Playwright (slow).
WEBSITE_MAX_DEPTH               BFS depth cap (default 3).
WEBSITE_MAX_PAGES               Pages-per-domain cap (default 500).
WEBSITE_MAX_CONCURRENT_TASKS    Parallel page fetches (default 5).
WEBSITE_TIMEOUT                 Per-request timeout in seconds (default 30).
WEBSITE_RESPECT_ROBOTS          '0' to ignore robots.txt (default '1').
WEBSITE_FOLLOW_EXTERNAL         '1' to follow links off the seed domain
                                (default 0; same-host BFS).
WEBSITE_DOWNLOAD_IMAGES         '1' to download discovered images (default 1).
WEBSITE_DOWNLOAD_PDFS           '1' to download/rasterise PDFs (default 1).
WEBSITE_MIN_IMAGE_BYTES         Reject images smaller than N bytes (default 1024).
WEBSITE_MAX_IMAGE_BYTES         Reject images larger than N bytes (default 10MB).
WEBSITE_MIN_IMAGE_DIM           Reject images with min(w,h) < N px (default 32).
WEBSITE_MAX_PDF_BYTES           Reject PDFs larger than N bytes (default 50MB).
WEBSITE_URL_ALLOW               Comma-separated wildcard allow patterns.
WEBSITE_URL_BLOCK               Comma-separated wildcard block patterns.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.url_filter import URLFilter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif",
    ".tiff", ".tif", ".jfif",
}
SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
PDF_EXTS = {".pdf"}
SKIP_EXTS = {
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map",
}

SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemaps.xml",
    "/sitemap/sitemap.xml",
    "/wp-sitemap.xml",
    "/sitemap-index.xml",
]

# Social media domains we never crawl — they're handled by their own
# dedicated collectors and following them off a seed page just amplifies
# the surface area we have to filter against.
SOCIAL_MEDIA_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "tiktok.com", "linkedin.com", "youtube.com", "youtu.be",
    "pinterest.com", "reddit.com", "snapchat.com", "telegram.org",
    "t.me", "whatsapp.com", "wa.me", "discord.com", "discord.gg",
}

# Identifies link tags we strip during link extraction.
JS_PROTOCOL_PREFIXES = ("javascript:", "mailto:", "tel:", "#", "data:")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Regex used by the toolkit for free-text URL extraction.
TEXT_LINK_RE = re.compile(
    r"https?://[^\s<>\"'\)]+",
    re.IGNORECASE,
)

# CSS background-image extraction.
CSS_BG_RE = re.compile(
    r'background-image:\s*url\(["\']?([^"\'()]+)["\']?\)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers (pulled from websitetoolkit/src/utils.py)
# ---------------------------------------------------------------------------

def _normalize_url(url: str, base: str | None = None) -> str:
    """Canonicalise a URL: lowercase host, strip fragments, drop default ports."""
    try:
        if base:
            url = urljoin(base, url)
        p = urlparse(url)
        if not p.scheme or not p.netloc:
            return url
        netloc = p.netloc.lower()
        # Strip default ports.
        if netloc.endswith(":80") and p.scheme == "http":
            netloc = netloc[:-3]
        elif netloc.endswith(":443") and p.scheme == "https":
            netloc = netloc[:-4]
        path = p.path or "/"
        # Drop fragment, normalise empty query.
        return urlunparse((p.scheme, netloc, path, p.params, p.query, ""))
    except Exception:
        return url


def _registrable_domain(host: str) -> str:
    """eTLD+1 approximation — strips leading 'www.'."""
    h = host.lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def _same_domain(url1: str, url2: str) -> bool:
    try:
        return _registrable_domain(urlparse(url1).netloc) == _registrable_domain(urlparse(url2).netloc)
    except Exception:
        return False


def _is_social_media(url: str) -> bool:
    try:
        host = _registrable_domain(urlparse(url).netloc)
        return any(host == d or host.endswith("." + d) for d in SOCIAL_MEDIA_DOMAINS)
    except Exception:
        return False


def _is_image_url(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in IMAGE_EXTS)
    except Exception:
        return False


def _is_pdf_url(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        return path.endswith(".pdf")
    except Exception:
        return False


def _extract_text_links(text: str, base: str | None = None) -> set[str]:
    """Pull free-text http(s) URLs out of a blob (toolkit's text-link mode)."""
    out: set[str] = set()
    if not text:
        return out
    try:
        for m in TEXT_LINK_RE.findall(text):
            out.add(_normalize_url(m, base))
    except Exception:
        pass
    return out


def _ext_from_url(url: str, default: str = "jpg") -> str:
    try:
        path = urlparse(url).path.lower()
        for ext in IMAGE_EXTS | PDF_EXTS:
            if path.endswith(ext):
                return ext.lstrip(".")
    except Exception:
        pass
    return default


def _categorize_link(url: str) -> str:
    """Toolkit's link categorisation (used for telemetry only)."""
    try:
        u = url.lower()
        path = urlparse(u).path
    except Exception:
        return "other"
    if _is_social_media(url):
        return "social_media_skip"
    if path.endswith(".pdf"):
        return "pdfs"
    if any(path.endswith(ext) for ext in IMAGE_EXTS):
        return "images"
    if any(path.endswith(ext) for ext in (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z")):
        return "documents"
    if any(seg in u for seg in ("/api/", "/feed/", "/rss/", ".xml", ".json")):
        return "api_or_feed"
    if any(path.endswith(ext) for ext in SKIP_EXTS):
        return "static_asset"
    return "internal_links"


# ---------------------------------------------------------------------------
# Robots.txt cache
# ---------------------------------------------------------------------------

class _RobotsCache:
    """Tiny per-host robots.txt cache. Honours 'Disallow' for User-Agent: *."""

    def __init__(self) -> None:
        self._cache: dict[str, list[str]] = {}
        self._sitemaps: dict[str, list[str]] = {}

    async def load(self, base_url: str, client: httpx.AsyncClient) -> None:
        try:
            host = _registrable_domain(urlparse(base_url).netloc)
            if host in self._cache:
                return
            robots_url = urljoin(base_url, "/robots.txt")
            resp = await client.get(robots_url)
            self._cache[host] = []
            self._sitemaps[host] = []
            if resp.status_code != 200:
                return
            ua_star = False
            for raw in resp.text.splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "user-agent":
                    ua_star = (v == "*")
                elif k == "disallow" and ua_star and v:
                    self._cache[host].append(v)
                elif k == "sitemap" and v:
                    self._sitemaps[host].append(v)
        except Exception as e:
            logger.debug("robots.txt load failed for %s: %s", base_url, e)

    def is_allowed(self, url: str) -> bool:
        try:
            host = _registrable_domain(urlparse(url).netloc)
            path = urlparse(url).path or "/"
            for prefix in self._cache.get(host, []):
                if path.startswith(prefix):
                    return False
            return True
        except Exception:
            return True

    def sitemaps_for(self, url: str) -> list[str]:
        try:
            host = _registrable_domain(urlparse(url).netloc)
            return list(self._sitemaps.get(host, []))
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class WebsiteCollector(BaseCollector):
    SOURCE_NAME = "website"

    def __init__(self) -> None:
        super().__init__()
        # BFS / concurrency
        self._max_depth = int(os.getenv("WEBSITE_MAX_DEPTH", "3"))
        self._max_pages = int(os.getenv("WEBSITE_MAX_PAGES", "500"))
        self._max_concurrent = int(os.getenv("WEBSITE_MAX_CONCURRENT_TASKS", "5"))
        self._sem = asyncio.Semaphore(self._max_concurrent)
        # HTTP
        self._timeout = httpx.Timeout(float(os.getenv("WEBSITE_TIMEOUT", "30")), connect=10.0)
        # Behaviour toggles
        self._respect_robots = os.getenv("WEBSITE_RESPECT_ROBOTS", "1") == "1"
        self._follow_external = os.getenv("WEBSITE_FOLLOW_EXTERNAL", "0") == "1"
        self._download_images = os.getenv("WEBSITE_DOWNLOAD_IMAGES", "1") == "1"
        self._download_pdfs = os.getenv("WEBSITE_DOWNLOAD_PDFS", "1") == "1"
        self._use_tor = os.getenv("WEBSITE_USE_TOR", "0") == "1"
        self._use_playwright = os.getenv("WEBSITE_USE_PLAYWRIGHT", "0") == "1"
        # Quality gates
        self._min_image_bytes = int(os.getenv("WEBSITE_MIN_IMAGE_BYTES", "1024"))
        self._max_image_bytes = int(os.getenv("WEBSITE_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
        self._min_image_dim = int(os.getenv("WEBSITE_MIN_IMAGE_DIM", "32"))
        self._max_pdf_bytes = int(os.getenv("WEBSITE_MAX_PDF_BYTES", str(50 * 1024 * 1024)))
        # URL filter (allow/block wildcard patterns)
        self._url_filter = URLFilter.from_env("WEBSITE_URL_ALLOW", "WEBSITE_URL_BLOCK")
        # robots.txt cache
        self._robots = _RobotsCache()
        # Per-run dedup of media URLs we've already enqueued (cross-page).
        self._seen_media: set[str] = set()
        # Optional PDF processor (rasterisation) — only when pymupdf is present.
        self._pdf_processor = None
        try:
            from src.core.pdf_processor import PDFProcessor
            self._pdf_processor = PDFProcessor()
        except Exception as e:
            logger.debug("PDF processor unavailable: %s", e)

    # ------------------------------------------------------------------ #
    # BaseCollector overrides
    # ------------------------------------------------------------------ #

    @property
    def account_media_dir(self) -> Path:
        # Website is shared infra (no per-account isolation).
        path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]) -> None:
        for url in targets:
            if self._stop.is_set():
                break
            seed = url if url.startswith(("http://", "https://")) else f"https://{url}"
            logger.info("Collecting website/%s", seed)
            try:
                await self.spider_domain(seed, max_depth=self._max_depth, max_pages=self._max_pages)
                await self.checkpoint.save_progress(seed)
            except Exception as e:
                logger.error("Failed website/%s: %s", seed, e)
                try:
                    await self.send_to_dlq(seed, seed, str(e))
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # HTTP client construction (Tor-aware)
    # ------------------------------------------------------------------ #

    def _build_client(self, domain: str) -> httpx.AsyncClient:
        """Return an httpx.AsyncClient, optionally Tor-routed.

        WEBSITE_USE_TOR=1 + TOR_PROXY_ENABLED=1 → Tor sidecar SOCKS5.
        Otherwise → direct httpx.AsyncClient (passthrough).
        """
        ua = self.user_agents.get_for_domain(domain) if hasattr(self.user_agents, "get_for_domain") else DEFAULT_USER_AGENT
        headers = {
            "User-Agent": ua or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.7",
        }
        if self._use_tor:
            try:
                from src.core import tor_proxy
                if tor_proxy.is_enabled():
                    client = tor_proxy.get_proxied_client(
                        consumer="website",
                        timeout=float(self._timeout.read or 30.0),
                        headers=headers,
                        follow_redirects=True,
                    )
                    # tor_proxy clients expose .httpx_client OR ARE the httpx client
                    inner = getattr(client, "httpx_client", client)
                    if isinstance(inner, httpx.AsyncClient):
                        return inner
            except Exception as e:
                logger.debug("Tor proxy fallback to direct: %s", e)
        return httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers=headers,
        )

    # ------------------------------------------------------------------ #
    # Public API: fetch_url / spider_domain / extract_media
    # ------------------------------------------------------------------ #

    async def fetch_url(self, url: str) -> Optional[httpx.Response]:
        """One-shot fetch. Returns None on failure / blocked / robots-disallowed."""
        domain = urlparse(url).netloc
        if not self._url_filter.is_allowed(url)[0]:
            return None
        client = self._build_client(domain)
        try:
            await self._robots.load(url, client) if self._respect_robots else None
            if self._respect_robots and not self._robots.is_allowed(url):
                logger.debug("robots.txt disallows %s", url)
                return None
            await self.wait_rate_limit(domain)
            async with self._sem:
                resp = await client.get(url)
            if self._use_playwright and resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                rendered = await self._render_html(url)
                if rendered:
                    # Replace body in-place. httpx.Response is read-only so we
                    # build a synthetic shim with `.text` set to rendered HTML
                    # and forward the rest.
                    return _RenderedResponse(resp, rendered)
            return resp
        except Exception as e:
            logger.debug("fetch_url(%s) failed: %s", url, e)
            return None
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    async def spider_domain(
        self,
        seed_url: str,
        max_depth: int = 3,
        max_pages: int = 500,
    ) -> dict[str, Any]:
        """BFS spider a single domain. Persists pages into ``website_pages``."""
        seed_url = _normalize_url(seed_url)
        domain = urlparse(seed_url).netloc
        await self._upsert_target(domain, seed_url)

        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(seed_url, 0)]
        stats = {"pages": 0, "images": 0, "pdfs": 0, "errors": 0, "skipped": 0}

        client = self._build_client(domain)
        try:
            if self._respect_robots:
                await self._robots.load(seed_url, client)
                # Surface robots.txt-listed sitemaps for richer discovery.
                for sm in self._robots.sitemaps_for(seed_url):
                    await self._ingest_sitemap(client, sm, queue, visited, domain)

            # Probe common sitemap locations for an extra seed bonus.
            for path in SITEMAP_PATHS:
                await self._ingest_sitemap(client, urljoin(seed_url, path), queue, visited, domain)

            while queue and stats["pages"] < max_pages and not self._stop.is_set():
                url, depth = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)

                if not self._url_filter.is_allowed(url)[0]:
                    stats["skipped"] += 1
                    continue
                if self._respect_robots and not self._robots.is_allowed(url):
                    stats["skipped"] += 1
                    continue

                await self.wait_rate_limit(domain)
                try:
                    async with self._sem:
                        resp = await client.get(url)
                except Exception as e:
                    logger.debug("GET %s failed: %s", url, e)
                    stats["errors"] += 1
                    self.rate_limiter.report_failure(domain) if hasattr(self.rate_limiter, "report_failure") else None
                    continue

                if resp.status_code != 200:
                    stats["errors"] += 1
                    continue
                ct = resp.headers.get("content-type", "").lower()

                # PDF
                if "application/pdf" in ct or _is_pdf_url(url):
                    if self._download_pdfs:
                        await self._handle_pdf(resp.content, url, domain)
                        stats["pdfs"] += 1
                    continue

                if "text/html" not in ct and "application/xhtml" not in ct:
                    continue

                # Optionally re-fetch via Playwright for JS-heavy pages.
                html = resp.text
                if self._use_playwright:
                    rendered = await self._render_html(url)
                    if rendered:
                        html = rendered

                stats["pages"] += 1
                meta = self._extract_metadata(html)
                images = self._extract_images(html, url)
                links = self._extract_links(html, url)
                content_text = self._extract_text(html)

                await self._upsert_page(
                    domain=domain,
                    url=url,
                    html=html,
                    status=resp.status_code,
                    title=meta.get("title"),
                    description=meta.get("description"),
                    content_text=content_text,
                    images=images,
                    internal_links=[l for l in links if _same_domain(l, url)],
                    external_links=[l for l in links if not _same_domain(l, url)],
                )

                if self._download_images:
                    for img in images:
                        img_url = img.get("url")
                        if not img_url or img_url in self._seen_media:
                            continue
                        self._seen_media.add(img_url)
                        cid = hashlib.sha256(img_url.encode("utf-8")).hexdigest()[:16]
                        if self.is_known(cid):
                            continue
                        try:
                            await self._download_image(
                                img_url=img_url,
                                source_url=url,
                                entity_id=domain,
                                content_id=cid,
                                alt=img.get("alt", ""),
                                client=client,
                            )
                            stats["images"] += 1
                        except Exception as e:
                            logger.debug("image download failed %s: %s", img_url, e)
                            stats["errors"] += 1

                # Enqueue children
                if depth < max_depth:
                    for link in links:
                        if link in visited:
                            continue
                        if not self._follow_external and not _same_domain(link, seed_url):
                            continue
                        if _is_social_media(link):
                            continue
                        # Skip obvious static assets early.
                        if any(link.lower().endswith(ext) for ext in SKIP_EXTS):
                            continue
                        queue.append((link, depth + 1))

                self.rate_limiter.report_success(domain) if hasattr(self.rate_limiter, "report_success") else None

        finally:
            try:
                await client.aclose()
            except Exception:
                pass

        logger.info(
            "Spider %s done: pages=%d images=%d pdfs=%d errors=%d skipped=%d",
            domain, stats["pages"], stats["images"], stats["pdfs"],
            stats["errors"], stats["skipped"],
        )
        return stats

    async def extract_media(self, url: str) -> dict[str, list]:
        """Fetch a single page and return its image+pdf URLs (no download)."""
        resp = await self.fetch_url(url)
        if resp is None or resp.status_code != 200:
            return {"images": [], "pdfs": []}
        ct = resp.headers.get("content-type", "").lower()
        if "text/html" not in ct:
            return {"images": [], "pdfs": []}
        html = resp.text
        images = self._extract_images(html, url)
        links = self._extract_links(html, url)
        pdfs = [l for l in links if _is_pdf_url(l)]
        return {"images": images, "pdfs": pdfs}

    # ------------------------------------------------------------------ #
    # HTML extraction (BeautifulSoup-backed)
    # ------------------------------------------------------------------ #

    def _soup(self, html: str):
        from bs4 import BeautifulSoup  # imported lazily to keep import cheap
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            return BeautifulSoup(html, "html.parser")

    def _extract_metadata(self, html: str) -> dict[str, str]:
        """Pull title / meta description / canonical out of the head."""
        out: dict[str, str] = {}
        try:
            soup = self._soup(html)
            if soup.title and soup.title.string:
                out["title"] = soup.title.string.strip()[:512]
            for meta in soup.find_all("meta"):
                name = (meta.get("name") or meta.get("property") or "").lower()
                content = meta.get("content") or ""
                if not content:
                    continue
                if name in ("description", "og:description") and "description" not in out:
                    out["description"] = content.strip()[:1024]
                elif name == "keywords" and "keywords" not in out:
                    out["keywords"] = content.strip()[:1024]
            for link in soup.find_all("link", rel=True):
                rel = link.get("rel")
                if isinstance(rel, list):
                    rel = rel[0] if rel else ""
                if rel and rel.lower() == "canonical" and link.get("href"):
                    out["canonical"] = link["href"]
                    break
        except Exception as e:
            logger.debug("metadata extract failed: %s", e)
        return out

    def _extract_text(self, html: str) -> str:
        """Extract plaintext body from HTML, stripping scripts/styles/nav."""
        try:
            soup = self._soup(html)
            # Remove noise elements
            for tag in soup(["script", "style", "noscript", "iframe"]):
                tag.decompose()
            # Get text with space separator
            text = soup.get_text(separator=" ", strip=True)
            # Collapse whitespace
            import re
            text = re.sub(r'\s+', ' ', text)
            return text.strip()
        except Exception as e:
            logger.debug("text extract failed: %s", e)
            return ""

    def _extract_links(self, html: str, base_url: str) -> list[str]:
        """All href / form action / iframe src / script src + free-text URLs."""
        seen: set[str] = set()
        out: list[str] = []
        try:
            soup = self._soup(html)
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if any(href.lower().startswith(p) for p in JS_PROTOCOL_PREFIXES):
                    continue
                u = _normalize_url(href, base_url)
                if u and u not in seen:
                    seen.add(u)
                    out.append(u)
            for f in soup.find_all("form", action=True):
                u = _normalize_url(f["action"], base_url)
                if u and u not in seen:
                    seen.add(u)
                    out.append(u)
            for i in soup.find_all("iframe", src=True):
                u = _normalize_url(i["src"], base_url)
                if u and u not in seen:
                    seen.add(u)
                    out.append(u)
        except Exception as e:
            logger.debug("link extract failed: %s", e)
        # Free-text URLs (toolkit's text-link mode).
        for u in _extract_text_links(html, base_url):
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _extract_images(self, html: str, base_url: str) -> list[dict[str, str]]:
        """Multi-source image extraction (img / srcset / picture / CSS bg / link).

        Mirrors ``photo_scraper.PhotoScraper.extract_images_from_page``.
        Returns a list of {url, alt, source} dicts.
        """
        out: list[dict[str, str]] = []
        seen: set[str] = set()

        def _emit(url: str, alt: str, source: str) -> None:
            u = _normalize_url(url, base_url)
            if not u or u in seen:
                return
            # Reject obvious tracking pixels by extension.
            if any(u.lower().endswith(ext) for ext in (".css", ".js", ".woff", ".woff2", ".ttf")):
                return
            seen.add(u)
            out.append({"url": u, "alt": alt or "", "source": source})

        try:
            soup = self._soup(html)
            # 1. <img>
            for img in soup.find_all("img"):
                src = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("data-original")
                )
                alt = img.get("alt", "") or ""
                if src:
                    _emit(src, alt, "img_tag")
                # srcset
                srcset = img.get("srcset")
                if srcset:
                    try:
                        for chunk in srcset.split(","):
                            url_part = chunk.strip().split(" ")[0]
                            if url_part:
                                _emit(url_part, alt, "srcset")
                    except Exception:
                        pass

            # 2. <picture><source>
            for source in soup.find_all("source"):
                ss = source.get("srcset")
                if not ss:
                    continue
                try:
                    for chunk in ss.split(","):
                        url_part = chunk.strip().split(" ")[0]
                        if url_part:
                            _emit(url_part, "", "picture_source")
                except Exception:
                    pass

            # 3. <style> tags (CSS background-image)
            for style in soup.find_all("style"):
                txt = style.string or ""
                if not txt:
                    continue
                for m in CSS_BG_RE.findall(txt):
                    _emit(m, "", "css_background")

            # 4. inline style="background-image:..."
            for el in soup.find_all(attrs={"style": True}):
                style_attr = el.get("style") or ""
                for m in CSS_BG_RE.findall(style_attr):
                    _emit(m, "", "inline_style")

            # 5. <a href> linking directly to an image file
            for a in soup.find_all("a", href=True):
                if _is_image_url(a["href"]):
                    text = (a.get_text(strip=True) or "")[:128]
                    _emit(a["href"], text, "link_to_image")

            # 6. og:image meta
            for meta in soup.find_all("meta"):
                prop = (meta.get("property") or meta.get("name") or "").lower()
                if prop in ("og:image", "twitter:image") and meta.get("content"):
                    _emit(meta["content"], "", "og_image")
        except Exception as e:
            logger.debug("image extract failed: %s", e)
        return out

    # ------------------------------------------------------------------ #
    # Sitemap discovery (mirrors websitetoolkit/src/sitemap_parser.py)
    # ------------------------------------------------------------------ #

    async def _ingest_sitemap(
        self,
        client: httpx.AsyncClient,
        sitemap_url: str,
        queue: list[tuple[str, int]],
        visited: set[str],
        seed_domain: str,
        depth_limit: int = 2,
    ) -> None:
        """Fetch + parse a sitemap, enqueue same-domain URLs at depth 0."""
        try:
            await self.wait_rate_limit(seed_domain)
            resp = await client.get(sitemap_url)
            if resp.status_code != 200:
                return
            ct = resp.headers.get("content-type", "").lower()
            if "xml" not in ct and "text/plain" not in ct:
                return
            urls, child_sitemaps = self._parse_sitemap(resp.text)
            for u in urls:
                if not _same_domain(u, sitemap_url):
                    continue
                if u in visited:
                    continue
                queue.append((u, 0))
            # Sitemap-of-sitemaps
            if depth_limit > 0:
                for sm in child_sitemaps[:50]:
                    await self._ingest_sitemap(client, sm, queue, visited, seed_domain, depth_limit - 1)
        except Exception as e:
            logger.debug("sitemap %s ingest failed: %s", sitemap_url, e)

    def _parse_sitemap(self, xml_text: str) -> tuple[list[str], list[str]]:
        """Return (page_urls, child_sitemap_urls) from a sitemap XML body."""
        urls: list[str] = []
        sitemaps: list[str] = []
        try:
            # Avoid ElementTree-XXE risks: use a defensive regex pass which is
            # plenty for sitemap structure.
            for m in re.findall(r"<loc>\s*([^<\s][^<]*?)\s*</loc>", xml_text, re.IGNORECASE):
                u = _normalize_url(m.strip())
                if not u:
                    continue
                if u.endswith(".xml") or "sitemap" in u.lower():
                    sitemaps.append(u)
                else:
                    urls.append(u)
        except Exception as e:
            logger.debug("sitemap parse failed: %s", e)
        return urls, sitemaps

    # ------------------------------------------------------------------ #
    # Playwright (optional, deferred render path)
    # ------------------------------------------------------------------ #

    async def _render_html(self, url: str) -> Optional[str]:
        """Optional Playwright render. Returns rendered HTML or None."""
        if not self._use_playwright:
            return None
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            logger.debug("Playwright not installed: %s", e)
            return None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    html = await page.content()
                    return html
                finally:
                    await browser.close()
        except Exception as e:
            logger.debug("Playwright render failed for %s: %s", url, e)
            return None

    # ------------------------------------------------------------------ #
    # Image / PDF download — quality gated
    # ------------------------------------------------------------------ #

    async def _download_image(
        self,
        *,
        img_url: str,
        source_url: str,
        entity_id: str,
        content_id: str,
        alt: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Quality-gated image download mirroring photo_scraper.download_image."""
        if not self._url_filter.is_allowed(img_url)[0]:
            return
        # HEAD first to short-circuit oversized downloads.
        try:
            head = await client.head(img_url, timeout=15.0)
            cl = head.headers.get("content-length")
            if cl and int(cl) > self._max_image_bytes:
                logger.debug("img too large by HEAD: %s (%s)", img_url, cl)
                return
            ct = head.headers.get("content-type", "").lower()
            if ct and not ct.startswith("image/"):
                # Some CDNs lie on HEAD; fall through to GET with a stricter check.
                pass
        except Exception:
            pass

        try:
            resp = await client.get(img_url, timeout=30.0)
        except Exception as e:
            logger.debug("img GET failed %s: %s", img_url, e)
            return
        if resp.status_code != 200:
            return
        data = resp.content
        if len(data) < self._min_image_bytes:
            return
        if len(data) > self._max_image_bytes:
            return
        ct = resp.headers.get("content-type", "").lower()
        if ct and not ct.startswith("image/") and not _is_image_url(img_url):
            return

        # Optional: dimension gate via PIL.
        width = height = None
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
                if min(width, height) < self._min_image_dim:
                    return
        except Exception:
            # PIL missing or can't read → trust the byte-size gate alone.
            pass

        item = {
            "entity_id": entity_id,
            "entity_name": entity_id,
            "content_type": "image",
            "content_id": content_id,
            "url": img_url,
            "extension": _ext_from_url(img_url, "jpg"),
            "source_url": source_url,
            "alt": alt,
            "width": width,
            "height": height,
            "data": data,
        }
        await self.download_media(item)

    async def _handle_pdf(self, pdf_data: bytes, source_url: str, domain: str) -> None:
        """Persist a PDF + (optional) rasterise pages into media_items."""
        if len(pdf_data) > self._max_pdf_bytes:
            logger.debug("PDF too large, skipping: %s (%d)", source_url, len(pdf_data))
            return
        cid = hashlib.sha256(pdf_data).hexdigest()[:16]
        if self.is_known(cid):
            return
        item = {
            "entity_id": domain,
            "entity_name": domain,
            "content_type": "pdf",
            "content_id": cid,
            "url": source_url,
            "extension": "pdf",
            "source_url": source_url,
            "data": pdf_data,
        }
        await self.download_media(item)

        # Rasterise pages → image media items (best-effort).
        if self._pdf_processor is None:
            return
        try:
            pages = self._pdf_processor.rasterise(pdf_data) if hasattr(self._pdf_processor, "rasterise") else []
        except Exception as e:
            logger.debug("PDF rasterise failed for %s: %s", source_url, e)
            pages = []
        for idx, png_bytes in enumerate(pages):
            page_cid = hashlib.sha256(f"{cid}:{idx}".encode()).hexdigest()[:16]
            if self.is_known(page_cid):
                continue
            await self.download_media({
                "entity_id": domain,
                "entity_name": domain,
                "content_type": "image",
                "content_id": page_cid,
                "url": f"{source_url}#page={idx + 1}",
                "extension": "png",
                "source_url": source_url,
                "data": png_bytes,
            })

    # ------------------------------------------------------------------ #
    # Generic media writer (used by image + pdf paths)
    # ------------------------------------------------------------------ #

    async def download_media(self, item: dict) -> None:
        cid = item["content_id"]
        if self.is_known(cid):
            return
        ext = item.get("extension", "jpg")
        filename = self.build_filename(
            item["entity_id"], item["entity_name"],
            item["content_type"], cid, extension=ext,
        )
        dest_dir = self.account_media_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        try:
            data = item.get("data")
            if data is None:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                    resp = await client.get(item["url"])
                    resp.raise_for_status()
                    data = resp.content
            sha = self.sha256_bytes(data)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, dest)
            except BaseException:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "source_url": item.get("source_url"),
                "alt": item.get("alt"),
                "width": item.get("width"),
                "height": item.get("height"),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
            }
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")
            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                width=item.get("width"),
                height=item.get("height"),
                sha256=sha,
                source_url=item.get("source_url"),
                metadata=metadata,
            )
            self._known_ids.add(cid)
        except Exception as e:
            logger.debug("download_media failed %s: %s", item.get("url"), e)

    # ------------------------------------------------------------------ #
    # DB persistence
    # ------------------------------------------------------------------ #

    async def _upsert_target(self, domain: str, start_url: str) -> None:
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO website_targets (domain, start_url, status, updated_at)
                    VALUES ($1, $2, 'crawling', NOW())
                    ON CONFLICT (domain) DO UPDATE
                       SET start_url = EXCLUDED.start_url,
                           status = 'crawling',
                           updated_at = NOW()
                    """,
                    domain, start_url,
                )
        except Exception as e:
            logger.debug("upsert_target failed for %s: %s", domain, e)

    async def _upsert_page(
        self,
        *,
        domain: str,
        url: str,
        html: str,
        status: int,
        title: Optional[str],
        description: Optional[str],
        content_text: str,
        images: list[dict],
        internal_links: list[str],
        external_links: list[str],
    ) -> None:
        if not self.pool:
            return
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        try:
            import json as _json
            async with self.pool.acquire() as conn:
                target_row = await conn.fetchrow(
                    "SELECT id FROM website_targets WHERE domain = $1",
                    domain,
                )
                if not target_row:
                    return
                await conn.execute(
                    """
                    INSERT INTO website_pages
                        (target_id, url, url_hash, title, meta_description,
                         content_text, content_html, internal_links, external_links,
                         images, status_code, fetched_at, collected_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,NOW(),NOW())
                    ON CONFLICT (url) DO UPDATE
                       SET title = EXCLUDED.title,
                           meta_description = EXCLUDED.meta_description,
                           content_text = EXCLUDED.content_text,
                           content_html = EXCLUDED.content_html,
                           internal_links = EXCLUDED.internal_links,
                           external_links = EXCLUDED.external_links,
                           images = EXCLUDED.images,
                           status_code = EXCLUDED.status_code,
                           fetched_at = NOW()
                    """,
                    target_row["id"], url, url_hash,
                    (title or "")[:512] if title else None,
                    (description or "")[:1024] if description else None,
                    (content_text or "")[:100_000] if content_text else None,  # 100KB cap
                    html[: 2 * 1024 * 1024],  # 2 MB hard cap on stored HTML
                    internal_links[:1000],
                    external_links[:1000],
                    _json.dumps(images[:200], default=str),
                    status,
                )
        except Exception as e:
            logger.debug("upsert_page failed for %s: %s", url, e)

    async def cleanup(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Tiny shim used when Playwright re-renders a page mid-fetch_url
# ---------------------------------------------------------------------------

class _RenderedResponse:
    """Duck-typed httpx.Response replacement holding Playwright HTML."""

    def __init__(self, base: httpx.Response, rendered_html: str):
        self._base = base
        self.text = rendered_html
        self.content = rendered_html.encode("utf-8", errors="ignore")
        self.status_code = base.status_code
        self.headers = base.headers
        self.url = base.url


__all__ = ["WebsiteCollector"]
