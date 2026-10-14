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
SEARCH_MAX_ACTIVE_DOMAINS      Max concurrently active spider/download domains (default 4).
SEARCH_MAX_REQUESTS_PER_DOMAIN Per-domain in-flight request cap (default 2).
SEARCH_DOMAIN_DELAY_SECONDS    Base delay before each domain-paced request.
SEARCH_DOMAIN_JITTER_SECONDS   Additional random request delay.
SEARCH_DDG_RETRIES             Retry attempts per DDG text/image call on
                               transient failures ("malformed headers",
                               dropped connections, timeouts, 5xx; default 3).
                               Genuine 429s are never retried here — the
                               politeness gate and rate limiter own those.
SEARCH_DDG_TIMEOUT             Per-attempt timeout (seconds) for the DDGS
                               client (default 30).
SEARCH_DDG_RETRY_BASE_SECONDS  Base backoff between DDG retries; waits grow
                               as base * 2**attempt plus jitter (default 2.0).
SEARCH_BING_PAGES              Pages of Bing results to scrape (default 3).
SEARCH_SERPER_THRESHOLD        Use Serper only when DDG+Bing < this many results
                               (default 5; conserves API credits).
TOR_PROXY_ENABLED              '1' to route HTTP through the Tor sidecar.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import logging
import os
import random
import re
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from src.core.base_collector import BaseCollector
from src.core.domain_pacing import DomainPacer, record_domain_pacing_event
from src.core.rate_limit_events import record_rate_limit_event
from src.core.request_persona import build_persona_headers
from src.core.scrape_pacing import sleep_before_pre_cooldown_retry
from src.collectors.search.parse import (
    is_content_url as _parse_is_content_url,
    CONTENT_EXTENSIONS as _parse_CONTENT_EXTENSIONS,
    ICON_KEYWORDS as _parse_ICON_KEYWORDS,
)
from src.core.search_cache import SearchCache
from src.core.vault import (
    VAULT_ROOT,
    assert_media_write_allowed,
    write_atomic_artifact,
    write_atomic_artifact_from_path,
)
from .bucket_crawler import expand_dorks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants pulled from searchtoolkit/src/app.py
# ---------------------------------------------------------------------------

CONTENT_EXTENSIONS = _parse_CONTENT_EXTENSIONS
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".jfif"}

# Office/text documents and videos the search collector also captures (parity
# with the website spider — COLLECTION_SPEC "media, documents and videos").
DOC_EXTENSIONS = {
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".rtf", ".csv", ".odt", ".ods", ".odp",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v",
    ".mpeg", ".mpg", ".wmv", ".flv", ".ogv", ".3gp",
}
# Explicitly excluded — audio + code/executables are never downloaded.
AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac", ".aac",
    ".opus", ".wma", ".aiff", ".mid", ".midi",
}
CODE_EXTENSIONS = {
    ".js", ".mjs", ".ts", ".jsx", ".tsx", ".py", ".rb", ".php", ".java",
    ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".sh", ".bat", ".ps1", ".pl",
    ".lua", ".sql", ".exe", ".dll", ".so", ".dylib", ".bin", ".msi",
    ".apk", ".jar", ".war",
}
_DOC_CONTENT_TYPES = (
    "application/msword",
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument",
    "application/rtf", "text/rtf", "text/csv",
)
_DOC_CONTENT_TYPE_EXTENSIONS = {
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/csv": ".csv",
}


def _document_extension_from_content_type(content_type: str) -> str | None:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return _DOC_CONTENT_TYPE_EXTENSIONS.get(media_type)

_DYNAMIC_TERM_STOPWORDS = frozenset({
    "about", "after", "again", "also", "and", "are", "because", "been", "before",
    "being", "but", "can", "class", "com", "could", "day", "did", "does", "for",
    "from", "get", "had", "has", "have", "her", "here", "him", "his", "how",
    "http", "https", "into", "its", "just", "like", "more", "most", "new", "not",
    "now", "off", "one", "only", "our", "out", "over", "she", "school", "see",
    "some", "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "too", "under", "use", "very", "was", "well", "were",
    "what", "when", "where", "which", "who", "will", "with", "www", "year", "your",
})

# Exposure terms are rotated through these approved person/document searches in
# addition to the existing cloud-bucket and PDF templates. A full-name term is
# emitted from profile display-name fields so the resume query keeps its meaning.
_DYNAMIC_PERSON_DOCUMENT_DORKS = (
    '"{q}" filetype:pdf',
    '"{q}" (site:github.com OR site:linkedin.com OR site:twitter.com)',
    'filetype:pdf "{q}"',
    '"{q}" filetype:(pdf OR docx OR xlsx OR pptx)',
    '"{q}" filetype:pdf resume',
    'site:target.com filetype:pdf "{q}"',
)


def _expand_dynamic_dorks(term: str) -> list[str]:
    """Return the rotating cloud and person/document searches for one term."""
    return [
        *expand_dorks([term]),
        *(template.format(q=term) for template in _DYNAMIC_PERSON_DOCUMENT_DORKS),
    ]

_DYNAMIC_TERM_SOURCE_QUERIES = (
    ("instagram_profiles", """
        SELECT 'instagram' AS platform, id::text AS source_reference,
               username, full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM instagram_profiles
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR full_name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("tiktok_profiles", """
        SELECT 'tiktok' AS platform, id::text AS source_reference,
               username, nickname AS full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM tiktok_profiles
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR nickname IS NOT NULL OR bio IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("lemon8_profiles", """
        SELECT 'lemon8' AS platform, id::text AS source_reference,
               username, nickname AS full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM lemon8_profiles
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR nickname IS NOT NULL OR bio IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("pinterest_profiles", """
        SELECT 'pinterest' AS platform, username AS source_reference,
               username, display_name AS full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM pinterest_profiles
         WHERE probed_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR display_name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY probed_at DESC LIMIT $1
    """),
    ("snapchat_profiles", """
        SELECT 'snapchat' AS platform, username AS source_reference,
               username, display_name AS full_name, display_name AS text_blob, ARRAY[]::text[] AS hashtags
          FROM snapchat_profiles
         WHERE probed_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR display_name IS NOT NULL)
         ORDER BY probed_at DESC LIMIT $1
    """),
    ("facebook_profiles", """
        SELECT 'facebook' AS platform, id::text AS source_reference,
               username, display_name AS full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM facebook_profiles
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR display_name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("x_profiles", """
        SELECT 'x' AS platform, id::text AS source_reference,
               username, display_name AS full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM x_profiles
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR display_name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("bluesky_profiles", """
        SELECT 'bluesky' AS platform, did AS source_reference,
               username, display_name AS full_name, bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM bluesky_profiles
         WHERE probed_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR display_name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY probed_at DESC LIMIT $1
    """),
    ("youtube_channels", """
        SELECT 'youtube' AS platform, id::text AS source_reference,
               custom_url AS username, title AS full_name, description AS text_blob, keywords AS hashtags
          FROM youtube_channels
         WHERE updated_at >= now() - interval '90 days'
           AND (custom_url IS NOT NULL OR title IS NOT NULL OR description IS NOT NULL OR keywords IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("instagram_posts", """
        SELECT 'instagram' AS platform, p.id::text AS source_reference,
               u.username, p.caption AS text_blob, p.hashtags
          FROM instagram_posts p
          LEFT JOIN instagram_profiles u ON u.id = p.profile_id
         WHERE p.collected_at >= now() - interval '90 days'
           AND (p.caption IS NOT NULL OR p.hashtags IS NOT NULL)
         ORDER BY p.collected_at DESC LIMIT $1
    """),
    ("tiktok_posts", """
        SELECT 'tiktok' AS platform, p.id::text AS source_reference,
               u.username, concat_ws(' ', p.title, p.description) AS text_blob, p.hashtags
          FROM tiktok_posts p
          LEFT JOIN tiktok_profiles u ON u.id = p.profile_id
         WHERE p.collected_at >= now() - interval '90 days'
           AND (p.title IS NOT NULL OR p.description IS NOT NULL OR p.hashtags IS NOT NULL)
         ORDER BY p.collected_at DESC LIMIT $1
    """),
    ("lemon8_posts", """
        SELECT 'lemon8' AS platform, id::text AS source_reference,
               username, concat_ws(' ', title, description) AS text_blob, hashtags
          FROM lemon8_posts
         WHERE collected_at >= now() - interval '90 days'
           AND (title IS NOT NULL OR description IS NOT NULL OR hashtags IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("threads_posts", """
        SELECT 'threads' AS platform, id::text AS source_reference,
               author_username AS username, caption AS text_blob, hashtags
          FROM threads_posts
         WHERE collected_at >= now() - interval '90 days'
           AND (caption IS NOT NULL OR hashtags IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("facebook_posts", """
        SELECT 'facebook' AS platform, id::text AS source_reference,
               author_username AS username, caption AS text_blob, hashtags
          FROM facebook_posts
         WHERE collected_at >= now() - interval '90 days'
           AND (caption IS NOT NULL OR hashtags IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("x_posts", """
        SELECT 'x' AS platform, id::text AS source_reference,
               author_username AS username, caption AS text_blob, hashtags
          FROM x_posts
         WHERE collected_at >= now() - interval '90 days'
           AND (caption IS NOT NULL OR hashtags IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("pinterest_pins", """
        SELECT 'pinterest' AS platform, pin_id AS source_reference,
               username, concat_ws(' ', title, description) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM pinterest_pins
         WHERE collected_at >= now() - interval '90 days'
           AND (title IS NOT NULL OR description IS NOT NULL OR username IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("pinterest_boards", """
        SELECT 'pinterest' AS platform, board_id AS source_reference,
               username, NULL::text AS full_name,
               concat_ws(' ', name, description) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM pinterest_boards
         WHERE collected_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR name IS NOT NULL OR description IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("youtube_videos", """
        SELECT 'youtube' AS platform, v.id::text AS source_reference,
               c.custom_url AS username, concat_ws(' ', v.title, v.description) AS text_blob,
               v.tags AS hashtags
          FROM youtube_videos v
          LEFT JOIN youtube_channels c ON c.id = v.channel_id
         WHERE v.collected_at >= now() - interval '90 days'
           AND (v.title IS NOT NULL OR v.description IS NOT NULL OR v.tags IS NOT NULL)
         ORDER BY v.collected_at DESC LIMIT $1
    """),
    ("youtube_community_posts", """
        SELECT 'youtube' AS platform, p.id::text AS source_reference,
               c.custom_url AS username, p.text AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM youtube_community_posts p
          LEFT JOIN youtube_channels c ON c.platform_channel_id = p.channel_id
         WHERE p.collected_at >= now() - interval '90 days'
           AND (p.text IS NOT NULL OR c.custom_url IS NOT NULL)
         ORDER BY p.collected_at DESC LIMIT $1
    """),
    ("github_users", """
        SELECT 'github' AS platform, id::text AS source_reference,
               login AS username, name AS full_name,
               concat_ws(' ', bio, company, blog, location) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM github_users
         WHERE collected_at >= now() - interval '90 days'
           AND (login IS NOT NULL OR name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("github_repos", """
        SELECT 'github' AS platform, platform_repo_id::text AS source_reference,
               split_part(full_name, '/', 1) AS username, NULL::text AS full_name,
               concat_ws(' ', name, description, language) AS text_blob, topics AS hashtags
          FROM github_repos
         WHERE collected_at >= now() - interval '90 days'
           AND (name IS NOT NULL OR description IS NOT NULL OR topics IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("strava_athletes", """
        SELECT 'strava' AS platform, id::text AS source_reference,
               username, concat_ws(' ', firstname, lastname) AS full_name,
               concat_ws(' ', profile, city, state, country) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM strava_athletes
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR firstname IS NOT NULL OR lastname IS NOT NULL OR profile IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("strava_activities", """
        SELECT 'strava' AS platform, platform_activity_id::text AS source_reference,
               NULL::text AS username, NULL::text AS full_name,
               concat_ws(' ', name, type, sport_type, description) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM strava_activities
         WHERE collected_at >= now() - interval '90 days'
           AND (name IS NOT NULL OR description IS NOT NULL OR sport_type IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("airbnb_hosts", """
        SELECT 'airbnb' AS platform, host_id AS source_reference,
               NULL::text AS username, display_name AS full_name,
               bio AS text_blob, ARRAY[]::text[] AS hashtags
          FROM airbnb_hosts
         WHERE collected_at >= now() - interval '90 days'
           AND (display_name IS NOT NULL OR bio IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("airbnb_listings", """
        SELECT 'airbnb' AS platform, listing_id AS source_reference,
               NULL::text AS username, NULL::text AS full_name,
               concat_ws(' ', title, description, location, property_type) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM airbnb_listings
         WHERE collected_at >= now() - interval '90 days'
           AND (title IS NOT NULL OR description IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("airbnb_reviews", """
        SELECT 'airbnb' AS platform, review_id AS source_reference,
               NULL::text AS username, NULL::text AS full_name,
               comments AS text_blob, ARRAY[]::text[] AS hashtags
          FROM airbnb_reviews
         WHERE collected_at >= now() - interval '90 days' AND comments IS NOT NULL
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("snapchat_stories", """
        SELECT 'snapchat' AS platform, story_id AS source_reference,
               username, NULL::text AS full_name, title AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM snapchat_stories
         WHERE collected_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR title IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("snapchat_spotlights", """
        SELECT 'snapchat' AS platform, spotlight_id AS source_reference,
               username, NULL::text AS full_name, title AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM snapchat_spotlights
         WHERE collected_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR title IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("snapchat_highlights", """
        SELECT 'snapchat' AS platform, highlight_id AS source_reference,
               username, NULL::text AS full_name, title AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM snapchat_highlights
         WHERE collected_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR title IS NOT NULL)
         ORDER BY collected_at DESC LIMIT $1
    """),
    ("telegram_chats", """
        SELECT 'telegram' AS platform, id::text AS source_reference,
               username, NULL::text AS full_name,
               concat_ws(' ', title, description) AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM telegram_chats
         WHERE updated_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR title IS NOT NULL OR description IS NOT NULL)
         ORDER BY updated_at DESC LIMIT $1
    """),
    ("paypal_profiles", """
        SELECT 'paypal' AS platform, username AS source_reference,
               username, display_name AS full_name, NULL::text AS text_blob,
               ARRAY[]::text[] AS hashtags
          FROM paypal_profiles
         WHERE probed_at >= now() - interval '90 days'
           AND (username IS NOT NULL OR display_name IS NOT NULL)
         ORDER BY probed_at DESC LIMIT $1
    """),
)

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
        # Malformed PDFs (yearbooks/dean's-list scans) make MuPDF's C layer spew
        # "format error: No common ancestor in structure tree" etc. straight to
        # stderr at high volume — flooding logs and tripping the error-flood
        # self-heal into needless restarts. Silence the C-level chatter (parsing
        # still works / degrades gracefully); this toggle is process-global.
        try:
            fitz.TOOLS.mupdf_display_errors(False)
        except Exception:
            pass
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
    # Search and exposure share public providers, so lock each operation rather
    # than holding the provider mutex for an entire large target batch.
    COLLECT_MUTEX_SCOPE = "operation"

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
        # Documents (word/ppt/excel/text) and videos — parity with website spider.
        self._download_docs = os.getenv("SEARCH_DOWNLOAD_DOCS", "1") == "1"
        self._download_videos = os.getenv("SEARCH_DOWNLOAD_VIDEOS", "1") == "1"
        self._max_doc_bytes = int(os.getenv("SEARCH_MAX_DOC_BYTES", str(50 * 1024 * 1024)))
        self._document_index_enabled = os.getenv("SEARCH_DOCUMENT_INDEX_ENABLED", "1") == "1"
        self._max_document_text_chars = min(
            200_000,
            max(1_000, int(os.getenv("SEARCH_MAX_DOCUMENT_TEXT_CHARS", "100000"))),
        )
        self._max_document_archive_bytes = min(
            200 * 1024 * 1024,
            max(1_000_000, int(os.getenv("SEARCH_MAX_DOCUMENT_ARCHIVE_BYTES", str(100 * 1024 * 1024)))),
        )
        self._dynamic_terms_enabled = os.getenv("SEARCH_DYNAMIC_TERMS_ENABLED", "1") == "1"
        self._dynamic_term_cap = min(10, max(0, int(os.getenv("SEARCH_DYNAMIC_TERM_CAP", "10"))))
        self._dynamic_source_limit = min(500, max(1, int(os.getenv("SEARCH_DYNAMIC_SOURCE_ROWS", "100"))))
        self._dynamic_term_ttl_days = min(90, max(1, int(os.getenv("SEARCH_DYNAMIC_TERM_TTL_DAYS", "7"))))
        self._dynamic_refresh_hours = min(168, max(0, int(os.getenv("SEARCH_DYNAMIC_REFRESH_HOURS", "0"))))
        self._dynamic_query_interval_days = min(
            30, max(0, int(os.getenv("SEARCH_DYNAMIC_QUERY_INTERVAL_DAYS", "0")))
        )
        # 0 = no video size cap (user decision); videos are streamed to disk.
        self._max_video_bytes = int(os.getenv("SEARCH_MAX_VIDEO_BYTES", "0"))
        self._video_chunk_bytes = int(os.getenv("SEARCH_VIDEO_CHUNK_BYTES", str(1024 * 1024)))
        self._spider_pages = os.getenv("SEARCH_SPIDER_PAGES", "1") == "1"
        self._bing_pages = int(os.getenv("SEARCH_BING_PAGES", "3"))
        self._serper_threshold = int(os.getenv("SEARCH_SERPER_THRESHOLD", "5"))
        self._concurrent_downloads = int(os.getenv("SEARCH_CONCURRENT_DOWNLOADS", "5"))
        self._serper_api_key = os.getenv("SERPER_API_KEY", "").strip()
        self._serper_quota = int(os.getenv("SERPER_DAILY_QUOTA", "2500"))
        self._domain_pacing = DomainPacer(
            self.SOURCE_NAME,
            env_prefix="SEARCH",
            max_active_domains=4,
            max_per_domain=2,
            delay_seconds=1.0,
            jitter_seconds=2.0,
        )

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
        self._search_scope_cooldowns: dict[str, float] = {}
        self._search_rate_limit_streaks: dict[str, int] = {}
        self._last_search_query_rate_limited = False
        self._last_search_query_lock_skipped = False

        # Register Serper quota with the unified tracker if available.
        self._register_serper_quota()

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

    async def _record_domain_http_status(
        self,
        url: str,
        status_code: int,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if status_code not in (403, 429):
            return
        event = f"http_{status_code}"
        self._domain_pacing.count(event)
        await self._record_domain_pacing_event(
            event,
            url,
            status_code=status_code,
            metadata=metadata,
        )

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
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }
        url = f"https://{domain}/" if domain else DEFAULT_DDG_DOMAIN
        headers, _persona_metadata = build_persona_headers(headers, url, source=self.SOURCE_NAME)
        return headers

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> int | None:
        raw = resp.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0, int(float(raw)))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
        except Exception:
            return None

    @staticmethod
    def _looks_like_rate_limit_error(error: Exception) -> bool:
        msg = str(error).lower()
        return any(
            marker in msg
            for marker in (
                "429",
                "too many requests",
                "rate limit",
                "rate-limit",
                "ratelimit",
                "please wait",
                "please-wait",
                "quota exhausted",
            )
        )

    #: Markers for transient DuckDuckGo failures worth retrying. DDG's edge
    #: intermittently returns responses whose headers reqwest's strict parser
    #: rejects ("malformed headers"); the Rust ddgs 9.x client exposes no
    #: header/HTTP-version knobs, so retry-with-backoff is the mitigation.
    _DDG_TRANSIENT_MARKERS = (
        "malformed headers",
        "error sending request",
        "connection",
        "reset",
        "timeout",
        "timed out",
        "temporarily",
        "try again",
        "502",
        "503",
        "504",
    )

    @classmethod
    def _is_ddg_transient_error(cls, error: Exception) -> bool:
        msg = str(error).lower()
        return any(marker in msg for marker in cls._DDG_TRANSIENT_MARKERS)

    @staticmethod
    def _default_429_cooldown_seconds() -> int | None:
        raw = os.getenv("SEARCH_RATE_LIMIT_COOLDOWN_SECONDS", "600")
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = 600
        return value if value > 0 else None

    @staticmethod
    def _search_cooldown_key(engine: str, scope: str, account: str | None = None) -> str:
        return f"{engine}:{account or '-'}:{scope}"

    def _search_scope_cooldown_remaining(
        self,
        engine: str,
        scope: str,
        account: str | None = None,
    ) -> float:
        key = self._search_cooldown_key(engine, scope, account)
        remaining = self._search_scope_cooldowns.get(key, 0.0) - asyncio.get_running_loop().time()
        if remaining <= 0:
            self._search_scope_cooldowns.pop(key, None)
            return 0.0
        return remaining

    def _search_scope_cooling_down(
        self,
        engine: str,
        scope: str,
        account: str | None = None,
    ) -> bool:
        remaining = self._search_scope_cooldown_remaining(engine, scope, account)
        if remaining > 0:
            logger.info("search/%s scope %s cooling down for %.0fs", engine, scope, remaining)
            return True
        return False

    def _set_search_scope_cooldown(
        self,
        engine: str,
        scope: str,
        seconds: int | None,
        account: str | None = None,
    ) -> None:
        if not seconds or seconds <= 0:
            return
        key = self._search_cooldown_key(engine, scope, account)
        deadline = asyncio.get_running_loop().time() + float(seconds)
        self._search_scope_cooldowns[key] = max(self._search_scope_cooldowns.get(key, 0.0), deadline)

    async def _record_search_rate_limit(
        self,
        *,
        engine: str,
        scope: str,
        account: str | None = None,
        status_code: int | None = 429,
        cooldown_seconds: int | None = None,
        cooldown_active: bool = True,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        if cooldown_active and response is not None and cooldown_seconds is None:
            cooldown_seconds = self._retry_after_seconds(response)
        event_metadata = {"engine": engine}
        if metadata:
            event_metadata.update(metadata)
        if not cooldown_active:
            event_metadata["cooldown_active"] = False
            cooldown_seconds = None
        elif status_code == 429:
            key = self._search_cooldown_key(engine, scope, account)
            streak = self._search_rate_limit_streaks.get(key, 0) + 1
            self._search_rate_limit_streaks[key] = streak
            base = self._default_429_cooldown_seconds() or 0
            try:
                max_cooldown = max(
                    base,
                    int(float(os.getenv("SEARCH_RATE_LIMIT_MAX_COOLDOWN_SECONDS", "21600"))),
                )
            except (TypeError, ValueError):
                max_cooldown = max(base, 21600)
            exponential = min(max_cooldown, base * (2 ** min(streak - 1, 16)))
            retry_after = self._retry_after_seconds(response) if response is not None else None
            cooldown_seconds = max(
                cooldown_seconds or 0,
                retry_after or 0,
                exponential,
            ) or None
        if cooldown_active and status_code == 429:
            self._set_search_scope_cooldown(engine, scope, cooldown_seconds, account)
        await record_rate_limit_event(
            self.pool,
            # Search and exposure share one provider cooldown namespace.
            source="search",
            account=account,
            scope=scope,
            status_code=status_code,
            cooldown_seconds=cooldown_seconds,
            reason=reason,
            metadata=event_metadata,
        )

    async def _retry_search_after_429(
        self,
        fetch,
        *,
        engine: str,
        scope: str,
        account: str | None = None,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ):
        # A 429 is already a clear stop signal. Retrying the same endpoint adds
        # load while the provider is throttling us. The caller records the
        # response, honors Retry-After, and applies an exponential cooldown.
        logger.info("search/%s received 429 for %s; skipping retry", engine, scope)
        return None

    async def _search_scope_cooldown_active(self, engine: str, scope: str) -> bool:
        """Check both the local timer and durable provider cooldown before a call."""
        if self._search_scope_cooling_down(engine, scope):
            return True
        if self.pool is None:
            return False
        try:
            async with self.pool.acquire() as conn:
                remaining = await conn.fetchval(
                    """
                    SELECT GREATEST(
                        0,
                        EXTRACT(EPOCH FROM (
                            created_at + cooldown_seconds * interval '1 second' - NOW()
                        ))
                    )::int
                    FROM rate_limit_events
                    WHERE source = 'search' AND scope = $1 AND status_code = 429
                      AND cooldown_seconds > 0
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    scope,
                    timeout=5,
                )
        except Exception:
            return False
        if remaining and int(remaining) > 0:
            self._set_search_scope_cooldown(engine, scope, int(remaining))
            return True
        return False

    def _clear_search_scope_cooldown(self, engine: str, scope: str) -> None:
        key = self._search_cooldown_key(engine, scope)
        self._search_scope_cooldowns.pop(key, None)
        self._search_rate_limit_streaks.pop(key, None)

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

    @staticmethod
    def _extract_dynamic_term_candidates(rows: Iterable[dict], limit: int = 10) -> list[dict[str, str | None]]:
        """Extract balanced handles, names, hashtags, and caption/bio keywords."""
        counts: dict[tuple[str, str], int] = {}
        sources: dict[tuple[str, str], tuple[str, str | None]] = {}
        word_pattern = re.compile(r"[^\W_][\w.-]{2,31}", re.UNICODE)

        def add(term: str, kind: str, row: dict) -> None:
            normalized = re.sub(r"\s+", " ", term.strip().lstrip("@#")).casefold()
            if len(normalized) < 2 or len(normalized) > 80:
                return
            if kind == "keyword" and (len(normalized) < 3 or normalized in _DYNAMIC_TERM_STOPWORDS):
                return
            if kind == "username" and not re.fullmatch(r"[\w.-]{2,80}", normalized, re.UNICODE):
                return
            if kind == "hashtag" and not re.fullmatch(r"[\w-]{2,80}", normalized, re.UNICODE):
                return
            if kind == "full_name" and (
                len(normalized.split()) < 2
                or not re.fullmatch(r"[\w][\w .’'-]{0,78}[\w]", normalized, re.UNICODE)
            ):
                return
            key = (kind, normalized)
            counts[key] = counts.get(key, 0) + 1
            sources.setdefault(
                key,
                (str(row.get("platform") or "unknown"),
                 str(row.get("source_reference")) if row.get("source_reference") is not None else None),
            )

        for row in rows:
            username = str(row.get("username") or "").strip()
            if username:
                if "://" in username:
                    username = urlparse(username).path.rstrip("/").rsplit("/", 1)[-1]
                elif "/" in username:
                    username = username.rstrip("/").rsplit("/", 1)[-1]
                add(username, "username", row)

            full_name = str(row.get("full_name") or "").strip()
            if full_name:
                add(full_name, "full_name", row)

            hashtags = row.get("hashtags") or []
            if isinstance(hashtags, str):
                hashtags = [hashtags]
            for hashtag in hashtags:
                for value in word_pattern.findall(str(hashtag).replace("#", " ")):
                    add(value, "hashtag", row)

            text_blob = str(row.get("text_blob") or "")[:4_000]
            text_blob = re.sub(r"https?://\S+|@[\w.]+|#[\w-]+", " ", text_blob)
            for keyword in set(word_pattern.findall(text_blob)):
                add(keyword, "keyword", row)

        priorities = {"username": 0, "full_name": 1, "hashtag": 2, "keyword": 3}
        selected: list[tuple[str, str]] = []
        for kind, quota in (("username", 3), ("full_name", 2), ("hashtag", 2), ("keyword", 3)):
            ranked = sorted(
                ((key, count) for key, count in counts.items() if key[0] == kind),
                key=lambda item: (-item[1], item[0][1]),
            )
            selected.extend(key for key, _ in ranked[:quota])
        unique_selected: list[tuple[str, str]] = []
        selected_terms: set[str] = set()
        for key in selected:
            if key[1] not in selected_terms:
                unique_selected.append(key)
                selected_terms.add(key[1])
        if len(unique_selected) < max(0, limit):
            remainder = sorted(
                (key for key in counts if key not in selected and key[1] not in selected_terms),
                key=lambda key: (-counts[key], priorities[key[0]], key[1]),
            )
            unique_selected.extend(remainder[: max(0, limit) - len(unique_selected)])

        return [
            {
                "term": term,
                "term_kind": kind,
                "source_platform": sources[(kind, term)][0],
                "source_reference": sources[(kind, term)][1],
            }
            for kind, term in unique_selected[: max(0, limit)]
        ]

    async def _refresh_dynamic_search_terms(self, conn) -> None:
        if self._dynamic_refresh_hours > 0:
            refresh_due = await conn.fetchval(
                """
                SELECT NOT EXISTS (
                    SELECT 1 FROM service_cursors
                     WHERE service = 'search_dynamic_terms_v1'
                       AND last_processed_at > now() - ($1 * interval '1 hour')
                )
                """,
                self._dynamic_refresh_hours,
            )
            if not refresh_due:
                return

        source_rows = []
        for source_name, query in _DYNAMIC_TERM_SOURCE_QUERIES:
            try:
                source_rows.extend(
                    await conn.fetch(query, self._dynamic_source_limit, timeout=10)
                )
            except Exception as exc:
                logger.debug("dynamic search source skipped table=%s error=%s", source_name, type(exc).__name__)

        candidates = self._extract_dynamic_term_candidates(source_rows, self._dynamic_term_cap)
        if candidates:
            await conn.executemany(
                """
                INSERT INTO search_dynamic_terms (
                    term, term_kind, source_platform, source_reference, expires_at
                ) VALUES ($1, $2, $3, $4, now() + ($5 * interval '1 day'))
                ON CONFLICT (term) DO UPDATE SET
                    term_kind = EXCLUDED.term_kind,
                    source_platform = EXCLUDED.source_platform,
                    source_reference = EXCLUDED.source_reference,
                    last_seen_at = now(),
                    expires_at = CASE
                        WHEN search_dynamic_terms.zero_result_since IS NULL
                        THEN EXCLUDED.expires_at
                        ELSE search_dynamic_terms.expires_at
                    END,
                    updated_at = now()
                """,
                [
                    (item["term"], item["term_kind"], item["source_platform"],
                     item["source_reference"], self._dynamic_term_ttl_days)
                    for item in candidates
                ],
            )
        await conn.execute(
            """
            DELETE FROM search_dynamic_terms
             WHERE expires_at <= now()
                OR zero_result_since <= now() - ($1 * interval '1 day')
            """,
            self._dynamic_term_ttl_days,
        )
        await conn.execute(
            """
            DELETE FROM search_dynamic_terms
             WHERE term NOT IN (
                SELECT term FROM search_dynamic_terms
                 WHERE expires_at > now()
                 ORDER BY total_result_count DESC, last_seen_at DESC, search_count DESC, term
                 LIMIT $1
             )
            """,
            self._dynamic_term_cap,
        )
        await conn.execute(
            """
            INSERT INTO service_cursors (service, last_processed_at, status)
            VALUES ('search_dynamic_terms_v1', now(), 'idle')
            ON CONFLICT (service) DO UPDATE SET
                last_processed_at = now(), status = 'idle'
            """
        )
        logger.info("Refreshed %d dynamic search terms from %d recent source rows", len(candidates), len(source_rows))

    async def _prepare_dynamic_search_queries(self) -> list[dict[str, Any]]:
        if not self._dynamic_terms_enabled or self._dynamic_term_cap <= 0 or self.pool is None:
            return []
        try:
            async with self.pool.acquire() as conn:
                await self._refresh_dynamic_search_terms(conn)
                rows = await conn.fetch(
                    """
                    SELECT term, term_kind, source_platform, source_reference, template_cursor
                      FROM search_dynamic_terms
                     WHERE expires_at > now() AND next_search_at <= now()
                     ORDER BY next_search_at, last_searched_at NULLS FIRST,
                              last_seen_at DESC, term
                     LIMIT $1
                    """,
                    self._dynamic_term_cap,
                )
            items = []
            for row in rows:
                term = str(row["term"])
                templates = _expand_dynamic_dorks(term)
                if not templates:
                    continue
                cursor = int(row["template_cursor"] or 0) % len(templates)
                items.append({
                    **dict(row),
                    "query": templates[cursor],
                    "next_template_cursor": (cursor + 1) % len(templates),
                })
            return items
        except Exception as exc:
            logger.debug("dynamic search query preparation skipped: %s", type(exc).__name__)
            return []

    async def _mark_dynamic_search_query(
        self,
        item: dict[str, Any],
        *,
        result_count: int = 0,
        completed: bool = True,
    ) -> None:
        if self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE search_dynamic_terms
                       SET last_searched_at = CASE WHEN $2 THEN now() ELSE last_searched_at END,
                           next_search_at = now() + CASE
                               WHEN $2 THEN ($3 * interval '1 day')
                               ELSE interval '2 hours'
                           END,
                           search_count = search_count + CASE WHEN $2 THEN 1 ELSE 0 END,
                           template_cursor = CASE WHEN $2 THEN $4 ELSE template_cursor END,
                           last_result_count = CASE WHEN $2 THEN $5 ELSE last_result_count END,
                           total_result_count = total_result_count + CASE WHEN $2 THEN $5 ELSE 0 END,
                           zero_result_since = CASE
                               WHEN NOT $2 THEN zero_result_since
                               WHEN $5 > 0 THEN NULL
                               ELSE COALESCE(zero_result_since, now())
                           END,
                           expires_at = CASE
                               WHEN NOT $2 THEN expires_at
                               WHEN $5 > 0 THEN now() + ($6 * interval '1 day')
                               ELSE COALESCE(expires_at, now() + ($6 * interval '1 day'))
                           END,
                           updated_at = now()
                     WHERE term = $1
                    """,
                    item["term"],
                    completed,
                    self._dynamic_query_interval_days,
                    item["next_template_cursor"],
                    max(0, int(result_count)),
                    self._dynamic_term_ttl_days,
                )
        except Exception as exc:
            logger.debug("dynamic search result schedule update skipped: %s", type(exc).__name__)

    @asynccontextmanager
    async def _shared_search_engine_mutex(self):
        acquire = self._collect_mutex_acquire
        release = self._collect_mutex_release
        if not callable(acquire):
            yield True
            return

        logged_wait = False
        while not self._stop.is_set():
            handle = await acquire(self.SOURCE_NAME)
            if handle is not False:
                try:
                    yield True
                finally:
                    if callable(release) and handle is not None:
                        await release(handle)
                return
            if not logged_wait:
                logger.info("%s waiting for the shared search-engine mutex", self.SOURCE_NAME)
                logged_wait = True
            await asyncio.sleep(random.uniform(0.5, 1.5))
        yield False

    async def collect(self, targets: list[str]):
        # Pace between queries to reduce search-engine 429s (the ddgs brave
        # backend throttles aggressively). Env-tunable; 0 disables.
        query_delay = float(os.getenv("SEARCH_QUERY_DELAY_SECONDS", "3"))
        # A target that is a URL is a DIRECT seed (open directory / cloud bucket):
        # crawl it straight away (expand_paste_sites -> _spider_page, which also
        # enumerates S3/GCS ListBucketResult buckets). Everything else is a query.
        dynamic_items = await self._prepare_dynamic_search_queries()
        dynamic_by_query = {item["query"]: item for item in dynamic_items}
        direct = [t for t in targets if t.startswith(("http://", "https://"))]
        static_queries = [t for t in targets if not t.startswith(("http://", "https://"))]
        queries = list(dict.fromkeys([*(item["query"] for item in dynamic_items), *static_queries]))
        if direct:
            logger.info("Crawling %d direct seed URL(s) (open dir / bucket)", len(direct))
            async with self._shared_search_engine_mutex() as acquired:
                if not acquired:
                    return
                try:
                    await self.expand_paste_sites(direct)
                    for u in direct:
                        await self.checkpoint.save_progress(u)
                    await self.heartbeat_source_health()
                except Exception as e:
                    logger.exception("direct seed crawl failed: %s", e)
        for query in queries:
            if self._stop.is_set():
                break
            logger.info("Collecting search/%s", query)
            try:
                results = await self.search_query(query)
                if self._last_search_query_lock_skipped:
                    break
                if query in dynamic_by_query:
                    await self._mark_dynamic_search_query(
                        dynamic_by_query[query],
                        result_count=len(results),
                        completed=not self._last_search_query_rate_limited,
                    )
                await self.checkpoint.save_progress(query)
                await self.heartbeat_source_health()
            except Exception as e:
                logger.exception("Failed search/%s: %s", query, e)
                if query in dynamic_by_query:
                    await self._mark_dynamic_search_query(
                        dynamic_by_query[query], completed=False,
                    )
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
        self._last_search_query_lock_skipped = False
        async with self._shared_search_engine_mutex() as acquired:
            if not acquired:
                self._last_search_query_lock_skipped = True
                return []
            return await self._search_query_unlocked(query, engines=engines)

    async def _search_query_unlocked(
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
        self._last_search_query_rate_limited = not any((
            "ddg" in engines and self._DDGS is not None,
            "bing" in engines and self._BS is not None,
            "serper" in engines and bool(self._serper_api_key),
        ))
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
                self._last_search_query_rate_limited = True
                await self._record_search_rate_limit(
                    engine="serper",
                    account=self._serper_api_key[:8] or None,
                    scope="serper_quota",
                    status_code=None,
                    reason="serper account quota exhausted",
                    metadata={
                        "query": query,
                        "daily_quota": self._serper_quota,
                    },
                )
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
            for seed in self._domain_pacing.order(seed_urls):
                if self._stop.is_set():
                    break
                try:
                    if depth > 0:
                        # Recursive: walk sub-directories/links so open dirs get fully
                        # crawled and discovered links feed back in (no stagnation).
                        n = await self._crawl_seed(client, seed, max_depth=depth)
                        all_assets.append({"url": seed, "source_url": seed, "engine": "spider", "saved": n})
                    else:
                        discovered = await self._spider_page(client, seed)
                        for i, url in enumerate(sorted(discovered)):
                            all_assets.append({"url": url, "source_url": seed, "rank": i + 1, "engine": "spider"})
                            if self._download_images:
                                await self._download_asset(query=seed, hit={"url": url, "rank": i + 1}, source_url=seed)
                except Exception as e:
                    logger.warning("expand_paste_sites failed for %s: %s", seed, e)
        await self._record_domain_pacing_event(
            "crawl_summary",
            seed_urls[0] if seed_urls else "search-spider",
            metadata={
                "seed_count": len(seed_urls),
                "assets": len(all_assets),
                "domain_pacing": self._domain_pacing.snapshot().as_dict(),
            },
        )
        return all_assets

    async def _extract_pdf_links(self, pdf_url: str) -> list[str]:
        """Pull http(s) links out of a just-downloaded PDF (annotations + text) so
        they can be spidered too. Best-effort; needs PyMuPDF."""
        try:
            import fitz  # PyMuPDF
        except Exception:
            return []
        try:
            async with self.pool.acquire() as conn:
                fp = await conn.fetchval(
                    "SELECT file_path FROM media_items WHERE source='search' AND source_url=$1 ORDER BY collected_at DESC LIMIT 1",
                    pdf_url,
                )
        except Exception:
            return []
        if not fp or not os.path.exists(fp):
            return []

        def _extract(path):
            links = set()
            try:
                doc = fitz.open(path)
                for page in doc:
                    for l in page.get_links():
                        u = l.get("uri", "")
                        if u.startswith("http"):
                            links.add(u)
                    for m in re.findall(r"https?://[^\s)>\]\"']+", page.get_text() or ""):
                        links.add(m.rstrip(".,);"))
                doc.close()
            except Exception:
                return []
            return list(links)[:50]
        try:
            return await asyncio.to_thread(_extract, fp)
        except Exception:
            return []

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
            scope = urlparse(url).netloc or host or "crawl_seed"
            if self._search_scope_cooling_down("spider", scope):
                continue
            try:
                async def _get_seed():
                    async with self._domain_pacing.slot(url):
                        await self.wait_rate_limit(scope)
                        return await client.get(url, headers=self._headers(urlparse(url).netloc))

                resp = await _get_seed()
                if resp.status_code == 429:
                    retry_resp = await self._retry_search_after_429(
                        _get_seed,
                        engine="spider",
                        scope=scope,
                        reason="seed crawl returned 429",
                        metadata={"url": url, "seed": seed},
                    )
                    if retry_resp is not None:
                        resp = retry_resp
            except Exception:
                continue
            if resp.status_code != 200:
                if resp.status_code == 429:
                    await self._record_search_rate_limit(
                        engine="spider",
                        scope=scope,
                        status_code=429,
                        reason="seed crawl returned 429",
                        metadata={"url": url, "seed": seed},
                        response=resp,
                    )
                await self._record_domain_http_status(
                    url,
                    resp.status_code,
                    metadata={"engine": "spider", "seed": seed},
                )
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
                        # PDFs: extract their embedded links and feed them back into
                        # the crawl so we don't go stagnant (user request).
                        if d < max_depth and cand.lower().split("?")[0].endswith(".pdf"):
                            for lk in await self._extract_pdf_links(cand):
                                if lk not in seen and len(seen) + len(frontier) < max_pages:
                                    frontier.append((lk, d + 1))
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
        yahoo_cooling = await self._search_scope_cooldown_active("yahoo", "search.yahoo.com")
        bing_cooling = await self._search_scope_cooldown_active("bing", "bing.com")
        if yahoo_cooling and bing_cooling:
            self._last_search_query_rate_limited = True
            logger.info("[DDG] Yahoo and Bing providers cooling down; skipping requests")
            return []
        if yahoo_cooling or bing_cooling:
            self._last_search_query_rate_limited = True

        loop = asyncio.get_event_loop()
        text_rate_limit: dict[str, str] | None = None
        image_rate_limit: dict[str, str] | None = None

        # Retry config for transient DDG failures (intermittent "malformed
        # headers" responses from DDG's edge, dropped connections, timeouts,
        # 5xx). Env-tunable; SEARCH_DDG_RETRIES=1 disables retries.
        ddg_retries = max(1, int(os.getenv("SEARCH_DDG_RETRIES", "3")))
        ddg_timeout = int(os.getenv("SEARCH_DDG_TIMEOUT", "30"))
        ddg_retry_base = float(os.getenv("SEARCH_DDG_RETRY_BASE_SECONDS", "2.0"))

        def _ddg_call(kind: str) -> list[dict]:
            """Run one DDGS text/images call, retrying transient failures.

            Runs inside the executor thread, so time.sleep is safe here.
            Genuine 429s are NOT retried — the politeness gate and unified
            rate limiter own those.
            """
            last_exc: Optional[Exception] = None
            for attempt in range(ddg_retries):
                try:
                    with DDGS(timeout=ddg_timeout) as ddgs:
                        if kind == "text":
                            return list(ddgs.text(
                                query,
                                max_results=self._max_results,
                                backend="yahoo",
                            ))
                        return list(ddgs.images(
                            query,
                            max_results=self._max_results,
                            backend="bing",
                        ))
                except Exception as e:  # noqa: BLE001 - classified below
                    last_exc = e
                    if self._looks_like_rate_limit_error(e):
                        raise
                    if not self._is_ddg_transient_error(e) or attempt + 1 >= ddg_retries:
                        raise
                    wait = ddg_retry_base * (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[DDG] %s search transient failure (attempt %d/%d): %s — retrying in %.1fs",
                        kind, attempt + 1, ddg_retries, e, wait,
                    )
                    time.sleep(wait)
            assert last_exc is not None  # for type checkers
            raise last_exc

        def _do_text() -> list[dict]:
            nonlocal text_rate_limit
            out: list[dict] = []
            try:
                for r in _ddg_call("text"):
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
                if self._looks_like_rate_limit_error(e):
                    text_rate_limit = {"message": str(e)}
                logger.warning("[DDG] text search failed: %s", e)
            return out

        def _do_images() -> list[dict]:
            nonlocal image_rate_limit
            out: list[dict] = []
            try:
                for r in _ddg_call("images"):
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
                if self._looks_like_rate_limit_error(e):
                    image_rate_limit = {"message": str(e)}
                logger.debug("[DDG] image search failed: %s", e)
            return out

        async def _paced_ddg_call(fn, host: str):
            # Each DDGS call is pinned to one backend and paced through the
            # same per-host slots used by direct HTTP search requests.
            async with self._domain_pacing.slot(f"https://{host}/"):
                await self.wait_rate_limit(host)
                return await loop.run_in_executor(None, fn)

        text_hits = [] if yahoo_cooling else await _paced_ddg_call(_do_text, "search.yahoo.com")
        image_hits = [] if bing_cooling else await _paced_ddg_call(_do_images, "bing.com")
        if text_rate_limit is not None:
            self._last_search_query_rate_limited = True
            await self._record_search_rate_limit(
                engine="yahoo",
                scope="search.yahoo.com",
                status_code=429,
                reason="ddg text search rate-limit response",
                metadata={"query": query, "kind": "text", **text_rate_limit},
            )
        if image_rate_limit is not None:
            self._last_search_query_rate_limited = True
            await self._record_search_rate_limit(
                engine="bing",
                scope="bing.com",
                status_code=429,
                reason="ddg image search rate-limit response",
                metadata={"query": query, "kind": "image", **image_rate_limit},
            )
        if not yahoo_cooling and text_rate_limit is None:
            self._clear_search_scope_cooldown("yahoo", "search.yahoo.com")
        if not bing_cooling and image_rate_limit is None:
            self._clear_search_scope_cooldown("bing", "bing.com")
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
                scope = "bing.com"
                if await self._search_scope_cooldown_active("bing", scope):
                    self._last_search_query_rate_limited = True
                    break
                try:
                    async def _get_bing():
                        async with self._domain_pacing.slot(url):
                            await self.wait_rate_limit(scope)
                            return await client.get(url, headers=self._headers(scope))

                    resp = await _get_bing()
                    if resp.status_code == 429:
                        retry_resp = await self._retry_search_after_429(
                            _get_bing,
                            engine="bing",
                            scope=scope,
                            reason="bing search returned 429",
                            metadata={"query": query, "page": page, "url": url},
                        )
                        if retry_resp is not None:
                            resp = retry_resp
                    if resp.status_code != 200:
                        if resp.status_code == 429:
                            self._last_search_query_rate_limited = True
                            await self._record_search_rate_limit(
                                engine="bing",
                                scope=scope,
                                status_code=429,
                                reason="bing search returned 429",
                                metadata={"query": query, "page": page, "url": url},
                                response=resp,
                            )
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
                scope = "google.serper.dev"
                account = self._serper_api_key[:8] or None
                if await self._search_scope_cooldown_active("serper", scope):
                    self._last_search_query_rate_limited = True
                    break
                try:
                    async def _post_serper():
                        async with self._domain_pacing.slot(url):
                            await self.wait_rate_limit(scope)
                            return await client.post(url, headers=headers, json=payload)

                    resp = await _post_serper()
                    if resp.status_code == 401:
                        logger.error("[Serper] API key unauthorized")
                        break
                    if resp.status_code == 429:
                        retry_resp = await self._retry_search_after_429(
                            _post_serper,
                            engine="serper",
                            account=account,
                            scope=scope,
                            reason="serper search returned 429",
                            metadata={
                                "query": query,
                                "page": page + 1,
                                "url": url,
                            },
                        )
                        if retry_resp is not None:
                            resp = retry_resp
                    if resp.status_code == 429:
                        self._last_search_query_rate_limited = True
                        await self._record_search_rate_limit(
                            engine="serper",
                            account=account,
                            scope=scope,
                            status_code=429,
                            reason="serper search returned 429",
                            metadata={
                                "query": query,
                                "page": page + 1,
                                "url": url,
                            },
                            response=resp,
                        )
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
                if await self._upsert_result(query, hit):
                    self._progress_count += 1
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

    async def _upsert_result(self, query: str, hit: dict) -> bool:
        if self.pool is None:
            return False
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM search_queries WHERE query = $1 AND engine = $2",
                query, "waterfall",
            )
            if not row:
                return False
            result = await conn.fetchrow(
                """
                INSERT INTO search_results
                    (query_id, url, title, snippet, rank, domain, date_published)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (query_id, url) DO NOTHING
                RETURNING id
                """,
                row["id"],
                hit["url"],
                hit.get("title"),
                hit.get("snippet"),
                hit.get("rank"),
                hit.get("domain") or urlparse(hit["url"]).netloc,
                hit.get("date_published"),
            )
            return result is not None

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
                scope = urlparse(url).netloc or "bucket"
                if self._search_scope_cooling_down("bucket", scope):
                    break
                try:
                    async def _get_bucket():
                        async with self._domain_pacing.slot(url):
                            await self.wait_rate_limit(scope)
                            return await client.get(url, headers=self._headers(urlparse(url).netloc))

                    r = await _get_bucket()
                    if r.status_code == 429:
                        retry_resp = await self._retry_search_after_429(
                            _get_bucket,
                            engine="bucket",
                            scope=scope,
                            reason="bucket listing returned 429",
                            metadata={"url": url, "root": root},
                        )
                        if retry_resp is not None:
                            r = retry_resp
                    if r.status_code != 200:
                        if r.status_code == 429:
                            await self._record_search_rate_limit(
                                engine="bucket",
                                scope=scope,
                                status_code=429,
                                reason="bucket listing returned 429",
                                metadata={"url": url, "root": root},
                                response=r,
                            )
                        await self._record_domain_http_status(
                            url,
                            r.status_code,
                            metadata={"engine": "bucket", "root": root},
                        )
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
        scope = urlparse(page_url).netloc or "spider_page"
        if self._search_scope_cooling_down("spider", scope):
            return discovered
        try:
            async def _get_spider_page():
                async with self._domain_pacing.slot(page_url):
                    await self.wait_rate_limit(scope)
                    return await client.get(page_url, headers=self._headers(urlparse(page_url).netloc))

            resp = await _get_spider_page()
            if resp.status_code == 429:
                retry_resp = await self._retry_search_after_429(
                    _get_spider_page,
                    engine="spider",
                    scope=scope,
                    reason="spider page returned 429",
                    metadata={"url": page_url},
                )
                if retry_resp is not None:
                    resp = retry_resp
            if resp.status_code != 200:
                if resp.status_code == 429:
                    await self._record_search_rate_limit(
                        engine="spider",
                        scope=scope,
                        status_code=429,
                        reason="spider page returned 429",
                        metadata={"url": page_url},
                        response=resp,
                    )
                await self._record_domain_http_status(
                    page_url,
                    resp.status_code,
                    metadata={"engine": "spider"},
                )
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

        _ext = os.path.splitext(urlparse(url.lower()).path)[1]

        # Never download audio or code/executables (user rule).
        if _ext in AUDIO_EXTENSIONS or _ext in CODE_EXTENSIONS:
            return False

        # Videos: stream to disk (no cap) BEFORE buffering the body into memory.
        if self._download_videos and _ext in VIDEO_EXTENSIONS:
            saved = await self._stream_video(query, cid, url, source_url or url)
            if saved:
                self._domain_pacing.count("videos_found")
            return saved

        engine = hit.get("engine") or "download"
        scope = urlparse(url).netloc or "download"
        if self._search_scope_cooling_down(engine, scope):
            return False

        async with self._sem:
            try:
                async with self._make_client(timeout=30.0) as client:
                    async def _get_asset():
                        async with self._domain_pacing.slot(url):
                            await self.wait_rate_limit(scope)
                            return await client.get(url, headers=self._headers(urlparse(url).netloc))

                    resp = await _get_asset()
                    if resp.status_code == 429:
                        retry_resp = await self._retry_search_after_429(
                            _get_asset,
                            engine=engine,
                            scope=scope,
                            reason="asset download returned 429",
                            metadata={
                                "query": query,
                                "url": url,
                                "source_url": source_url,
                                "rank": hit.get("rank"),
                            },
                        )
                        if retry_resp is not None:
                            resp = retry_resp
                    if resp.status_code != 200:
                        if resp.status_code == 429:
                            await self._record_search_rate_limit(
                                engine=engine,
                                scope=scope,
                                status_code=429,
                                reason="asset download returned 429",
                                metadata={
                                    "query": query,
                                    "url": url,
                                    "source_url": source_url,
                                    "rank": hit.get("rank"),
                                },
                                response=resp,
                            )
                        await self._record_domain_http_status(
                            url,
                            resp.status_code,
                            metadata={
                                "engine": engine,
                                "query": query,
                                "source_url": source_url,
                            },
                        )
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
        is_doc = self._download_docs and (
            ext in DOC_EXTENSIONS
            or any(t in ctype for t in _DOC_CONTENT_TYPES)
        )

        q_slug = hashlib.sha256(query.encode()).hexdigest()[:12] if query else "spider"
        q_name = (query or "spider")[:50]

        if is_pdf:
            saved = await self._save_pdf(
                q_slug, q_name, cid, data, source_url or url,
                title=hit.get("title"), snippet=hit.get("snippet"), search_query=query,
            )
            if saved:
                self._domain_pacing.count("pdfs_found")
            return saved
        if is_doc:
            document_ext = ext if ext in DOC_EXTENSIONS else (
                _document_extension_from_content_type(ctype) or ext
            )
            saved = await self._save_document(
                q_slug, q_name, cid, data, source_url or url, document_ext,
                title=hit.get("title"), snippet=hit.get("snippet"), search_query=query,
            )
            if saved:
                self._domain_pacing.count("docs_found")
            return saved
        if is_image:
            saved = await self._save_image(q_slug, q_name, cid, data, source_url or url)
            if saved:
                self._domain_pacing.count("media_found")
            return saved
        # Unknown content-type: try as image (PIL will reject if it's not).
        saved = await self._save_image(q_slug, q_name, cid, data, source_url or url)
        if saved:
            self._domain_pacing.count("media_found")
        return saved

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
            image_metadata = self._extract_image_metadata(img)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
        except Exception:
            return False

        filename = self.build_filename(
            entity_id, entity_name, "image", content_id, extension="jpg",
        )

        loop = asyncio.get_event_loop()

        def _encode_image() -> bytes:
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=95)
            return out.getvalue()

        try:
            stored_data = await loop.run_in_executor(None, _encode_image)
        except Exception:
            logger.exception("save image failed cid=%s url=%s", content_id, source_url)
            return False

        sha = self.sha256_bytes(stored_data)
        meta = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "content_type": "image",
            "content_id": content_id,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
            "width": w,
            "height": h,
            "image_metadata": image_metadata,
            "rebuild_target_tables": ["media_items", "search_results", "search_queries"],
        }
        artifact = write_atomic_artifact(
            source=self.SOURCE_NAME,
            artifact_id=content_id,
            artifact_kind="media_blob",
            data=stored_data,
            extension="jpg",
            expected_sha256=sha,
            metadata={
                **meta,
                "filename": filename,
                "request_url": source_url,
            },
            root=VAULT_ROOT,
        )
        if not artifact.path:
            logger.debug("save image vault write failed cid=%s: %s", content_id, artifact.error)
            return False
        meta["vault_artifact"] = {
            "ok": artifact.ok,
            "partial": artifact.partial,
            "path": artifact.relative_path,
            "blob_path": artifact.blob_relative_path,
            "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
            "duplicate_blob": artifact.duplicate_blob,
            "error": artifact.error,
        }

        try:
            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=entity_name,
                content_type="image",
                content_id=content_id,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                width=w,
                height=h,
                sha256=artifact.sha256,
                source_url=source_url,
                metadata=meta,
            )
            if artifact.partial:
                await self.send_to_dlq(entity_id, content_id, f"vault artifact partial: {artifact.error}")
        except Exception as e:
            logger.debug("insert_media_item failed: %s", e)

        await self._index_search_document(
            content_id=content_id,
            data=stored_data,
            extension=".jpg",
            source_url=source_url,
            title=entity_name,
            content_text="",
            extraction_method="pillow_image_metadata",
            page_count=None,
            metadata={
                "search_query": entity_id[:500],
                "source_host": urlparse(source_url).netloc[:255],
                "byte_size": len(stored_data),
                "indexed_chars": 0,
                "image": image_metadata,
            },
        )

        self._known_ids.add(content_id)
        return True

    @staticmethod
    def _extract_image_metadata(image) -> dict[str, Any]:
        """Return dimensions and a small EXIF allowlist, excluding GPS/MakerNote."""
        metadata: dict[str, Any] = {
            "format": str(getattr(image, "format", "") or "")[:40] or None,
            "mode": str(getattr(image, "mode", "") or "")[:40] or None,
            "width": int(image.size[0]),
            "height": int(image.size[1]),
        }
        try:
            from PIL.ExifTags import TAGS

            exif = image.getexif()
            allowed = {"DateTime", "DateTimeOriginal", "Make", "Model", "Software", "Orientation", "Artist", "Copyright", "ImageDescription"}
            selected = {}
            for tag_id, value in exif.items():
                name = TAGS.get(tag_id)
                if name not in allowed:
                    continue
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                if isinstance(value, (int, float, bool)):
                    selected[name] = value
                else:
                    selected[name] = str(value).replace("\x00", " ").strip()[:256]
            if selected:
                metadata["exif"] = selected
        except Exception:
            pass
        return metadata

    @staticmethod
    def _extract_document_text(
        data: bytes,
        extension: str,
        *,
        fitz_module=None,
        max_pages: int = 50,
        max_chars: int = 100_000,
        max_archive_bytes: int = 100 * 1024 * 1024,
    ) -> tuple[str, str, int | None]:
        """Extract bounded text from common document formats without executing content."""
        ext = (extension or "").lower()
        if ext == ".pdf":
            if fitz_module is None:
                return "", "metadata_only", None
            try:
                document = fitz_module.open(stream=data, filetype="pdf")
                try:
                    page_limit = min(len(document), max(0, max_pages))
                    parts = []
                    page_count = 0
                    for index in range(page_limit):
                        parts.append(document.load_page(index).get_text("text"))
                        page_count += 1
                        if sum(map(len, parts)) >= max_chars:
                            break
                    return "\n".join(parts)[:max_chars], "pymupdf", page_count
                finally:
                    document.close()
            except Exception:
                return "", "metadata_only", None

        if ext in {".txt", ".csv", ".rtf"}:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("cp1252", errors="replace")
            if ext == ".rtf":
                text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
                text = re.sub(r"\\par\b", "\n", text, flags=re.IGNORECASE)
                text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", text)
                text = text.replace("{", " ").replace("}", " ")
            return text[:max_chars], "rtf" if ext == ".rtf" else "text", None

        if ext not in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
            return "", "metadata_only", None

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > 2_000:
                    return "", "archive_limit", None
                remaining_archive_bytes = max(0, max_archive_bytes)
                if ext in {".docx", ".odt"}:
                    selected = [
                        info for info in infos
                        if info.filename == "word/document.xml"
                        or info.filename == "content.xml"
                        or info.filename.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes"))
                    ]
                elif ext == ".xlsx":
                    selected = [
                        info for info in infos
                        if info.filename == "xl/sharedStrings.xml"
                        or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", info.filename)
                    ]
                else:
                    selected = [
                        info for info in infos
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename)
                        or info.filename == "content.xml"
                    ]

                parts = []
                remaining = max_chars
                limited = False
                for info in selected:
                    if remaining <= 0 or remaining_archive_bytes <= 0:
                        limited = True
                        break
                    entry_limit = min(8 * 1024 * 1024, remaining_archive_bytes)
                    entry_parts = []
                    entry_bytes = 0
                    with archive.open(info) as member:
                        while entry_bytes < entry_limit:
                            chunk = member.read(min(64 * 1024, entry_limit - entry_bytes))
                            if not chunk:
                                break
                            entry_parts.append(chunk)
                            entry_bytes += len(chunk)
                    remaining_archive_bytes -= entry_bytes
                    if entry_bytes >= entry_limit:
                        limited = True
                    root = ET.fromstring(b"".join(entry_parts))
                    if ext in {".odt", ".ods", ".odp"}:
                        extracted = " ".join(part.strip() for part in root.itertext() if part and part.strip())
                    else:
                        names = {"t", "v"} if ext == ".xlsx" else {"t"}
                        extracted = " ".join(
                            node.text.strip() for node in root.iter()
                            if node.tag.rsplit("}", 1)[-1] in names and node.text and node.text.strip()
                        )
                    if extracted:
                        parts.append(extracted[:remaining])
                        remaining -= len(extracted)
                method = "opendocument" if ext in {".odt", ".ods", ".odp"} else "openxml"
                if limited:
                    method = f"{method}_limited"
                return "\n".join(parts)[:max_chars], method, None
        except (
            OSError, ValueError, RuntimeError, EOFError, zlib.error,
            zipfile.BadZipFile, ET.ParseError,
        ):
            return "", "metadata_only", None

    async def _index_downloaded_document(
        self,
        *,
        content_id: str,
        data: bytes,
        extension: str,
        source_url: str,
        title: str | None,
        snippet: str | None,
        search_query: str,
    ) -> None:
        if not self._document_index_enabled or self.pool is None:
            return
        try:
            content_text, extraction_method, page_count = await asyncio.to_thread(
                self._extract_document_text,
                data,
                extension,
                fitz_module=self._fitz,
                max_pages=self._max_pdf_pages,
                max_chars=self._max_document_text_chars,
                max_archive_bytes=self._max_document_archive_bytes,
            )
            # PostgreSQL text values reject NUL bytes, which can occur in
            # malformed or binary-looking files that still decode as text.
            content_text = content_text.replace("\x00", " ")
            metadata = {
                "search_query": search_query[:500],
                "snippet": (snippet or "")[:2_000],
                "source_host": urlparse(source_url).netloc[:255],
                "byte_size": len(data),
                "indexed_chars": len(content_text),
            }
            await self._index_search_document(
                content_id=content_id,
                data=data,
                extension=extension,
                source_url=source_url,
                title=title,
                content_text=content_text,
                extraction_method=extraction_method,
                page_count=page_count,
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug("document text index update skipped content_id=%s error=%s", content_id, type(exc).__name__)

    async def _index_search_document(
        self,
        *,
        content_id: str,
        data: bytes,
        extension: str,
        source_url: str,
        title: str | None,
        content_text: str,
        extraction_method: str,
        page_count: int | None,
        metadata: dict[str, Any],
    ) -> None:
        if not self._document_index_enabled or self.pool is None:
            return
        try:
            async with self.pool.acquire() as conn:
                media_item_id = await conn.fetchval(
                    "SELECT id FROM media_items WHERE source = 'search' AND content_id = $1 LIMIT 1",
                    content_id,
                )
                if media_item_id is None:
                    return
                await conn.execute(
                    """
                    INSERT INTO search_documents (
                        media_item_id, source_url, title, file_extension, sha256,
                        content_text, extraction_method, page_count, metadata
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                    ON CONFLICT (media_item_id) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        title = EXCLUDED.title,
                        file_extension = EXCLUDED.file_extension,
                        sha256 = EXCLUDED.sha256,
                        content_text = EXCLUDED.content_text,
                        extraction_method = EXCLUDED.extraction_method,
                        page_count = EXCLUDED.page_count,
                        metadata = EXCLUDED.metadata,
                        indexed_at = now()
                    """,
                    media_item_id,
                    source_url,
                    (title or "")[:1_000] or None,
                    (extension or ".bin")[:20],
                    hashlib.sha256(data).hexdigest(),
                    content_text,
                    extraction_method,
                    page_count,
                    json.dumps(metadata),
                )
        except Exception as exc:
            logger.debug(
                "search document index update skipped content_id=%s error=%s",
                content_id,
                type(exc).__name__,
            )

    async def _save_pdf(
        self,
        entity_id: str,
        entity_name: str,
        content_id: str,
        data: bytes,
        source_url: str,
        *,
        title: str | None = None,
        snippet: str | None = None,
        search_query: str | None = None,
    ) -> bool:
        # Persist the PDF blob first (dedup'd by content_id)
        filename = self.build_filename(
            entity_id, entity_name, "pdf", content_id, extension="pdf",
        )
        dest_dir = self.account_media_dir / "pdf"
        loop = asyncio.get_event_loop()
        sha = self.sha256_bytes(data)
        meta = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "content_type": "pdf",
            "content_id": content_id,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
            "rebuild_target_tables": ["media_items", "search_results", "search_queries"],
        }
        artifact = write_atomic_artifact(
            source=self.SOURCE_NAME,
            artifact_id=content_id,
            artifact_kind="media_blob",
            data=data,
            extension="pdf",
            expected_sha256=sha,
            metadata={
                **meta,
                "filename": filename,
                "request_url": source_url,
            },
            root=VAULT_ROOT,
        )
        if not artifact.path:
            logger.debug("save pdf vault write failed cid=%s: %s", content_id, artifact.error)
            return False
        meta["vault_artifact"] = {
            "ok": artifact.ok,
            "partial": artifact.partial,
            "path": artifact.relative_path,
            "blob_path": artifact.blob_relative_path,
            "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
            "duplicate_blob": artifact.duplicate_blob,
            "error": artifact.error,
        }
        try:
            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=entity_name,
                content_type="pdf",
                content_id=content_id,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                source_url=source_url,
                metadata=meta,
            )
            if artifact.partial:
                await self.send_to_dlq(entity_id, content_id, f"vault artifact partial: {artifact.error}")
        except Exception as e:
            logger.debug("insert_media_item failed: %s", e)

        await self._index_downloaded_document(
            content_id=content_id,
            data=data,
            extension=".pdf",
            source_url=source_url,
            title=title or entity_name,
            snippet=snippet,
            search_query=search_query or entity_name,
        )

        # Optionally rasterise pages → JPGs
        if self._fitz is not None:
            await loop.run_in_executor(
                None, self._extract_pdf_pages, data, dest_dir, Path(filename).stem,
            )

        self._known_ids.add(content_id)
        return True

    async def _save_document(
        self,
        entity_id: str,
        entity_name: str,
        content_id: str,
        data: bytes,
        source_url: str,
        ext: str,
        *,
        title: str | None = None,
        snippet: str | None = None,
        search_query: str | None = None,
    ) -> bool:
        """Persist an office/text document (word/ppt/excel/csv/txt/rtf) with
        content_type 'document'. Byte-capped."""
        if not data or len(data) > self._max_doc_bytes:
            return False
        clean_ext = (ext or ".bin").lstrip(".") or "bin"
        filename = self.build_filename(
            entity_id, entity_name, "document", content_id, extension=clean_ext,
        )
        sha = self.sha256_bytes(data)
        meta = {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "content_type": "document",
            "content_id": content_id,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
            "rebuild_target_tables": ["media_items", "search_results", "search_queries"],
        }
        artifact = write_atomic_artifact(
            source=self.SOURCE_NAME,
            artifact_id=content_id,
            artifact_kind="media_blob",
            data=data,
            extension=clean_ext,
            expected_sha256=sha,
            metadata={
                **meta,
                "filename": filename,
                "request_url": source_url,
            },
            root=VAULT_ROOT,
        )
        if not artifact.path:
            logger.debug("save document vault write failed cid=%s: %s", content_id, artifact.error)
            return False
        meta["vault_artifact"] = {
            "ok": artifact.ok,
            "partial": artifact.partial,
            "path": artifact.relative_path,
            "blob_path": artifact.blob_relative_path,
            "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
            "duplicate_blob": artifact.duplicate_blob,
            "error": artifact.error,
        }
        try:
            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=entity_name,
                content_type="document",
                content_id=content_id,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                source_url=source_url,
                metadata=meta,
            )
            if artifact.partial:
                await self.send_to_dlq(entity_id, content_id, f"vault artifact partial: {artifact.error}")
        except Exception as e:
            logger.debug("insert_media_item failed: %s", e)

        await self._index_downloaded_document(
            content_id=content_id,
            data=data,
            extension=clean_ext if clean_ext.startswith(".") else f".{clean_ext}",
            source_url=source_url,
            title=title or entity_name,
            snippet=snippet,
            search_query=search_query or entity_name,
        )
        self._known_ids.add(content_id)
        return True

    async def _stream_video(
        self,
        query: str,
        content_id: str,
        url: str,
        source_url: str,
    ) -> bool:
        """Stream a video to disk in chunks (never buffered whole), no size cap
        by default (SEARCH_MAX_VIDEO_BYTES=0). content_type 'video'."""
        if self.is_known(content_id):
            return False
        engine = "video"
        scope = urlparse(url).netloc or "video"
        if self._search_scope_cooling_down(engine, scope):
            return False
        ext = (os.path.splitext(urlparse(url.lower()).path)[1] or ".mp4").lstrip(".") or "mp4"
        q_slug = hashlib.sha256(query.encode()).hexdigest()[:12] if query else "spider"
        q_name = (query or "spider")[:50]
        filename = self.build_filename(q_slug, q_name, "video", content_id, extension=ext)
        dest_dir = self.account_media_dir / "video"
        assert_media_write_allowed(dest_dir / filename)
        dest_dir.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        size = 0
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".part")
        try:
            async with self._sem:
                with os.fdopen(fd, "wb") as f:
                    async with self._make_client(timeout=120.0) as client:
                        retry_delay: float | None = None
                        for attempt in (1, 2):
                            async with self._domain_pacing.slot(url):
                                await self.wait_rate_limit(scope)
                                async with client.stream(
                                    "GET", url, headers=self._headers(urlparse(url).netloc)
                                ) as resp:
                                    if resp.status_code == 429 and attempt == 1:
                                        retry_delay = await sleep_before_pre_cooldown_retry(
                                            "search",
                                            scope,
                                            status_code=429,
                                            reason="video stream returned 429",
                                        )
                                        if retry_delay is not None:
                                            continue
                                    if retry_delay is not None and resp.status_code != 429:
                                        await self._record_search_rate_limit(
                                            engine=engine,
                                            scope=scope,
                                            status_code=429,
                                            cooldown_active=False,
                                            reason=f"video stream returned 429; retry returned HTTP {resp.status_code}",
                                            metadata={
                                                "query": query,
                                                "url": url,
                                                "source_url": source_url,
                                                "pre_cooldown_retry": True,
                                                "retry_status_code": resp.status_code,
                                                "retry_delay_seconds": retry_delay,
                                            },
                                        )
                                        retry_delay = None
                                    if resp.status_code != 200:
                                        if resp.status_code == 429:
                                            await self._record_search_rate_limit(
                                                engine=engine,
                                                scope=scope,
                                                status_code=429,
                                                reason="video stream returned 429",
                                                metadata={
                                                    "query": query,
                                                    "url": url,
                                                    "source_url": source_url,
                                                },
                                                response=resp,
                                            )
                                        await self._record_domain_http_status(
                                            url,
                                            resp.status_code,
                                            metadata={
                                                "engine": engine,
                                                "query": query,
                                                "source_url": source_url,
                                            },
                                        )
                                        raise RuntimeError(f"status {resp.status_code}")
                                    ct = resp.headers.get("content-type", "").lower()
                                    if ct and not (ct.startswith("video/")
                                                   or ct == "application/octet-stream"):
                                        raise RuntimeError(f"non-video content-type {ct}")
                                    async for chunk in resp.aiter_bytes(self._video_chunk_bytes):
                                        if not chunk:
                                            continue
                                        size += len(chunk)
                                        if self._max_video_bytes and size > self._max_video_bytes:
                                            raise RuntimeError("video exceeds cap")
                                        f.write(chunk)
                                        hasher.update(chunk)
                                    break
            if size < self._min_file_size:
                raise RuntimeError("video too small")
        except Exception as e:
            logger.debug("video stream failed for %s: %s", url, e)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

        sha = hasher.hexdigest()
        meta = {
            "entity_id": q_slug,
            "entity_name": q_name,
            "content_type": "video",
            "content_id": content_id,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": source_url,
            "file_size": size,
            "rebuild_target_tables": ["media_items", "search_results", "search_queries"],
        }
        artifact = write_atomic_artifact_from_path(
            source=self.SOURCE_NAME,
            artifact_id=content_id,
            artifact_kind="media_blob",
            source_path=tmp_path,
            extension=ext,
            expected_sha256=sha,
            metadata={
                **meta,
                "filename": filename,
                "request_url": url,
            },
            root=VAULT_ROOT,
            delete_source=True,
        )
        if tmp_path and not os.path.exists(tmp_path):
            tmp_path = None
        if not artifact.path:
            logger.debug("video stream vault write failed cid=%s: %s", content_id, artifact.error)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False
        meta["vault_artifact"] = {
            "ok": artifact.ok,
            "partial": artifact.partial,
            "path": artifact.relative_path,
            "blob_path": artifact.blob_relative_path,
            "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
            "duplicate_blob": artifact.duplicate_blob,
            "error": artifact.error,
        }
        try:
            await self.insert_media_item(
                entity_id=q_slug,
                entity_name=q_name,
                content_type="video",
                content_id=content_id,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                source_url=source_url,
                metadata=meta,
            )
            if artifact.partial:
                await self.send_to_dlq(q_slug, content_id, f"vault artifact partial: {artifact.error}")
        except Exception as e:
            logger.debug("insert_media_item failed: %s", e)
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
            assert_media_write_allowed(pages_dir / f"{base_filename}_page_1.jpg")
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
