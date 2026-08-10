"""Lemon8 collector — Wave 2 hardened port of ``lemon8toolkit/``.

Ported from the standalone ``lemon8toolkit/`` (main.py + src/scraper.py,
src/downloader.py, src/account_manager.py, src/rate_limiter.py,
src/profile_photo_tracker.py, src/tracking.py, src/graph_builder.py,
src/path_manager.py, src/progress.py, src/reconciler.py, src/config.py)
into the unified collector framework.

ABSORBED (parity targets, ~95%):
    - Cookie-jar loading (Netscape format) + tt_webid extraction
    - For-You feed scrape (web HTML + optional pylemon8 API path)
    - User profile + posts scrape (HTML + JSON island parsing)
    - Topic / tag-page scrape with pagination
    - Profile-photo extraction + per-user avatar download
    - Image URL high-quality enhancement (CDN shrink-param stripping)
    - Small-image / thumbnail filtering (min width/height/file-size)
    - Hashtag / user-handle / tag-id discovery from HTML + JSON
    - Spider queue (legacy ``lemon8_spider_queue``) + Wave 0
      ``SpiderDiscover`` adapter (``Lemon8EdgeFetcher``) over related
      creators discovered from feed / profile / topic HTML
    - Adaptive per-domain rate limiting (Wave 0 ``adaptive_rate``
      surfaced via ``HumanLikeRateLimiter`` on the BaseCollector)
    - Per-account daily quota cap (Wave 0 ``account_quota``)
    - Content dedupe via ``dedupe_hash.sha256_bytes`` (BaseCollector
      delegates to the Wave 0 module)
    - Atomic media-file writes via vault blobs + source-occurrence sidecars
    - Profile + post upserts (``lemon8_profiles``, ``lemon8_posts``)
      with deterministic FYP card-id synthesis when no platform id exists

DROPPED (intentionally — out of scope for read-only ingest):
    - Standalone web UI / dashboard (lemon8toolkit had none, but the
      toolkit's interactive ``main()`` CLI is dropped)
    - CLI / setup wizard (``main()`` argparse + interactive prompts)
    - Any write/post/upload endpoints (lemon8 has none in the toolkit
      either; included here for symmetry with the IG/TikTok rules)
    - Like / comment / follow writes (read-only; no graph mutation)
    - Toolkit's own SQLite tracking DB (``tracking.py``,
      ``profile_photo_tracker``'s pickled state, ``progress.py``'s JSON
      checkpoint files) — replaced by unified Postgres + checkpoint
      manager from ``BaseCollector``
    - Toolkit's local file-tree ``path_manager`` — unified collector
      uses ``BaseCollector.account_media_dir`` instead

DEFERRED (left as TODO for a later wave; non-blocking):
    - ``graph_builder.py`` cross-platform graph export (the spider
      already populates ``lemon8_spider_queue``; a downstream graph
      job can read directly from the unified DB)
    - ``reconciler.py`` tier1/tier2 reconciliation jobs (download
      auditing) — unified collector relies on DLQ + checkpoint replay
    - Per-account multi-cookie rotation pool (toolkit ``account_manager``
      supports many cookie sets; this port reads a single
      ``LEMON8_COOKIES_FILE`` — sufficient for current scheduler)

⚠️  IP-CONFLICT WARNING
   Instagram, TikTok, and Lemon8 MUST NEVER run simultaneously when
   sharing a public IP. Lemon8 is operated by ByteDance and shares
   anti-bot fingerprinting infrastructure with TikTok; concurrent
   traffic from the same IP across any two of {IG, TikTok, Lemon8}
   triggers immediate cross-platform challenges/bans. The scheduler /
   concurrency rule that enforces this lives outside this module
   (see ``scheduler/`` mutex group); the collector itself does no
   enforcement. If you're invoking ``run()`` directly from a script,
   ensure no IG/TikTok collector is in flight.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx

from src.core.base_collector import BaseCollector
from src.collectors.lemon8.parse import (
    normalize_username as _parse_normalize_username,
    clean_media_url as _parse_clean_media_url,
    is_valid_media_url as _parse_is_valid_media_url,
    is_small_image as _parse_is_small_image,
    is_profile_photo_url as _parse_is_profile_photo_url,
)
from src.core.human_rate_limiter import OperationType
from src.core.file_naming import sanitize_name
from src.core.rate_limit_events import record_rate_limit_event
from src.core.dynamic_cooldown import get_dynamic_cooldown, record_dynamic_cooldown
from src.core.scrape_pacing import sleep_rate_limit
from src.core.vault import VAULT_ROOT, write_atomic_artifact
from src.core.user_change_tracker import (
    UserChangeTracker,
    LEMON8_TRACKED_FIELDS,
)

# Wave 0 modules — soft-imported so unit-time AST/import remains clean
# even in stripped environments. Real production env has all of these.
try:  # pragma: no cover — optional at import time
    from src.core.account_quota import AccountQuotaTracker, QuotaConfig
except Exception:  # noqa: BLE001
    AccountQuotaTracker = None  # type: ignore[assignment]
    QuotaConfig = None  # type: ignore[assignment]

try:  # pragma: no cover — optional at import time
    from src.core.spider_discover import Edge, EdgeType, SpiderDiscover
except Exception:  # noqa: BLE001
    Edge = None  # type: ignore[assignment]
    EdgeType = None  # type: ignore[assignment]
    SpiderDiscover = None  # type: ignore[assignment]

try:  # pragma: no cover — optional at import time
    from src.core.dedupe_hash import sha256_bytes as _dedupe_sha256_bytes
except Exception:  # noqa: BLE001
    _dedupe_sha256_bytes = None  # type: ignore[assignment]

# Follow-aware access recording (Phase 0, step 1) — mirrors the instagram
# collector's wiring. Records which cookie-account could/couldn't see a target
# into profile_access_{summary,attempts} so SmartAccountSelector can later
# route targets. Defensive import — collection still works without it.
try:  # pragma: no cover — optional at import time
    from src.core.profile_access import ProfileAccessRepository, SmartAccountSelector
except Exception:  # noqa: BLE001
    ProfileAccessRepository = None  # type: ignore[assignment]
    SmartAccountSelector = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

API_BASE = "https://api.lemon8-app.com"
LEMON8_BASE_URL = "https://www.lemon8-app.com"
FEED_URL = f"{LEMON8_BASE_URL}/FEED/FORYOU"
USER_URL_PATTERN = f"{LEMON8_BASE_URL}/@{{}}"
TAG_URL_PATTERN = f"{LEMON8_BASE_URL}/topic/{{}}"

# Optional pylemon8 integration — never required.
try:  # pragma: no cover - optional dep
    from pylemon8 import Lemon8 as _PyLemon8  # type: ignore
    PYLEMON8_AVAILABLE = True
except Exception:  # ImportError or downstream errors
    _PyLemon8 = None
    PYLEMON8_AVAILABLE = False

_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"<>?]+)\?[^\s'\"<>]+")


def _safe_log_text(value) -> str:
    """Keep signed CDN/API query strings out of logs and DLQ rows."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", str(value))


def _enhance_image_url(url: str, target_width: int = 2160) -> str:
    """Remove CDN shrink/thumbnail params to get highest-quality image."""
    url = re.sub(r"[?&]w=\d+", "", url)
    url = re.sub(r"[?&]h=\d+", "", url)
    url = re.sub(r"[?&]q=\d+", "", url)
    url = re.sub(r"/w:\d+/", "/", url)
    url = re.sub(r"/h:\d+/", "/", url)
    url = re.sub(r"/thumb/\d+x\d+/", "/", url)
    url = re.sub(r"~tplv-[a-z0-9]+-[a-z0-9_]+\.[a-z]+", "", url)
    return url


class Lemon8EdgeFetcher:
    """Adapter exposing Lemon8's discovered-creator graph to ``SpiderDiscover``.

    Lemon8 has no public follower/following endpoint — the toolkit instead
    discovers related creators via co-occurrence on profile pages, the FYP
    feed, and topic/tag pages. We expose those discoveries as outbound
    ``FOLLOWING``-style edges so the unified spider can BFS over them.
    ``supported_edge_types`` is fixed to ``(FOLLOWING,)`` since that's the
    only signal we can scrape without an authenticated session.
    """

    if EdgeType is not None:  # pragma: no branch — set at import time
        supported_edge_types: tuple = (EdgeType.FOLLOWING,)
    else:  # SpiderDiscover not importable in this env
        supported_edge_types: tuple = ()

    def __init__(self, collector: "Lemon8Collector") -> None:
        self._c = collector

    async def fetch_edges(self, node_id: str, edge_type) -> AsyncIterator:
        """Stream ``Edge(source, target, edge_type)`` for related creators.

        ``node_id`` is the Lemon8 username (without ``@``). We delegate to
        ``collect_following`` which yields raw username strings discovered
        from the profile/feed/topic HTML.
        """
        if Edge is None or EdgeType is None:
            return
        if edge_type not in self.supported_edge_types:
            raise NotImplementedError(f"unsupported edge type: {edge_type}")
        async for related_username in self._c.collect_following(node_id):
            if not related_username:
                continue
            yield Edge(source=node_id, target=related_username, edge_type=edge_type)


class Lemon8Collector(BaseCollector):
    SOURCE_NAME = "lemon8"
    USE_HUMAN_RATE_LIMITER = True

    # Default daily quota — Lemon8/ByteDance throttles aggressively in
    # parallel with TikTok. 500 profile views / day per cookie set is
    # the empirically-safe ceiling per the toolkit's account_manager.
    DEFAULT_DAILY_QUOTA = int(os.getenv("LEMON8_DAILY_QUOTA", "500"))

    @staticmethod
    def _discover_cookie_file() -> str:
        """Return the best per-username cookie file under credentials/lemon8/.

        Mirrors the tiktok discovery pattern: prefer named lemon8_<username>.txt
        files, skip empty/placeholder stubs (< 1 KB, or the exact legacy names
        ``cookies.txt`` / ``lemon8_cookies.txt`` when a named sibling exists).
        Returns the largest non-empty candidate (a real Netscape jar is
        typically 2-15 KB; an empty stub is 0 B). Never raises."""
        import glob
        try:
            candidates = list(glob.glob("credentials/lemon8/lemon8_*.txt"))
            candidates.extend(glob.glob("credentials/lemon8/cookies.txt"))
            if not candidates:
                return ""
            has_named = any(
                os.path.basename(p) not in ("lemon8_cookies.txt", "cookies.txt")
                for p in candidates
            )
            scored: list[tuple[int, str]] = []
            for p in candidates:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size < 1024:
                    continue
                if has_named and os.path.basename(p) in (
                    "lemon8_cookies.txt", "cookies.txt",
                ):
                    continue
                scored.append((size, p))
            if not scored:
                return ""
            scored.sort(key=lambda t: (-t[0], t[1]))
            chosen = scored[0][1]
            logger.info(
                "lemon8: auto-discovered cookie file %s (%d bytes, %d candidates)",
                chosen, scored[0][0], len(candidates),
            )
            return chosen
        except Exception:
            return ""

    def __init__(self):
        super().__init__()
        self._cookies_file = os.getenv("LEMON8_COOKIES_FILE", "")
        # Auto-discover per-username cookie file when the env var is unset,
        # so dropped-in credentials/lemon8/lemon8_<username>.txt works.
        if not self._cookies_file:
            self._cookies_file = self._discover_cookie_file()
        self._cookies: dict[str, str] = {}
        if self._cookies_file and os.path.isfile(self._cookies_file):
            self._cookies = self._parse_cookies(self._cookies_file)
        self._sem = asyncio.Semaphore(2)

        # account_quota: register a daily cap so the scheduler can refuse
        # new work once we've hit it. Soft-fail on any setup error so the
        # collector still runs in stripped environments.
        self._quota = AccountQuotaTracker() if AccountQuotaTracker is not None else None
        if self._quota is not None and QuotaConfig is not None:
            try:
                self._quota.register(
                    "lemon8",
                    QuotaConfig(daily_limit=self.DEFAULT_DAILY_QUOTA),
                )
            except Exception:  # noqa: BLE001 — registration is local-only
                logger.debug("lemon8: quota registration skipped", exc_info=True)

        self._min_width = int(os.getenv("LEMON8_MIN_WIDTH", "320"))
        self._min_height = int(os.getenv("LEMON8_MIN_HEIGHT", "320"))
        self._min_file_size = int(os.getenv("LEMON8_MIN_FILE_SIZE", "8192"))
        self._hq_width = int(os.getenv("LEMON8_HIGH_QUALITY_WIDTH", "2160"))
        self._enhance_urls = os.getenv("LEMON8_IMAGE_ENHANCEMENT", "true").lower() == "true"
        self._profile_photos = os.getenv("LEMON8_PROFILE_PHOTO_ENABLED", "true").lower() == "true"
        self._feed_enabled = os.getenv("LEMON8_FEED_ENABLED", "true").lower() == "true"
        self._tag_pages = int(os.getenv("LEMON8_TAG_PAGES", "10"))
        self._target_limit_per_cycle = max(
            0,
            int(os.getenv("LEMON8_TARGETS_PER_CYCLE", "6") or "0"),
        )
        self._spider_queue_per_cycle = max(
            0,
            int(os.getenv("LEMON8_SPIDER_QUEUE_PER_CYCLE", "8") or "0"),
        )
        self._feed_pages_per_cycle = max(
            1,
            int(os.getenv("LEMON8_FEED_PAGES_PER_CYCLE", "2") or "1"),
        )
        self._feed_media_per_cycle = max(
            0,
            int(os.getenv("LEMON8_FEED_MEDIA_PER_CYCLE", "40") or "0"),
        )
        self._fyp_detail_per_cycle = max(
            0,
            int(os.getenv("LEMON8_FYP_DETAIL_PER_CYCLE", "2") or "0"),
        )
        try:
            self._rate_limit_cooldown_seconds = max(
                0,
                int(os.getenv("LEMON8_RATE_LIMIT_COOLDOWN_SECONDS", "900")),
            )
        except (TypeError, ValueError):
            self._rate_limit_cooldown_seconds = 900
        try:
            self._rate_limit_cooldown_max_seconds = max(
                self._rate_limit_cooldown_seconds or 1,
                int(os.getenv("LEMON8_RATE_LIMIT_MAX_COOLDOWN_SECONDS", "21600")),
            )
        except (TypeError, ValueError):
            self._rate_limit_cooldown_max_seconds = max(
                self._rate_limit_cooldown_seconds or 1,
                21600,
            )
        try:
            self._optional_rate_limit_sleep_cap_seconds = max(
                0,
                int(os.getenv("LEMON8_OPTIONAL_RATE_LIMIT_SLEEP_CAP_SECONDS", "30")),
            )
        except (TypeError, ValueError):
            self._optional_rate_limit_sleep_cap_seconds = 30
        try:
            self._source_rate_limit_sleep_cap_seconds = max(
                0,
                int(os.getenv(
                    "LEMON8_SOURCE_RATE_LIMIT_SLEEP_CAP_SECONDS",
                    str(self._rate_limit_cooldown_seconds or 300),
                )),
            )
        except (TypeError, ValueError):
            self._source_rate_limit_sleep_cap_seconds = self._rate_limit_cooldown_seconds or 300
        self._discovered_users: set[str] = set()
        self._discovered_tags: set[str] = set()
        # FAMOUS-FILTER (Bryan): skip Lemon8 accounts at/above this follower count.
        # 0 disables. Best-effort: follower_count is parsed from profile HTML.
        self._famous_follower_cap = int(os.getenv("LEMON8_FAMOUS_FOLLOWER_CAP", "0") or "0")
        # Follow-aware access tracker (lazy — needs self.pool, created on first
        # use in _record_profile_access). Persists profile-fetch outcomes into
        # profile_access_{summary,attempts}. Toggle via LEMON8_ACCESS_TRACKING.
        self._access_repo = None
        self._access_tracking = os.getenv("LEMON8_ACCESS_TRACKING", "1") == "1"

    @staticmethod
    def _extract_follower_count(html: str) -> int:
        """Best-effort parse of follower/fans count from a Lemon8 profile page.

        Lemon8 embeds profile JSON in the HTML; the follower field appears as
        "fans_count":N or "follower_count":N. Returns 0 if not found (which means
        the famous cap can't apply -- we fail open and collect rather than guess).
        """
        import re
        for marker in ('"fans_count":', '"follower_count":', '"followers_count":'):
            idx = html.find(marker)
            if idx != -1:
                m = re.match(r"\s*(\d+)", html[idx + len(marker):])
                if m:
                    return int(m.group(1))
        return 0

    @staticmethod
    def _parse_cookies(path: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or not line: continue
                    parts = line.split("\t")
                    if len(parts) >= 7: cookies[parts[5]] = parts[6]
        except Exception as e: logger.debug("_parse_cookies failed for %s: %s", path, e)
        return cookies

    @property
    def account_media_dir(self) -> Path:
        acc_name = Path(self._cookies_file).stem if self._cookies_file else "default"
        path = self.media_dir / f"account_{sanitize_name(acc_name)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agents.get_for_domain("lemon8-app.com"), "Accept": "application/json", "Referer": "https://www.lemon8-app.com/"}

    async def collect(self, targets: list[str]):
        async with httpx.AsyncClient(timeout=30, cookies=self._cookies, headers=self._headers(), follow_redirects=True) as client:
            cycle_targets = targets
            if self._target_limit_per_cycle:
                cycle_targets = targets[: self._target_limit_per_cycle]
                if len(targets) > len(cycle_targets):
                    logger.info(
                        "lemon8: processing %d/%d configured targets this cycle",
                        len(cycle_targets),
                        len(targets),
                    )
            for username in cycle_targets:
                if self._stop.is_set(): break
                if username.startswith("#"):
                    try: await self._collect_tag(client, username.lstrip("#"))
                    except Exception as e:
                        safe_error = _safe_log_text(e)
                        await self._record_http_status_event(
                            e,
                            scope="tag_fetch",
                            subject=username,
                            url=TAG_URL_PATTERN.format(username.lstrip("#")),
                        )
                        logger.error("Tag collection failed for %s: %s", username, safe_error)
                    continue
                logger.info("Collecting lemon8/%s", username)
                try:
                    await self._collect_user(client, username)
                    await self.checkpoint.save_progress(username)
                except Exception as e:
                    status_code = self._http_status_from_error(e)
                    safe_error = _safe_log_text(e)
                    if status_code == 404:
                        logger.info("lemon8: skip unavailable profile %s: HTTP 404", username)
                        await self._mark_profile_unavailable(username, "http_404")
                        await self.checkpoint.save_progress(username)
                        continue
                    await self._record_http_status_event(
                        e,
                        scope="profile_fetch",
                        subject=username,
                        url=USER_URL_PATTERN.format(username.lstrip("@")),
                    )
                    logger.error("Failed lemon8/%s: %s", username, safe_error)
                    await self.send_to_dlq(username, username, safe_error)
            if self._feed_enabled:
                try:
                    await self._collect_feed(client)
                except Exception as e:
                    safe_error = _safe_log_text(e)
                    await self._record_http_status_event(
                        e,
                        scope="feed_fetch",
                        subject="feed",
                        url=FEED_URL,
                    )
                    logger.error("Feed collection failed: %s", safe_error)

        if os.getenv("LEMON8_SPIDER_ENABLED", "true").lower() == "true":
            await self._process_spider_queue(max_items=self._spider_queue_per_cycle)

    async def _mark_profile_unavailable(self, username: str, reason: str) -> None:
        """Stop retrying Lemon8 profile targets that the platform says do not exist."""
        if self.pool is None or not username:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE collection_targets
                    SET status = 'unavailable',
                        error_message = $3,
                        metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                            'unavailable_reason', $3,
                            'unavailable_at', NOW()
                        )
                    WHERE source = $1
                      AND target_id = $2
                      AND status IN ('pending', 'error', 'active')
                    """,
                    self.SOURCE_NAME,
                    username,
                    reason,
                )
        except Exception:
            logger.debug("lemon8: failed to mark %s unavailable", username, exc_info=True)

    async def _process_spider_queue(self, max_items: int | None = None):
        async with httpx.AsyncClient(timeout=30, cookies=self._cookies, headers=self._headers(), follow_redirects=True) as client:
            processed = 0
            while not self._stop.is_set():
                if max_items is not None and max_items > 0 and processed >= max_items:
                    logger.info("lemon8 spider queue: processed %d queued target(s) this cycle", processed)
                    break
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow("""
                        UPDATE lemon8_spider_queue
                        SET status = 'processing'
                        WHERE id = (
                            SELECT id FROM lemon8_spider_queue
                            WHERE status = 'pending'
                            ORDER BY priority ASC, collected_at ASC
                            LIMIT 1
                        )
                        RETURNING platform_user_id
                    """)
                if not row: break
                try:
                    await self._collect_user(client, row['platform_user_id'])
                    processed += 1
                    async with self.pool.acquire() as conn:
                        await conn.execute("UPDATE lemon8_spider_queue SET status = 'completed' WHERE platform_user_id = $1", row['platform_user_id'])
                except Exception as e:
                    await self._record_http_status_event(
                        e,
                        scope="spider_profile_fetch",
                        subject=row['platform_user_id'],
                        url=USER_URL_PATTERN.format(str(row['platform_user_id']).lstrip("@")),
                    )
                    async with self.pool.acquire() as conn:
                        await conn.execute("UPDATE lemon8_spider_queue SET status = 'failed' WHERE platform_user_id = $1", row['platform_user_id'])

    async def _enqueue_spider_user(self, username: str, source: str = "feed"):
        """Add a discovered user to the spider queue (best-effort)."""
        if not username:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO lemon8_spider_queue (platform_user_id, source, priority, status, collected_at)
                    VALUES ($1, $2, 5, 'pending', $3)
                    ON CONFLICT (platform_user_id) DO NOTHING
                """, username, source, datetime.now(timezone.utc))
        except Exception as e:
            logger.debug("spider enqueue skipped for %s: %s", username, e)

    async def _stable_id_for_handle(self, username: str) -> Optional[str]:
        """Return a previously-captured STABLE lemon8 id (userNNNN / long numeric)
        for this handle, if one exists, so a later fetch that fails to parse the
        id doesn't re-key the profile to the vanity handle (SYNC #39)."""
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT platform_user_id FROM lemon8_profiles "
                    "WHERE username = $1 AND platform_user_id ~ '^(user[0-9]+|[0-9]{6,})$' "
                    "ORDER BY updated_at DESC NULLS LAST LIMIT 1",
                    username,
                )
                return row["platform_user_id"] if row else None
        except Exception as exc:
            logger.debug("lemon8 stable-id lookup failed for %s: %s", username, exc)
            return None

    async def _ensure_post_profile(
        self,
        conn,
        user_id: Optional[str],
        username: Optional[str] = None,
    ) -> Optional[str]:
        """Return or create a minimal Lemon8 profile for post attribution.

        Feed-card/detail paths can expose only the handle, not the stable
        userNNNN id. Prefer existing stable-keyed profiles by username, then
        create a handle-keyed stub so posts do not remain permanently orphaned.
        """
        clean_user_id = str(user_id).strip() if user_id else None
        clean_username = self._normalize_username(username or clean_user_id)

        if clean_user_id:
            row = await conn.fetchrow(
                "SELECT id FROM lemon8_profiles WHERE platform_user_id = $1",
                clean_user_id,
            )
            if row:
                return str(row["id"])

        if clean_username:
            row = await conn.fetchrow(
                """
                SELECT id
                FROM lemon8_profiles
                WHERE LOWER(username) = LOWER($1)
                   OR LOWER(platform_user_id) = LOWER($1)
                ORDER BY (platform_user_id ~ '^(user[0-9]+|[0-9]{6,})$') DESC,
                         updated_at DESC NULLS LAST
                LIMIT 1
                """,
                clean_username,
            )
            if row:
                return str(row["id"])

        platform_user_id = clean_user_id or clean_username
        if not platform_user_id:
            return None

        row = await conn.fetchrow(
            """
            INSERT INTO lemon8_profiles (platform_user_id, username, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (platform_user_id) DO UPDATE SET
                username = COALESCE(lemon8_profiles.username, EXCLUDED.username),
                updated_at = NOW()
            RETURNING id
            """,
            platform_user_id,
            clean_username,
        )
        return str(row["id"]) if row else None

    async def _upsert_profile(self, user_id: str, username: str, data: dict):
        # ── User-intelligence diff: snapshot the row BEFORE upserting so the
        # change tracker can compare old → new and emit one row per changed
        # field into lemon8_user_changes. Wrapped in try/except so any failure
        # (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, nickname, bio, avatar_url, "
                    "followers_count, following_count, like_count "
                    "FROM lemon8_profiles WHERE platform_user_id = $1",
                    user_id,
                )
        except Exception as exc:
            logger.debug("user_change_tracker[lemon8]: prev-row fetch failed: %s", exc)

        async with self.pool.acquire() as conn:
            # #39 retro-migration: if we now have a STABLE id (user_id != handle)
            # but an old vanity-keyed row exists for this handle, rename its key in
            # place (posts reference the profile UUID, not platform_user_id, so
            # they follow automatically) rather than inserting a duplicate. Guarded
            # so it no-ops when a stable-keyed row already exists.
            if user_id != username:
                await conn.execute(
                    "UPDATE lemon8_profiles SET platform_user_id = $1 "
                    "WHERE platform_user_id = $2 AND username = $2 "
                    "AND NOT EXISTS (SELECT 1 FROM lemon8_profiles p2 WHERE p2.platform_user_id = $1)",
                    user_id, username,
                )
            await conn.execute("""
                INSERT INTO lemon8_profiles (
                    platform_user_id, username, nickname, avatar_url, bio, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    username = EXCLUDED.username, nickname = EXCLUDED.nickname, updated_at = EXCLUDED.updated_at
            """, user_id, username, data.get("nickname"), data.get("avatar_url"), data.get("signature"),
                 datetime.now(timezone.utc))

        try:
            tracker = UserChangeTracker(self.pool)
            # Normalize prev_row (DB column names) into LEMON8_TRACKED_FIELDS
            # key-space (which uses the more-canonical follower_count /
            # biography / profile_pic_url naming used across platforms).
            current_normalized: dict | None = None
            if prev_row is not None:
                pr = dict(prev_row)
                current_normalized = {
                    "username":         pr.get("username"),
                    "nickname":         pr.get("nickname"),
                    "biography":        pr.get("bio"),
                    "profile_pic_url":  pr.get("avatar_url"),
                    "follower_count":   pr.get("followers_count"),
                    "following_count":  pr.get("following_count"),
                    "like_count":       pr.get("like_count"),
                }
            new_snapshot = {
                "username":         username,
                "nickname":         data.get("nickname"),
                "biography":        data.get("signature") or data.get("bio")
                                       or data.get("biography"),
                "profile_pic_url":  data.get("avatar_url") or data.get("profile_pic_url"),
                "follower_count":   data.get("follower_count") or data.get("followers_count"),
                "following_count":  data.get("following_count"),
                "like_count":       data.get("like_count"),
                "post_count":       data.get("post_count") or data.get("posts_count"),
                "region":           data.get("region"),
            }
            await tracker.detect_and_log(
                table="lemon8_user_changes",
                pk_col="user_id",
                pk_val=str(user_id),
                current_row=current_normalized,
                new_row=new_snapshot,
                fields=LEMON8_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker[lemon8]: detect_and_log failed: %s", exc)

    @staticmethod
    def _resolve_post_id(post_data: dict) -> Optional[str]:
        """Best-effort extraction of a stable platform_post_id from heterogeneous
        FYP / profile / topic card shapes. Falls back to a deterministic hash
        of the post's primary URL(s) so FYP cards (which have no exposed id)
        still upsert deterministically.
        """
        for key in (
            "id", "itemId", "item_id", "note_id", "noteId",
            "card_id", "cardId", "post_id", "postId",
            "platform_post_id", "aweme_id",
        ):
            val = post_data.get(key)
            if val:
                s = str(val).strip()
                if s and s.lower() not in ("none", "null", "0"):
                    return s

        # Synthesize from URLs / media so the same card maps to the same row.
        seed_parts: list[str] = []
        for k in ("share_url", "shareUrl", "url", "permalink"):
            v = post_data.get(k)
            if isinstance(v, str) and v:
                seed_parts.append(v)
        media = post_data.get("media") or []
        if isinstance(media, list):
            for m in media[:5]:
                if isinstance(m, dict):
                    u = m.get("url") or m.get("src")
                    if isinstance(u, str) and u:
                        seed_parts.append(u)
        if not seed_parts:
            # Last resort: stringified payload
            try:
                seed_parts.append(json.dumps(post_data, sort_keys=True, default=str)[:512])
            except Exception:
                return None
        seed = "|".join(seed_parts)
        return "fyp_" + hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:32]

    async def _link_lemon8_media(self, note_id: str, username: str, url: str, is_video: bool) -> None:
        """Group a feed-card media URL under its post so lemon8_posts.image_urls /
        video_url is populated (was 0% — media existed as loose files, unlinked to
        posts). Idempotent: creates a minimal post row if absent, else appends the
        URL to image_urls (deduped). A later detail fetch fills title/stats."""
        if not note_id or not url:
            return
        post_url = f"https://www.lemon8-app.com/@{username}/{note_id}"
        async with self.pool.acquire() as conn:
            profile_uuid = await self._ensure_post_profile(conn, username, username)
            if is_video:
                await conn.execute(
                    """
                    INSERT INTO lemon8_posts (platform_post_id, profile_id, username, video_url, post_url)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (platform_post_id) DO UPDATE SET
                        profile_id = COALESCE(lemon8_posts.profile_id, EXCLUDED.profile_id),
                        video_url = COALESCE(lemon8_posts.video_url, EXCLUDED.video_url),
                        username  = COALESCE(lemon8_posts.username, EXCLUDED.username),
                        post_url  = COALESCE(lemon8_posts.post_url, EXCLUDED.post_url)
                    """,
                    note_id, profile_uuid, username, url, post_url,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO lemon8_posts (platform_post_id, profile_id, username, image_urls, post_url)
                    VALUES ($1, $2, $3, ARRAY[$4], $5)
                    ON CONFLICT (platform_post_id) DO UPDATE SET
                        profile_id = COALESCE(lemon8_posts.profile_id, EXCLUDED.profile_id),
                        image_urls = (
                            SELECT array_agg(DISTINCT e)
                            FROM unnest(COALESCE(lemon8_posts.image_urls, '{}') || EXCLUDED.image_urls) e
                        ),
                        username = COALESCE(lemon8_posts.username, EXCLUDED.username),
                        post_url = COALESCE(lemon8_posts.post_url, EXCLUDED.post_url)
                    """,
                    note_id, profile_uuid, username, url, post_url,
                )

    async def _upsert_post(self, user_id: str, post_data: dict):
        platform_post_id = self._resolve_post_id(post_data)
        if not platform_post_id:
            logger.debug("lemon8 _upsert_post: skipped — no resolvable id; keys=%s",
                         list(post_data.keys())[:10])
            return

        # Pull image / video URLs from attached media descriptors when present.
        image_urls: list[str] = []
        video_url: Optional[str] = None
        for m in (post_data.get("media") or []):
            if not isinstance(m, dict):
                continue
            u = m.get("url")
            if not u:
                continue
            mtype = m.get("media_type") or m.get("content_type")
            if mtype == "video" and not video_url:
                video_url = u
            else:
                image_urls.append(u)

        async with self.pool.acquire() as conn:
            profile_uuid = await self._ensure_post_profile(conn, user_id, post_data.get("username") or user_id)
            # Build post_url from platform_post_id if it looks like a real numeric ID
            post_url = None
            if platform_post_id and platform_post_id.isdigit():
                post_url = f"https://www.lemon8-app.com/@{user_id}/{platform_post_id}"
            try:
                await conn.execute("""
                    INSERT INTO lemon8_posts (
                        platform_post_id, profile_id, username, title, description,
                        image_urls, video_url,
                        like_count, comment_count, post_url, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (platform_post_id) DO UPDATE SET
                        profile_id    = COALESCE(lemon8_posts.profile_id, EXCLUDED.profile_id),
                        like_count    = EXCLUDED.like_count,
                        comment_count = EXCLUDED.comment_count,
                        image_urls    = COALESCE(EXCLUDED.image_urls, lemon8_posts.image_urls),
                        video_url     = COALESCE(EXCLUDED.video_url,  lemon8_posts.video_url),
                        title         = COALESCE(NULLIF(EXCLUDED.title, ''),       lemon8_posts.title),
                        description   = COALESCE(NULLIF(EXCLUDED.description, ''), lemon8_posts.description),
                        username      = COALESCE(EXCLUDED.username, lemon8_posts.username),
                        post_url      = COALESCE(EXCLUDED.post_url,  lemon8_posts.post_url),
                        metadata      = EXCLUDED.metadata
                """,
                    platform_post_id, profile_uuid, user_id,
                    post_data.get("title"), post_data.get("description"),
                    image_urls or None, video_url,
                    int(post_data.get("stats", {}).get("likeCount", 0) or 0),
                    int(post_data.get("stats", {}).get("commentCount", 0) or 0),
                    post_url,
                    json.dumps(post_data, default=str))
                return True
            except Exception as e:
                logger.warning("lemon8 _upsert_post failed for %s: %s", platform_post_id, e)
                return False

    def _access_account_label(self) -> str:
        """Stable account identifier for profile-access recording.

        Lemon8 has a single-cookie-file account model (no rotation pool), so we
        use the cookie file's stem (e.g. ``lemon8_<username>``) — the same label
        ``account_media_dir`` uses — falling back to ``lemon8_default`` when no
        cookie file is configured. record_attempt requires a non-empty account.
        """
        try:
            if self._cookies_file:
                stem = Path(self._cookies_file).stem
                if stem:
                    return stem
        except Exception:  # noqa: BLE001 — label is best-effort
            pass
        return "lemon8_default"

    @staticmethod
    def _rate_limit_scope_alias(scope: str, metadata: dict[str, Any] | None = None) -> str:
        if scope == "note_detail_fetch":
            return "detail"
        if scope == "avatar_profile_fetch":
            return "avatar"
        if scope == "media_download" and (metadata or {}).get("content_type") == "profile_photo":
            return "avatar_media"
        if scope in {"profile_fetch", "spider_profile_fetch", "collect_profile_fetch"}:
            return "profile"
        return scope[:32]

    @staticmethod
    def _is_optional_rate_limit_scope(scope: str, metadata: dict[str, Any] | None = None) -> bool:
        alias = Lemon8Collector._rate_limit_scope_alias(scope, metadata)
        return alias in {"detail", "avatar", "avatar_media"}

    async def _cooldown_active_for_scope(
        self,
        scope: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        state = await get_dynamic_cooldown(
            self.pool,
            source="lemon8",
            scope=self._rate_limit_scope_alias(scope, metadata),
            account=self._access_account_label(),
        )
        if not state or not state.active:
            return False
        logger.info(
            "lemon8: skipping %s while dynamic cooldown is active for %ds (streak=%d)",
            scope,
            state.seconds_remaining,
            state.streak,
        )
        return True

    @staticmethod
    def _http_status_from_error(error: Exception) -> int | None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            return None
        try:
            return int(status)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _rate_limit_url_metadata(url: str | None) -> dict[str, str]:
        if not url:
            return {}
        try:
            parsed = urlparse(str(url))
            metadata: dict[str, str] = {}
            if parsed.netloc:
                metadata["url_host"] = parsed.netloc
            if parsed.path:
                metadata["url_path"] = parsed.path[:200]
            return metadata
        except Exception:
            return {}

    async def _record_http_status_event(
        self,
        error: Exception,
        *,
        scope: str,
        subject: str | None = None,
        url: str | None = None,
        record_access_errors: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Persist Lemon8 throttle/auth failures for dashboards and Telegram.

        404s are common for removed Lemon8 profiles, and CDN 403s are usually
        expiring media signatures. Keep those out of source health unless the
        caller explicitly identifies the failure as a source-page access issue.
        """
        status_code = self._http_status_from_error(error)
        if status_code is None:
            return False
        dynamic_state = None
        if status_code == 429:
            if self._rate_limit_cooldown_seconds:
                dynamic_state = await record_dynamic_cooldown(
                    self.pool,
                    source="lemon8",
                    scope=self._rate_limit_scope_alias(scope, metadata),
                    account=self._access_account_label(),
                    base_seconds=self._rate_limit_cooldown_seconds,
                    max_seconds=self._rate_limit_cooldown_max_seconds,
                    write_source_cursor=not self._is_optional_rate_limit_scope(scope, metadata),
                )
                cooldown_seconds = dynamic_state.seconds_remaining
            else:
                cooldown_seconds = None
        elif record_access_errors and status_code in (401, 403):
            cooldown_seconds = None
        else:
            return False

        event_metadata: dict[str, Any] = {
            "subject": subject,
            "collector": "lemon8",
        }
        event_metadata.update(self._rate_limit_url_metadata(url))
        if metadata:
            event_metadata.update(metadata)
        if dynamic_state is not None:
            event_metadata.update({
                "dynamic_cooldown_service": dynamic_state.service,
                "dynamic_cooldown_streak": dynamic_state.streak,
                "dynamic_cooldown_scope": dynamic_state.scope,
            })

        await record_rate_limit_event(
            self.pool,
            source="lemon8",
            account=self._access_account_label(),
            scope=scope,
            status_code=status_code,
            cooldown_seconds=cooldown_seconds,
            reason=f"HTTP {status_code} while collecting Lemon8 {scope}",
            metadata=event_metadata,
        )
        if status_code == 429 and cooldown_seconds:
            trigger = getattr(self.rate_limiter, "trigger_emergency_cooldown", None)
            if callable(trigger):
                previous = getattr(self.rate_limiter, "emergency_cooldown", None)
                try:
                    if previous is not None:
                        self.rate_limiter.emergency_cooldown = float(cooldown_seconds)
                    trigger("lemon8-app.com")
                except Exception:
                    logger.debug("lemon8: local cooldown trigger failed", exc_info=True)
                finally:
                    if previous is not None:
                        self.rate_limiter.emergency_cooldown = previous
            if self._is_optional_rate_limit_scope(scope, metadata):
                sleep_seconds = min(cooldown_seconds, max(0, self._optional_rate_limit_sleep_cap_seconds))
            else:
                sleep_seconds = min(cooldown_seconds, max(0, self._source_rate_limit_sleep_cap_seconds))
            if sleep_seconds:
                await sleep_rate_limit(sleep_seconds)
        return True

    async def _record_profile_access(self, username, can_access, is_private=None,
                                     is_followed=False, error=None):
        """Record whether this cookie-account could see ``username`` into
        profile_access_{summary,attempts} (follow-aware selector, Phase 0).

        Best-effort and fully isolated: any failure here is swallowed so profile
        collection is never affected. No new network calls — it only persists
        the outcome of a fetch we already made. Mirrors the instagram
        collector's _record_profile_access.
        """
        if not self._access_tracking or ProfileAccessRepository is None:
            return
        if self.pool is None:
            return
        try:
            if self._access_repo is None:
                self._access_repo = ProfileAccessRepository(self.pool)
            await self._access_repo.record_attempt(
                source="lemon8",
                target_id=str(username),
                account=self._access_account_label(),
                can_access=can_access,
                is_public=(None if is_private is None else (not is_private)),
                is_followed=is_followed,
                error=error,
            )
        except Exception as e:  # noqa: BLE001 — never let tracking break collection
            logger.debug("lemon8: access-tracking record failed for %s: %s", username, e)

    # ──────────────────────────────────────────────────────────────────────
    # Collection entrypoints
    # ──────────────────────────────────────────────────────────────────────
    async def _collect_user(self, client: httpx.AsyncClient, username: str):
        await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
        url = USER_URL_PATTERN.format(username.lstrip("@"))
        resp = await client.get(url)
        # Follow-aware access recording (Phase 0):
        #   401/403 = clear access-denial → record can_access=False.
        #   404     = user does not exist (Lemon8 currently 404s several
        #             handles) — NOT an access denial; record nothing.
        # raise_for_status() below still raises for all of these, so the
        # existing error flow (caller catch → DLQ) is unchanged.
        if resp.status_code in (401, 403):
            await self._record_profile_access(
                username, False, error=f"HTTP {resp.status_code}",
            )
        resp.raise_for_status()
        html = resp.text

        # Stable lemon8 id is "userNNNN" and is the correct identity key — the
        # vanity handle re-keys on rename and creates duplicate profiles for the
        # same person (SYNC #39). Prefer a stable id from any of the known JSON
        # markers, and only accept a value that looks stable (userNNNN / long
        # numeric); fall back to the handle just as before if none is found (no
        # regression). Prefer a previously-captured stable id for this handle over
        # re-keying it to the vanity fallback.
        user_id = username
        for marker in ('"user_id":"', '"userId":"', '"uid":"', '"sec_uid":"'):
            idx = html.find(marker)
            if idx == -1:
                continue
            end = html.find('"', idx + len(marker))
            candidate = html[idx + len(marker):end] if end != -1 else ""
            if candidate and re.fullmatch(r"user\d+|\d{6,}", candidate):
                user_id = candidate
                break
        if user_id == username:
            prior = await self._stable_id_for_handle(username)
            if prior:
                user_id = prior

        avatar_url = self._extract_avatar(html) if self._profile_photos else None
        await self._upsert_profile(user_id, username, {"nickname": username, "avatar_url": avatar_url})

        # Success: this account CAN see the target — record it for the
        # follow-aware selector. Lemon8 has no is_private notion in the
        # profile HTML we parse, so is_private stays None (is_public=None).
        await self._record_profile_access(username, True)

        # FAMOUS-FILTER: skip post/media collection for accounts at/above the cap.
        # Profile row is still upserted (we know the account); only content is skipped.
        followers = self._extract_follower_count(html)
        if self._famous_follower_cap and followers >= self._famous_follower_cap:
            logger.info("lemon8: skipping famous user %s (%d followers >= cap %d)",
                        username, followers, self._famous_follower_cap)
            return

        if self._profile_photos and avatar_url:
            await self.download_media({
                "entity_id": user_id, "entity_name": username,
                "content_type": "profile_photo",
                "content_id": f"profile_{user_id}",
                "url": avatar_url, "extension": "jpg",
            })

        posts = self._extract_posts(html, user_id, username)
        for post in posts:
            if self._stop.is_set(): break
            await self._upsert_post(user_id, post)
            for media_item in post.get("media", []):
                if not self.is_known(media_item["content_id"]):
                    await self.download_media(media_item)

    async def _fetch_note_detail(self, client: httpx.AsyncClient,
                                username: str, note_id: str) -> dict | None:
        """Fetch individual note page and extract post detail (title, stats, media).

        Tries the HTML note page at /@username/note_id and extracts structured
        data from embedded JSON. Returns a dict suitable for _upsert_post(), or
        None if extraction fails.
        """
        if await self._cooldown_active_for_scope("note_detail_fetch"):
            return None
        url = f"https://www.lemon8-app.com/@{username}/{note_id}"
        await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
        try:
            resp = await client.get(url, headers=self._headers())
            if resp.status_code != 200:
                if resp.status_code in (401, 403, 429):
                    request = getattr(resp, "request", None) or httpx.Request("GET", url)
                    err = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=request,
                        response=resp,
                    )
                    await self._record_http_status_event(
                        err,
                        scope="note_detail_fetch",
                        subject=f"{username}/{note_id}",
                        url=url,
                    )
                return None
            html = resp.text
        except Exception as e:
            await self._record_http_status_event(
                e,
                scope="note_detail_fetch",
                subject=f"{username}/{note_id}",
                url=url,
            )
            return None

        # Try to extract post data from embedded JSON
        detail: dict = {"id": note_id}
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script", {"type": "application/json"}):
                try:
                    data = json.loads(script.string or "{}")
                except Exception:
                    continue
                post = self._find_post_in_json(data, note_id)
                if post:
                    detail.update(post)
                    break
        except Exception:
            pass

        # Fallback: extract from og: meta tags
        title_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
        desc_m = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
        if title_m and not detail.get("title"):
            from html import unescape
            detail["title"] = unescape(title_m.group(1))
        if desc_m and not detail.get("description"):
            from html import unescape
            detail["description"] = unescape(desc_m.group(1))

        if not detail.get("title") and not detail.get("description"):
            return None
        return detail

    @staticmethod
    def _find_post_in_json(obj, note_id: str, depth: int = 8) -> dict | None:
        """Recursively search JSON for a post object matching note_id."""
        if depth <= 0 or not isinstance(obj, (dict, list)):
            return None
        if isinstance(obj, dict):
            obj_id = str(obj.get("id") or obj.get("itemId") or obj.get("note_id") or "")
            if obj_id == note_id and (obj.get("title") or obj.get("desc")):
                return {
                    "id": note_id,
                    "title": obj.get("title") or obj.get("desc"),
                    "description": obj.get("desc") or obj.get("title"),
                    "stats": {
                        "likeCount": obj.get("digg_count") or obj.get("likeCount") or 0,
                        "commentCount": obj.get("comment_count") or obj.get("commentCount") or 0,
                    },
                }
            for v in obj.values():
                r = Lemon8Collector._find_post_in_json(v, note_id, depth - 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = Lemon8Collector._find_post_in_json(item, note_id, depth - 1)
                if r:
                    return r
        return None

    async def _collect_feed(self, client: httpx.AsyncClient):
        """For-You-Page (FYP) feed scraping. Tries pylemon8 if available, falls back to web."""
        pages = max(1, self._feed_pages_per_cycle)
        result: dict[str, Any] = {}

        # 1) Optional pylemon8 path
        if PYLEMON8_AVAILABLE:
            try:
                result = await self._scrape_feed_with_api("foryou", pages)
            except Exception as e:
                logger.info("pylemon8 feed failed, falling back to web: %s", _safe_log_text(e))
                result = {}

        # 2) Web fallback
        if not result or not result.get("media_items"):
            try:
                result = await self._scrape_feed_with_web(client, pages)
            except Exception as e:
                logger.error("Web feed scrape failed: %s", _safe_log_text(e))
                return

        media_items = result.get("media_items", []) or []
        users = result.get("discovered_users", []) or []
        tags = result.get("discovered_tags", []) or []

        logger.info("Feed scraped: %d media, %d users, %d tags", len(media_items), len(users), len(tags))

        for u in users:
            self._discovered_users.add(u)
            await self._enqueue_spider_user(u, source="feed")

        for t in tags:
            self._discovered_tags.add(t)

        # Download media discovered from FYP. Skip profile photos unless enabled.
        # Also upsert a structured row into lemon8_posts so FYP-derived data is
        # queryable (synthesised id falls back to a hash when absent).
        max_media = self._feed_media_per_cycle or max(50, pages * 30)
        detail_enabled = os.getenv("LEMON8_FYP_DETAIL_FETCH", "false").lower() == "true"
        detail_seen: set[str] = set()
        detail_fetches = 0
        for item in media_items[: max_media]:
            if self._stop.is_set(): break
            url = item.get("url")
            if not url:
                continue
            uname = item.get("username") or "feed"
            entity_id = uname
            is_video = item.get("media_type") == "video"
            ext = "mp4" if is_video else "jpg"
            ctype = "profile_photo" if item.get("is_profile_photo") else (
                "video" if is_video else "image"
            )

            # Resolve the note (post) id from the card so media can be GROUPED BY POST:
            # embed it in content_id AND link it into lemon8_posts.image_urls/video_url
            # (was 0% — the media existed only as loose files). Falls back to the old
            # feed_<hash> key when the card exposes no id (algorithmic FYP thumbnails).
            note_id = ""
            if not item.get("is_profile_photo"):
                note_id = str(item.get("note_id") or item.get("itemId") or item.get("item_id") or "")
                if not note_id and item.get("href"):
                    nm = re.search(r'/(\d{10,20})(?:\?|$)', item["href"])
                    if nm:
                        note_id = nm.group(1)
            uhash = hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()
            content_id = f"{note_id}_{uhash}" if note_id else "feed_" + uhash
            enhanced_url = _enhance_image_url(url) if self._enhance_urls else url

            if note_id:
                try:
                    await self._link_lemon8_media(note_id, uname, enhanced_url, is_video)
                except Exception as e:
                    logger.debug("lemon8 media-link failed for note %s: %s", note_id, e)

                # Optional deep detail fetch (title/desc/stats). Bound and dedupe
                # this lane so one Lemon8 FYP pass cannot monopolize ByteDance IO.
                if (
                    detail_enabled
                    and note_id not in detail_seen
                    and (
                        self._fyp_detail_per_cycle <= 0
                        or detail_fetches < self._fyp_detail_per_cycle
                    )
                ):
                    detail_seen.add(note_id)
                    detail_fetches += 1
                    try:
                        detail = await self._fetch_note_detail(client, uname, note_id)
                        if detail:
                            ok = await self._upsert_post(entity_id, detail)
                            if ok:
                                logger.info("lemon8 FYP detail: upserted post %s for %s", note_id, uname)
                    except Exception as e:
                        logger.debug("lemon8 FYP detail fetch %s failed: %s", note_id, _safe_log_text(e))

            self._progress_count += 1
            if self.is_known(content_id):
                continue
            await self.download_media({
                "entity_id": entity_id,
                "entity_name": uname,
                "content_type": ctype,
                "content_id": content_id,
                "url": enhanced_url,
                "extension": ext,
                "raw": item,
            })

    async def _collect_tag(self, client: httpx.AsyncClient, tag_id: str):
        """Scrape a topic/tag landing page using the same web-extraction pipeline as feed."""
        tag_id = tag_id.strip().lstrip("#")
        if not tag_id:
            return
        url = TAG_URL_PATTERN.format(tag_id)
        await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            html_content = resp.text
        except Exception as e:
            safe_error = _safe_log_text(e)
            await self._record_http_status_event(
                e,
                scope="tag_fetch",
                subject=tag_id,
                url=url,
            )
            status_code = self._http_status_from_error(e)
            if status_code == 404:
                logger.info("lemon8: skip unavailable tag %s: HTTP 404", tag_id)
            else:
                logger.error("Tag fetch failed %s: %s", tag_id, safe_error)
            return

        media_items = self._extract_media_items_from_feed_cards(html_content, include_profile_images=self._profile_photos)
        if not media_items:
            media_items = self._extract_media_items_from_html(html_content, include_profile_images=self._profile_photos)
        users = self._extract_user_handles(html_content)

        logger.info("Tag %s: %d media, %d users", tag_id, len(media_items), len(users))
        for u in users:
            await self._enqueue_spider_user(u, source=f"tag:{tag_id}")

        for item in media_items[: max(50, self._tag_pages * 30)]:
            if self._stop.is_set(): break
            url = item.get("url")
            if not url:
                continue
            uname = item.get("username") or f"tag_{tag_id}"
            content_id = "tag_" + hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()
            if self.is_known(content_id):
                continue
            ext = "mp4" if item.get("media_type") == "video" else "jpg"
            ctype = "profile_photo" if item.get("is_profile_photo") else (
                "video" if item.get("media_type") == "video" else "image"
            )
            await self.download_media({
                "entity_id": uname,
                "entity_name": uname,
                "content_type": ctype,
                "content_id": content_id,
                "url": _enhance_image_url(url) if self._enhance_urls else url,
                "extension": ext,
                "raw": item,
            })

    # ──────────────────────────────────────────────────────────────────────
    # FYP / feed scraping (ported from old toolkit)
    # ──────────────────────────────────────────────────────────────────────
    async def _scrape_feed_with_api(self, category: str = "foryou", pages: int = 1) -> dict[str, Any]:
        """pylemon8 API feed fetch. Optional — only runs if pylemon8 is importable."""
        if not PYLEMON8_AVAILABLE or _PyLemon8 is None:
            raise RuntimeError("pylemon8 not available")

        loop = asyncio.get_event_loop()

        def _sync_fetch():
            api = _PyLemon8()
            feed_obj = api.feed(category)
            all_media: list[dict] = []
            users: set[str] = set()
            tag_ids: set[str] = set()
            for _ in range(pages):
                items = feed_obj.get_items() or []
                all_media.extend(self._extract_media_items_from_pylemon8_items(items, include_profile_images=self._profile_photos))
                users.update(self._extract_users_from_pylemon8_items(items))
            return all_media, users, tag_ids

        await self.rate_limiter.async_wait("lemon8-app.com", OperationType.FEED_FETCH if hasattr(OperationType, "FEED_FETCH") else OperationType.PROFILE_VIEW)
        all_media, users, tag_ids = await loop.run_in_executor(None, _sync_fetch)
        unique_items = self._deduplicate_media_items(all_media)
        return {
            "feed_type": category,
            "pages_scraped": pages,
            "media_items": unique_items,
            "media_urls": [i["url"] for i in unique_items],
            "discovered_users": list(users),
            "discovered_tags": list(tag_ids),
            "scrape_timestamp": datetime.now(timezone.utc).isoformat(),
            "total_media": len(unique_items),
            "method": "pylemon8_api",
        }

    async def _scrape_feed_with_web(self, client: httpx.AsyncClient, pages: int = 1) -> dict[str, Any]:
        """Traditional web scrape of /FEED/FORYOU with cursor-based pagination."""
        all_media: list[dict] = []
        all_users: set[str] = set()
        all_tag_ids: set[str] = set()
        current_cursor = "0"

        for page in range(pages):
            if self._stop.is_set():
                break
            url = FEED_URL
            if page > 0 and current_cursor:
                url = self._add_cursor_to_url(FEED_URL, current_cursor)

            await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
            try:
                resp = await client.get(url, headers={**self._headers(), "Referer": FEED_URL})
                resp.raise_for_status()
                html_content = resp.text
            except Exception as e:
                safe_error = _safe_log_text(e)
                await self._record_http_status_event(
                    e,
                    scope="feed_fetch",
                    subject=f"page:{page + 1}",
                    url=url,
                )
                logger.warning("Feed page %d fetch failed: %s", page + 1, safe_error)
                break

            media_items = self._extract_media_items_from_feed_cards(html_content, include_profile_images=self._profile_photos)
            if not media_items:
                media_items = self._extract_media_items_from_html(html_content, include_profile_images=self._profile_photos)

            if not any(item.get("username") for item in media_items):
                dom_items = self._extract_media_items_from_dom(html_content)
                if dom_items:
                    by_url = {i["url"]: i for i in media_items}
                    for di in dom_items:
                        existing = by_url.get(di["url"])
                        if existing:
                            if not existing.get("username") and di.get("username"):
                                existing["username"] = di["username"]
                        else:
                            media_items.append(di)

            if media_items:
                all_media.extend(media_items)
            else:
                for media_url in self._extract_media_urls(html_content):
                    mi = self._build_media_item(media_url)
                    if mi:
                        all_media.append(mi)

            all_users.update(self._extract_user_handles(html_content))
            all_tag_ids.update(self._extract_tag_ids(html_content))

            # Try to extract next cursor
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_content, "html.parser")
                found_cursor = False
                for script in soup.find_all("script", {"type": "application/json"}):
                    try:
                        data = json.loads(script.string or "{}")
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        continue
                    cur = self._find_key_in_json(data, ["cursor", "max_cursor", "next_cursor"])
                    if cur:
                        current_cursor = str(cur)
                        found_cursor = True
                        break
                if not found_cursor:
                    current_cursor = str(len(all_media))
            except Exception:
                current_cursor = str(len(all_media))

        unique_items = self._deduplicate_media_items(all_media)
        return {
            "feed_type": "foryou",
            "pages_scraped": pages,
            "media_items": unique_items,
            "media_urls": [i["url"] for i in unique_items],
            "discovered_users": list(all_users),
            "discovered_tags": list(all_tag_ids),
            "scrape_timestamp": datetime.now(timezone.utc).isoformat(),
            "total_media": len(unique_items),
            "method": "web_scraping",
        }

    # ──────────────────────────────────────────────────────────────────────
    # HTML / JSON extraction helpers (ported)
    # ──────────────────────────────────────────────────────────────────────
    def _extract_avatar(self, html: str) -> str | None:
        """Multi-fallback avatar extraction:
        1. og:image meta tag
        2. JSON markers in HTML (avatar_url, avatarUrl, avatarLarger, etc.)
        3. Embedded <script> JSON payloads containing avatar keys
        """
        # 1. og:image — often works even when API endpoints are broken
        og_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)', html)
        if og_match:
            url = og_match.group(1).replace("&amp;", "&")
            if url.startswith("http") and _parse_is_profile_photo_url(url):
                return _enhance_image_url(url) if self._enhance_urls else url

        # 2. Direct JSON markers
        for marker in ['"avatarLarger":"', '"avatar_url":"', '"avatarUrl":"',
                       '"avatarThumb":"', '"profilePhoto":"', '"profile_image":"']:
            idx = html.find(marker)
            if idx != -1:
                end = html.find('"', idx + len(marker))
                url = html[idx + len(marker):end].replace("\\u002F", "/")
                if url and url.startswith("http"):
                    return _enhance_image_url(url) if self._enhance_urls else url

        # 3. Scan <script type="application/json"> blocks for avatar keys
        avatar_keys = ("avatarLarger", "avatarThumb", "avatarUrl", "avatar_url",
                       "avatar", "profilePhoto")
        for block_start in re.finditer(r'<script[^>]*type=["\']application/json["\'][^>]*>', html):
            end_tag = html.find("</script>", block_start.end())
            if end_tag == -1:
                continue
            raw = html[block_start.end():end_tag].strip()
            try:
                blob = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            url = self._search_json_for_avatar(blob, avatar_keys)
            if url:
                return _enhance_image_url(url) if self._enhance_urls else url

        return None

    @staticmethod
    def _search_json_for_avatar(obj: Any, keys: tuple[str, ...], depth: int = 6) -> str | None:
        if depth <= 0 or obj is None:
            return None
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v.replace("\\u002F", "/")
            for v in obj.values():
                result = Lemon8Collector._search_json_for_avatar(v, keys, depth - 1)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj[:20]:
                result = Lemon8Collector._search_json_for_avatar(item, keys, depth - 1)
                if result:
                    return result
        return None

    def _extract_posts(self, html: str, user_id: str, username: str) -> list[dict]:
        """Extract posts (with media) from a profile/feed HTML page using embedded JSON."""
        posts: list[dict] = []
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("bs4 not available — _extract_posts returning [].")
            return posts

        try:
            soup = BeautifulSoup(html, "html.parser")
            seen_post_ids: set[str] = set()
            for script in soup.find_all("script", {"type": "application/json"}):
                try:
                    data = json.loads(script.string or "{}")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                for items in self._find_item_lists_in_json(data):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        post_id = (
                            item.get("itemId") or item.get("item_id") or item.get("id") or item.get("postId")
                        )
                        if not post_id:
                            continue
                        post_id = str(post_id)
                        if post_id in seen_post_ids:
                            continue
                        seen_post_ids.add(post_id)

                        media_descs = self._extract_media_items_from_pylemon8_items(
                            [item], include_profile_images=False
                        )
                        media_for_db: list[dict] = []
                        for m in media_descs:
                            url = m.get("url")
                            if not url:
                                continue
                            ext = "mp4" if m.get("media_type") == "video" else "jpg"
                            ctype = "video" if m.get("media_type") == "video" else "image"
                            cid = post_id + "_" + hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()
                            media_for_db.append({
                                "entity_id": user_id,
                                "entity_name": username,
                                "content_type": ctype,
                                "content_id": cid,
                                "url": _enhance_image_url(url) if self._enhance_urls else url,
                                "extension": ext,
                                "raw": m,
                            })

                        stats = {
                            "likeCount": item.get("likeCount") or item.get("diggCount") or 0,
                            "commentCount": item.get("commentCount") or 0,
                        }
                        posts.append({
                            "id": post_id,
                            "title": item.get("title") or "",
                            "description": item.get("shortContent") or item.get("desc") or "",
                            "stats": stats,
                            "media": media_for_db,
                            "raw": item,
                        })
        except Exception as e:
            logger.warning("post extraction error for %s: %s", username, e)

        # Fallback: if no posts via JSON, build pseudo-post from feed-card media
        if not posts:
            try:
                media_items = self._extract_media_items_from_feed_cards(html, include_profile_images=False)
                if media_items:
                    media_for_db = []
                    for m in media_items[:50]:
                        url = m.get("url")
                        if not url:
                            continue
                        ext = "mp4" if m.get("media_type") == "video" else "jpg"
                        ctype = "video" if m.get("media_type") == "video" else "image"
                        cid = "p_" + hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()
                        media_for_db.append({
                            "entity_id": user_id,
                            "entity_name": username,
                            "content_type": ctype,
                            "content_id": cid,
                            "url": _enhance_image_url(url) if self._enhance_urls else url,
                            "extension": ext,
                            "raw": m,
                        })
                    if media_for_db:
                        # NOTE: Fallback feed-card pseudo-posts also lack real content.
                        # Skip upsert; only download media (same reason as FYP above).
                        # posts.append({
                        #     "id": f"profile_{user_id}",
                        #     "title": "",
                        #     "description": "",
                        #     "stats": {"likeCount": 0, "commentCount": 0},
                        #     "media": media_for_db,
                        # })
                        pass  # DISABLED 2026-06-02
            except Exception:
                pass
        return posts

    # ── URL / media helpers ──────────────────────────────────────────────
    def _normalize_username(self, value: Optional[str]) -> Optional[str]:
        return _parse_normalize_username(value)

    def _clean_media_url(self, url: str) -> str:
        return _parse_clean_media_url(url)

    def _is_valid_media_url(self, url: str) -> bool:
        return _parse_is_valid_media_url(url)

    def _is_small_image(self, url: str) -> bool:
        return _parse_is_small_image(url)

    def _is_profile_photo_url(self, url: str) -> bool:
        return _parse_is_profile_photo_url(url)

    def _build_media_item(self, url: str, username: Optional[str] = None,
                          is_profile_photo: bool = False) -> Optional[dict]:
        cleaned = self._clean_media_url(url)
        if not cleaned or not self._is_valid_media_url(cleaned):
            return None
        norm_u = self._normalize_username(username) if username else None
        return {
            "url": cleaned,
            "username": norm_u,
            "is_profile_photo": is_profile_photo,
            "media_type": "image" if any(
                ext in cleaned.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]
            ) else "video",
        }

    def _deduplicate_media_items(self, media_items: list[dict]) -> list[dict]:
        out: dict[str, dict] = {}
        for item in media_items:
            url = item.get("url")
            if not url:
                continue
            existing = out.get(url)
            if existing is None:
                out[url] = dict(item)
                continue
            if not existing.get("username") and item.get("username"):
                existing["username"] = item["username"]
            if not existing.get("is_profile_photo") and item.get("is_profile_photo"):
                existing["is_profile_photo"] = True
            if existing.get("media_type") != "video" and item.get("media_type") == "video":
                existing["media_type"] = "video"
        return list(out.values())

    def _add_cursor_to_url(self, base_url: str, cursor: str) -> str:
        parsed = urlparse(base_url)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params["cursor"] = str(cursor)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(params), parsed.fragment))

    def _find_key_in_json(self, data: Any, keys: list[str]) -> Any:
        if isinstance(data, dict):
            for k in keys:
                if k in data:
                    return data[k]
            for v in data.values():
                r = self._find_key_in_json(v, keys)
                if r is not None:
                    return r
        elif isinstance(data, list):
            for it in data:
                r = self._find_key_in_json(it, keys)
                if r is not None:
                    return r
        return None

    def _find_item_lists_in_json(self, data: Any, found: Optional[list[list[dict]]] = None) -> list[list[dict]]:
        if found is None:
            found = []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list) and value and all(isinstance(it, dict) for it in value):
                    first = value[0]
                    if any(f in first for f in [
                        "authorInfo", "author", "user", "imageResource", "imageList",
                        "videoResource", "video", "videoList", "coverResource",
                        "largeImage", "coverImage",
                    ]):
                        found.append(value)
                if isinstance(value, (dict, list)):
                    self._find_item_lists_in_json(value, found)
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, (dict, list)):
                    self._find_item_lists_in_json(it, found)
        return found

    def _extract_urls_from_resource_value(self, value: Any) -> list[str]:
        urls: list[str] = []
        if isinstance(value, str):
            if self._is_valid_media_url(value):
                urls.append(value)
        elif isinstance(value, dict):
            url_list = value.get("urlList")
            if isinstance(url_list, list):
                for it in url_list:
                    urls.extend(self._extract_urls_from_resource_value(it))
            else:
                for k in ["url", "uri", "src", "playAddr"]:
                    if k in value:
                        urls.extend(self._extract_urls_from_resource_value(value[k]))
        elif isinstance(value, list):
            for it in value:
                urls.extend(self._extract_urls_from_resource_value(it))

        seen: set[str] = set()
        out: list[str] = []
        for u in urls:
            cu = self._clean_media_url(u)
            if cu and cu not in seen:
                out.append(cu)
                seen.add(cu)
        return out

    def _extract_profile_photo_urls_from_author(self, author_info: dict) -> list[str]:
        if not isinstance(author_info, dict):
            return []
        keys = [
            "avatar", "avatarLarger", "avatarLarge", "avatarMedium",
            "avatarThumb", "avatarUrl", "profilePhoto", "profileImage",
        ]
        urls: list[str] = []
        for k in keys:
            if k in author_info:
                urls.extend(self._extract_urls_from_resource_value(author_info[k]))
        seen, out = set(), []
        for u in urls:
            if u and u not in seen:
                out.append(u); seen.add(u)
        return out

    def _extract_username_from_author(self, author_info: Any) -> Optional[str]:
        if not isinstance(author_info, dict):
            return None
        for k in ["uniqueId", "username", "userName", "screenName", "handle",
                  "displayName", "linkName", "userId", "uid", "secUid", "nickName"]:
            v = author_info.get(k)
            if isinstance(v, str) and v.strip():
                return self._normalize_username(v)
        nested = self._find_key_in_json(author_info, [
            "uniqueId", "username", "userName", "screenName", "handle",
            "displayName", "linkName", "userId", "uid", "secUid", "nickName",
        ])
        if isinstance(nested, str) and nested.strip():
            return self._normalize_username(nested)
        return None

    def _extract_username_from_item(self, item: dict) -> Optional[str]:
        author = item.get("authorInfo") or item.get("author") or item.get("user") or {}
        u = self._extract_username_from_author(author)
        if u:
            return u
        for k in ["uniqueId", "username", "userName", "authorId", "linkName", "userId", "uid"]:
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return self._normalize_username(v)
        return None

    def _extract_media_items_from_pylemon8_items(self, items: list, include_profile_images: bool = False) -> list[dict]:
        out: list[dict] = []
        try:
            for item in items:
                if not isinstance(item, dict):
                    continue
                author = item.get("authorInfo") or item.get("author") or item.get("user") or {}
                username = self._extract_username_from_item(item)
                added = False

                for vk in ["videoResource", "video", "videoList", "videoUrl", "playAddr"]:
                    vr = item.get(vk)
                    if not vr:
                        continue
                    vurls = self._extract_urls_from_resource_value(vr)
                    if not vurls:
                        continue
                    mi = self._build_media_item(vurls[0], username=username)
                    if mi:
                        out.append(mi); added = True

                for ik in ["imageResource", "imageList"]:
                    ir = item.get(ik)
                    if not ir:
                        continue
                    if isinstance(ir, list):
                        for entry in ir:
                            iurls = self._extract_urls_from_resource_value(entry)
                            if iurls:
                                mi = self._build_media_item(iurls[-1], username=username)
                                if mi:
                                    out.append(mi); added = True
                    else:
                        iurls = self._extract_urls_from_resource_value(ir)
                        if iurls:
                            mi = self._build_media_item(iurls[-1], username=username)
                            if mi:
                                out.append(mi); added = True

                if not added:
                    for ck in ["coverResource", "largeImage", "coverImage"]:
                        cr = item.get(ck)
                        if not cr:
                            continue
                        curls = self._extract_urls_from_resource_value(cr)
                        if curls:
                            mi = self._build_media_item(curls[-1], username=username)
                            if mi:
                                out.append(mi); added = True
                            break

                if include_profile_images and self._profile_photos:
                    for au in self._extract_profile_photo_urls_from_author(author):
                        mi = self._build_media_item(au, username=username, is_profile_photo=True)
                        if mi:
                            out.append(mi)
        except Exception as e:
            logger.debug("extract_media_items_from_pylemon8_items error: %s", e)
        return self._deduplicate_media_items(out)

    def _extract_users_from_pylemon8_items(self, items: list) -> set[str]:
        users: set[str] = set()
        try:
            for item in items:
                if not isinstance(item, dict):
                    continue
                u = self._extract_username_from_item(item)
                if u:
                    users.add(u)
                author = item.get("authorInfo") or item.get("author") or item.get("user") or {}
                if isinstance(author, dict):
                    for k in ["uniqueId", "linkName", "username", "userName", "userId"]:
                        v = author.get(k)
                        if isinstance(v, str) and v.strip():
                            n = self._normalize_username(v)
                            if n:
                                users.add(n)
                for fld in ("title", "shortContent", "desc"):
                    val = item.get(fld)
                    if isinstance(val, str):
                        for m in re.findall(r"@([a-zA-Z0-9_\.]+)", val):
                            users.add(m.lower())
        except Exception:
            pass
        return users

    def _extract_media_items_from_html(self, html_content: str, include_profile_images: bool = False) -> list[dict]:
        out: list[dict] = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            for script in soup.find_all("script", {"type": "application/json"}):
                try:
                    data = json.loads(script.string or "{}")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                for items in self._find_item_lists_in_json(data):
                    out.extend(self._extract_media_items_from_pylemon8_items(items, include_profile_images=include_profile_images))
        except Exception as e:
            logger.debug("extract_media_items_from_html: %s", e)
        return self._deduplicate_media_items(out)

    def _extract_media_items_from_feed_cards(self, html_content: str, include_profile_images: bool = False) -> list[dict]:
        out: list[dict] = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            for card in soup.find_all("a", class_="article_card"):
                href = card.get("href", "") or ""
                m = re.search(r"/@([a-zA-Z0-9_.]{3,30})", href)
                if not m:
                    user_link = card.find("a", href=re.compile(r"/@[a-zA-Z0-9_.]{3,30}"))
                    if user_link:
                        m = re.search(r"/@([a-zA-Z0-9_.]{3,30})", user_link.get("href", "") or "")
                if not m:
                    continue
                username = self._normalize_username(m.group(1))
                note_id = ""
                nm = re.search(r'/(\d{10,20})(?:\?|$)', href)
                if nm:
                    note_id = nm.group(1)
                for img in card.find_all("img", src=True):
                    src = img.get("src")
                    if not src or not self._is_valid_media_url(src):
                        continue
                    if self._is_small_image(src):
                        if include_profile_images and self._is_profile_photo_url(src):
                            mi = self._build_media_item(src, username=username, is_profile_photo=True)
                            if mi:
                                out.append(mi)
                        continue
                    mi = self._build_media_item(src, username=username)
                    if mi:
                        if note_id:
                            mi["note_id"] = note_id
                        mi["href"] = href
                        out.append(mi)
        except Exception as e:
            logger.debug("extract_media_items_from_feed_cards: %s", e)
        return self._deduplicate_media_items(out)

    def _extract_media_urls_from_fragment_html(self, fragment_html: str, include_small_images: bool = False) -> list[str]:
        urls: list[str] = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(fragment_html, "html.parser")
            for tag, attr in [("img", "src"), ("img", "data-src"), ("video", "src"),
                              ("video", "data-src"), ("source", "src"), ("source", "data-src")]:
                for el in soup.find_all(tag):
                    v = el.get(attr)
                    if not v or not self._is_valid_media_url(v):
                        continue
                    if tag == "img" and not include_small_images and self._is_small_image(v):
                        continue
                    urls.append(self._clean_media_url(v))
            for pat in [r'https?://[^"\']*tiktokcdn[^"\']*', r'https?://[^"\']*byteimg[^"\']*']:
                for m in re.findall(pat, fragment_html, re.IGNORECASE):
                    if self._is_valid_media_url(m):
                        if not include_small_images and self._is_small_image(m):
                            continue
                        urls.append(self._clean_media_url(m))
        except Exception:
            pass
        seen, out = set(), []
        for u in urls:
            if u and u not in seen:
                out.append(u); seen.add(u)
        return out

    def _extract_media_items_from_dom(self, html_content: str) -> list[dict]:
        out: list[dict] = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link.get("href", "") or ""
                m = re.search(r"/@([a-zA-Z0-9_.]{3,30})", href)
                if not m:
                    continue
                username = self._normalize_username(m.group(1))
                node = link
                chosen: list[str] = []
                for _ in range(6):
                    node = node.parent
                    if node is None:
                        break
                    candidate = self._extract_media_urls_from_fragment_html(str(node))
                    if 0 < len(candidate) <= 8:
                        chosen = candidate
                        break
                for u in chosen:
                    mi = self._build_media_item(u, username=username)
                    if mi:
                        out.append(mi)
        except Exception:
            pass
        return self._deduplicate_media_items(out)

    def _extract_media_urls(self, html_content: str) -> list[str]:
        urls: list[str] = []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")
            for script in soup.find_all("script", {"type": "application/json"}):
                try:
                    data = json.loads(script.string or "{}")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                for u in self._extract_urls_from_json(data):
                    urls.append(u)

            patterns = [
                r'"(https?://[^"]*tiktokcdn[^"]*)"',
                r'"(https?://[^"]*byteimg[^"]*)"',
                r'"(https?://[^"]*muscdn[^"]*)"',
                r'"(https?://[^"]*\.(mp4|jpg|jpeg|png|gif|webm|m4v)[^"]*)"',
                r'"(https?://[^"]*lemon8[^"]*\.(mp4|jpg|jpeg|png|gif)[^"]*)"',
            ]
            for pat in patterns:
                for m in re.findall(pat, html_content, re.IGNORECASE):
                    u = m[0] if isinstance(m, tuple) else m
                    if self._is_valid_media_url(u):
                        urls.append(u)
        except Exception as e:
            logger.debug("extract_media_urls: %s", e)
        seen, out = set(), []
        for u in urls:
            cu = self._clean_media_url(u)
            if cu and cu not in seen:
                out.append(cu); seen.add(cu)
        return out

    def _extract_urls_from_json(self, data: Any, urls: Optional[list] = None) -> list[str]:
        if urls is None:
            urls = []
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, str) and self._is_valid_media_url(v):
                    urls.append(v)
                elif isinstance(v, (dict, list)):
                    self._extract_urls_from_json(v, urls)
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, str) and self._is_valid_media_url(it):
                    urls.append(it)
                elif isinstance(it, (dict, list)):
                    self._extract_urls_from_json(it, urls)
        return urls

    def _extract_user_handles(self, html_content: str) -> set[str]:
        handles: set[str] = set()
        try:
            patterns = [
                r"@([a-zA-Z0-9_\.]{3,30})",
                r'"uniqueId":"([a-zA-Z0-9_\.]{3,30})"',
                r'"username":"([a-zA-Z0-9_\.]{3,30})"',
                r'"displayName":"@?([a-zA-Z0-9_\.]{3,30})"',
            ]
            excluded = {
                "lemon8", "tiktok", "admin", "official", "font", "media",
                "keyframes", "supports", "import", "charset", "root",
                "container", "wrapper", "header", "footer", "sidebar",
                "content", "article", "section", "button", "input",
            }
            for pat in patterns:
                for m in re.findall(pat, html_content, re.IGNORECASE):
                    n = m.strip("@").lower()
                    if len(n) > 2 and n not in excluded:
                        handles.add(n)

            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_content, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = link.get("href") or ""
                    for pat in [r"/@([a-zA-Z0-9_\.]{3,30})", r"/user/([a-zA-Z0-9_\.]{3,30})", r"user=([a-zA-Z0-9_\.]{3,30})"]:
                        m = re.search(pat, href)
                        if m:
                            uname = m.group(1).lower()
                            if len(uname) > 2:
                                handles.add(uname)
                for script in soup.find_all("script", {"type": "application/json"}):
                    try:
                        data = json.loads(script.string or "{}")
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        continue
                    self._extract_users_from_json(data, handles)
            except Exception:
                pass
        except Exception as e:
            logger.debug("extract_user_handles: %s", e)
        return handles

    def _extract_users_from_json(self, data: Any, users_set: set[str]):
        if isinstance(data, dict):
            for k in ["uniqueId", "username", "displayName", "authorId", "userId"]:
                if k in data and isinstance(data[k], str):
                    n = data[k].strip("@").lower()
                    if len(n) > 2 and n.replace("_", "").replace(".", "").isalnum():
                        users_set.add(n)
            for v in data.values():
                if isinstance(v, (dict, list)):
                    self._extract_users_from_json(v, users_set)
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, (dict, list)):
                    self._extract_users_from_json(it, users_set)

    def _extract_tag_ids(self, html_content: str) -> set[str]:
        tag_ids: set[str] = set()
        try:
            patterns = [
                r"/topic/(\d+)", r'"topicId":"(\d+)"', r'"tagId":"(\d+)"',
                r'"challengeId":"(\d+)"', r"topic=(\d+)", r"tag=(\d+)",
            ]
            for pat in patterns:
                for m in re.findall(pat, html_content, re.IGNORECASE):
                    if len(m) > 5:
                        tag_ids.add(m)
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_content, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = link.get("href") or ""
                    for pat in [r"/topic/(\d+)", r"/tag/(\d+)", r"/challenge/(\d+)",
                                r"[?&]topic=(\d+)", r"[?&]tag=(\d+)"]:
                        m = re.search(pat, href)
                        if m and len(m.group(1)) > 5:
                            tag_ids.add(m.group(1))
                for script in soup.find_all("script", {"type": "application/json"}):
                    try:
                        data = json.loads(script.string or "{}")
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        continue
                    self._extract_tags_from_json(data, tag_ids)
            except Exception:
                pass
        except Exception as e:
            logger.debug("extract_tag_ids: %s", e)
        return tag_ids

    def _extract_tags_from_json(self, data: Any, tags_set: set[str]):
        if isinstance(data, dict):
            for k in ["topicId", "tagId", "challengeId", "hashtag", "topic"]:
                if k in data:
                    v = data[k]
                    if isinstance(v, str) and v.isdigit() and len(v) > 5:
                        tags_set.add(v)
                    elif isinstance(v, (int, float)) and len(str(int(v))) > 5:
                        tags_set.add(str(int(v)))
            for v in data.values():
                if isinstance(v, (dict, list)):
                    self._extract_tags_from_json(v, tags_set)
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, (dict, list)):
                    self._extract_tags_from_json(it, tags_set)

    # ──────────────────────────────────────────────────────────────────────
    # Backfill — unified hook
    # ──────────────────────────────────────────────────────────────────────
    async def get_backfill_items(self, batch_size: int) -> list[dict]:
        """Find profiles missing avatars and resolve URLs by scraping."""
        if not self.pool or not self._profile_photos:
            return []
        if await self._cooldown_active_for_scope("avatar_profile_fetch"):
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.platform_user_id, p.username
                FROM lemon8_profiles p
                LEFT JOIN media_items mi
                    ON mi.source = 'lemon8'
                    AND mi.content_id = 'profile_' || p.platform_user_id
                WHERE mi.id IS NULL
                  AND p.username IS NOT NULL
                LIMIT $1
            """, batch_size)
        items: list[dict] = []
        for row in rows:
            if self._stop.is_set():
                break
            username = row["username"]
            user_id = row["platform_user_id"]
            try:
                avatar_url = await self._resolve_avatar_url(username)
            except Exception as e:
                logger.debug("lemon8 backfill avatar resolve %s: %s", username, _safe_log_text(e))
                continue
            if not avatar_url:
                continue
            # Update the profile row with the found avatar
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE lemon8_profiles SET avatar_url = $1 WHERE platform_user_id = $2",
                        avatar_url, user_id,
                    )
            except Exception:
                pass
            items.append({
                "entity_id": user_id,
                "entity_name": username,
                "content_type": "profile_photo",
                "content_id": f"profile_{user_id}",
                "url": avatar_url,
                "extension": "jpg",
            })
        return items

    async def _resolve_avatar_url(self, username: str) -> str | None:
        """Fetch profile page and extract avatar URL using multi-fallback."""
        username = username.lstrip("@")
        if await self._cooldown_active_for_scope("avatar_profile_fetch"):
            return None
        try:
            await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
        except Exception:
            pass
        async with httpx.AsyncClient(
            timeout=30, cookies=self._cookies,
            headers=self._headers(), follow_redirects=True,
        ) as client:
            try:
                resp = await client.get(USER_URL_PATTERN.format(username))
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                safe_error = _safe_log_text(e)
                await self._record_http_status_event(
                    e,
                    scope="avatar_profile_fetch",
                    subject=username,
                    url=USER_URL_PATTERN.format(username),
                )
                logger.debug("lemon8 avatar fetch %s: %s", username, safe_error)
                return None
            avatar = self._extract_avatar(html)
            if avatar:
                return avatar
            # Last resort: try _data Remix endpoint
            try:
                data_url = f"{LEMON8_BASE_URL}/@{username}?_data=routes/%24user_link_name"
                resp2 = await client.get(data_url)
                if resp2.status_code == 200:
                    for line in resp2.text.split("\n"):
                        line = line.strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            blob = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        url = self._search_json_for_avatar(
                            blob,
                            ("avatarLarger", "avatarThumb", "avatarUrl",
                             "avatar_url", "avatar", "profilePhoto"),
                        )
                        if url:
                            return _enhance_image_url(url) if self._enhance_urls else url
            except Exception:
                pass
        return None

    # ──────────────────────────────────────────────────────────────────────
    # Media download
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _build_lemon8_source_url(item: dict) -> str | None:
        """Canonical Lemon8 URL for media_items.source_url.

        Lemon8's content_id conventions vary by scrape path (feed / profile
        / discover) so a clean per-content_id URL isn't always derivable. We
        take a tiered approach:
          - profile_photo: derive the user profile page from entity_name
                           (matches USER_URL_PATTERN in this collector).
          - everything else with an ``item['url']`` (the CDN URL we downloaded
                           from): use that — expiring signatures notwithstanding,
                           it preserves lineage back to the source.
          - fallback: entity's profile page if we at least have the username.
        Returns None only when there is no username AND no url — a state that
        shouldn't reach download_media in practice."""
        ctype = (item.get("content_type") or "").strip()
        username = (item.get("entity_name") or "").strip().lstrip("@")
        cdn_url = (item.get("url") or "").strip() or None
        if ctype == "profile_photo" and username:
            return f"https://www.lemon8-app.com/@{username}"
        if cdn_url:
            return cdn_url
        if username:
            return f"https://www.lemon8-app.com/@{username}"
        return None

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return False
        content_type = item.get("content_type")
        if content_type == "profile_photo" and await self._cooldown_active_for_scope(
            "media_download",
            metadata={"content_type": content_type},
        ):
            return False
        filename = self.build_filename(item["entity_id"], item["entity_name"], item["content_type"], cid, extension=item.get("extension", "jpg"))
        try:
            await self.rate_limiter.async_wait("lemon8-app.com", OperationType.MEDIA_DOWNLOAD)
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(item["url"])
                resp.raise_for_status()
                data = resp.content
            if len(data) < self._min_file_size:
                return False
            source_url = self._build_lemon8_source_url(item)
            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {}),
                "rebuild_target_tables": ["media_items", "lemon8_posts", "lemon8_profiles"],
            }
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=cid,
                artifact_kind="media_blob",
                data=data,
                extension=item.get("extension", "jpg"),
                metadata={
                    **metadata,
                    "filename": filename,
                    "source_url": source_url,
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
            inserted = await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                metadata=metadata,
                source_url=source_url,
            )
            if artifact.partial:
                await self.send_to_dlq(item["entity_id"], cid, f"vault artifact partial: {artifact.error}")
            if inserted:
                self._known_ids.add(cid)
            return bool(inserted)
        except Exception as e:
            safe_error = _safe_log_text(e)
            await self._record_http_status_event(
                e,
                scope="media_download",
                subject=str(item.get("entity_name") or item.get("entity_id") or cid),
                url=item.get("url"),
                record_access_errors=False,
                metadata={"content_id": cid, "content_type": item.get("content_type")},
            )
            status_code = self._http_status_from_error(e)
            if status_code in (403, 404):
                logger.warning("lemon8 media unavailable %s: HTTP %s", cid, status_code)
                return False
            else:
                logger.error("Download failed %s: %s", cid, safe_error)
            await self.send_to_dlq(item["entity_id"], cid, safe_error)
            return False

    async def cleanup(self):
        pass

    # ──────────────────────────────────────────────────────────────────────
    # Public single-target API (task brief — required method names)
    #
    # These thin wrappers let external callers (scheduler, spider, manual
    # CLI) drive a single username at a time without going through the
    # batch ``collect()`` entrypoint. Each constructs its own short-lived
    # httpx.AsyncClient with the same cookie/header/redirect semantics
    # as ``collect()``.
    # ──────────────────────────────────────────────────────────────────────
    async def collect_user_profile(self, username: str) -> Optional[dict]:
        """Scrape + upsert one user's profile (without enumerating posts).

        Returns the resolved ``{user_id, username, avatar_url}`` dict on
        success or ``None`` on hard failure. Profile photo is downloaded
        when ``LEMON8_PROFILE_PHOTO_ENABLED`` is true (default).
        """
        if not username:
            return None
        username = username.lstrip("@")
        async with httpx.AsyncClient(
            timeout=30, cookies=self._cookies,
            headers=self._headers(), follow_redirects=True,
        ) as client:
            try:
                await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
                url = USER_URL_PATTERN.format(username)
                resp = await client.get(url)
                if resp.status_code in (401, 403):
                    await self._record_profile_access(
                        username, False, error=f"HTTP {resp.status_code}",
                    )
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                status_code = self._http_status_from_error(e)
                safe_error = _safe_log_text(e)
                if status_code == 404:
                    logger.info("lemon8: skip unavailable profile %s: HTTP 404", username)
                    return None
                await self._record_http_status_event(
                    e,
                    scope="profile_fetch",
                    subject=username,
                    url=USER_URL_PATTERN.format(username),
                )
                logger.warning("collect_user_profile %s: %s", username, safe_error)
                return None

            user_id = username
            marker = '"user_id":"'
            idx = html.find(marker)
            if idx != -1:
                end = html.find('"', idx + len(marker))
                user_id = html[idx + len(marker):end]

            avatar_url = self._extract_avatar(html) if self._profile_photos else None
            try:
                await self._upsert_profile(
                    user_id, username,
                    {"nickname": username, "avatar_url": avatar_url},
                )
            except Exception as e:
                logger.warning("collect_user_profile upsert %s: %s", username, e)

            await self._record_profile_access(username, True)

            if self._profile_photos and avatar_url:
                try:
                    await self.download_media({
                        "entity_id": user_id, "entity_name": username,
                        "content_type": "profile_photo",
                        "content_id": f"profile_{user_id}",
                        "url": avatar_url, "extension": "jpg",
                    })
                except Exception as e:
                    logger.debug("collect_user_profile avatar dl %s: %s", username, _safe_log_text(e))

            return {"user_id": user_id, "username": username, "avatar_url": avatar_url}

    async def collect_user_posts(self, username: str) -> list[dict]:
        """Scrape + upsert one user's posts. Returns the upserted post dicts.

        Heavy lifting is delegated to ``_extract_posts``; this method just
        provides a clean single-target driver for the scheduler / spider.
        """
        if not username:
            return []
        username = username.lstrip("@")
        async with httpx.AsyncClient(
            timeout=30, cookies=self._cookies,
            headers=self._headers(), follow_redirects=True,
        ) as client:
            try:
                await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
                url = USER_URL_PATTERN.format(username)
                resp = await client.get(url)
                if resp.status_code in (401, 403):
                    await self._record_profile_access(
                        username, False, error=f"HTTP {resp.status_code}",
                    )
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                status_code = self._http_status_from_error(e)
                safe_error = _safe_log_text(e)
                if status_code == 404:
                    logger.info("lemon8: skip unavailable posts profile %s: HTTP 404", username)
                    return []
                await self._record_http_status_event(
                    e,
                    scope="profile_posts_fetch",
                    subject=username,
                    url=USER_URL_PATTERN.format(username),
                )
                logger.warning("collect_user_posts %s: %s", username, safe_error)
                return []

            user_id = username
            marker = '"user_id":"'
            idx = html.find(marker)
            if idx != -1:
                end = html.find('"', idx + len(marker))
                user_id = html[idx + len(marker):end]

            await self._record_profile_access(username, True)

            posts = self._extract_posts(html, user_id, username)
            for post in posts:
                if self._stop.is_set():
                    break
                try:
                    await self._upsert_post(user_id, post)
                except Exception as e:
                    logger.debug("collect_user_posts upsert: %s", e)
                for media_item in post.get("media", []):
                    if not self.is_known(media_item.get("content_id", "")):
                        try:
                            await self.download_media(media_item)
                        except Exception as e:
                            logger.debug("collect_user_posts media dl: %s", _safe_log_text(e))
            return posts

    async def collect_following(self, username: str) -> AsyncIterator[str]:
        """Yield related-creator usernames discovered for ``username``.

        Lemon8 has no public follow graph, so "following" is approximated
        by user-handles co-occurring on the seed user's profile page —
        the same signal the toolkit's spider uses. Each yielded handle is
        also enqueued onto ``lemon8_spider_queue`` (best-effort) so legacy
        consumers continue to work alongside the Wave 0 spider.
        """
        if not username:
            return
        username = username.lstrip("@")
        try:
            async with httpx.AsyncClient(
                timeout=30, cookies=self._cookies,
                headers=self._headers(), follow_redirects=True,
            ) as client:
                await self.rate_limiter.async_wait("lemon8-app.com", OperationType.PROFILE_VIEW)
                url = USER_URL_PATTERN.format(username)
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            status_code = self._http_status_from_error(e)
            safe_error = _safe_log_text(e)
            if status_code == 404:
                logger.info("lemon8: skip unavailable following profile %s: HTTP 404", username)
                return
            await self._record_http_status_event(
                e,
                scope="following_fetch",
                subject=username,
                url=USER_URL_PATTERN.format(username),
            )
            logger.warning("collect_following %s: %s", username, safe_error)
            return

        seen: set[str] = {username.lower()}
        try:
            handles = self._extract_user_handles(html)
        except Exception as e:
            logger.debug("collect_following extract %s: %s", username, e)
            handles = set()

        for handle in handles:
            if not handle or handle.lower() in seen:
                continue
            seen.add(handle.lower())
            # Best-effort legacy spider-queue enqueue — does not block yield.
            try:
                await self._enqueue_spider_user(handle, source=f"profile:{username}")
            except Exception:
                pass
            yield handle

    async def spider_related_creators(
        self,
        seed: str,
        *,
        max_hops: Optional[int] = None,
    ) -> int:
        """BFS related-creator discovery starting from ``seed`` username.

        Uses Wave 0 ``SpiderDiscover`` over our ``Lemon8EdgeFetcher``.
        Returns the number of nodes visited (0 if SpiderDiscover or the
        DB pool is unavailable). All hard errors are caught and logged so
        the scheduler can keep moving.
        """
        if SpiderDiscover is None or self.pool is None:
            logger.info("spider_related_creators: SpiderDiscover/pool unavailable")
            return 0
        if not seed:
            return 0
        seed = seed.lstrip("@").strip()
        if not seed:
            return 0
        try:
            spider = self.make_spider_discover(max_hops=max_hops)
        except Exception as e:
            logger.warning("spider_related_creators init %s: %s", seed, e)
            return 0
        try:
            return await spider.run(seeds=[seed])
        except Exception as e:
            logger.warning("spider_related_creators %s: %s", seed, e)
            return 0

    def make_edge_fetcher(self) -> "Lemon8EdgeFetcher":
        """Build a Wave 0 ``EdgeFetcher`` over this collector."""
        return Lemon8EdgeFetcher(self)

    def make_spider_discover(self, *, max_hops: Optional[int] = None):
        """Build a ``SpiderDiscover`` for the unified spider queue."""
        if SpiderDiscover is None:
            raise RuntimeError("src.core.spider_discover not importable")
        return SpiderDiscover(
            platform="lemon8",
            fetcher=self.make_edge_fetcher(),
            pool=self.pool,
            max_hops=max_hops if max_hops is not None else int(
                os.getenv("LEMON8_SPIDER_DEPTH", "2")
            ),
            concurrency=int(os.getenv("LEMON8_SPIDER_CONCURRENCY", "2")),
        )
