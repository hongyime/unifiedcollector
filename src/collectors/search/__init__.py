"""Search collector — multi-engine search → spider → quality-gated download.

This module subsumes ``searchtoolkit/`` (a prior standalone toolkit) and routes
all cross-cutting concerns through Wave 0 cores so that search work shares
infrastructure with the other 11 collectors.

ABSORBED FROM ``searchtoolkit/``
--------------------------------
* ``main.py``                   — entry-point shim (replaced by unified scheduler).
* ``src/app.py::search_duckduckgo`` — DDG text + image search → ``_search_ddg``.
* ``src/app.py::search_bing``       — Bing HTML scrape → ``_search_bing``.
* ``src/app.py::search_serper``     — Serper.dev (Google) API → ``_search_serper``,
                                      with account_quota tracking on the API key.
* ``src/app.py::get_search_results``— DDG → Bing → Serper waterfall → ``search_query``.
* ``src/app.py::spider_page``       — HTML → image/PDF URL extraction →
                                      ``expand_paste_sites`` + ``_spider_page``.
* ``src/app.py::is_content_url``    — icon/sprite filter → ``_is_content_url``.
* ``src/app.py::download_with_quality_gate`` — size/dimension/dedup gate →
                                      ``_quality_gated_download``.
* ``src/app.py::process_image``     — PIL JPG re-encode → ``_save_image_as_jpg``.
* ``src/app.py::extract_pdf_pages_as_jpg`` — PyMuPDF page rasterise →
                                      ``_extract_pdf_pages``.
* ``src/app.py::DeduplicationTracker`` — in-memory sha256 set → ``self._dedup``.
* ``src/tor_manager.py``        — subprocess Tor daemon. Replaced by the
                                  unified ``src.core.tor_proxy`` factory
                                  (TOR_PROXY_ENABLED=1 routes via sidecar).
* ``src/rate_limiter.py``       — adaptive per-domain backoff. Replaced by the
                                  ``AdaptiveRateLimiter`` from BaseCollector.
* ``src/search_cache.py``       — file-based TTL cache → ``src.core.search_cache``.
* ``src/state_manager.py``      — toolkit's SQLite state. Replaced by the
                                  unified Postgres ``search_queries`` /
                                  ``search_results`` tables + the checkpoint
                                  manager.

DROPPED (NOT PORTED)
--------------------
* ``src/app.py::search_chrome`` — undetected-chromedriver Selenium fallback. Heavy
  binary dep, slow, and we already have DDG + Bing + Serper.
* ``mode_search_extract`` / ``mode_bing_images`` / ``mode_dork_runner`` interactive
  CLI menus. Replaced by ``run(targets)`` + the unified scheduler.
* Standalone web UI (Flask). Unified dashboard covers it.
* ``download_path_manager`` interactive prompts. We use ``self.media_dir``.
* ``setup.bat`` / ``quick_actions.bat`` Windows launchers — dev-only.
* Bing-image-downloader format/quality CLI filters. Folded into the generic
  quality gate.
* Scrape-and-post / writes to remote services — we are READ-ONLY ingest.

DEFERRED
--------
* Tor circuit rotation (NEWNYM) on per-engine 429. Plumbing is in place via
  ``src.core.tor_proxy.new_circuit()``; firing it after a 429 is a 1-line
  follow-up.
* Brave / Exa engines (no toolkit code; would be net-new wrappers).
* PDF page extraction is wired but optional (skipped if pymupdf import fails).

ENVIRONMENT VARS
----------------
SERPER_API_KEY                 Serper.dev API key (optional; conserves Google).
SERPER_DAILY_QUOTA             Daily request cap for the Serper key (default 2500).
SEARCH_MAX_RESULTS             Max results per query across engines (default 50).
SEARCH_MIN_DIMENSION           Reject images smaller than NxN px (default 200).
SEARCH_MIN_FILE_SIZE           Reject files below N bytes (default 10240).
SEARCH_MAX_PDF_PAGES           Cap PDF page extraction (default 50).
SEARCH_CACHE_TTL_HOURS         Search-result cache TTL (default 24).
SEARCH_CONCURRENT_DOWNLOADS    Parallel asset downloads (default 5).
SEARCH_DOWNLOAD_IMAGES         '1' to download images for each result URL (default 1).
SEARCH_SPIDER_PAGES            '1' to fetch HTML and extract embedded media
                               (default 0; opt-in because it amplifies traffic).
SEARCH_BING_PAGES              Pages of Bing results to scrape (default 3).
SEARCH_SERPER_THRESHOLD        Use Serper only when DDG+Bing < this many results
                               (default 5; conserves API credits).
TOR_PROXY_ENABLED              '1' to route HTTP through the Tor sidecar.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.search.parse import (
    is_content_url as _parse_is_content_url,
    CONTENT_EXTENSIONS as _parse_CONTENT_EXTENSIONS,
    ICON_KEYWORDS as _parse_ICON_KEYWORDS,
)
from src.core.search_cache import SearchCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pulled from searchtoolkit/src/app.py
# ---------------------------------------------------------------------------

CONTENT_EXTENSIONS = _parse_CONTENT_EXTENSIONS
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".jfif"}

ICON_KEYWORDS = _parse_ICON_KEYWORDS
SKIP_EXTENSIONS = {".svg", ".webp", ".ico", ".cur", ".gif"}

DEFAULT_DDG_DOMAIN = "https://duckduckgo.com"
DEFAULT_BING_DOMAIN = "https://www.bing.com"
DEFAULT_SERPER_DOMAIN = "https://google.serper.dev"


# ---------------------------------------------------------------------------
# Optional-import shims — keep search.py importable even when an optional
# dep is missing in a slim image. Each engine self-disables if its driver
# isn't on path; the collector keeps running with the engines that work.
# ---------------------------------------------------------------------------

def _try_import_ddgs():
    try:
        from ddgs import DDGS  # type: ignore
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
            return DDGS
        except ImportError:
            return None


def _try_import_bs4():
    try:
        from bs4 import BeautifulSoup  # type: ignore
        return BeautifulSoup
    except ImportError:
        return None


def _try_import_pil():
    try:
        from PIL import Image  # type: ignore
        Image.MAX_IMAGE_PIXELS = None
        return Image
    except ImportError:
        return None


def _try_import_fitz():
    try:
        import fitz  # type: ignore  # PyMuPDF
        return fitz
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class SearchCollector(BaseCollector):
    """Multi-engine search → spider → quality-gated download.

    ``run(targets)`` consumes a list of search queries. For each query we:
        1. Hit DDG (text + image) → Bing → Serper in a waterfall.
        2. Persist hits to ``search_queries`` / ``search_results``.
        3. Optionally spider each result page for embedded images/PDFs.
        4. Quality-gate + dedup + atomically save the binaries.
    """

    SOURCE_NAME = "search"

    # Engines we know how to drive. Order matters — the waterfall consults
    # them in this sequence and short-circuits when ``max_results`` is met.
    DEFAULT_ENGINES = ("ddg", "bing", "serper")

    def __init__(self):
        super().__init__()
        # Tunables
        self._max_results = int(os.getenv("SEARCH_MAX_RESULTS", "50"))
        self._min_dim = int(os.getenv("SEARCH_MIN_DIMENSION", "200"))
        self._min_file_size = int(os.getenv("SEARCH_MIN_FILE_SIZE", "10240"))
        self._max_pdf_pages = int(os.getenv("SEARCH_MAX_PDF_PAGES", "50"))
        self._download_images = os.getenv("SEARCH_DOWNLOAD_IMAGES", "1") == "1"
        self._spider_pages = os.getenv("SEARCH_SPIDER_PAGES", "1") == "1"
        self._bing_pages = int(os.getenv("SEARCH_BING_PAGES", "3"))
        self._serper_threshold = int(os.getenv("SEARCH_SERPER_THRESHOLD", "5"))
        self._concurrent_downloads = int(os.getenv("SEARCH_CONCURRENT_DOWNLOADS", "5"))
        self._serper_api_key = os.getenv("SERPER_API_KEY", "").strip()
        self._serper_quota = int(os.getenv("SERPER_DAILY_QUOTA", "2500"))

        # Sub-systems
        cache_dir = Path("data") / "search_cache"
        self._cache = SearchCache(
            cache_dir=cache_dir,
            ttl_hours=float(os.getenv("SEARCH_CACHE_TTL_HOURS", "24")),
        )
        self._sem = asyncio.Semaphore(self._concurrent_downloads)

        # Optional drivers — None means engine is disabled.
        self._DDGS = _try_import_ddgs()
        self._BS = _try_import_bs4()
        self._PIL = _try_import_pil()
        self._fitz = _try_import_fitz()

        # In-memory sha256 dedup (per-process). Disk-first dedup via _known_ids
        # handled by BaseCollector.
        self._content_hashes: set[str] = set()
        self._duplicates_skipped = 0

        # Register Serper quota with the unified tracker if available.
        self._register_serper_quota()

    # ------------------------------------------------------------------ #
    # Tor / HTTP plumbing
    # ------------------------------------------------------------------ #

    def _make_client(self, timeout: float = 30.0, follow_redirects: bool = True) -> httpx.AsyncClient:
        """Build an httpx client routed via Tor sidecar when enabled.

        Falls back to a direct httpx.AsyncClient if the tor_proxy import
        path is unavailable (keeps tests cheap).
        """
        try:
            from src.core.tor_proxy import is_enabled as _tor_enabled, _socks_url  # type: ignore
            if _tor_enabled():
                proxy = _socks_url()
                return httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=follow_redirects,
                    max_redirects=5,
                    proxy=proxy,
                )
        except Exception:
            pass
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=follow_redirects,
            max_redirects=5,
        )

    def _headers(self, domain: str = "") -> dict[str, str]:
        ua = (
            self.user_agents.get_for_domain(domain)
            if domain
            else self.user_agents.get_random()
        )
        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }

    # ------------------------------------------------------------------ #
    # Quota wiring
    # ------------------------------------------------------------------ #

    def _register_serper_quota(self) -> None:
        """Register the Serper API key under account_quota. Best-effort."""
        if not self._serper_api_key:
            return
        try:
            from src.core.account_quota import (
                get_default_tracker,
                QuotaConfig,
            )
            tracker = get_default_tracker()
            if tracker is None:
                return
            tracker.register("serper", QuotaConfig(daily_limit=self._serper_quota))
        except Exception as e:
            logger.debug("serper quota registration skipped: %s", e)

    async def _serper_has_quota(self) -> bool:
        if not self._serper_api_key:
            return False
        try:
            from src.core.account_quota import get_default_tracker
            tracker = get_default_tracker()
            if tracker is None:
                return True
            return await tracker.has_quota("serper", self._serper_api_key[:8], 1)
        except Exception:
            return True

    async def _serper_consume(self) -> None:
        try:
            from src.core.account_quota import get_default_tracker
            tracker = get_default_tracker()
            if tracker is None:
                return
            await tracker.consume("serper", self._serper_api_key[:8], 1)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # BaseCollector contract
    # ------------------------------------------------------------------ #

    @property
    def account_media_dir(self) -> Path:
        path = self.media_dir / "default"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        # Pace between queries to reduce search-engine 429s (the ddgs brave
        # backend throttles aggressively). Env-tunable; 0 disables.
        query_delay = float(os.getenv("SEARCH_QUERY_DELAY_SECONDS", "3"))
        # A target that is a URL is a DIRECT seed (open directory / cloud bucket):
        # crawl it straight away (expand_paste_sites -> _spider_page, which also
        # enumerates S3/GCS ListBucketResult buckets). Everything else is a query.
        direct = [t for t in targets if t.startswith(("http://", "https://"))]
        queries = [t for t in targets if not t.startswith(("http://", "https://"))]
        if direct:
            logger.info("Crawling %d direct seed URL(s) (open dir / bucket)", len(direct))
            try:
                await self.expand_paste_sites(direct)
                for u in direct:
                    await self.checkpoint.save_progress(u)
            except Exception as e:
                logger.exception("direct seed crawl failed: %s", e)
        for query in queries:
            if self._stop.is_set():
                break
            logger.info("Collecting search/%s", query)
            try:
                await self.search_query(query)
                await self.checkpoint.save_progress(query)
            except Exception as e:
                logger.exception("Failed search/%s: %s", query, e)
                await self.send_to_dlq(query, query, str(e))
            if query_delay > 0 and not self._stop.is_set():
                await asyncio.sleep(query_delay)

    # ------------------------------------------------------------------ #
    # Public entry points (search_query / expand_paste_sites)
    # ------------------------------------------------------------------ #

    async def search_query(
        self,
        query: str,
        engines: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        """Run a multi-engine search waterfall and persist results.

        Mirrors ``searchtoolkit.app.get_search_results`` but emits structured
        result dicts ``{url, title, snippet, rank, engine, domain}`` and
        persists them to ``search_results``.
        """
        engines = tuple(engines) if engines else self.DEFAULT_ENGINES
        all_results: dict[str, dict] = {}  # url -> result dict (dedupe across engines)
        rank_counter = 0

        # --- 1) DDG ------------------------------------------------------
        if "ddg" in engines and self._DDGS is not None:
            ddg_hits = await self._search_ddg(query)
            for hit in ddg_hits:
                url = hit.get("url")
                if not url or url in all_results:
                    continue
                rank_counter += 1
                hit["rank"] = rank_counter
                all_results[url] = hit
            if len(all_results) >= self._max_results:
                return await self._finalise_query(query, list(all_results.values()))

        # --- 2) Bing -----------------------------------------------------
        if "bing" in engines and self._BS is not None:
            bing_hits = await self._search_bing(query, num_pages=self._bing_pages)
            for hit in bing_hits:
                url = hit.get("url")
                if not url or url in all_results:
                    continue
                rank_counter += 1
                hit["rank"] = rank_counter
                all_results[url] = hit
            if len(all_results) >= self._max_results:
                return await self._finalise_query(query, list(all_results.values()))

        # --- 3) Serper (Google) — only when DDG+Bing came up dry --------
        if (
            "serper" in engines
            and self._serper_api_key
            and len(all_results) < self._serper_threshold
        ):
            if await self._serper_has_quota():
                serper_hits = await self._search_serper(query)
                await self._serper_consume()
                for hit in serper_hits:
                    url = hit.get("url")
                    if not url or url in all_results:
                        continue
                    rank_counter += 1
                    hit["rank"] = rank_counter
                    all_results[url] = hit
            else:
                logger.warning("Serper quota exhausted; skipping for query=%r", query)

        return await self._finalise_query(query, list(all_results.values()))

    async def expand_paste_sites(self, seed_urls: list[str]) -> list[dict]:
        """Spider each seed URL and return discovered image/PDF asset URLs.

        Mirrors ``searchtoolkit.app.spider_page`` but async + uses the unified
        Tor proxy. If ``SEARCH_DOWNLOAD_IMAGES`` is on, kicks off downloads
        for each discovered asset (rank-tagged for traceability).
        """
        if self._BS is None:
            logger.warning("expand_paste_sites: bs4 unavailable; nothing to do")
            return []

        all_assets: list[dict] = []
        depth = int(os.getenv("SEARCH_SPIDER_DEPTH", "2"))   # recurse into sub-dirs/links
        async with self._make_client() as client:
            for seed in seed_urls:
                if self._stop.is_set():
                    break
                try:
                    if depth > 0:
                        # Recursive: walk sub-directories/links so open dirs get fully
                        # crawled and discovered links feed back in (no stagnation).
                        n = await self._crawl_seed(client, seed, max_depth=depth)
                        all_assets.append({"url": seed, "source_url": seed, "engine": "spider", "saved": n})
                    else:
                        domain = urlparse(seed).netloc or "seed"
                        await self.wait_rate_limit(domain)
                        discovered = await self._spider_page(client, seed)
                        for i, url in enumerate(sorted(discovered)):
                            all_assets.append({"url": url, "source_url": seed, "rank": i + 1, "engine": "spider"})
                            if self._download_images:
                                await self._download_asset(query=seed, hit={"url": url, "rank": i + 1}, source_url=seed)
                except Exception as e:
                    logger.warning("expand_paste_sites failed for %s: %s", seed, e)
        return all_assets

    async def _crawl_seed(self, client, seed: str, max_depth: int = 2, max_pages: int = 150) -> int:
        """BFS-crawl a seed URL: download content (images/PDFs), enumerate buckets,
        and recurse into same-host sub-path links (open-directory trees, page links).
        Bounded by max_depth + max_pages so it can't run away."""
        from . import bucket_crawler as _bc
        host = urlparse(seed).netloc
        base_path = urlparse(seed).path.rsplit("/", 1)[0]
        seen: set[str] = set()
        frontier = [(seed, 0)]
        saved = 0
        while frontier and len(seen) < max_pages and not self._stop.is_set():
            url, d = frontier.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                await self.wait_rate_limit(urlparse(url).netloc or host)
                resp = await client.get(url, headers=self._headers(urlparse(url).netloc))
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            ctype = resp.headers.get("content-type", "").lower()
            # open bucket -> enumerate the whole thing
            if ("xml" in ctype or _bc.looks_like_bucket_host(url)) and _bc.is_bucket_listing(resp.text):
                for m in await self._enumerate_bucket(client, url, first_body=resp.text):
                    if self._download_images and await self._download_asset(query=seed, hit={"url": m}, source_url=url):
                        saved += 1
                continue
            if "html" not in ctype:
                if self._is_content_url(url) and self._download_images and await self._download_asset(query=seed, hit={"url": url}, source_url=url):
                    saved += 1
                continue
            try:
                soup = self._BS(resp.text, "html.parser")
            except Exception:
                continue
            for tag in soup.find_all(["a", "img", "source"]):
                href = tag.get("href") or tag.get("src")
                if not href:
                    continue
                cand = urljoin(url, href)
                p = urlparse(cand)
                if self._is_content_url(cand):
                    if self._download_images and await self._download_asset(query=seed, hit={"url": cand}, source_url=url):
                        saved += 1
                elif d < max_depth and p.scheme in ("http", "https") and p.netloc == host \
                        and p.path.startswith(base_path) and cand not in seen and "#" not in cand:
                    frontier.append((cand, d + 1))
        if saved:
            logger.info("spider %s -> %d asset(s) across %d page(s)", seed, saved, len(seen))
        return saved

    # ------------------------------------------------------------------ #
    # Engine drivers
    # ------------------------------------------------------------------ #

    async def _search_ddg(self, query: str) -> list[dict]:
        """DDG text + image search, served via the executor (sync SDK)."""
        cached = self._cache.get(query, "ddg")
        if cached is not None:
            logger.info("[DDG] cache hit for %r (%d)", query, len(cached))
            return cached

        DDGS = self._DDGS
        if DDGS is None:
            return []

        loop = asyncio.get_event_loop()

        def _do_text() -> list[dict]:
            out: list[dict] = []
            try:
                with DDGS() as ddgs:
                    for r in ddgs.text(query, max_results=self._max_results):
                        # ddgs (new) uses 'href'; legacy duckduckgo_search uses 'link'
                        url = r.get("href") or r.get("link") or r.get("url")
                        if not url:
                            continue
                        out.append({
                            "url": url,
                            "title": r.get("title"),
                            "snippet": r.get("body") or r.get("snippet"),
                            "engine": "ddg",
                            "domain": urlparse(url).netloc,
                        })
            except Exception as e:
                logger.warning("[DDG] text search failed: %s", e)
            return out

        def _do_images() -> list[dict]:
            out: list[dict] = []
            try:
                with DDGS() as ddgs:
                    for r in ddgs.images(query, max_results=self._max_results):
                        url = r.get("image") or r.get("url")
                        if not url:
                            continue
                        out.append({
                            "url": url,
                            "title": r.get("title"),
                            "snippet": r.get("source"),
                            "engine": "ddg-img",
                            "domain": urlparse(url).netloc,
                        })
            except Exception as e:
                logger.debug("[DDG] image search failed: %s", e)
            return out

        # Politeness gate — let the rate limiter throttle us.
        await self.wait_rate_limit("duckduckgo.com")

        text_hits = await loop.run_in_executor(None, _do_text)
        image_hits = await loop.run_in_executor(None, _do_images)
        all_hits = text_hits + image_hits
        logger.info("[DDG] %r → %d text, %d images", query, len(text_hits), len(image_hits))

        if all_hits:
            self._cache.put(query, all_hits, engine="ddg")
        return all_hits

    async def _search_bing(self, query: str, num_pages: int = 3) -> list[dict]:
        """Bing HTML scrape — paginated."""
        cached = self._cache.get(query, "bing")
        if cached is not None:
            logger.info("[Bing] cache hit for %r (%d)", query, len(cached))
            return cached

        if self._BS is None:
            return []

        results: list[dict] = []
        seen: set[str] = set()
        encoded_query = quote_plus(query)

        async with self._make_client(timeout=15.0) as client:
            for page in range(0, num_pages * 10, 10):
                if self._stop.is_set():
                    break
                url = f"{DEFAULT_BING_DOMAIN}/search?q={encoded_query}&first={page}"
                await self.wait_rate_limit("bing.com")
                try:
                    resp = await client.get(url, headers=self._headers("bing.com"))
                    if resp.status_code != 200:
                        logger.debug("[Bing] status=%d page=%d", resp.status_code, page)
                        continue
                    soup = self._BS(resp.text, "html.parser")
                    for result in soup.select("li.b_algo"):
                        a = result.select_one("h2 a")
                        if not a:
                            continue
                        link = a.get("href")
                        if not link or not link.startswith("http") or link in seen:
                            continue
                        seen.add(link)
                        title = a.get_text(strip=True)
                        snippet_el = result.select_one("p, .b_caption p")
                        snippet = snippet_el.get_text(strip=True) if snippet_el else None
                        results.append({
                            "url": link,
                            "title": title,
                            "snippet": snippet,
                            "engine": "bing",
                            "domain": urlparse(link).netloc,
                        })
                except Exception as e:
                    logger.warning("[Bing] page=%d error: %s", page, e)
                # Polite jitter between pages
                await asyncio.sleep(random.uniform(1.0, 2.5))

        logger.info("[Bing] %r → %d results", query, len(results))
        if results:
            self._cache.put(query, results, engine="bing")
        return results

    async def _search_serper(self, query: str) -> list[dict]:
        """Serper.dev (Google) JSON API — paginated, regional gl=sg."""
        cached = self._cache.get(query, "serper")
        if cached is not None:
            logger.info("[Serper] cache hit for %r (%d)", query, len(cached))
            return cached

        if not self._serper_api_key:
            return []

        results: list[dict] = []
        seen: set[str] = set()
        url = f"{DEFAULT_SERPER_DOMAIN}/search"
        headers = {
            "X-API-KEY": self._serper_api_key,
            "Content-Type": "application/json",
        }

        async with self._make_client(timeout=15.0) as client:
            for page in range(3):
                if self._stop.is_set():
                    break
                payload = {
                    "q": query,
                    "page": page + 1,
                    "num": 100,
                    "gl": os.getenv("SERPER_GL", "sg"),
                    "hl": os.getenv("SERPER_HL", "en"),
                }
                await self.wait_rate_limit("google.serper.dev")
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 401:
                        logger.error("[Serper] API key unauthorized")
                        break
                    if resp.status_code == 429:
                        logger.warning("[Serper] quota hit, breaking")
                        break
                    if resp.status_code != 200:
                        logger.warning("[Serper] status=%d", resp.status_code)
                        break
                    data = resp.json()
                    organic = data.get("organic", [])
                    if not organic:
                        break
                    for item in organic:
                        link = item.get("link")
                        if not link or link in seen:
                            continue
                        seen.add(link)
                        results.append({
                            "url": link,
                            "title": item.get("title"),
                            "snippet": item.get("snippet"),
                            "engine": "serper",
                            "domain": urlparse(link).netloc,
                            "date_published": item.get("date"),
                        })
                except Exception as e:
                    logger.warning("[Serper] page=%d error: %s", page, e)
                    break

        logger.info("[Serper] %r → %d results", query, len(results))
        if results:
            self._cache.put(query, results, engine="serper")
        return results

    # ------------------------------------------------------------------ #
    # Persistence + post-processing
    # ------------------------------------------------------------------ #

    async def _finalise_query(self, query: str, hits: list[dict]) -> list[dict]:
        """Persist the query+hits then optionally fetch their assets."""
        if not hits:
            return []
        try:
            await self._upsert_query(query)
            for hit in hits:
                await self._upsert_result(query, hit)
        except Exception as e:
            logger.warning("DB persist failed for %r: %s", query, e)

        if self._download_images:
            tasks = []
            for hit in hits:
                tasks.append(self._download_asset(query, hit, source_url=hit.get("url")))
            # Run in parallel, but bounded by self._sem inside _download_asset.
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._spider_pages:
            await self.expand_paste_sites([h["url"] for h in hits if h.get("url")])

        logger.info("Search %r: %d results persisted (tor=%s)", query, len(hits), os.getenv("TOR_PROXY_ENABLED") == "1")
        return hits

    async def _upsert_query(self, query: str) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO search_queries (query, engine)
                VALUES ($1, $2)
                ON CONFLICT (query, engine) DO NOTHING
                """,
                query, "waterfall",
            )

    async def _upsert_result(self, query: str, hit: dict) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM search_queries WHERE query = $1 AND engine = $2",
                query, "waterfall",
            )
            if not row:
                return
            await conn.execute(
                """
                INSERT INTO search_results
                    (query_id, url, title, snippet, rank, domain, date_published)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                row["id"],
                hit["url"],
                hit.get("title"),
                hit.get("snippet"),
                hit.get("rank"),
                hit.get("domain") or urlparse(hit["url"]).netloc,
                hit.get("date_published"),
            )

    # ------------------------------------------------------------------ #
    # Spider (HTML → image/PDF URL extraction)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_content_url(url: str) -> bool:
        """Filter URLs to plausible image/PDF assets, skipping icons/sprites."""
        return _parse_is_content_url(url)

    async def _enumerate_bucket(
        self,
        client: httpx.AsyncClient,
        any_url: str,
        first_body: Optional[str] = None,
    ) -> set[str]:
        """Walk an open S3-style bucket listing and return its media object URLs.

        Bounded by SEARCH_BUCKET_MAX_PAGES / SEARCH_BUCKET_MAX_KEYS. Handles both
        S3 v2 (continuation-token) and v1/GCS (marker) pagination.
        """
        from . import bucket_crawler as _bc

        root = _bc.bucket_root(any_url)
        max_pages = int(os.getenv("SEARCH_BUCKET_MAX_PAGES", "25"))
        max_keys = int(os.getenv("SEARCH_BUCKET_MAX_KEYS", "3000"))
        found: set[str] = set()
        url = root
        body = first_body
        for _page in range(max_pages):
            if body is None:
                try:
                    r = await client.get(url, headers=self._headers(urlparse(url).netloc))
                    if r.status_code != 200:
                        break
                    body = r.text
                except Exception as e:
                    logger.debug("bucket fetch error %s: %s", url, e)
                    break
            if not _bc.is_bucket_listing(body):
                break
            urls, token = _bc.parse_bucket_listing(body, root)
            for m in _bc.media_only(urls):
                found.add(m)
            if not token or len(found) >= max_keys:
                break
            url = _bc.next_page_url(root, token)
            body = None
        if found:
            logger.info("bucket %s -> %d media object(s)", root, len(found))
        return set(list(found)[:max_keys])

    async def _spider_page(
        self,
        client: httpx.AsyncClient,
        page_url: str,
        target_format: str = "all",
    ) -> set[str]:
        """Fetch a page and yield image/PDF URLs that pass _is_content_url."""
        if self._BS is None:
            return set()
        discovered: set[str] = set()
        try:
            resp = await client.get(page_url, headers=self._headers(urlparse(page_url).netloc))
            if resp.status_code != 200:
                return discovered
            ctype = resp.headers.get("content-type", "").lower()
            if "html" not in ctype:
                # Open cloud bucket? Its root returns an S3-style XML listing.
                from . import bucket_crawler as _bc
                if ("xml" in ctype or _bc.looks_like_bucket_host(page_url)) and _bc.is_bucket_listing(resp.text):
                    return await self._enumerate_bucket(client, page_url, first_body=resp.text)
                if self._is_content_url(page_url):
                    discovered.add(page_url)
                return discovered

            soup = self._BS(resp.text, "html.parser")
            candidates: set[str] = set()

            for tag in soup.find_all("img"):
                src = tag.get("src")
                if src:
                    candidates.add(urljoin(page_url, src))
                srcset = tag.get("srcset")
                if srcset:
                    for part in srcset.split(","):
                        first = part.strip().split()[0] if part.strip() else ""
                        if first:
                            candidates.add(urljoin(page_url, first))
            for tag in soup.find_all("a", href=True):
                candidates.add(urljoin(page_url, tag["href"]))
            for tag in soup.find_all("source"):
                srcset = tag.get("srcset")
                if srcset:
                    for part in srcset.split(","):
                        first = part.strip().split()[0] if part.strip() else ""
                        if first:
                            candidates.add(urljoin(page_url, first))
            for tag in soup.find_all(["embed", "object"]):
                src = tag.get("src") or tag.get("data")
                if src:
                    candidates.add(urljoin(page_url, src))

            for cand in candidates:
                if not self._is_content_url(cand):
                    continue
                ext = os.path.splitext(urlparse(cand.lower()).path)[1]
                if target_format == "image" and ext == ".pdf":
                    continue
                if target_format == "pdf" and ext != ".pdf":
                    continue
                discovered.add(cand)
        except Exception as e:
            logger.debug("spider error on %s: %s", page_url, e)
        return discovered

    # ------------------------------------------------------------------ #
    # Quality-gated download (the BaseCollector.download_media impl)
    # ------------------------------------------------------------------ #

    async def download_media(self, item: dict):
        """Wrapper that delegates to _download_asset.

        Required by the BaseCollector ABC. Direct callers should prefer
        ``_download_asset`` which understands query/source_url context.
        """
        await self._download_asset(
            query=item.get("entity_name", ""),
            hit={
                "url": item["url"],
                "rank": item.get("rank"),
                "title": item.get("entity_name"),
            },
            source_url=item.get("source_url"),
        )

    async def _download_asset(self, query: str, hit: dict, source_url: Optional[str] = None) -> bool:
        """Quality-gated download with dedup + atomic write + DB row."""
        url = hit.get("url")
        if not url:
            return False

        # URL-based content_id for disk-first dedup
        cid = hashlib.sha256(url.encode()).hexdigest()[:16]
        if self.is_known(cid):
            return False

        async with self._sem:
            try:
                async with self._make_client(timeout=30.0) as client:
                    resp = await client.get(url, headers=self._headers(urlparse(url).netloc))
                    if resp.status_code != 200:
                        return False
                    data = resp.content
                    ctype = resp.headers.get("content-type", "").lower()
            except Exception as e:
                logger.debug("download fetch failed for %s: %s", url, e)
                return False

        # ---- Size gate ----
        if len(data) < self._min_file_size:
            return False

        # ---- sha256 dedup (in-memory) ----
        sha = hashlib.sha256(data).hexdigest()
        if sha in self._content_hashes:
            self._duplicates_skipped += 1
            return False
        self._content_hashes.add(sha)

        ext = os.path.splitext(urlparse(url.lower()).path)[1]
        is_pdf = "pdf" in ctype or ext == ".pdf"
        is_image = (
            "image" in ctype
            or ext in IMAGE_EXTENSIONS
        )

        q_slug = hashlib.sha256(query.encode()).hexdigest()[:12] if query else "spider"
        q_name = (query or "spider")[:50]

        if is_pdf:
            return await self._save_pdf(q_slug, q_name, cid, data, source_url or url)
        if is_image:
            return await self._save_image(q_slug, q_name, cid, data, source_url or url)
        # Unknown content-type: try as image (PIL will reject if it's not).
        return await self._save_image(q_slug, q_name, cid, data, source_url or url)

    async def _save_image(
        self,
        entity_id: str,
        entity_name: str,
        content_id: str,
        data: bytes,
        source_url: str,
    ) -> bool:
        if self._PIL is None:
            return False
        try:
            img = self._PIL.open(io.BytesIO(data))
            w, h = img.size
            if w < self._min_dim or h < self._min_dim:
                return False
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
        except Exception:
            return False

        filename = self.build_filename(
            entity_id, entity_name, "image", content_id, extension="jpg",
        )
        dest_dir = self.account_media_dir / "image"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        loop = asyncio.get_event_loop()

        def _encode_and_write() -> int:
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    img.save(f, format="JPEG", quality=95)
                    f.flush()
                    os.fsync(f.fileno())
                size = os.path.getsize(tmp_path)
                os.replace(tmp_path, dest)
                return size
            except BaseException:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

        try:
            file_size = await loop.run_in_executor(None, _encode_and_write)
        except Exception:
            logger.exception("save image failed cid=%s url=%s", content_id, source_url)
            return False

        sha = self.sha256_bytes(data)
        meta = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "content_type": "image",
            "content_id": content_id,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
            "width": w,
            "height": h,
        }
        try:
            self.save_json(meta, dest_dir / f"{Path(filename).stem}_metadata.json")
        except Exception:
            pass

        try:
            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=entity_name,
                content_type="image",
                content_id=content_id,
                filename=filename,
                file_path=str(dest),
                file_size=file_size,
                width=w,
                height=h,
                sha256=sha,
                source_url=source_url,
                metadata=meta,
            )
        except Exception as e:
            logger.debug("insert_media_item failed: %s", e)

        self._known_ids.add(content_id)
        return True

    async def _save_pdf(
        self,
        entity_id: str,
        entity_name: str,
        content_id: str,
        data: bytes,
        source_url: str,
    ) -> bool:
        # Persist the PDF blob first (dedup'd by content_id)
        filename = self.build_filename(
            entity_id, entity_name, "pdf", content_id, extension="pdf",
        )
        dest_dir = self.account_media_dir / "pdf"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        loop = asyncio.get_event_loop()

        def _atomic_write() -> int:
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                size = os.path.getsize(tmp_path)
                os.replace(tmp_path, dest)
                return size
            except BaseException:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise

        try:
            file_size = await loop.run_in_executor(None, _atomic_write)
        except Exception:
            logger.exception("save pdf failed cid=%s url=%s", content_id, source_url)
            return False

        sha = self.sha256_bytes(data)
        meta = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "content_type": "pdf",
            "content_id": content_id,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
        }
        try:
            self.save_json(meta, dest_dir / f"{Path(filename).stem}_metadata.json")
        except Exception:
            pass
        try:
            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=entity_name,
                content_type="pdf",
                content_id=content_id,
                filename=filename,
                file_path=str(dest),
                file_size=file_size,
                sha256=sha,
                source_url=source_url,
                metadata=meta,
            )
        except Exception as e:
            logger.debug("insert_media_item failed: %s", e)

        # Optionally rasterise pages → JPGs
        if self._fitz is not None:
            await loop.run_in_executor(
                None, self._extract_pdf_pages, data, dest_dir, Path(filename).stem,
            )

        self._known_ids.add(content_id)
        return True

    def _extract_pdf_pages(self, data: bytes, out_dir: Path, base_filename: str) -> int:
        """Rasterise PDF pages to JPGs (mirror of toolkit's extract_pdf_pages_as_jpg)."""
        if self._fitz is None or self._PIL is None:
            return 0
        try:
            doc = self._fitz.open(stream=data, filetype="pdf")
            page_count = len(doc)
            if page_count == 0:
                return 0
            zoom = 4 if page_count <= 10 else (2 if page_count <= 30 else 1)
            mat = self._fitz.Matrix(zoom, zoom)
            pages_dir = out_dir / f"{base_filename}_pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            saved = 0
            for page_num in range(min(page_count, self._max_pdf_pages)):
                page = doc.load_page(page_num)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = self._PIL.frombytes("RGB", [pix.width, pix.height], pix.samples)
                out_path = pages_dir / f"{base_filename}_page_{page_num + 1}.jpg"
                img.save(out_path, format="JPEG", quality=95)
                saved += 1
            doc.close()
            return saved
        except Exception as e:
            logger.warning("PDF page extract failed for %s: %s", base_filename, e)
            return 0

    async def cleanup(self):
        """Hook for scheduler — nothing persistent to clean."""
        return None
