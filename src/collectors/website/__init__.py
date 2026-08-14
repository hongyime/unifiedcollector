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
WEBSITE_TARGET_TIMEOUT_SECONDS  Per-target wall-clock cap before deferring
                                the site behind the rest of the queue
                                (default 1500).
WEBSITE_TARGET_RETRY_BACKOFF_SECONDS
                                How long to skip targets that hit the
                                wall-clock cap (default 21600 = 6h).
WEBSITE_TARGET_RETRY_BACKOFF_MAX_SECONDS
                                Maximum exponential retry backoff for repeated
                                wall-clock timeout targets (default 604800 = 7d).
WEBSITE_RESPECT_ROBOTS          '0' to ignore robots.txt (default '1').
WEBSITE_ROBOTS_POLICY           respect | allowlist_override (default respect).
WEBSITE_ROBOTS_OVERRIDE_DOMAINS Comma-separated owned/authorized domains where
                                robots.txt can be bypassed when policy is
                                allowlist_override.
WEBSITE_FOLLOW_EXTERNAL         '1' to follow links off the seed domain
                                (default 0; same-host BFS).
WEBSITE_MAX_ACTIVE_DOMAINS      Max concurrently active crawl domains (default 4).
WEBSITE_MAX_REQUESTS_PER_DOMAIN Per-domain in-flight request cap (default 2).
WEBSITE_DOMAIN_DELAY_SECONDS    Base delay before each domain-paced request.
WEBSITE_DOMAIN_JITTER_SECONDS   Additional random request delay.
WEBSITE_DOWNLOAD_IMAGES         '1' to download discovered images (default 1).
WEBSITE_DOWNLOAD_PDFS           '1' to download/rasterise PDFs (default 1).
WEBSITE_MIN_IMAGE_BYTES         Reject images smaller than N bytes (default 1024).
WEBSITE_MAX_IMAGE_BYTES         Reject images larger than N bytes (default 10MB).
WEBSITE_MIN_IMAGE_DIM           Reject images with min(w,h) < N px (default 32).
WEBSITE_MAX_PDF_BYTES           Reject PDFs larger than N bytes (default 50MB).
WEBSITE_URL_ALLOW               Comma-separated wildcard allow patterns.
WEBSITE_URL_BLOCK               Comma-separated wildcard block patterns.
WEBSITE_URL_POLICY_FILE         Optional text policy file with allow/block lines;
                                defaults to config/sources/website.url-policy.txt.
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
from typing import Any, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.domain_pacing import DomainPacer, record_domain_pacing_event
from src.core.request_persona import build_persona_headers
from src.core.vault import (
    VAULT_ROOT,
    assert_media_write_allowed,
    write_atomic_artifact,
    write_atomic_artifact_from_path,
)
from src.core.scrape_pacing import headless_dwell
from src.core.url_filter import URLFilter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_URL_POLICY_FILE = (
    Path(__file__).resolve().parents[3] / "config" / "sources" / "website.url-policy.txt"
)

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif",
    ".tiff", ".tif", ".jfif",
}
SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
PDF_EXTS = {".pdf"}

# Documents we DO capture (COLLECTION_SPEC tier 3 whitelist: pdf/word/ppt/
# excel/text/office — no executables, no code). PDF has its own richer path
# (rasterisation) so it stays out of this set.
DOC_EXTS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".rtf", ".csv", ".odt", ".ods", ".odp",
}
# Videos we DO capture. Downloaded with NO size cap (user decision) which means
# they MUST be streamed to disk, never buffered into memory.
VIDEO_EXTS = {
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
    ".mpeg", ".mpg", ".wmv", ".flv", ".ogv", ".3gp",
}
# Audio + code/asset extensions we EXCLUDE entirely — never crawl, never
# download (folded into SKIP_EXTS below so both the enqueue guard and the
# download dispatch drop them). Matches the user's "no audio, no code files,
# no html assets" rule for the website/search spiders.
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac", ".aac",
    ".opus", ".wma", ".aiff", ".mid", ".midi",
}
CODE_EXTS = {
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".php",
    ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".sh", ".bat",
    ".ps1", ".pl", ".lua", ".swift", ".kt", ".scala", ".sql", ".yaml",
    ".yml", ".toml", ".ini", ".exe", ".dll", ".so", ".dylib", ".bin",
    ".msi", ".apk", ".jar", ".war",
}
SKIP_EXTS = {
    ".css", ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map",
} | AUDIO_EXTS | CODE_EXTS

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

PLAYWRIGHT_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--js-flags=--max-old-space-size=512",
    "--disable-background-timer-throttling",
    "--renderer-process-limit=10",
]

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


def _csv_domains(raw: str | None) -> set[str]:
    return {
        item.strip().lower().strip(".")
        for item in (raw or "").split(",")
        if item.strip()
    }


def _host_matches_suffix(host: str, suffixes: set[str]) -> str | None:
    normalized = (host or "").lower().strip(".")
    for suffix in sorted(suffixes, key=len, reverse=True):
        if normalized == suffix or normalized.endswith("." + suffix):
            return suffix
    return None


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


def _is_document_url(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in DOC_EXTS)
    except Exception:
        return False


def _is_video_url(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in VIDEO_EXTS)
    except Exception:
        return False


_DOC_CONTENT_TYPES = (
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument",
    "application/rtf",
    "text/rtf",
    "text/csv",
)


def _is_document_content_type(ct: str) -> bool:
    """True for office/document MIME types. Deliberately excludes text/plain and
    text/html so we don't hoover up arbitrary text endpoints — .txt/.csv files
    are caught by extension instead."""
    return any(t in ct for t in _DOC_CONTENT_TYPES)


def _ext_from_known(url: str, exts: set[str], default: str) -> str:
    try:
        path = urlparse(url).path.lower()
        for ext in exts:
            if path.endswith(ext):
                return ext.lstrip(".")
    except Exception:
        pass
    return default


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
        self._domain_pacing = DomainPacer(
            self.SOURCE_NAME,
            env_prefix="WEBSITE",
            max_active_domains=4,
            max_per_domain=2,
            delay_seconds=1.5,
            jitter_seconds=2.0,
        )
        # HTTP
        self._timeout = httpx.Timeout(float(os.getenv("WEBSITE_TIMEOUT", "30")), connect=10.0)
        self._target_timeout = float(os.getenv("WEBSITE_TARGET_TIMEOUT_SECONDS", "1500"))
        self._target_retry_backoff = float(os.getenv("WEBSITE_TARGET_RETRY_BACKOFF_SECONDS", "21600"))
        self._target_retry_backoff_max = float(os.getenv("WEBSITE_TARGET_RETRY_BACKOFF_MAX_SECONDS", "604800"))
        # Behaviour toggles
        self._respect_robots = os.getenv("WEBSITE_RESPECT_ROBOTS", "1") == "1"
        self._robots_policy = os.getenv("WEBSITE_ROBOTS_POLICY", "respect").strip().lower() or "respect"
        self._robots_override_domains = _csv_domains(os.getenv("WEBSITE_ROBOTS_OVERRIDE_DOMAINS"))
        self._robots_override_seen: set[str] = set()
        self._follow_external = os.getenv("WEBSITE_FOLLOW_EXTERNAL", "0") == "1"
        self._download_images = os.getenv("WEBSITE_DOWNLOAD_IMAGES", "1") == "1"
        self._download_pdfs = os.getenv("WEBSITE_DOWNLOAD_PDFS", "1") == "1"
        # Office documents (word/ppt/excel/text) and videos. Videos are streamed
        # to disk with NO size cap (user decision); docs keep a sane byte cap.
        self._download_docs = os.getenv("WEBSITE_DOWNLOAD_DOCS", "1") == "1"
        self._download_videos = os.getenv("WEBSITE_DOWNLOAD_VIDEOS", "1") == "1"
        self._use_tor = os.getenv("WEBSITE_USE_TOR", "0") == "1"
        self._use_playwright = os.getenv("WEBSITE_USE_PLAYWRIGHT", "0") == "1"
        # Quality gates
        self._min_image_bytes = int(os.getenv("WEBSITE_MIN_IMAGE_BYTES", "1024"))
        self._max_image_bytes = int(os.getenv("WEBSITE_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))
        self._min_image_dim = int(os.getenv("WEBSITE_MIN_IMAGE_DIM", "32"))
        self._max_pdf_bytes = int(os.getenv("WEBSITE_MAX_PDF_BYTES", str(50 * 1024 * 1024)))
        self._max_doc_bytes = int(os.getenv("WEBSITE_MAX_DOC_BYTES", str(50 * 1024 * 1024)))
        # 0 = no cap (user chose uncapped web video). A positive value caps the
        # streamed video size (bytes) as a safety valve if disk pressure appears.
        self._max_video_bytes = int(os.getenv("WEBSITE_MAX_VIDEO_BYTES", "0"))
        # Chunk size for streaming video to disk (default 1 MiB).
        self._video_chunk_bytes = int(os.getenv("WEBSITE_VIDEO_CHUNK_BYTES", str(1024 * 1024)))
        # URL filter (allow/block wildcard patterns)
        self._url_filter = URLFilter.from_env(
            "WEBSITE_URL_ALLOW",
            "WEBSITE_URL_BLOCK",
            policy_file_var="WEBSITE_URL_POLICY_FILE",
            policy_file_default=str(DEFAULT_URL_POLICY_FILE),
        )
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

    async def _record_domain_pacing_event(
        self,
        event_type: str,
        url: str,
        *,
        status_code: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await record_domain_pacing_event(
            self.pool,
            source=self.SOURCE_NAME,
            event_type=event_type,
            url=url,
            status_code=status_code,
            metadata=metadata,
        )

    def _robots_override_match(self, url: str) -> str | None:
        if self._robots_policy != "allowlist_override" or not self._robots_override_domains:
            return None
        host = urlparse(url).hostname or urlparse(url).netloc
        return _host_matches_suffix(host, self._robots_override_domains)

    def _should_respect_robots(self, url: str) -> bool:
        if not self._respect_robots:
            return False
        if self._robots_override_match(url):
            return False
        return True

    async def _record_robots_override(self, url: str) -> None:
        matched = self._robots_override_match(url)
        if not matched:
            return
        host = urlparse(url).hostname or urlparse(url).netloc or url
        key = f"{host}:{matched}"
        if key in self._robots_override_seen:
            return
        self._robots_override_seen.add(key)
        self._domain_pacing.count("robots_override")
        await self._record_domain_pacing_event(
            "robots_override",
            url,
            metadata={
                "policy": self._robots_policy,
                "matched_domain": matched,
                "reason": "explicit_owned_or_authorized_domain",
            },
        )

    async def collect(self, targets: list[str]) -> None:
        seed_pairs: list[tuple[str, str]] = [
            (url, url if url.startswith(("http://", "https://")) else f"https://{url}")
            for url in targets
        ]
        by_seed: dict[str, list[tuple[str, str]]] = {}
        for pair in seed_pairs:
            by_seed.setdefault(pair[1], []).append(pair)
        ordered_pairs: list[tuple[str, str]] = []
        for seed in self._domain_pacing.order(seed for _url, seed in seed_pairs):
            ordered_pairs.append(by_seed[seed].pop(0))

        async def _collect_one(url: str, seed: str) -> None:
            if self._stop.is_set():
                return
            backoff_left = await self._target_backoff_left(url, seed)
            if backoff_left > 0:
                logger.info(
                    "Skipping website/%s: retry backoff %.0fs remaining after prior timeout",
                    seed,
                    backoff_left,
                )
                self._domain_pacing.count("retry_backoff")
                await self._record_domain_pacing_event(
                    "retry_backoff",
                    seed,
                    metadata={"backoff_seconds_remaining": round(backoff_left, 1)},
                )
                await self.checkpoint.save_progress(seed)
                return
            logger.info("Collecting website/%s", seed)
            try:
                stats = await asyncio.wait_for(
                    self.spider_domain(seed, max_depth=self._max_depth, max_pages=self._max_pages),
                    timeout=self._target_timeout,
                )
                await self._mark_target_finished(url, seed, stats)
                await self.checkpoint.save_progress(seed)
            except asyncio.TimeoutError:
                msg = f"target exceeded {self._target_timeout:.0f}s wall-clock cap"
                logger.info("Deferred website/%s: %s", seed, msg)
                await self._defer_target(url, seed, msg)
                await self.checkpoint.save_progress(seed)
            except Exception as e:
                logger.error("Failed website/%s: %s", seed, e)
                await self._defer_target(url, seed, str(e))
                await self.checkpoint.save_progress(seed)
                try:
                    await self.send_to_dlq(seed, seed, str(e))
                except Exception:
                    pass

        if ordered_pairs:
            workers = min(
                len(ordered_pairs),
                max(1, int(os.getenv("WEBSITE_TARGET_CONCURRENCY", str(self._domain_pacing.max_active_domains)))),
                self._domain_pacing.max_active_domains,
            )
            if workers <= 1:
                for url, seed in ordered_pairs:
                    await _collect_one(url, seed)
            else:
                queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
                for pair in ordered_pairs:
                    queue.put_nowait(pair)

                async def _worker() -> None:
                    while not queue.empty() and not self._stop.is_set():
                        pair = await queue.get()
                        try:
                            await _collect_one(*pair)
                        finally:
                            queue.task_done()

                await asyncio.gather(*(_worker() for _ in range(workers)))

        # Broad discovery runs after explicit targets so it cannot starve root
        # crawls when the discovered frontier is large.
        try:
            await self._promote_discovered_sg_domains(
                cap=int(os.getenv("WEBSITE_AUTODISCOVER_CAP", "50")))
        except Exception as e:
            logger.debug("website autodiscover failed: %s", e)

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
        headers, _persona_metadata = build_persona_headers(headers, f"https://{domain or 'example.com'}/", source=self.SOURCE_NAME)
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
            respect_robots = self._should_respect_robots(url)
            if respect_robots:
                await self._robots.load(url, client)
            else:
                await self._record_robots_override(url)
            if respect_robots and not self._robots.is_allowed(url):
                logger.debug("robots.txt disallows %s", url)
                self._domain_pacing.count("robots_blocked")
                await self._record_domain_pacing_event("robots_blocked", url)
                return None
            async with self._domain_pacing.slot(url):
                await self.wait_rate_limit(domain)
                async with self._sem:
                    resp = await client.get(url)
            if resp.status_code in (403, 429):
                event = f"http_{resp.status_code}"
                self._domain_pacing.count(event)
                await self._record_domain_pacing_event(event, url, status_code=resp.status_code)
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
        stats = {"pages": 0, "images": 0, "pdfs": 0, "docs": 0, "videos": 0,
                 "errors": 0, "skipped": 0, "robots_blocked": 0,
                 "robots_override": 0, "http_403": 0, "http_429": 0}

        client = self._build_client(domain)
        try:
            respect_seed_robots = self._should_respect_robots(seed_url)
            if respect_seed_robots:
                await self._robots.load(seed_url, client)
                # Surface robots.txt-listed sitemaps for richer discovery.
                for sm in self._robots.sitemaps_for(seed_url):
                    await self._ingest_sitemap(client, sm, queue, visited, domain)
            else:
                await self._record_robots_override(seed_url)
                stats["robots_override"] += 1

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
                respect_url_robots = self._should_respect_robots(url)
                if not respect_url_robots:
                    await self._record_robots_override(url)
                if respect_url_robots and not self._robots.is_allowed(url):
                    stats["skipped"] += 1
                    stats["robots_blocked"] += 1
                    self._domain_pacing.count("robots_blocked")
                    await self._record_domain_pacing_event("robots_blocked", url)
                    continue

                # Videos are streamed to disk (no cap) — detect by extension and
                # handle BEFORE the blanket client.get() below, which would
                # otherwise buffer a multi-GB body into memory and OOM the worker.
                if self._download_videos and _is_video_url(url):
                    if url not in self._seen_media:
                        self._seen_media.add(url)
                        try:
                            async with self._domain_pacing.slot(url):
                                await self.wait_rate_limit(domain)
                                async with self._sem:
                                    if await self._stream_video(client, url, domain):
                                        self._domain_pacing.count("videos_found")
                                        stats["videos"] += 1
                        except RuntimeError as e:
                            text = str(e)
                            if "status 403" in text or "status 429" in text:
                                code = 429 if "status 429" in text else 403
                                key = f"http_{code}"
                                stats[key] += 1
                                self._domain_pacing.count(key)
                                await self._record_domain_pacing_event(key, url, status_code=code)
                            logger.debug("video stream failed %s: %s", url, e)
                            stats["errors"] += 1
                        except Exception as e:
                            logger.debug("video stream failed %s: %s", url, e)
                            stats["errors"] += 1
                    continue

                try:
                    async with self._domain_pacing.slot(url):
                        await self.wait_rate_limit(domain)
                        async with self._sem:
                            resp = await client.get(url)
                except Exception as e:
                    logger.debug("GET %s failed: %s", url, e)
                    stats["errors"] += 1
                    self.rate_limiter.report_failure(domain) if hasattr(self.rate_limiter, "report_failure") else None
                    continue

                if resp.status_code != 200:
                    stats["errors"] += 1
                    if resp.status_code in (403, 429):
                        key = f"http_{resp.status_code}"
                        stats[key] += 1
                        self._domain_pacing.count(key)
                        await self._record_domain_pacing_event(key, url, status_code=resp.status_code)
                    continue
                ct = resp.headers.get("content-type", "").lower()

                # PDF
                if "application/pdf" in ct or _is_pdf_url(url):
                    if self._download_pdfs:
                        await self._handle_pdf(resp.content, url, domain)
                        self._domain_pacing.count("pdfs_found")
                        stats["pdfs"] += 1
                    continue

                # Office documents (word/ppt/excel/text). Detect by extension or
                # by a non-HTML office/text content-type. resp.content is already
                # buffered here, which is fine — docs are byte-capped.
                if self._download_docs and (
                    _is_document_url(url) or _is_document_content_type(ct)
                ):
                    await self._handle_document(resp.content, url, domain)
                    self._domain_pacing.count("docs_found")
                    stats["docs"] += 1
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
                            self._domain_pacing.count("media_found")
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
            "Spider %s done: pages=%d images=%d pdfs=%d docs=%d videos=%d "
            "errors=%d skipped=%d",
            domain, stats["pages"], stats["images"], stats["pdfs"],
            stats["docs"], stats["videos"], stats["errors"], stats["skipped"],
        )
        await self._record_domain_pacing_event(
            "crawl_summary",
            seed_url,
            metadata={
                **stats,
                "domain_pacing": self._domain_pacing.snapshot().as_dict(),
            },
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
            async with self._domain_pacing.slot(sitemap_url):
                await self.wait_rate_limit(seed_domain)
                resp = await client.get(sitemap_url)
            if resp.status_code != 200:
                if resp.status_code in (403, 429):
                    event = f"http_{resp.status_code}"
                    self._domain_pacing.count(event)
                    await self._record_domain_pacing_event(event, sitemap_url, status_code=resp.status_code)
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
                browser = await p.chromium.launch(
                    headless=True,
                    args=PLAYWRIGHT_LAUNCH_ARGS,
                )
                try:
                    context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
                    page = await context.new_page()
                    await headless_dwell("website render goto")
                    await page.goto(url, wait_until="networkidle", timeout=30_000)
                    await headless_dwell("website render content")
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
        img_domain = urlparse(img_url).netloc or entity_id
        # HEAD first to short-circuit oversized downloads.
        try:
            async with self._domain_pacing.slot(img_url):
                await self.wait_rate_limit(img_domain)
                head = await client.head(img_url, timeout=15.0)
            if head.status_code in (403, 429):
                event = f"http_{head.status_code}"
                self._domain_pacing.count(event)
                await self._record_domain_pacing_event(event, img_url, status_code=head.status_code)
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
            async with self._domain_pacing.slot(img_url):
                await self.wait_rate_limit(img_domain)
                resp = await client.get(img_url, timeout=30.0)
        except Exception as e:
            logger.debug("img GET failed %s: %s", img_url, e)
            return
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                event = f"http_{resp.status_code}"
                self._domain_pacing.count(event)
                await self._record_domain_pacing_event(event, img_url, status_code=resp.status_code)
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

    async def _handle_document(self, doc_data: bytes, source_url: str, domain: str) -> None:
        """Persist an office/text document (word/ppt/excel/csv/txt/rtf) into
        media_items with content_type 'document'. Byte-capped; audio/code/html
        never reach here (excluded via SKIP_EXTS + content-type gate)."""
        if not doc_data:
            return
        if len(doc_data) > self._max_doc_bytes:
            logger.debug("doc too large, skipping: %s (%d)", source_url, len(doc_data))
            return
        cid = hashlib.sha256(doc_data).hexdigest()[:16]
        if self.is_known(cid):
            return
        await self.download_media({
            "entity_id": domain,
            "entity_name": domain,
            "content_type": "document",
            "content_id": cid,
            "url": source_url,
            "extension": _ext_from_known(source_url, DOC_EXTS, "bin"),
            "source_url": source_url,
            "data": doc_data,
        })

    async def _stream_video(self, client: httpx.AsyncClient, video_url: str,
                            domain: str) -> bool:
        """Stream a video to disk in chunks (never buffered whole) and register
        it in media_items as content_type 'video'. No size cap by default
        (WEBSITE_MAX_VIDEO_BYTES=0); a positive cap aborts + discards an
        over-large file. Returns True if a video row was written.

        Dedup is by content SHA computed while streaming, so a partial/aborted
        download leaves no media_items row and can be retried next cycle.
        """
        cid = hashlib.sha256(video_url.encode("utf-8")).hexdigest()[:16]
        if self.is_known(cid):
            return False
        ext = _ext_from_known(video_url, VIDEO_EXTS, "mp4")
        dest_dir = self.account_media_dir / "video"
        assert_media_write_allowed(dest_dir / f"video_{cid}.{ext}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        size = 0
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                async with client.stream("GET", video_url) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"status {resp.status_code}")
                    ct = resp.headers.get("content-type", "").lower()
                    if ct and not (ct.startswith("video/")
                                   or ct in ("application/octet-stream",)
                                   or _is_video_url(video_url)):
                        raise RuntimeError(f"non-video content-type {ct}")
                    async for chunk in resp.aiter_bytes(self._video_chunk_bytes):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if self._max_video_bytes and size > self._max_video_bytes:
                            raise RuntimeError(
                                f"video exceeds cap {self._max_video_bytes}")
                        f.write(chunk)
                        hasher.update(chunk)
            if size == 0:
                raise RuntimeError("empty video body")
            sha = hasher.hexdigest()
            filename = self.build_filename(domain, domain, "video", cid, extension=ext)
            metadata = {
                "entity_id": domain,
                "entity_name": domain,
                "content_type": "video",
                "content_id": cid,
                "source_url": video_url,
                "file_size": size,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "rebuild_target_tables": ["media_items", "website_pages", "website_targets"],
            }
            artifact = write_atomic_artifact_from_path(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                source_path=tmp_path,
                extension=ext,
                expected_sha256=sha,
                metadata={
                    **metadata,
                    "filename": filename,
                    "request_url": video_url,
                },
                root=VAULT_ROOT,
                delete_source=True,
            )
            if tmp_path and not os.path.exists(tmp_path):
                tmp_path = None
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            await self.insert_media_item(
                entity_id=domain,
                entity_name=domain,
                content_type="video",
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                source_url=video_url,
                metadata=metadata,
            )
            if artifact.partial:
                await self.send_to_dlq(domain, cid, f"vault artifact partial: {artifact.error}")
            self._known_ids.add(cid)
            logger.debug("video stored: %s (%d bytes)", video_url, size)
            return True
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

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
        try:
            data = item.get("data")
            if data is None:
                async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                    resp = await client.get(item["url"])
                    resp.raise_for_status()
                    data = resp.content
            sha = self.sha256_bytes(data)
            source_url = item.get("source_url") or item.get("url")
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "source_url": source_url,
                "alt": item.get("alt"),
                "width": item.get("width"),
                "height": item.get("height"),
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "website_pages", "website_targets"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=ext,
                expected_sha256=sha,
                metadata={
                    **metadata,
                    "filename": filename,
                    "request_url": item.get("url"),
                },
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }
            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                width=item.get("width"),
                height=item.get("height"),
                sha256=artifact.sha256,
                source_url=source_url,
                metadata=metadata,
            )
            if artifact.partial:
                await self.send_to_dlq(item["entity_id"], cid, f"vault artifact partial: {artifact.error}")
            self._known_ids.add(cid)
        except Exception as e:
            logger.debug("download_media failed %s: %s", item.get("url"), e)

    # ------------------------------------------------------------------ #
    # DB persistence
    # ------------------------------------------------------------------ #

    async def _promote_discovered_sg_domains(self, cap: int = 50) -> int:
        """PERPETUAL DISCOVERY: mine external links from recently-crawled pages for
        NEW Singapore (.sg) domains and queue them into collection_targets so the
        crawler picks them up next cycle. Each newly-crawled .sg site reveals more
        .sg links -> the crawl frontier keeps expanding (was static: the collector
        discovered sites but never added them). Gated + bounded + .sg-only so it
        stays Singapore-focused and doesn't crawl the whole web."""
        if os.getenv("WEBSITE_AUTODISCOVER_SG", "true").lower() != "true" or not self.pool:
            return 0
        scan = int(os.getenv("WEBSITE_AUTODISCOVER_SCAN", "20000"))
        candidates: list[str] = []
        try:
            async with self.pool.acquire() as conn:
                # Source 1: external links from recently-crawled SG pages.
                for r in await conn.fetch(
                        "SELECT DISTINCT unnest(external_links) AS link FROM website_pages "
                        "WHERE collected_at > now() - interval '3 days' AND external_links IS NOT NULL "
                        f"LIMIT {scan}"):
                    candidates.append(r["link"])
                # Source 2 (richer): .sg domains from recent search results.
                for r in await conn.fetch(
                        "SELECT DISTINCT domain FROM search_results "
                        "WHERE domain LIKE '%.sg' AND collected_at > now() - interval '30 days' "
                        f"LIMIT {scan}"):
                    candidates.append(r["domain"])
        except Exception as e:
            logger.debug("autodiscover scan failed: %s", e)
            return 0
        added, seen = 0, set()
        async with self.pool.acquire() as conn:
            for cand in candidates:
                if added >= cap:
                    break
                try:
                    # cand may be a full URL (external_links) or a bare domain (search).
                    host = (urlparse(cand).netloc or cand).lower().split(":")[0]
                except Exception:
                    continue
                if host.startswith("www."):
                    host = host[4:]
                if not host or host in seen or not host.endswith(".sg"):
                    continue
                seen.add(host)
                # Skip if already a target or already crawled.
                if await conn.fetchval("SELECT 1 FROM collection_targets WHERE source='website' AND target_id=$1", host):
                    continue
                if await conn.fetchval("SELECT 1 FROM website_targets WHERE domain=$1", host):
                    continue
                await conn.execute(
                    "INSERT INTO collection_targets (source, target_id, target_name, status, priority) "
                    "VALUES ('website', $1, $1, 'pending', 0) ON CONFLICT (source, target_id) DO NOTHING",
                    host)
                added += 1
        if added:
            logger.info("website: perpetual discovery queued %d new .sg domains for crawling", added)
        return added

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

    async def _mark_target_finished(
        self,
        target_id: str,
        seed_url: str,
        stats: dict[str, Any],
    ) -> None:
        """Mark a configured website target as attempted so one site cannot
        recrawl forever from the top of the queue.
        """
        if not self.pool:
            return
        domain = urlparse(seed_url).netloc
        target_ids = list({target_id, seed_url, seed_url.rstrip("/")})
        pages = int(stats.get("pages") or 0)
        images = int(stats.get("images") or 0)
        pdfs = int(stats.get("pdfs") or 0)
        docs = int(stats.get("docs") or 0)
        videos = int(stats.get("videos") or 0)
        errors = int(stats.get("errors") or 0)
        summary = (
            f"last crawl: pages={pages} images={images} pdfs={pdfs} "
            f"docs={docs} videos={videos} errors={errors}"
        )
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE collection_targets
                       SET collection_count = collection_count + 1,
                           last_collection_at = NOW(),
                           status = 'completed',
                           error_message = $2
                     WHERE source = 'website'
                       AND target_id = ANY($1::text[])
                    """,
                    target_ids,
                    summary[:1000],
                )
                await conn.execute(
                    """
                    UPDATE website_targets
                       SET status = 'completed',
                           collected_at = NOW(),
                           updated_at = NOW()
                     WHERE domain = $1
                    """,
                    domain,
                )
        except Exception as e:
            logger.debug("mark website target finished failed for %s: %s", seed_url, e)

    async def _target_backoff_left(self, target_id: str, seed_url: str) -> float:
        """Return seconds left before retrying a target that timed out.

        Static configured targets are still passed to this collector even when
        their DB status is error. This guard keeps slow sites from monopolising
        cycles after every worker restart while preserving scheduled retries.
        """
        if not self.pool or self._target_retry_backoff <= 0:
            return 0.0
        target_ids = list({target_id, seed_url, seed_url.rstrip("/")})
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT last_collection_at,
                           COALESCE(collection_count, 0) AS collection_count,
                           error_message
                      FROM collection_targets
                     WHERE source = 'website'
                       AND target_id = ANY($1::text[])
                       AND status = 'error'
                       AND last_collection_at IS NOT NULL
                       AND COALESCE(error_message, '') ILIKE 'target exceeded%'
                     ORDER BY last_collection_at DESC
                     LIMIT 1
                    """,
                    target_ids,
                )
                if not row:
                    return 0.0
                last_collection_at = row.get("last_collection_at")
                if not isinstance(last_collection_at, datetime):
                    return 0.0
                if last_collection_at.tzinfo is None:
                    last_collection_at = last_collection_at.replace(tzinfo=timezone.utc)
                attempts = max(1, int(row.get("collection_count") or 0))
                multiplier = 2 ** min(attempts - 1, 8)
                backoff = self._target_retry_backoff * multiplier
                if self._target_retry_backoff_max > 0:
                    backoff = min(backoff, self._target_retry_backoff_max)
                elapsed = (datetime.now(timezone.utc) - last_collection_at).total_seconds()
                return max(0.0, float(backoff) - max(0.0, elapsed))
        except Exception as e:
            logger.debug("website target backoff lookup failed for %s: %s", seed_url, e)
            return 0.0

    async def _defer_target(self, target_id: str, seed_url: str, reason: str) -> None:
        """Move a failing website target behind fresh pending targets.

        The generic worker still retries status='error' rows eventually, but
        lowering priority prevents a single slow/broken site from starving the
        whole website backlog after every watchdog restart.
        """
        if not self.pool:
            return
        domain = urlparse(seed_url).netloc
        target_ids = list({target_id, seed_url, seed_url.rstrip("/")})
        error = reason[:1000]
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE collection_targets
                       SET status = 'error',
                           collection_count = collection_count + 1,
                           priority = LEAST(COALESCE(priority, 0), 0),
                           last_collection_at = NOW(),
                           error_message = $2
                     WHERE source = 'website'
                       AND target_id = ANY($1::text[])
                    """,
                    target_ids,
                    error,
                )
                await conn.execute(
                    """
                    UPDATE website_targets
                       SET status = 'error',
                           updated_at = NOW()
                     WHERE domain = $1
                    """,
                    domain,
                )
        except Exception as e:
            logger.debug("defer website target failed for %s: %s", seed_url, e)

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
