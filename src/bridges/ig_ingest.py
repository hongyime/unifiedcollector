"""Social ingest bridge — receives media scraped by the UnifiedCollector Bridge
Chrome extension (which uses your logged-in browser session) and persists it like
any other collected media: downloads the file to the media drive and upserts a
media_items row. This sidesteps the GraphQL-400 / login-wall / signing problems
the headless collectors hit, because the extension scrapes as the real logged-in
user (same-origin fetches / reading the page's own embedded state).

Module is still named ig_ingest for compose compatibility
(`python -m src.bridges.ig_ingest`) but it is now MULTI-PLATFORM.

Run:  python -m src.bridges.ig_ingest   (listens on 0.0.0.0:8765)

Generic endpoints (CORS-open so the extension service worker can call them):
  GET  /social/targets?platform=<p>  -> {"targets":[{username,hop}], "usernames":[...], "max_hop"}
  POST /social/ingest   <- {"platform","username","items":[{content_id,content_type,url,entity_name}]}
                        -> queues downloads (returns instantly) {"accepted": N}
  POST /social/discover <- {"platform","source","hop","discovered":[{username,follower_count}]}
  GET  /health          -> {"ok": true}

Back-compat instagram aliases (the older extension builds call these):
  GET /ig/targets, POST /ig/ingest, POST /ig/discover  (== platform=instagram)

NON-BLOCKING INGEST: downloads run in a background asyncio task bounded by a
semaphore, so the POST returns immediately. This is important — the MV3 service
worker that calls us gets idle-killed if a request takes too long, which produced
the "message channel closed before a response was received" errors. We ack fast
and download out-of-band.
"""
import asyncio
import json
import logging
import base64
import os
import re
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from src.db.connection import get_pool, close_pool
from src.core.media_filter import inspect as inspect_media
from src.core.priority_hints import refresh_collector_priority_hints
from src.core.proximity import refresh_account_proximity_cache
from src.core.rate_limit_events import record_rate_limit_event
from src.core.dynamic_cooldown import record_dynamic_cooldown
from src.core.strava_route_queue import fetch_strava_route_capture_queue
from src.core.vault import (
    VAULT_ROOT,
    assert_media_write_allowed,
    verify_media_item_db_consistency,
    write_atomic_artifact,
    write_media_sidecar,
    write_raw_payload,
)
from src.collectors.strava import _derive_gps_route_fields

# Follow-aware access recording (Phase 0). The extension IS the live IG path, so
# recording access outcomes here populates profile_access_{summary,attempts} far
# faster than the 429'd headless collector. Defensive import — ingest still works
# without it.
try:
    from src.core.profile_access import ProfileAccessRepository
except Exception:  # pragma: no cover
    ProfileAccessRepository = None  # type: ignore[assignment]

# Profile change-history (Tier 4). The change tracker was only wired into the
# 429'd headless collector, so the LIVE extension path never recorded bio/
# username/follower changes (instagram_user_changes = 0). Wire it here too.
try:
    from src.core.user_change_tracker import UserChangeTracker, INSTAGRAM_TRACKED_FIELDS
except Exception:  # pragma: no cover
    UserChangeTracker = None  # type: ignore[assignment]
    INSTAGRAM_TRACKED_FIELDS = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("social_ingest")

MEDIA_ROOT = os.getenv("COLLECTOR_DRIVE_PATH", "/media")
PORT = int(os.getenv("IG_INGEST_PORT", "8765"))

# DM raw-sample capture (#35). Files land in DM_SAMPLE_DIR as
# <platform>_<n>.bin. n is a monotonically increasing index derived from the
# max existing index (NOT a count), so pruning can't cause an old-index reuse
# that would overwrite a not-yet-pruned file. Rotation keeps only the newest
# DM_SAMPLE_CAP_PER_PLATFORM files per platform by mtime, so the directory
# can't grow unbounded on active sockets (P1.1). Cap overridable via env.
DM_SAMPLE_DIR = "/tmp/dm_samples"
DM_SAMPLE_CAP_PER_PLATFORM = int(os.getenv("DM_SAMPLE_CAP", "200"))
MIN_BYTES = int(os.getenv("IG_INGEST_MIN_BYTES", "1024"))
DL_CONCURRENCY = int(os.getenv("SOCIAL_INGEST_CONCURRENCY", "4"))
try:
    SOCIAL_INGEST_UPLOAD_CONCURRENCY = max(
        1,
        int(os.getenv("SOCIAL_INGEST_UPLOAD_CONCURRENCY", "1")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_UPLOAD_CONCURRENCY = 1
try:
    SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY = max(
        1,
        int(os.getenv("SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY", "2")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY = 2
SOCIAL_INGEST_CLIENT_MAX_MB = int(os.getenv("SOCIAL_INGEST_CLIENT_MAX_MB", "512"))
try:
    BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS = max(
        0.25,
        float(os.getenv("BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS", "8.0")),
    )
except (TypeError, ValueError):
    BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS = 8.0
try:
    DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS = max(
        0.25,
        float(os.getenv("DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS", "6.0")),
    )
except (TypeError, ValueError):
    DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS = 6.0
try:
    IG_COOLDOWN_READ_TIMEOUT_SECONDS = max(
        0.25,
        float(os.getenv("IG_COOLDOWN_READ_TIMEOUT_SECONDS", "2.0")),
    )
except (TypeError, ValueError):
    IG_COOLDOWN_READ_TIMEOUT_SECONDS = 2.0
try:
    SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS = max(
        1.0,
        float(os.getenv("SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS", "8.0")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS = 8.0
try:
    SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS = max(
        SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS,
        float(os.getenv("SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS", "30.0")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS = 30.0
try:
    SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS = max(
        SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS,
        float(os.getenv("SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS", "60.0")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS = 60.0
try:
    SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS = max(
        SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS,
        float(os.getenv("SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS", "30.0")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS = 30.0
try:
    SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS = max(
        1.0,
        float(os.getenv("SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS", "4.0")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS = 4.0
SOCIAL_INGEST_PREP_DB_ON_STARTUP = os.getenv("SOCIAL_INGEST_PREP_DB_ON_STARTUP", "0").lower() in {
    "1",
    "true",
    "yes",
}
try:
    BROWSER_CONTENT_STALE_SECONDS = max(
        300,
        int(os.getenv("BROWSER_CONTENT_STALE_SECONDS", "3600")),
    )
except (TypeError, ValueError):
    BROWSER_CONTENT_STALE_SECONDS = 3600
try:
    BROWSER_CONTENT_HINT_TTL_SECONDS = max(
        30,
        int(os.getenv("BROWSER_CONTENT_HINT_TTL_SECONDS", "300")),
    )
except (TypeError, ValueError):
    BROWSER_CONTENT_HINT_TTL_SECONDS = 300
try:
    BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS = max(
        0.05,
        float(os.getenv("BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS", "0.75")),
    )
except (TypeError, ValueError):
    BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS = 0.75
UC_EXTENSION_EXPECTED_VERSION = os.getenv("UC_EXTENSION_EXPECTED_VERSION", "").strip()
try:
    SOCIAL_INGEST_STARTUP_DDL_TIMEOUT_SECONDS = max(
        15.0,
        float(os.getenv("SOCIAL_INGEST_STARTUP_DDL_TIMEOUT_SECONDS", "60.0")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_STARTUP_DDL_TIMEOUT_SECONDS = 60.0
STRAVA_BROWSER_429_COOLDOWN_SECONDS = int(os.getenv("STRAVA_BROWSER_429_COOLDOWN_SECONDS", "1800"))
try:
    STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS = max(
        STRAVA_BROWSER_429_COOLDOWN_SECONDS,
        int(os.getenv("STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS", "21600")),
    )
except (TypeError, ValueError):
    STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS = max(STRAVA_BROWSER_429_COOLDOWN_SECONDS, 21600)
try:
    STRAVA_BROWSER_429_MEMORY_SECONDS = max(
        0,
        int(os.getenv("STRAVA_BROWSER_429_MEMORY_SECONDS", "21600")),
    )
except (TypeError, ValueError):
    STRAVA_BROWSER_429_MEMORY_SECONDS = 21600
try:
    TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS = max(
        60,
        int(os.getenv("TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS", "1800")),
    )
except (TypeError, ValueError):
    TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS = 1800
try:
    TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS = max(
        60,
        int(os.getenv("TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS", "900")),
    )
except (TypeError, ValueError):
    TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS = 900
_BROWSER_CONTENT_HINT_CACHE: dict[str, tuple[float, dict]] = {}
_BROWSER_CONTENT_HINT_INFLIGHT: set[str] = set()
_BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS = {"x", "facebook", "tiktok", "lemon8", "threads"}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_THREADS_SYNTHETIC_MEDIA_ID = re.compile(r"^(?:img|vid)_[a-z0-9]+$", re.IGNORECASE)

# Platforms the bridge may push. Each may carry its own famous-cap / hop config.
# Only instagram currently spiders (followers/following graph); the others scrape
# whatever the open page exposes, so they have no spider table.
KNOWN_PLATFORMS = {"instagram", "tiktok", "lemon8", "x", "threads", "facebook", "strava"}
BROWSER_DIAGNOSTIC_PLATFORMS = {"bridge"}
_DM_PROBE_TARGET_TABLES = {platform: ["dm_probe_log"] for platform in KNOWN_PLATFORMS}

_BROWSER_CAPTURE_TARGET_TABLES = {
    "profile": {
        "instagram": ["instagram_profiles"],
        "x": ["x_profiles"],
        "facebook": ["facebook_profiles"],
    },
    "posts": {
        "instagram": ["instagram_posts"],
        "threads": ["threads_posts"],
        "facebook": ["facebook_posts"],
        "x": ["x_posts"],
    },
    "comments": {
        "instagram": ["instagram_comments"],
    },
    "dms": {
        "instagram": ["instagram_dm_thread", "instagram_dm"],
    },
    "dm_probe": _DM_PROBE_TARGET_TABLES,
    "dm_sample": _DM_PROBE_TARGET_TABLES,
    "dm_frame": _DM_PROBE_TARGET_TABLES,
    "dm_decoded": {
        "instagram": ["instagram_dm_thread", "instagram_dm"],
        "tiktok": ["tiktok_dm_thread", "tiktok_dm"],
    },
    "strava_streams": {
        "strava": ["strava_activities", "strava_gps_streams"],
    },
}
_BROWSER_CAPTURE_COMPRESSED_ENDPOINTS = {"profile", "posts", "comments"}

# 2-hop spider (instagram only): the extension scrapes a target's media AND, when
# the target's hop < MAX_HOP, crawls its followers/following and POSTs them to
# discover; we store them at hop+1 in instagram_spider_targets (a channel SEPARATE
# from collection_targets so the .targets file-sync never wipes them). Famous
# accounts (follower_count > cap) are dropped — we want your network, not celebs.
IG_SPIDER_MAX_HOP = int(os.getenv("INSTA_SPIDER_HOPS", "2"))
IG_SPIDER_FAMOUS_CAP = int(os.getenv("INSTA_SPIDER_FAMOUS_CAP", "100000"))
IG_SPIDER_TARGETS_LIMIT = int(os.getenv("IG_SPIDER_TARGETS_LIMIT", "250"))
SOCIAL_TARGET_CACHE_REFRESH_ON_REQUEST = os.getenv("SOCIAL_TARGET_CACHE_REFRESH_ON_REQUEST", "0").strip().lower() in {"1", "true", "yes", "on"}
SOCIAL_TARGET_CACHE_REFRESH_SECONDS = int(os.getenv("SOCIAL_TARGET_CACHE_REFRESH_SECONDS", "300"))
SOCIAL_TARGET_CACHE_REFRESH_INLINE_BUDGET_SECONDS = float(os.getenv("SOCIAL_TARGET_CACHE_REFRESH_INLINE_BUDGET_SECONDS", "0.25"))
SOCIAL_TARGET_RESPONSE_CACHE_SECONDS = float(os.getenv("SOCIAL_TARGET_RESPONSE_CACHE_SECONDS", "45.0"))
SOCIAL_TARGET_QUERY_TIMEOUT_SECONDS = float(os.getenv("SOCIAL_TARGET_QUERY_TIMEOUT_SECONDS", "2.0"))
SOCIAL_TARGET_STALE_RESPONSE_SECONDS = float(os.getenv("SOCIAL_TARGET_STALE_RESPONSE_SECONDS", "600.0"))
X_PROFILE_TARGET_REVISIT_SECONDS = int(os.getenv("X_PROFILE_TARGET_REVISIT_SECONDS", str(12 * 60 * 60)))
X_PROFILE_TARGET_RETRY_SECONDS = int(os.getenv("X_PROFILE_TARGET_RETRY_SECONDS", str(45 * 60)))
TIKTOK_FOLLOW_OWNER_FALLBACK = (
    os.getenv("TIKTOK_FOLLOW_OWNER_FALLBACK", "").strip().lstrip("@") or None
)
_SOCIAL_TARGET_CACHE_REFRESH_LAST = 0.0
_SOCIAL_TARGET_CACHE_REFRESH_LOCK: asyncio.Lock | None = None
_SOCIAL_TARGET_CACHE_REFRESH_TASKS: set[asyncio.Task] = set()
_SOCIAL_TARGET_RESPONSE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SOCIAL_TARGET_RESPONSE_LOCKS: dict[str, asyncio.Lock] = {}
_STRAVA_ROUTE_QUEUE_RESPONSE_CACHE: dict[str, tuple[float, dict]] = {}
_STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST: dict[str, float] = {}
STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS = float(os.getenv("STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS", "30.0"))
STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS = float(os.getenv("STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS", "2.0"))
STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS = float(os.getenv("STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS", "600.0"))

_SPIDER_DDL = """
CREATE TABLE IF NOT EXISTS instagram_spider_targets (
  username        TEXT PRIMARY KEY,
  hop             INT  NOT NULL DEFAULT 1,
  discovered_from TEXT,
  follower_count  INT,
  status          TEXT NOT NULL DEFAULT 'active',
  discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_scraped_at TIMESTAMPTZ
)
"""

_X_TARGETS_DDL = """
CREATE TABLE IF NOT EXISTS x_profile_targets (
  username text PRIMARY KEY,
  source text NOT NULL DEFAULT 'seen',
  priority integer NOT NULL DEFAULT 50,
  status text NOT NULL DEFAULT 'pending',
  next_visit_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_x_profile_targets_due
  ON x_profile_targets (status, next_visit_at, priority DESC);
CREATE INDEX IF NOT EXISTS idx_x_profile_targets_updated
  ON x_profile_targets (updated_at DESC);
CREATE TABLE IF NOT EXISTS x_edges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_username text,
  target_username text NOT NULL,
  post_id text,
  edge_type text NOT NULL,
  strength integer NOT NULL DEFAULT 50,
  evidence_url text,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_x_edges_natural
  ON x_edges (
    lower(coalesce(source_username, '')),
    lower(target_username),
    coalesce(post_id, ''),
    edge_type
  );
CREATE INDEX IF NOT EXISTS idx_x_edges_source
  ON x_edges (lower(source_username), edge_type, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_x_edges_target
  ON x_edges (lower(target_username), edge_type, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_x_edges_post
  ON x_edges (post_id);
"""

_TIKTOK_BROWSER_MEDIA_DDL = """
CREATE TABLE IF NOT EXISTS tiktok_browser_media_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  content_id text NOT NULL,
  username text,
  source_url text,
  url_hash text NOT NULL,
  asset_role text,
  content_type text,
  width integer,
  height integer,
  file_size bigint,
  mime_type text,
  extension_version text,
  ingest_mode text NOT NULL DEFAULT 'url',
  outcome text NOT NULL DEFAULT 'observed',
  reason text,
  needs_revisit boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tiktok_browser_media_candidate
  ON tiktok_browser_media_candidates (content_id, url_hash, ingest_mode);
CREATE INDEX IF NOT EXISTS idx_tiktok_browser_media_candidates_seen
  ON tiktok_browser_media_candidates (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_tiktok_browser_media_candidates_outcome
  ON tiktok_browser_media_candidates (outcome, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_tiktok_browser_media_candidates_revisit
  ON tiktok_browser_media_candidates (needs_revisit, last_seen DESC)
  WHERE needs_revisit;
CREATE TABLE IF NOT EXISTS tiktok_browser_revisit_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  content_id text NOT NULL,
  username text,
  post_url text,
  source_url text,
  reason text,
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 50,
  attempts integer NOT NULL DEFAULT 0,
  next_visit_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tiktok_browser_revisit_content
  ON tiktok_browser_revisit_queue (content_id);
CREATE INDEX IF NOT EXISTS idx_tiktok_browser_revisit_due
  ON tiktok_browser_revisit_queue (status, next_visit_at, priority DESC);
"""

_BROWSER_MEDIA_CANDIDATES_DDL = """
CREATE TABLE IF NOT EXISTS browser_media_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL,
  content_id text NOT NULL,
  username text,
  source_url text,
  url_hash text NOT NULL,
  asset_role text,
  content_type text,
  width integer,
  height integer,
  file_size bigint,
  mime_type text,
  extension_version text,
  ingest_mode text NOT NULL DEFAULT 'url',
  outcome text NOT NULL DEFAULT 'observed',
  reason text,
  needs_revisit boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_media_candidate
  ON browser_media_candidates (platform, content_id, url_hash, ingest_mode);
CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_platform_seen
  ON browser_media_candidates (platform, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_platform_outcome
  ON browser_media_candidates (platform, outcome, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_browser_media_candidates_revisit
  ON browser_media_candidates (platform, needs_revisit, last_seen DESC)
  WHERE needs_revisit;
CREATE TABLE IF NOT EXISTS browser_media_revisit_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform text NOT NULL,
  content_id text NOT NULL,
  username text,
  post_url text,
  source_url text,
  reason text,
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 50,
  attempts integer NOT NULL DEFAULT 0,
  next_visit_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_media_revisit_platform_content
  ON browser_media_revisit_queue (platform, content_id);
CREATE INDEX IF NOT EXISTS idx_browser_media_revisit_due
  ON browser_media_revisit_queue (platform, status, next_visit_at, priority DESC);
"""


async def _execute_ddl_script(conn, script: str) -> None:
    for statement in (s.strip() for s in str(script or "").split(";")):
        if statement:
            await conn.execute(statement)


_IG_EPOCH = 1314220021721  # Instagram media-id custom epoch (ms)
_IG_LONG = re.compile(r"\d{8,}")


def _ig_date_from_id(content_id: str):
    """Instagram media ids encode creation time: (id>>23)+epoch. Return YYYYMMDD."""
    m = _IG_LONG.search(content_id or "")
    if not m:
        return None
    try:
        d = datetime.fromtimestamp(((int(m.group(0)) >> 23) + _IG_EPOCH) / 1000, tz=timezone.utc)
        if 2010 <= d.year <= datetime.now(tz=timezone.utc).year + 1:
            return d.strftime("%Y%m%d")
    except Exception:
        return None
    return None


# Instagram/Threads shortcode alphabet — numeric media pk → the ~11-char code in
# the public URL (instagram.com/p/<code>, threads.com/post/<code>). The extension
# sometimes stores a wrong/long "code"; we derive the canonical one from the pk so
# verification URLs always open.
_SC_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _shortcode_from_id(media_id: str):
    """Reverse Instagram's base64 media-id encoding into the public shortcode."""
    m = _IG_LONG.search(str(media_id or ""))
    if not m:
        return None
    try:
        n = int(m.group(0))
    except Exception:
        return None
    if n <= 0:
        return None
    out = []
    while n > 0:
        out.append(_SC_ALPHABET[n & 63])
        n >>= 6
    return "".join(reversed(out))


def _verify_url(platform, media_id):
    """Canonical openable URL for a post, for manual spot-checking."""
    sc = _shortcode_from_id(media_id)
    if not sc:
        return None
    if platform == "instagram":
        return f"https://instagram.com/p/{sc}/"
    if platform == "threads":
        return f"https://threads.com/post/{sc}/"
    return None


def _date_prefix(item, platform="") -> str:
    """YYYYMMDD for the filename so files sort chronologically. Prefer the post's
    own taken_at; for instagram fall back to the date encoded in the media id;
    finally fall back to today (collection)."""
    epoch = item.get("taken_at")
    if epoch is None:
        meta = item.get("meta") or {}
        epoch = meta.get("taken_at") if isinstance(meta, dict) else None
    try:
        if epoch:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        pass
    if platform == "instagram":
        d = _ig_date_from_id(str(item.get("content_id") or ""))
        if d:
            return d
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d")


def _norm_platform(p, *, allow_diagnostics: bool = False):
    p = (p or "instagram").strip().lower()
    if p in {"twitter", "twitter / x", "twitter/x", "x.com"}:
        p = "x"
    if allow_diagnostics and p in BROWSER_DIAGNOSTIC_PLATFORMS:
        return p
    return p if p in KNOWN_PLATFORMS else "instagram"


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    # Chrome's Private Network Access (PNA) blocks HTTPS pages from fetching
    # loopback (127.0.0.1) unless the server opts in with this header. Some
    # origins (observed: https://www.lemon8-app.com) get PNA-enforced for
    # extension content-script fetches even though the extension declares
    # http://127.0.0.1/* in host_permissions — so the extension's direct-fetch
    # heartbeat fallback fails silently with `ERR net::ERR_FAILED` /
    # "Permission was denied for this request to access the `loopback` address
    # space." Opting in unblocks lemon8 while remaining safe for other
    # platforms (we never accept cross-origin credentialed requests: fetches
    # from content.js don't send cookies).
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


_STRUCTURED_CAPTURE_PATHS = {
    "/social/browser-media-candidates",
    "/social/comments",
    "/social/discover",
    "/social/dm-decoded",
    "/social/dm-frame",
    "/social/dm-probe",
    "/social/dm-sample",
    "/social/dms",
    "/social/posts",
    "/social/profile",
    "/social/seed",
    "/social/strava-route-visit",
    "/social/strava-streams",
    "/social/target-status",
    "/social/users",
    "/social/x-profile-target-result",
}


def _request_timeout_seconds(path: str) -> float:
    if path in {"/social/ingest-upload", "/social/ingest-upload-binary"}:
        return SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS
    if path == "/social/browser-heartbeat":
        return SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS
    if path in _STRUCTURED_CAPTURE_PATHS:
        return SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS
    return SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS


async def handle_options(request):
    return _cors(web.Response(status=204))


@web.middleware
async def request_timeout_middleware(request, handler):
    timeout_seconds = _request_timeout_seconds(request.path)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await handler(request)
    except TimeoutError:
        logger.warning(
            "social ingest request timed out after %.2fs method=%s path=%s",
            timeout_seconds,
            request.method,
            request.path,
        )
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "handler_timeout",
                "path": request.path,
            },
            status=503,
        ))


async def _ensure_app_pool(app):
    if app.get("pool") is not None:
        return app["pool"]
    async with asyncio.timeout(SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS):
        app["pool"] = await get_pool()
    app["startup_error"] = None
    return app["pool"]


def _schedule_app_task(app, coro, label: str) -> None:
    """Run best-effort bridge work without holding the browser request open."""
    async def _runner():
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("%s background task failed", label, exc_info=True)

    task = asyncio.create_task(_runner())
    app.setdefault("tasks", set()).add(task)
    task.add_done_callback(app["tasks"].discard)


@web.middleware
async def db_pool_middleware(request, handler):
    if request.method == "OPTIONS" or request.path in {
        "/health",
        "/social/browser-heartbeat",
        "/social/dm-heartbeat",
    }:
        return await handler(request)
    try:
        await _ensure_app_pool(request.app)
    except TimeoutError:
        request.app["startup_error"] = "db_pool_lazy_timeout"
        logger.warning(
            "social ingest lazy DB pool init timed out after %.2fs path=%s",
            SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS,
            request.path,
        )
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "db_pool_timeout",
                "path": request.path,
            },
            status=503,
        ))
    except Exception as exc:
        request.app["startup_error"] = f"db_pool_lazy_error:{exc.__class__.__name__}"
        logger.exception("social ingest lazy DB pool init failed path=%s", request.path)
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "db_pool_error",
                "path": request.path,
            },
            status=503,
        ))
    return await handler(request)


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------
async def _refresh_target_side_caches(pool) -> None:
    """Refresh target ranking side caches at most once per TTL.

    These cache builders can be expensive on the live DB. Running them for every
    browser /social/targets poll made target selection miss its response budget
    during active extension bursts.
    """
    global _SOCIAL_TARGET_CACHE_REFRESH_LAST, _SOCIAL_TARGET_CACHE_REFRESH_LOCK
    ttl = max(0, SOCIAL_TARGET_CACHE_REFRESH_SECONDS)
    now = time.time()
    if ttl and now - _SOCIAL_TARGET_CACHE_REFRESH_LAST < ttl:
        return
    lock = _SOCIAL_TARGET_CACHE_REFRESH_LOCK
    if lock is None:
        lock = asyncio.Lock()
        _SOCIAL_TARGET_CACHE_REFRESH_LOCK = lock
    if lock.locked():
        return

    async def _runner() -> None:
        global _SOCIAL_TARGET_CACHE_REFRESH_LAST
        async with lock:
            now = time.time()
            if ttl and now - _SOCIAL_TARGET_CACHE_REFRESH_LAST < ttl:
                return
            _SOCIAL_TARGET_CACHE_REFRESH_LAST = now
            try:
                await refresh_account_proximity_cache(pool)
                await refresh_collector_priority_hints(pool)
                _SOCIAL_TARGET_CACHE_REFRESH_LAST = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("target side-cache refresh failed", exc_info=True)

    task = asyncio.create_task(_runner())
    _SOCIAL_TARGET_CACHE_REFRESH_TASKS.add(task)
    task.add_done_callback(_SOCIAL_TARGET_CACHE_REFRESH_TASKS.discard)
    budget = max(0.0, SOCIAL_TARGET_CACHE_REFRESH_INLINE_BUDGET_SECONDS)
    if budget <= 0:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except asyncio.TimeoutError:
        logger.info(
            "target side-cache refresh continuing in background after %.2fs",
            budget,
        )


async def _targets_for(pool, platform):
    """seed targets (collection_targets) UNION instagram spider targets (IG only)."""
    seen = set()
    out = []
    if platform == "x":
        try:
            async with pool.acquire() as conn:
                seeds = await conn.fetch(
                    """
                    SELECT target_id AS username, priority
                    FROM collection_targets
                    WHERE source = 'x'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                queued = await conn.fetch(
                    """
                    SELECT username, source, priority, status, next_visit_at
                    FROM x_profile_targets
                    WHERE status IN ('pending', 'completed', 'failed')
                    ORDER BY
                        CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                        priority DESC,
                        next_visit_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                for row in list(seeds) + list(queued):
                    r = dict(row)
                    u = (r.get("username") or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({
                            "username": u,
                            "hop": 0,
                            "source": r.get("source") or "collection_targets",
                            "priority": int(r.get("priority") or 0),
                            "status": r.get("status") or "pending",
                            "next_visit_at": r["next_visit_at"].isoformat() if r.get("next_visit_at") else None,
                        })
        except Exception:
            logger.exception("targets query failed (%s)", platform)
        return out
    try:
        if SOCIAL_TARGET_CACHE_REFRESH_ON_REQUEST:
            await _refresh_target_side_caches(pool)
        async with pool.acquire() as conn:
            seeds = await conn.fetch(
                """
                SELECT ct.target_id, ct.priority
                FROM collection_targets ct
                WHERE ct.source = $1
                ORDER BY
                    ct.priority DESC,
                    ct.created_at ASC
                """,
                platform,
            )
            for r in seeds:
                u = r["target_id"]
                if u and u not in seen:
                    seen.add(u)
                    out.append({"username": u, "hop": 0})
            if platform == "instagram":
                spider = await conn.fetch(
                    """
                    SELECT s.username, s.hop
                    FROM instagram_spider_targets s
                    WHERE s.status='active' AND s.hop <= $1
                    ORDER BY
                        s.hop ASC,
                        s.last_scraped_at ASC NULLS FIRST,
                        s.discovered_at ASC
                    LIMIT $2
                    """,
                    IG_SPIDER_MAX_HOP,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                for r in spider:
                    u = r["username"]
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": int(r["hop"])})
            elif platform == "threads":
                # REVERSE cross-pollination: a Threads handle IS an Instagram handle,
                # so the real people we know on Instagram (your follow graph + spider)
                # are scrapeable Threads profiles. Hand them to the Threads tab to visit.
                ig = await conn.fetch(
                    """
                    SELECT s.username
                    FROM instagram_spider_targets s
                    WHERE s.status='active'
                    ORDER BY
                        s.last_scraped_at ASC NULLS FIRST,
                        s.discovered_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                ig2 = await conn.fetch(
                    """
                    SELECT ct.target_id AS username
                    FROM collection_targets ct
                    WHERE ct.source='instagram'
                    ORDER BY
                        ct.priority DESC,
                        ct.created_at ASC
                    """
                )
                for r in list(ig) + list(ig2):
                    u = (r["username"] or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": 1})
            elif platform == "x":
                queued = await conn.fetch(
                    """
                    SELECT username, source, priority, status, next_visit_at
                    FROM x_profile_targets
                    WHERE status IN ('pending', 'completed', 'failed')
                    ORDER BY
                        CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                        priority DESC,
                        next_visit_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                for r in queued:
                    u = (r["username"] or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({
                            "username": u,
                            "hop": 0,
                            "source": r["source"],
                            "priority": int(r["priority"] or 0),
                            "status": r["status"],
                            "next_visit_at": r["next_visit_at"].isoformat() if r["next_visit_at"] else None,
                        })
    except Exception:
        logger.exception("targets query failed (%s)", platform)
    return out


async def _cached_targets_for(pool, platform):
    ttl = max(0.0, SOCIAL_TARGET_RESPONSE_CACHE_SECONDS)
    stale_ttl = max(ttl, SOCIAL_TARGET_STALE_RESPONSE_SECONDS)
    query_timeout = max(0.1, SOCIAL_TARGET_QUERY_TIMEOUT_SECONDS)
    now = time.time()
    cached = _SOCIAL_TARGET_RESPONSE_CACHE.get(platform)
    if ttl and cached and now - cached[0] <= ttl:
        return cached[1]
    lock = _SOCIAL_TARGET_RESPONSE_LOCKS.get(platform)
    if lock is None:
        lock = asyncio.Lock()
        _SOCIAL_TARGET_RESPONSE_LOCKS[platform] = lock
    if lock.locked() and cached:
        return cached[1]
    async with lock:
        now = time.time()
        cached = _SOCIAL_TARGET_RESPONSE_CACHE.get(platform)
        if ttl and cached and now - cached[0] <= ttl:
            return cached[1]
        try:
            out = await asyncio.wait_for(_targets_for(pool, platform), timeout=query_timeout)
        except asyncio.TimeoutError:
            if cached and now - cached[0] <= stale_ttl:
                logger.warning(
                    "targets query timed out for %s after %.1fs; serving stale cache age=%.0fs",
                    platform,
                    query_timeout,
                    now - cached[0],
                )
                return cached[1]
            logger.warning(
                "targets query timed out for %s after %.1fs; serving empty list",
                platform,
                query_timeout,
            )
            return []
        _SOCIAL_TARGET_RESPONSE_CACHE[platform] = (time.time(), out)
        return out


async def get_targets(request):
    platform = _norm_platform(request.query.get("platform"))
    out = await _cached_targets_for(request.app["pool"], platform)
    return _cors(web.json_response({
        "platform": platform,
        "targets": out,
        "usernames": [t["username"] for t in out],  # back-compat
        "max_hop": IG_SPIDER_MAX_HOP if platform == "instagram" else 0,
    }))


async def get_targets_ig(request):  # /ig/targets alias
    request.query  # noqa
    out = await _cached_targets_for(request.app["pool"], "instagram")
    return _cors(web.json_response({
        "targets": out,
        "usernames": [t["username"] for t in out],
        "max_hop": IG_SPIDER_MAX_HOP,
    }))


async def x_profile_target_next(request):
    owner = (request.query.get("owner") or "").strip().lstrip("@")
    try:
        async with request.app["pool"].acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT username
                    FROM x_profile_targets
                    WHERE status IN ('pending', 'completed', 'failed', 'claimed')
                      AND next_visit_at <= now()
                    ORDER BY
                        CASE status
                          WHEN 'pending' THEN 0
                          WHEN 'failed' THEN 1
                          WHEN 'completed' THEN 2
                          ELSE 3
                        END,
                        priority DESC,
                        last_success_at ASC NULLS FIRST,
                        updated_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE x_profile_targets t
                SET status = 'claimed',
                    attempts = attempts + 1,
                    last_attempt_at = now(),
                    next_visit_at = now() + ($1::int * interval '1 second'),
                    metadata = metadata || $2::jsonb,
                    updated_at = now()
                FROM candidate
                WHERE t.username = candidate.username
                RETURNING t.username, t.source, t.priority, t.status,
                          t.attempts, t.last_success_at, t.next_visit_at
                """,
                X_PROFILE_TARGET_RETRY_SECONDS,
                json.dumps({"claimed_by": owner or None}),
            )
        target = None
        if row:
            target = {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in dict(row).items()
            }
        return _cors(web.json_response({
            "ok": True,
            "target": target,
            "revisit_seconds": X_PROFILE_TARGET_REVISIT_SECONDS,
        }))
    except Exception as e:
        logger.warning("x profile target next failed: %s", e)
        return _cors(web.json_response({"ok": False, "target": None, "error": str(e)}, status=500))


async def x_profile_target_result(request):
    body = await _safe_json(request)
    username = _x_handle(body.get("username"))
    status = str(body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or body.get("error") or "").strip()[:500] or None
    owner = str(body.get("owner") or "").strip().lstrip("@") or None
    if not username:
        return _cors(web.json_response({"ok": False, "error": "missing_username"}, status=400))
    if status in ("success", "completed", "ok"):
        next_seconds = X_PROFILE_TARGET_REVISIT_SECONDS
        db_status = "completed"
    elif status in ("unavailable", "missing", "not_found", "protected"):
        next_seconds = 7 * 24 * 60 * 60
        db_status = "unavailable"
    else:
        next_seconds = X_PROFILE_TARGET_RETRY_SECONDS
        db_status = "failed"
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                INSERT INTO x_profile_targets
                    (username, source, priority, status, next_visit_at, last_error, metadata)
                VALUES ($1, 'result', 50, $2, now() + ($3::int * interval '1 second'), $4, $5::jsonb)
                ON CONFLICT (username) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_success_at = CASE WHEN EXCLUDED.status = 'completed' THEN now() ELSE x_profile_targets.last_success_at END,
                    next_visit_at = EXCLUDED.next_visit_at,
                    last_error = EXCLUDED.last_error,
                    metadata = x_profile_targets.metadata || EXCLUDED.metadata,
                    updated_at = now()
                """,
                username,
                db_status,
                next_seconds,
                reason,
                json.dumps({"result_owner": owner, "result_status": status or db_status}),
            )
        return _cors(web.json_response({"ok": True, "username": username, "status": db_status}))
    except Exception as e:
        logger.warning("x profile target result failed: %s", e)
        return _cors(web.json_response({"ok": False, "error": str(e)}, status=500))


# ---------------------------------------------------------------------------
# IG throttle coordination — the headless collector persists its 429 cooldown
# (exponential, up to 4h) to service_cursors as "<expiry_epoch>:<streak>". Expose
# it so the EXTENSION can rest in sync: both paths share the same IG account, so if
# headless got rate-limited the extension must back off too (anti-ban). Cooperative
# throttling beats two independent clients each probing a flagged account.
#
# RESTART-SAFE (P2 review §4, verified 2026-07-03): this cooldown wall lives in the
# DB (service_cursors), NOT in process memory, so neither an ig_ingest restart nor a
# collector restart clears an active cooldown — the surviving row is re-read on the
# next request/cycle. The only in-memory state here is the download-concurrency
# Semaphore, which is a steady-state cap, not a backoff timer. No change needed.
# ---------------------------------------------------------------------------
async def ig_cooldown(request):
    pool = request.app["pool"]
    account = str(request.query.get("account") or request.query.get("owner") or "").strip() or None
    cooling, secs_left, streak = False, 0, 0
    degraded = False
    try:
        async with asyncio.timeout(IG_COOLDOWN_READ_TIMEOUT_SECONDS):
            async with pool.acquire() as conn:
                if account:
                    event = await conn.fetchrow(
                        """
                        SELECT created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS active_until,
                               CASE
                                 WHEN metadata->>'streak' ~ '^[0-9]+$' THEN (metadata->>'streak')::int
                                 ELSE NULL
                               END AS streak,
                               account
                        FROM rate_limit_events
                        WHERE source = 'instagram'
                          AND status_code = 429
                          AND account = $1
                          AND COALESCE(cooldown_seconds, 0) > 0
                          AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > NOW()
                        ORDER BY active_until DESC
                        LIMIT 1
                        """,
                        account,
                    )
                    if event and event["active_until"]:
                        active_until = event["active_until"]
                        if active_until.tzinfo is None:
                            active_until = active_until.replace(tzinfo=timezone.utc)
                        left = (active_until - datetime.now(timezone.utc)).total_seconds()
                        if left > 0:
                            cooling, secs_left = True, int(left)
                            streak = int(event["streak"] or 0)
                cursor_service = (
                    f"instagram_rate_limit:{account}"
                    if account
                    else "instagram_rate_limit"
                )
                if not cooling:
                    row = await conn.fetchval(
                        "SELECT last_processed_id FROM service_cursors WHERE service=$1",
                        cursor_service,
                    )
                    if row and ":" in str(row):
                        exp_s, streak_s = str(row).split(":", 1)
                        expiry = float(exp_s)
                        streak = int(float(streak_s))
                        left = expiry - time.time()
                        if left > 0:
                            cooling, secs_left = True, int(left)
    except TimeoutError:
        degraded = True
        logger.info(
            "ig_cooldown read timed out after %.2fs account=%s",
            IG_COOLDOWN_READ_TIMEOUT_SECONDS,
            account or "",
        )
    except Exception:
        degraded = True
        logger.debug("ig_cooldown read failed", exc_info=True)
    return _cors(web.json_response({
        "cooling": cooling,
        "secs_left": secs_left,
        "streak": streak,
        "account": account,
        "scope": "account" if account else "legacy_global",
        "cooldown_degraded": degraded,
    }))


# ---------------------------------------------------------------------------
# discover (instagram spider only)
# ---------------------------------------------------------------------------
async def _discover(pool, platform, body):
    if platform != "instagram":
        return {"added": 0, "reason": "no spider for " + platform}
    try:
        src_hop = int(body.get("hop", 0))
    except (TypeError, ValueError):
        src_hop = 0
    source = body.get("source")
    discovered = body.get("discovered") or []
    target_hop = src_hop + 1
    if target_hop > IG_SPIDER_MAX_HOP:
        return {"added": 0, "reason": "max_hop"}
    added = 0
    async with pool.acquire() as conn:
        for d in discovered:
            uname = (d.get("username") or "").strip().lstrip("@") if isinstance(d, dict) else str(d).strip()
            if not uname:
                continue
            fc = d.get("follower_count") if isinstance(d, dict) else None
            if isinstance(fc, int) and fc > IG_SPIDER_FAMOUS_CAP:
                continue
            res = await conn.execute(
                """
                INSERT INTO instagram_spider_targets (username, hop, discovered_from, follower_count)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (username) DO NOTHING
                """,
                uname, target_hop, source, fc if isinstance(fc, int) else None,
            )
            if res.endswith("1"):
                added += 1
    # every discovered follower/following is a user we've now seen
    await _record_users(pool, platform, discovered, "follow")
    logger.info("discover[%s] from %s (hop %d): +%d new", platform, source, src_hop, added)
    return {"added": added}


async def discover(request):
    platform = _norm_platform((await _safe_json(request)).get("platform"))
    body = await _safe_json(request)
    try:
        return _cors(web.json_response(await _discover(request.app["pool"], platform, body)))
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": 0, "error": "db"}, status=500))


async def discover_ig(request):  # /ig/discover alias
    body = await _safe_json(request)
    try:
        return _cors(web.json_response(await _discover(request.app["pool"], "instagram", body)))
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": 0, "error": "db"}, status=500))


async def target_status_handler(request):
    """Let the extension retire bad spider targets.

    Instagram returns HTTP 400/404 for some unavailable/private/deleted profile
    lookups. Those are not throttles and should not keep rotating through the
    active spider queue forever.
    """
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    username = (body.get("username") or "").strip().lstrip("@")
    status = (body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or "")[:120]
    if platform != "instagram" or not username:
        return _cors(web.json_response({"updated": 0}))
    if status not in {"unavailable", "skipped"}:
        status = "unavailable"
    updated = 0
    async with request.app["pool"].acquire() as conn:
        result = await conn.execute(
            """
            UPDATE instagram_spider_targets
            SET status = $2,
                last_scraped_at = now()
            WHERE username = $1
              AND status = 'active'
            """,
            username, status,
        )
        if result.endswith("1"):
            updated = 1
    if updated:
        logger.info("target-status[%s] %s -> %s (%s)", platform, username, status, reason)
    return _cors(web.json_response({"updated": updated, "status": status}))


# ---------------------------------------------------------------------------
# download + persist (generic over platform)
# ---------------------------------------------------------------------------
def _download_headers(platform: str, url: str, item: dict | None = None) -> dict:
    """Browser-like headers for CDN media fetched from extension-discovered URLs."""
    accept = "image/avif,image/webp,image/apng,image/svg+xml,image/*,video/*,*/*;q=0.8"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    }
    referers = {
        "facebook": "https://www.facebook.com/",
        "instagram": "https://www.instagram.com/",
        "threads": "https://www.threads.com/",
        "tiktok": "https://www.tiktok.com/",
        "x": "https://x.com/",
    }
    referer = referers.get(platform)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "cross-site"
        headers["Sec-Fetch-Mode"] = "no-cors"
        headers["Sec-Fetch-Dest"] = "image"
    return headers


def _threads_media_content_base_id(platform: str, content_id: str, sha256: str | None = None) -> str:
    """Stabilize extension-generated Threads media IDs with the stored bytes.

    The browser bridge sometimes sends short synthetic IDs like ``img_abc123``
    for DOM media candidates. Threads can reuse those IDs for separate carousel
    or feed blobs, so using the raw value as ``media_items.content_id`` lets
    distinct files overwrite each other before the vault consistency check runs.
    Real platform IDs are left untouched.
    """
    base = str(content_id or "")
    digest = str(sha256 or "").strip().lower()
    if platform == "threads" and digest and _THREADS_SYNTHETIC_MEDIA_ID.match(base):
        return f"{base}_{digest[:12]}"
    return base


def _media_store_content_id(media_kind: str, content_base_id: str) -> str:
    return content_base_id if media_kind == "post" else f"{media_kind}_{content_base_id}"


def _url_hash(url: str | None) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


def _item_meta(item: dict | None) -> dict:
    if not isinstance(item, dict):
        return {}
    meta = item.get("meta")
    return dict(meta) if isinstance(meta, dict) else {}


def _top_reject_reason(reject_stats: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(reject_stats, dict):
        return None, None
    examples = reject_stats.get("examples") if isinstance(reject_stats.get("examples"), dict) else {}
    best_reason = None
    best_count = -1
    for key, raw_count in reject_stats.items():
        if key == "examples":
            continue
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            continue
        if count > best_count:
            best_reason = str(key)
            best_count = count
    detail = examples.get(best_reason) if best_reason and isinstance(examples, dict) else None
    return best_reason, str(detail) if detail is not None else None


def _reject_count(value) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _inspect_media_payload(data: bytes, ct_header: str | None) -> tuple[bool, str | None, str | None, str | None, str | None]:
    ok, ext, mtype, reason = inspect_media(data, ct_header)
    if not ok:
        return False, ext, mtype, reason, None
    return True, ext, mtype, reason, hashlib.sha256(data).hexdigest()


def _write_extension_media_artifacts_sync(
    *,
    platform: str,
    safe_user: str,
    username: str,
    item: dict,
    data: bytes,
    ext: str,
    sha: str,
    ctype: str,
    media_kind: str,
    store_cid: str,
    dest: Path,
    url: str,
):
    meta = item.get("meta") or {}
    meta_obj = dict(meta) if isinstance(meta, dict) else {}
    artifact = write_atomic_artifact(
        source=platform,
        artifact_id=f"extension/{media_kind}/{store_cid}",
        artifact_kind="media_blob",
        data=data,
        extension=ext,
        expected_sha256=sha,
        metadata={
            **meta_obj,
            "entity_id": safe_user,
            "entity_name": item.get("entity_name") or username,
            "content_type": ctype,
            "content_id": store_cid,
            "kind": media_kind,
            "filename": dest.name,
            "source_url": url,
            "request_url": url,
            "ingest_path": "extension",
            "legacy_path": str(dest),
            "rebuild_target_tables": ["media_items"],
        },
        root=VAULT_ROOT,
    )
    if artifact.path is None:
        return artifact, None, None, None
    stored_path = artifact.path
    meta_obj["vault_artifact"] = {
        "ok": artifact.ok,
        "partial": artifact.partial,
        "path": artifact.relative_path,
        "blob_path": artifact.blob_relative_path,
        "sha256": artifact.sha256,
        "file_size": artifact.file_size,
        "sidecar_ok": artifact.sidecar.ok if artifact.sidecar else None,
        "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
        "duplicate_blob": artifact.duplicate_blob,
        "error": artifact.error,
    }
    sidecar = write_media_sidecar(
        source=platform,
        entity_id=safe_user,
        entity_name=item.get("entity_name") or username,
        content_type=ctype,
        content_id=store_cid,
        filename=dest.name,
        file_path=str(stored_path),
        file_size=len(data),
        width=None,
        height=None,
        sha256=sha,
        source_url=url,
        metadata=meta_obj,
        ingest_path="extension",
        kind=media_kind,
    )
    meta_obj["vault_sidecar"] = {
        "enabled": sidecar.enabled,
        "ok": sidecar.ok,
        "path": sidecar.relative_path,
        "error": sidecar.error,
    }
    return artifact, sidecar, json.dumps(meta_obj), stored_path


def _candidate_asset_role(item: dict | None, platform: str | None = None) -> str | None:
    meta = _item_meta(item)
    keys = []
    if platform:
        keys.append(f"{platform}_asset_role")
    keys.extend([
        "asset_role",
        "tiktok_asset_role",
        "x_asset_role",
        "facebook_asset_role",
        "threads_asset_role",
        "instagram_asset_role",
        "lemon8_asset_role",
    ])
    role = None
    for key in keys:
        role = meta.get(key)
        if role is None and isinstance(item, dict):
            role = item.get(key)
        if role:
            break
    role_s = str(role or "").strip().lower()
    return role_s or None


def _tiktok_asset_role(item: dict | None) -> str | None:
    return _candidate_asset_role(item, "tiktok")


def _candidate_dimensions(item: dict | None) -> tuple[int | None, int | None]:
    meta = _item_meta(item)
    width = _int(meta.get("width") if meta.get("width") is not None else (item or {}).get("width"))
    height = _int(meta.get("height") if meta.get("height") is not None else (item or {}).get("height"))
    return width, height


def _candidate_is_video(item: dict | None, platform: str | None = None) -> bool:
    role = _candidate_asset_role(item, platform) or ""
    ctype = str((item or {}).get("content_type") or "").lower()
    mime = str((item or {}).get("mime_type") or _item_meta(item).get("browser_upload_mime_type") or "").lower()
    url = str((item or {}).get("url") or "").lower()
    return (
        ctype == "video"
        or ctype.startswith("video/")
        or mime.startswith("video/")
        or "video" in role
        or "playaddr" in role
        or "/video/" in url
        or url.endswith(".mp4")
        or "video.twimg.com" in url
    )


def _tiktok_candidate_is_video(item: dict | None) -> bool:
    return _candidate_is_video(item, "tiktok")


def _looks_like_tiny_thumbnail(item: dict | None, detail: str | None = None) -> bool:
    role = _candidate_asset_role(item) or ""
    url = str((item or {}).get("url") or "").lower()
    detail_l = str(detail or "").lower()
    width, height = _candidate_dimensions(item)
    return (
        "too small" in detail_l
        or "thumbnail" in detail_l
        or "avatar" in role
        or "avatar" in url
        or "icon" in url
        or "logo" in url
        or bool(width and width < 160)
        or bool(height and height < 160)
    )


def _classify_tiktok_candidate_result(
    item: dict | None,
    *,
    saved: bool = False,
    reject_stats: dict | None = None,
    ingest_mode: str = "url",
    browser_result: dict | None = None,
) -> tuple[str, str | None, bool]:
    """Classify a TikTok media candidate into an actionable bucket.

    The aggregate saw/stored counters are useful but too blunt. This turns each
    candidate into a ledger row and queues only likely real video misses for a
    browser detail revisit.
    """
    if saved:
        return "stored", None, False

    result = browser_result if isinstance(browser_result, dict) else {}
    if result:
        if result.get("deduped") is True or _reject_count(result.get("deduped")) > 0:
            return "duplicate", "duplicate_sha256", False
        if _reject_count(result.get("saved")) > 0 or _reject_count(result.get("stored")) > 0:
            return "stored", None, False
        reason = str(result.get("reason") or "browser_upload_failed")
        if reason == "deferred_upload_budget":
            return (
                "deferred",
                reason,
                _tiktok_candidate_is_video(item) or bool((item or {}).get("browser_upload_only")),
            )
        if reason in {"duplicate_content_id", "duplicate_sha256"}:
            return "duplicate", reason, False
        if reason.startswith("http_") or reason in {"timeout", "failed", "browser_upload_failed"}:
            if _tiktok_candidate_is_video(item):
                return "short_lived_url", reason, True
            return "browser_fetch_failed", reason, False
        if reason in {"too_large", "too_large_header"}:
            return "too_large", reason, False
        if reason == "disallowed_host":
            return "disallowed_host", reason, False
        return "browser_upload_failed", reason, _tiktok_candidate_is_video(item)

    reason, detail = _top_reject_reason(reject_stats)
    if not reason:
        return "failed", None, _tiktok_candidate_is_video(item) and ingest_mode == "browser_upload"
    if reason in {"duplicate_content_id", "duplicate_sha256"}:
        return "duplicate", reason, False
    if reason == "invalid_media":
        if _looks_like_tiny_thumbnail(item, detail):
            return "tiny_thumbnail", detail or reason, False
        return "invalid_media", detail or reason, _tiktok_candidate_is_video(item)
    if reason == "http_status":
        if _tiktok_candidate_is_video(item):
            return "short_lived_url", detail or reason, True
        return "http_error", detail or reason, False
    if reason in {"bad_uploaded_media", "exception", "timeout", "artifact_write_failed"}:
        return reason, detail or reason, _tiktok_candidate_is_video(item)
    if reason == "vault_unavailable":
        return "vault_unavailable", detail or reason, False
    return reason, detail or reason, False


def _classify_browser_candidate_result(
    platform: str,
    item: dict | None,
    *,
    saved: bool = False,
    reject_stats: dict | None = None,
    ingest_mode: str = "url",
    browser_result: dict | None = None,
) -> tuple[str, str | None, bool]:
    if platform == "tiktok":
        return _classify_tiktok_candidate_result(
            item,
            saved=saved,
            reject_stats=reject_stats,
            ingest_mode=ingest_mode,
            browser_result=browser_result,
        )

    is_video = _candidate_is_video(item, platform)
    if saved:
        return "stored", None, False

    result = browser_result if isinstance(browser_result, dict) else {}
    if result:
        if result.get("deduped") is True or _reject_count(result.get("deduped")) > 0:
            return "duplicate", "duplicate_sha256", False
        if _reject_count(result.get("saved")) > 0 or _reject_count(result.get("stored")) > 0:
            return "stored", None, False
        reason = str(result.get("reason") or "browser_upload_failed")
        if reason == "deferred_upload_budget":
            return (
                "deferred",
                reason,
                is_video or bool((item or {}).get("browser_upload_only")),
            )
        if reason in {"duplicate_content_id", "duplicate_sha256"}:
            return "duplicate", reason, False
        if reason.startswith("http_") or reason in {"timeout", "failed", "browser_upload_failed"}:
            return "browser_fetch_failed", reason, is_video
        if reason in {"too_large", "too_large_header", "disallowed_host"}:
            return reason, reason, False
        if reason == "invalid_media" and _looks_like_tiny_thumbnail(item):
            return "tiny_thumbnail", reason, False
        return "browser_upload_failed", reason, is_video

    reason, detail = _top_reject_reason(reject_stats)
    if not reason:
        return "failed", None, is_video and ingest_mode == "browser_upload"
    if reason in {"duplicate_content_id", "duplicate_sha256"}:
        return "duplicate", reason, False
    if reason == "invalid_media":
        if _looks_like_tiny_thumbnail(item, detail):
            return "tiny_thumbnail", detail or reason, False
        return "invalid_media", detail or reason, is_video
    if reason == "http_status":
        return "http_error", detail or reason, is_video
    if reason in {"bad_uploaded_media", "exception", "timeout", "artifact_write_failed"}:
        return reason, detail or reason, is_video
    if reason == "vault_unavailable":
        return "vault_unavailable", detail or reason, False
    return reason, detail or reason, False


def _tiktok_candidate_post_url(item: dict | None, username: str | None) -> str | None:
    meta = _item_meta(item)
    for key in ("page_url", "post_url", "canonical_url"):
        value = str(meta.get(key) or (item or {}).get(key) or "").strip()
        if value.startswith("http"):
            return value
    source_url = str((item or {}).get("source_url") or (item or {}).get("url") or "").strip()
    if re.search(r"https?://(?:www\.)?tiktok\.com/@[^/]+/(?:video|photo)/", source_url):
        return source_url
    handle = _tiktok_handle(username)
    return f"https://www.tiktok.com/@{handle}" if handle else None


def _browser_candidate_post_url(platform: str, item: dict | None, username: str | None) -> str | None:
    meta = _item_meta(item)
    for key in ("page_url", "post_url", "canonical_url", "verify_url", "url"):
        value = str(meta.get(key) or (item or {}).get(key) or "").strip()
        if not value.startswith("http"):
            continue
        try:
            parsed = urlparse(value)
        except Exception:
            continue
        host = parsed.netloc.lower()
        path = parsed.path or ""
        if platform == "x" and host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            if re.match(r"^/[A-Za-z0-9_]{1,20}/status/\d+", path):
                return value
        elif platform == "threads" and host in {"www.threads.com", "threads.com", "www.threads.net", "threads.net"}:
            if re.match(r"^/@[^/]+/post/", path):
                return value
        elif platform == "lemon8" and ("lemon8" in host or "lemon8-app.com" in host):
            if re.search(r"/(?:@[^/]+/)?\d{6,}", path) or "/@" in path:
                return value
        elif platform == "facebook" and host.endswith("facebook.com"):
            if (
                any(token in path for token in ("/posts/", "/photos/", "/videos/"))
                or path.startswith("/photo")
                or path.startswith("/permalink.php")
                or path.startswith("/story.php")
            ):
                return value
    if platform == "x":
        meta_post_id = str(meta.get("post_id") or (item or {}).get("post_id") or "").strip()
        author = str(meta.get("author_username") or username or "").strip().lstrip("@")
        if author and meta_post_id:
            return f"https://x.com/{author}/status/{meta_post_id}"
    return None


def _browser_revisit_priority(platform: str, item: dict | None, outcome: str) -> int:
    if _candidate_is_video(item, platform):
        return 90
    if outcome in {"browser_fetch_failed", "http_error", "short_lived_url"}:
        return 75
    return 60


async def _record_tiktok_browser_candidate(
    pool,
    username: str | None,
    item: dict | None,
    *,
    ingest_mode: str,
    saved: bool = False,
    reject_stats: dict | None = None,
    extension_version: str | None = None,
    browser_result: dict | None = None,
) -> None:
    if not isinstance(item, dict):
        return
    url = str(item.get("url") or "").strip()
    if not url:
        return
    content_id = str(item.get("content_id") or _url_hash(url)[:32])[:300]
    meta = _item_meta(item)
    ext_version = extension_version or str(meta.get("extension_version") or "") or None
    width, height = _candidate_dimensions(item)
    file_size = _int(item.get("file_size") or meta.get("browser_upload_size"))
    mime_type = item.get("mime_type") or item.get("content_type_header") or meta.get("browser_upload_mime_type")
    outcome, reason, needs_revisit = _classify_tiktok_candidate_result(
        item,
        saved=saved,
        reject_stats=reject_stats,
        ingest_mode=ingest_mode,
        browser_result=browser_result,
    )
    post_url = _tiktok_candidate_post_url(item, username)
    priority = 95 if _tiktok_candidate_is_video(item) else 65
    metadata = {
        "raw_meta": meta,
        "browser_upload": bool(item.get("browser_upload")),
        "browser_upload_only": bool(item.get("browser_upload_only")),
    }
    if reject_stats:
        metadata["reject_stats"] = reject_stats
    if browser_result:
        metadata["browser_result"] = browser_result
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tiktok_browser_media_candidates
                  (content_id, username, source_url, url_hash, asset_role,
                   content_type, width, height, file_size, mime_type,
                   extension_version, ingest_mode, outcome, reason,
                   needs_revisit, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb)
                ON CONFLICT (content_id, url_hash, ingest_mode) DO UPDATE SET
                  username = COALESCE(EXCLUDED.username, tiktok_browser_media_candidates.username),
                  source_url = EXCLUDED.source_url,
                  asset_role = COALESCE(EXCLUDED.asset_role, tiktok_browser_media_candidates.asset_role),
                  content_type = COALESCE(EXCLUDED.content_type, tiktok_browser_media_candidates.content_type),
                  width = COALESCE(EXCLUDED.width, tiktok_browser_media_candidates.width),
                  height = COALESCE(EXCLUDED.height, tiktok_browser_media_candidates.height),
                  file_size = COALESCE(EXCLUDED.file_size, tiktok_browser_media_candidates.file_size),
                  mime_type = COALESCE(EXCLUDED.mime_type, tiktok_browser_media_candidates.mime_type),
                  extension_version = COALESCE(EXCLUDED.extension_version, tiktok_browser_media_candidates.extension_version),
                  outcome = EXCLUDED.outcome,
                  reason = EXCLUDED.reason,
                  needs_revisit = EXCLUDED.needs_revisit,
                  metadata = tiktok_browser_media_candidates.metadata || EXCLUDED.metadata,
                  last_seen = now()
                WHERE tiktok_browser_media_candidates.outcome IS DISTINCT FROM 'stored'
                  AND EXCLUDED.outcome = 'stored'
                """,
                content_id,
                username,
                url[:2000],
                _url_hash(url),
                _tiktok_asset_role(item),
                str(item.get("content_type") or "")[:40] or None,
                width,
                height,
                file_size,
                str(mime_type)[:120] if mime_type else None,
                ext_version,
                ingest_mode,
                outcome,
                reason,
                needs_revisit,
                json.dumps(metadata, default=str),
            )
            if needs_revisit:
                await conn.execute(
                    """
                    INSERT INTO tiktok_browser_revisit_queue
                      (content_id, username, post_url, source_url, reason,
                       priority, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                    ON CONFLICT (content_id) DO UPDATE SET
                      username = COALESCE(EXCLUDED.username, tiktok_browser_revisit_queue.username),
                      post_url = COALESCE(EXCLUDED.post_url, tiktok_browser_revisit_queue.post_url),
                      source_url = COALESCE(EXCLUDED.source_url, tiktok_browser_revisit_queue.source_url),
                      reason = EXCLUDED.reason,
                      priority = GREATEST(tiktok_browser_revisit_queue.priority, EXCLUDED.priority),
                      status = CASE
                        WHEN tiktok_browser_revisit_queue.status = 'completed'
                          THEN tiktok_browser_revisit_queue.status
                        ELSE 'pending'
                      END,
                      next_visit_at = CASE
                        WHEN tiktok_browser_revisit_queue.status = 'completed'
                          THEN tiktok_browser_revisit_queue.next_visit_at
                        ELSE LEAST(tiktok_browser_revisit_queue.next_visit_at, now())
                      END,
                      metadata = tiktok_browser_revisit_queue.metadata || EXCLUDED.metadata,
                      updated_at = now()
                    """,
                    content_id,
                    username,
                    post_url,
                    url[:2000],
                    reason or outcome,
                    priority,
                    json.dumps(metadata, default=str),
                )
    except Exception:
        logger.debug(
            "tiktok browser media candidate record failed content_id=%s mode=%s",
            content_id,
            ingest_mode,
            exc_info=True,
        )


async def _record_browser_media_candidate(
    pool,
    platform: str,
    username: str | None,
    item: dict | None,
    *,
    ingest_mode: str,
    saved: bool = False,
    reject_stats: dict | None = None,
    extension_version: str | None = None,
    browser_result: dict | None = None,
) -> None:
    if platform not in KNOWN_PLATFORMS or not isinstance(item, dict):
        return
    url = str(item.get("url") or "").strip()
    if not url:
        return
    content_id = str(item.get("content_id") or _url_hash(url)[:32])[:300]
    meta = _item_meta(item)
    ext_version = extension_version or str(meta.get("extension_version") or "") or None
    width, height = _candidate_dimensions(item)
    file_size = _int(item.get("file_size") or meta.get("browser_upload_size"))
    mime_type = item.get("mime_type") or item.get("content_type_header") or meta.get("browser_upload_mime_type")
    outcome, reason, needs_revisit = _classify_browser_candidate_result(
        platform,
        item,
        saved=saved,
        reject_stats=reject_stats,
        ingest_mode=ingest_mode,
        browser_result=browser_result,
    )
    metadata = {
        "raw_meta": meta,
        "browser_upload": bool(item.get("browser_upload")),
        "browser_upload_only": bool(item.get("browser_upload_only")),
    }
    if reject_stats:
        metadata["reject_stats"] = reject_stats
    if browser_result:
        metadata["browser_result"] = browser_result
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO browser_media_candidates
                  (platform, content_id, username, source_url, url_hash, asset_role,
                   content_type, width, height, file_size, mime_type,
                   extension_version, ingest_mode, outcome, reason,
                   needs_revisit, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
                ON CONFLICT (platform, content_id, url_hash, ingest_mode) DO UPDATE SET
                  username = COALESCE(EXCLUDED.username, browser_media_candidates.username),
                  source_url = EXCLUDED.source_url,
                  asset_role = COALESCE(EXCLUDED.asset_role, browser_media_candidates.asset_role),
                  content_type = COALESCE(EXCLUDED.content_type, browser_media_candidates.content_type),
                  width = COALESCE(EXCLUDED.width, browser_media_candidates.width),
                  height = COALESCE(EXCLUDED.height, browser_media_candidates.height),
                  file_size = COALESCE(EXCLUDED.file_size, browser_media_candidates.file_size),
                  mime_type = COALESCE(EXCLUDED.mime_type, browser_media_candidates.mime_type),
                  extension_version = COALESCE(EXCLUDED.extension_version, browser_media_candidates.extension_version),
                  outcome = EXCLUDED.outcome,
                  reason = EXCLUDED.reason,
                  needs_revisit = EXCLUDED.needs_revisit,
                  metadata = browser_media_candidates.metadata || EXCLUDED.metadata,
                  last_seen = now()
                WHERE browser_media_candidates.outcome IS DISTINCT FROM 'stored'
                  AND EXCLUDED.outcome = 'stored'
                """,
                platform,
                content_id,
                username,
                url[:2000],
                _url_hash(url),
                _candidate_asset_role(item, platform),
                str(item.get("content_type") or "")[:40] or None,
                width,
                height,
                file_size,
                str(mime_type)[:120] if mime_type else None,
                ext_version,
                ingest_mode,
                outcome,
                reason,
                needs_revisit,
                json.dumps(metadata, default=str),
            )
    except Exception:
        logger.debug(
            "browser media candidate record failed platform=%s content_id=%s mode=%s",
            platform,
            content_id,
            ingest_mode,
            exc_info=True,
        )

    if needs_revisit and platform != "tiktok":
        post_url = _browser_candidate_post_url(platform, item, username)
        priority = _browser_revisit_priority(platform, item, outcome)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO browser_media_revisit_queue
                      (platform, content_id, username, post_url, source_url, reason,
                       priority, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                    ON CONFLICT (platform, content_id) DO UPDATE SET
                      username = COALESCE(EXCLUDED.username, browser_media_revisit_queue.username),
                      post_url = COALESCE(EXCLUDED.post_url, browser_media_revisit_queue.post_url),
                      source_url = COALESCE(EXCLUDED.source_url, browser_media_revisit_queue.source_url),
                      reason = EXCLUDED.reason,
                      priority = GREATEST(browser_media_revisit_queue.priority, EXCLUDED.priority),
                      status = CASE
                        WHEN browser_media_revisit_queue.status = 'completed'
                          THEN browser_media_revisit_queue.status
                        ELSE 'pending'
                      END,
                      next_visit_at = CASE
                        WHEN browser_media_revisit_queue.status = 'completed'
                          THEN browser_media_revisit_queue.next_visit_at
                        ELSE LEAST(browser_media_revisit_queue.next_visit_at, now())
                      END,
                      metadata = browser_media_revisit_queue.metadata || EXCLUDED.metadata,
                      updated_at = now()
                    """,
                    platform,
                    content_id,
                    username,
                    post_url,
                    url[:2000],
                    reason or outcome,
                    priority,
                    json.dumps(metadata, default=str),
                )
        except Exception:
            logger.debug(
                "browser media revisit queue insert failed platform=%s content_id=%s",
                platform,
                content_id,
                exc_info=True,
            )

    if platform == "tiktok":
        await _record_tiktok_browser_candidate(
            pool,
            username,
            item,
            ingest_mode=ingest_mode,
            saved=saved,
            reject_stats=reject_stats,
            extension_version=extension_version,
            browser_result=browser_result,
        )


async def _download_and_save(pool, session, platform, username, item, reject_stats: dict | None = None) -> bool:
    def _reject(reason: str, detail: str | None = None) -> bool:
        if reject_stats is not None:
            reject_stats[reason] = int(reject_stats.get(reason, 0)) + 1
            if detail:
                examples = reject_stats.setdefault("examples", {})
                if isinstance(examples, dict) and reason not in examples:
                    examples[reason] = detail[:180]
        return False

    url = item.get("url")
    cid = str(item.get("content_id") or "")
    if not url or not cid:
        return _reject("missing_url_or_content_id")
    # media kind: post (default) | story | highlight. Stories/highlights live in
    # their own subtree and get a namespaced content_id so they never collide with
    # a feed post that happens to share an id.
    media_kind = (item.get("kind") or "post").lower()
    if media_kind not in ("post", "story", "highlight", "tagged", "profile"):
        media_kind = "post"
    content_base_id = _threads_media_content_base_id(platform, cid)
    needs_content_sha = content_base_id == cid and platform == "threads" and _THREADS_SYNTHETIC_MEDIA_ID.match(cid)
    # DB dedup id stays namespaced so a story/highlight/tagged/profile can't collide.
    store_cid = _media_store_content_id(media_kind, content_base_id)
    safe_user = _SAFE.sub("_", username)[:80] or "unknown"
    # filename kind label (no subfolders anymore — kind is encoded in the name)
    kindtag = {"story": "story_", "highlight": "hl_", "tagged": "tagged_", "profile": "profile_"}.get(media_kind, "")
    datestr = _date_prefix(item, platform)

    async def _record_vault_pause(reason: str) -> None:
        logger.warning(
            "vault/media unavailable before extension media write platform=%s cid=%s: %s",
            platform,
            store_cid,
            reason,
        )
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    platform,
                    safe_user,
                    store_cid,
                    f"vault/media unavailable before extension media write: {reason}"[:500],
                )
        except Exception:
            logger.debug(
                "vault/media unavailable DLQ insert failed for %s/%s",
                platform,
                store_cid,
                exc_info=True,
            )

    try:
        # dedup authority is media_items (source, content_id)
        if not needs_content_sha:
            async with pool.acquire() as conn:
                seen = await conn.fetchval(
                    "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2", platform, store_cid
                )
            if seen:
                return _reject("duplicate_content_id")

        try:
            assert_media_write_allowed(
                Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ".extension_write_check",
                media_root=MEDIA_ROOT,
            )
        except RuntimeError as exc:
            await _record_vault_pause(str(exc))
            return _reject("vault_unavailable", str(exc))

        data_bytes = item.get("data_bytes")
        data_b64 = item.get("data_b64")
        if isinstance(data_bytes, memoryview):
            data = data_bytes.tobytes()
            ct_header = item.get("mime_type") or item.get("content_type_header")
        elif isinstance(data_bytes, bytearray):
            data = bytes(data_bytes)
            ct_header = item.get("mime_type") or item.get("content_type_header")
        elif isinstance(data_bytes, bytes):
            data = data_bytes
            ct_header = item.get("mime_type") or item.get("content_type_header")
        elif data_b64:
            try:
                data = await asyncio.to_thread(base64.b64decode, str(data_b64), validate=True)
            except Exception as exc:
                return _reject("bad_uploaded_media", exc.__class__.__name__)
            ct_header = item.get("mime_type") or item.get("content_type_header")
        else:
            async with session.get(
                url,
                headers=_download_headers(platform, url, item),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    return _reject("http_status", str(r.status))
                ct_header = r.headers.get("content-type")
                data = await r.read()

        # GATE: keep only real PDF/image/video/audio above min size — drop
        # favicons, thumbnails, tracking pixels, sprite sheets, HTML error pages.
        ok, ext, mtype, reason, sha = await asyncio.to_thread(_inspect_media_payload, data, ct_header)
        if not ok:
            logger.debug("reject %s %s: %s", platform, store_cid, reason)
            return _reject("invalid_media", reason)
        if not sha:
            return _reject("invalid_media", "missing_sha256")
        if needs_content_sha:
            content_base_id = _threads_media_content_base_id(platform, cid, sha)
            store_cid = _media_store_content_id(media_kind, content_base_id)
            async with pool.acquire() as conn:
                seen = await conn.fetchval(
                    "SELECT 1 FROM media_items WHERE source=$1 AND content_id=$2", platform, store_cid
                )
            if seen:
                return _reject("duplicate_content_id")
        # CONTENT DEDUP: if these exact bytes are already stored for this source
        # (e.g. the same For-You image re-scraped under a different DOM content_id),
        # skip — no duplicate file or row. This is what kills the lemon8/tiktok
        # re-scrape duplication. Needs the (source, sha256) index for speed.
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM media_items WHERE source=$1 AND sha256=$2 LIMIT 1", platform, sha):
                return _reject("duplicate_sha256")
        ctype = "video" if mtype == "video" else ("pdf" if mtype == "pdf" else "photo")
        # Flat layout: /<platform>/account_<user>/<ctype>/  — kind + date live in the
        # filename: <YYYYMMDD>_<platform>_<user>_<kindtag><cid>.<ext> (sortable by date).
        dest_dir = Path(MEDIA_ROOT) / platform / f"account_{safe_user}" / ctype
        raw_cid = _SAFE.sub("_", content_base_id)[:100]
        dest = dest_dir / f"{datestr}_{platform}_{safe_user}_{kindtag}{raw_cid}.{ext}"
        try:
            assert_media_write_allowed(dest, media_root=MEDIA_ROOT)
        except RuntimeError as exc:
            await _record_vault_pause(str(exc))
            return _reject("vault_unavailable", str(exc))

        artifact, sidecar, meta_json, stored_path = await asyncio.to_thread(
            _write_extension_media_artifacts_sync,
            platform=platform,
            safe_user=safe_user,
            username=username,
            item=item,
            data=data,
            ext=ext,
            sha=sha,
            ctype=ctype,
            media_kind=media_kind,
            store_cid=store_cid,
            dest=dest,
            url=url,
        )
        if artifact.path is None:
            await _record_vault_pause(artifact.error or "artifact write failed")
            return _reject("artifact_write_failed", artifact.error or "artifact write failed")
        if sidecar is None or meta_json is None or stored_path is None:
            await _record_vault_pause("artifact sidecar state incomplete")
            return _reject("artifact_write_failed", "artifact sidecar state incomplete")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO media_items
                  (source, entity_id, entity_name, content_type, content_id,
                   filename, file_path, file_size, sha256, source_url, metadata, kind,
                   ingest_path)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12,'extension')
                ON CONFLICT (source, content_id) DO UPDATE SET
                   file_path = EXCLUDED.file_path,
                   file_size = EXCLUDED.file_size,
                   sha256 = EXCLUDED.sha256,
                   source_url = EXCLUDED.source_url,
                   metadata = EXCLUDED.metadata,
                   kind = EXCLUDED.kind,
                   ingest_path = 'extension'
                """,
                platform, safe_user, item.get("entity_name") or username, ctype, store_cid,
                dest.name, str(stored_path), len(data), sha, url, meta_json, media_kind,
            )
            if artifact.partial or not artifact.ok:
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    platform,
                    safe_user,
                    store_cid,
                    f"vault artifact partial: {artifact.error}",
                )
            if sidecar.enabled and not sidecar.ok:
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    platform,
                    safe_user,
                    store_cid,
                    f"vault sidecar write failed: {sidecar.error}",
                )
        try:
            async with pool.acquire() as conn:
                consistency = await verify_media_item_db_consistency(
                    conn,
                    source=platform,
                    content_id=store_cid,
                    file_path=str(stored_path),
                    file_size=len(data),
                    sha256=sha,
                    sidecar_path=sidecar.relative_path if sidecar.enabled else None,
                )
                consistency_meta = {
                    "vault_artifact_db_consistency": {
                        "ok": consistency.ok,
                        "errors": list(consistency.errors),
                    }
                }
                await conn.execute(
                    """
                    UPDATE media_items
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                    WHERE source = $1 AND content_id = $2
                    """,
                    platform,
                    store_cid,
                    json.dumps(consistency_meta, default=str),
                )
                if not consistency.ok:
                    await conn.execute(
                        """
                        INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                        VALUES ($1, $2, $3, $4)
                        """,
                        platform,
                        safe_user,
                        store_cid,
                        "vault artifact db consistency failed: "
                        + "; ".join(consistency.errors),
                    )
        except Exception:
            logger.warning(
                "vault sidecar status update failed for %s/%s",
                platform,
                store_cid,
                exc_info=True,
            )
        return True
    except Exception:
        logger.debug("save failed platform=%s cid=%s", platform, cid, exc_info=True)
        return _reject("exception")


def _merge_reject_stats(dest: dict, src: dict | None) -> None:
    if not isinstance(src, dict):
        return
    for key, value in src.items():
        if key == "examples":
            examples = dest.setdefault("examples", {})
            if isinstance(examples, dict) and isinstance(value, dict):
                for reason, detail in value.items():
                    examples.setdefault(reason, detail)
            continue
        try:
            dest[key] = int(dest.get(key, 0) or 0) + int(value or 0)
        except (TypeError, ValueError):
            dest[key] = value


async def _drain(app, platform, username, items, extension_version: str | None = None):
    """Background download worker — bounded concurrency, never blocks the POST."""
    pool, session, sem = app["pool"], app["session"], app["sem"]
    saved = 0
    reject_stats: dict = {}

    async def one(it):
        nonlocal saved
        async with sem:
            payload = it
            if extension_version and isinstance(it, dict):
                payload = dict(it)
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                payload["meta"] = {**meta, "extension_version": extension_version}
            item_stats: dict = {}
            item_saved = await _download_and_save(pool, session, platform, username, payload, item_stats)
            _merge_reject_stats(reject_stats, item_stats)
            if item_saved:
                saved += 1
            await _record_browser_media_candidate(
                pool,
                platform,
                username,
                payload if isinstance(payload, dict) else {},
                ingest_mode="url",
                saved=item_saved,
                reject_stats=item_stats,
                extension_version=extension_version,
            )

    await asyncio.gather(*(one(it) for it in items), return_exceptions=True)
    event_meta = {}
    if extension_version:
        event_meta["extension_version"] = extension_version
    if reject_stats:
        event_meta["reject_stats"] = reject_stats
    await _record_browser_ingest_event(
        pool,
        platform,
        "media",
        username,
        observed_count=len(items),
        stored_count=saved,
        metadata=event_meta or None,
    )
    if platform == "instagram":
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE instagram_spider_targets SET last_scraped_at=now() WHERE username=$1",
                    username,
                )
        except Exception:
            pass
    logger.info("ingest[%s] %s: %d/%d saved", platform, username, saved, len(items))


async def _record_browser_ingest_event(
    pool,
    platform: str,
    endpoint: str,
    subject: str | None = None,
    *,
    observed_count: int = 0,
    stored_count: int = 0,
    metadata: dict | None = None,
) -> None:
    try:
        async with asyncio.timeout(BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS):
            await _write_browser_ingest_event(
                pool,
                platform,
                endpoint,
                subject,
                observed_count=observed_count,
                stored_count=stored_count,
                metadata=metadata,
            )
    except TimeoutError:
        log = logger.debug if endpoint == "browser_heartbeat" else logger.warning
        log(
            "browser ingest telemetry timed out after %.2fs platform=%s endpoint=%s subject=%s",
            BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS,
            platform,
            endpoint,
            subject,
        )
    except Exception:
        logger.debug(
            "browser ingest telemetry insert failed platform=%s endpoint=%s subject=%s",
            platform,
            endpoint,
            subject,
            exc_info=True,
        )


async def _write_browser_ingest_event(
    pool,
    platform: str,
    endpoint: str,
    subject: str | None = None,
    *,
    observed_count: int = 0,
    stored_count: int = 0,
    metadata: dict | None = None,
) -> None:
    observed = max(0, int(observed_count or 0))
    stored = max(0, int(stored_count or 0))
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO browser_ingest_events
              (platform, endpoint, subject, observed_count, stored_count, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            platform,
            endpoint,
            subject,
            observed,
            stored,
            json.dumps(metadata or {}),
        )
        if _browser_event_marks_source_success(platform, endpoint, observed, stored, metadata):
            await conn.execute(
                """
                INSERT INTO source_health
                  (source, status, last_success_at, last_error, updated_at)
                VALUES ($1, 'running', NOW(), NULL, NOW())
                ON CONFLICT (source) DO UPDATE SET
                  status = 'running',
                  last_success_at = NOW(),
                  last_error = NULL,
                  updated_at = NOW()
                """,
                platform,
            )


def _browser_event_marks_source_success(
    platform: str,
    endpoint: str,
    observed_count: int,
    stored_count: int,
    metadata: dict | None = None,
) -> bool:
    if not platform or platform == "bridge" or endpoint == "browser_heartbeat":
        return False
    if observed_count > 0 or stored_count > 0:
        return True
    if not isinstance(metadata, dict):
        return False
    probe_reason = str(metadata.get("probe_reason") or "").strip()
    non_progress_probes = {
        "manual_backend_probe",
        "forced_recovery_started",
        "recoverable_error_shell",
    }
    return bool(probe_reason and probe_reason not in non_progress_probes)


async def _ingest(app, platform, body):
    username = body.get("username") or "unknown"
    items = body.get("items") or []
    if items:
        task = asyncio.create_task(_drain(app, platform, username, items, body.get("extension_version")))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)
    elif body.get("record_empty"):
        meta = {}
        if body.get("extension_version"):
            meta["extension_version"] = body.get("extension_version")
        if body.get("probe_reason"):
            meta["probe_reason"] = body.get("probe_reason")
        if isinstance(body.get("probe_meta"), dict):
            meta["probe_meta"] = body.get("probe_meta")
        await _record_browser_ingest_event(
            app["pool"],
            platform,
            "media",
            username,
            observed_count=0,
            stored_count=0,
            metadata=meta or None,
        )
    return {"accepted": len(items), "platform": platform}


async def _ingest_uploaded_media(app, platform, body):
    username = body.get("username") or "unknown"
    item = body.get("item") or {}
    if not isinstance(item, dict):
        item = {}
    item = dict(item)
    if body.get("data_b64") and not item.get("data_b64"):
        item["data_b64"] = body.get("data_b64")
    if body.get("mime_type") and not item.get("mime_type"):
        item["mime_type"] = body.get("mime_type")
    if body.get("url") and not item.get("url"):
        item["url"] = body.get("url")
    extension_version = body.get("extension_version")
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    item["meta"] = {
        **meta,
        "extension_version": extension_version,
        "browser_upload": True,
        "browser_upload_size": body.get("file_size"),
        "browser_upload_mime_type": body.get("mime_type"),
    }
    reject_stats: dict = {}
    upload_sem = app.get("upload_sem") or app.get("sem")
    if upload_sem is None:
        upload_sem = asyncio.Semaphore(SOCIAL_INGEST_UPLOAD_CONCURRENCY)
        app["upload_sem"] = upload_sem
    async with upload_sem:
        saved = await _download_and_save(app["pool"], app["session"], platform, username, item, reject_stats)
    await _record_browser_media_candidate(
        app["pool"],
        platform,
        username,
        item,
        ingest_mode="browser_upload",
        saved=bool(saved),
        reject_stats=reject_stats,
        extension_version=extension_version,
    )
    dedupe_reasons = {"duplicate_content_id", "duplicate_sha256"}
    deduped = any(_reject_count(reject_stats.get(reason)) > 0 for reason in dedupe_reasons)
    accepted = bool(saved or deduped)
    reason = None
    if not accepted and reject_stats:
        reason = max(reject_stats.items(), key=lambda kv: _reject_count(kv[1]))[0]
    event_meta = {
        "extension_version": extension_version,
        "ingest_mode": "browser_upload",
        "accepted": accepted,
        "saved": bool(saved),
        "deduped": deduped,
    }
    if item.get("content_id"):
        event_meta["content_id"] = str(item.get("content_id"))[:200]
    if body.get("file_size") is not None:
        event_meta["file_size"] = body.get("file_size")
    if body.get("mime_type"):
        event_meta["mime_type"] = body.get("mime_type")
    if reason:
        event_meta["reason"] = reason
    if reject_stats:
        event_meta["reject_stats"] = reject_stats
    await _record_browser_ingest_event(
        app["pool"],
        platform,
        "media",
        username,
        observed_count=1,
        stored_count=1 if accepted else 0,
        metadata=event_meta,
    )
    logger.info(
        "browser-upload[%s] %s: accepted=%d saved=%d deduped=%d reason=%s",
        platform,
        username,
        1 if accepted else 0,
        1 if saved else 0,
        1 if deduped else 0,
        reason or "ok",
    )
    return {
        "accepted": 1 if accepted else 0,
        "stored": 1 if accepted else 0,
        "saved": 1 if saved else 0,
        "deduped": deduped,
        "platform": platform,
        "reason": reason,
        "reject_stats": reject_stats,
    }


def _queued_browser_upload_response(platform: str, body: dict) -> dict:
    item = body.get("item") if isinstance(body.get("item"), dict) else {}
    return {
        "accepted": 1,
        "stored": 0,
        "saved": 0,
        "deduped": False,
        "queued": True,
        "platform": platform,
        "content_id": str(item.get("content_id") or "")[:200] if item else None,
    }


async def _record_browser_media_candidate_batch(app, platform, username, raw_items, extension_version):
    recorded = 0
    for entry in raw_items[:500]:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item") if isinstance(entry.get("item"), dict) else entry
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        if not isinstance(item, dict):
            continue
        await _record_browser_media_candidate(
            app["pool"],
            platform,
            username,
            item,
            ingest_mode=str(entry.get("ingest_mode") or "browser_upload")[:40],
            saved=bool(result.get("saved") or result.get("stored")),
            reject_stats=result.get("reject_stats") if isinstance(result.get("reject_stats"), dict) else None,
            extension_version=extension_version,
            browser_result=result,
        )
        recorded += 1
    if recorded:
        logger.info("browser media candidate ledger[%s] %s: recorded=%d", platform, username, recorded)


async def browser_media_candidates(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    username = body.get("username") or "unknown"
    extension_version = body.get("extension_version")
    raw_items = body.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    queued = min(len(raw_items), 500)
    if queued:
        _schedule_app_task(
            request.app,
            _record_browser_media_candidate_batch(
                request.app,
                platform,
                username,
                raw_items,
                extension_version,
            ),
            "browser_media_candidate_batch",
        )
    return _cors(web.json_response({"ok": True, "queued": queued, "platform": platform}))


async def tiktok_revisit_target(request):
    try:
        max_attempts = max(1, int(os.getenv("TIKTOK_BROWSER_REVISIT_MAX_ATTEMPTS", "5")))
    except (TypeError, ValueError):
        max_attempts = 5
    claim_timeout = TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS
    claim_hold = TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS
    try:
        async with request.app["pool"].acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH picked AS (
                  SELECT id, status AS previous_status
                  FROM tiktok_browser_revisit_queue
                  WHERE (
                      (status IN ('pending', 'failed') AND next_visit_at <= now())
                      OR (
                        status = 'claimed'
                        AND COALESCE(last_attempt_at, updated_at, created_at)
                            <= now() - ($2::int * interval '1 second')
                      )
                    )
                    AND attempts < $1
                  ORDER BY
                    CASE WHEN status = 'claimed' THEN 0 ELSE 1 END,
                    priority DESC,
                    next_visit_at ASC,
                    created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE tiktok_browser_revisit_queue q
                SET status = 'claimed',
                    attempts = q.attempts + 1,
                    last_attempt_at = now(),
                    next_visit_at = now() + ($3::int * interval '1 second'),
                    metadata = q.metadata || jsonb_build_object(
                      'last_claim_previous_status', picked.previous_status,
                      'last_claimed_at', now()
                    ),
                    updated_at = now()
                FROM picked
                WHERE q.id = picked.id
                RETURNING q.content_id, q.username, q.post_url, q.source_url,
                          q.reason, q.priority, q.attempts, picked.previous_status,
                          q.metadata
                """,
                max_attempts,
                claim_timeout,
                claim_hold,
            )
        if not row:
            return _cors(web.json_response({"ok": True, "target": None}))
        target = dict(row)
        if isinstance(target.get("metadata"), str):
            try:
                target["metadata"] = json.loads(target["metadata"])
            except Exception:
                target["metadata"] = {"raw": target["metadata"]}
        if target.get("metadata") is None:
            target["metadata"] = {}
        return _cors(web.json_response({"ok": True, "target": target}, dumps=lambda v: json.dumps(v, default=str)))
    except Exception as exc:
        logger.debug("tiktok revisit target claim failed", exc_info=True)
        return _cors(web.json_response({"ok": False, "target": None, "error": str(exc)[:300]}, status=500))


async def tiktok_revisit_result(request):
    body = await _safe_json(request)
    content_id = str(body.get("content_id") or "").strip()
    if not content_id:
        return _cors(web.json_response({"ok": False, "error": "missing content_id"}, status=400))
    raw_status = str(body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or raw_status or "unknown")[:300]
    success = raw_status in {"success", "ok", "stored", "completed"}
    unavailable = raw_status in {"unavailable", "private", "deleted", "no_media"}
    status = "completed" if success else ("unavailable" if unavailable else "failed")
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE tiktok_browser_revisit_queue
                SET status = $2,
                    reason = COALESCE($3, reason),
                    last_success_at = CASE WHEN $2 = 'completed' THEN now() ELSE last_success_at END,
                    next_visit_at = CASE
                      WHEN $2 = 'completed' THEN next_visit_at
                      WHEN $2 = 'unavailable' THEN now() + interval '7 days'
                      ELSE now() + (LEAST(3600, GREATEST(120, attempts * 300)) * interval '1 second')
                    END,
                    metadata = metadata || $4::jsonb,
                    updated_at = now()
                WHERE content_id = $1
                """,
                content_id,
                status,
                reason,
                json.dumps(
                    {
                        "last_result": {
                            "status": raw_status or status,
                            "reason": body.get("reason"),
                            "stored": body.get("stored"),
                            "observed": body.get("observed"),
                            "extension_version": body.get("extension_version"),
                            "reported_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    default=str,
                ),
            )
        return _cors(web.json_response({"ok": True, "status": status}))
    except Exception as exc:
        logger.debug("tiktok revisit result update failed content_id=%s", content_id, exc_info=True)
        return _cors(web.json_response({"ok": False, "error": str(exc)[:300]}, status=500))


def _browser_revisit_platform(value: str | None) -> str | None:
    platform = _norm_platform(value)
    if platform in {"x", "facebook", "threads", "lemon8"}:
        return platform
    return None


async def browser_revisit_target(request):
    platform = _browser_revisit_platform(request.query.get("platform"))
    if not platform:
        return _cors(web.json_response({"ok": False, "target": None, "error": "unsupported platform"}, status=400))
    try:
        max_attempts = max(1, int(os.getenv("BROWSER_MEDIA_REVISIT_MAX_ATTEMPTS", "5")))
    except (TypeError, ValueError):
        max_attempts = 5
    claim_timeout = TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS
    claim_hold = TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS
    try:
        async with request.app["pool"].acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH picked AS (
                  SELECT id, status AS previous_status
                  FROM browser_media_revisit_queue
                  WHERE platform = $1
                    AND (
                      (status IN ('pending', 'failed') AND next_visit_at <= now())
                      OR (
                        status = 'claimed'
                        AND COALESCE(last_attempt_at, updated_at, created_at)
                            <= now() - ($3::int * interval '1 second')
                      )
                    )
                    AND attempts < $2
                  ORDER BY
                    CASE WHEN status = 'claimed' THEN 0 ELSE 1 END,
                    priority DESC,
                    next_visit_at ASC,
                    created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE browser_media_revisit_queue q
                SET status = 'claimed',
                    attempts = q.attempts + 1,
                    last_attempt_at = now(),
                    next_visit_at = now() + ($4::int * interval '1 second'),
                    metadata = q.metadata || jsonb_build_object(
                      'last_claim_previous_status', picked.previous_status,
                      'last_claimed_at', now()
                    ),
                    updated_at = now()
                FROM picked
                WHERE q.id = picked.id
                RETURNING q.platform, q.content_id, q.username, q.post_url, q.source_url,
                          q.reason, q.priority, q.attempts, picked.previous_status,
                          q.metadata
                """,
                platform,
                max_attempts,
                claim_timeout,
                claim_hold,
            )
        if not row:
            return _cors(web.json_response({"ok": True, "target": None}))
        target = dict(row)
        if isinstance(target.get("metadata"), str):
            try:
                target["metadata"] = json.loads(target["metadata"])
            except Exception:
                target["metadata"] = {"raw": target["metadata"]}
        if target.get("metadata") is None:
            target["metadata"] = {}
        return _cors(web.json_response({"ok": True, "target": target}, dumps=lambda v: json.dumps(v, default=str)))
    except Exception as exc:
        logger.debug("browser media revisit target claim failed platform=%s", platform, exc_info=True)
        return _cors(web.json_response({"ok": False, "target": None, "error": str(exc)[:300]}, status=500))


async def browser_revisit_result(request):
    body = await _safe_json(request)
    platform = _browser_revisit_platform(body.get("platform"))
    content_id = str(body.get("content_id") or "").strip()
    if not platform:
        return _cors(web.json_response({"ok": False, "error": "unsupported platform"}, status=400))
    if not content_id:
        return _cors(web.json_response({"ok": False, "error": "missing content_id"}, status=400))
    raw_status = str(body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or raw_status or "unknown")[:300]
    success = raw_status in {"success", "ok", "stored", "completed"}
    unavailable = raw_status in {"unavailable", "private", "deleted", "no_media", "missing_revisit_url"}
    status = "completed" if success else ("unavailable" if unavailable else "failed")
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE browser_media_revisit_queue
                SET status = $3,
                    reason = COALESCE($4, reason),
                    last_success_at = CASE WHEN $3 = 'completed' THEN now() ELSE last_success_at END,
                    next_visit_at = CASE
                      WHEN $3 = 'completed' THEN next_visit_at
                      WHEN $3 = 'unavailable' THEN now() + interval '7 days'
                      ELSE now() + (LEAST(3600, GREATEST(120, attempts * 300)) * interval '1 second')
                    END,
                    metadata = metadata || $5::jsonb,
                    updated_at = now()
                WHERE platform = $1 AND content_id = $2
                """,
                platform,
                content_id,
                status,
                reason,
                json.dumps(
                    {
                        "last_result": {
                            "status": raw_status or status,
                            "reason": body.get("reason"),
                            "stored": body.get("stored"),
                            "observed": body.get("observed"),
                            "extension_version": body.get("extension_version"),
                            "reported_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    default=str,
                ),
            )
        return _cors(web.json_response({"ok": True, "status": status}))
    except Exception as exc:
        logger.debug(
            "browser media revisit result update failed platform=%s content_id=%s",
            platform,
            content_id,
            exc_info=True,
        )
        return _cors(web.json_response({"ok": False, "error": str(exc)[:300]}, status=500))


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{2,30}$")
_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,20}$")


def _derive_handle_from_caption(caption):
    """Recover a threads/facebook author handle when the extension left
    author_username empty but dumped the whole post block into the caption
    ("<handle>\\n<relative time>\\n<caption>"). Returns the first line only when
    it's a clean handle (no spaces) — this rejects repost headers like
    "x reposted 1h ago". Mirrors tmp/backfill_threads_author.py so new posts
    self-heal instead of landing with NULL author. Returns None if unsure."""
    if not caption:
        return None
    first = caption.split("\n", 1)[0].strip()
    return first if _HANDLE_RE.match(first) else None


def _ig_author_uid(ppid: str) -> str | None:
    """Recover the Instagram author's numeric user id from a platform_post_id of
    the shape `<media_id>_<author_uid>` (IDENTITY_KEYS.md). Returns None when the
    id has no embedded uid (a bare media id / shortcode). This trailing id equals
    instagram_profiles.platform_user_id."""
    if not ppid:
        return None
    uid = ppid.rsplit("_", 1)[-1] if "_" in ppid else ""
    return uid if uid.isdigit() else None


def _x_handle(value) -> str | None:
    handle = str(value or "").strip().lstrip("@").lower()
    return handle if _X_HANDLE_RE.match(handle) else None


_LEMON8_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
_TIKTOK_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,30}$")


def _tiktok_handle(value) -> str | None:
    handle = str(value or "").strip().lstrip("@")
    if not handle or "/" in handle or "?" in handle or "#" in handle:
        return None
    lowered = handle.lower()
    if lowered in {
        "foryou", "following", "explore", "search", "live", "messages",
        "login", "signup", "upload", "discover", "tag", "music", "video",
    }:
        return None
    return handle if _TIKTOK_HANDLE_RE.match(handle) else None


def _owner_account_for_follow(platform, context, owner):
    ctx_l = (context or "").lower()
    if ctx_l not in ("follow", "follower"):
        return None, None
    owner_account = None
    if isinstance(owner, dict):
        owner_account = (owner.get("username") or owner.get("id") or "") or None
    elif isinstance(owner, str):
        owner_account = owner.strip().lstrip("@") or None
    if owner_account:
        owner_account = str(owner_account).strip().lstrip("@") or None
    if not owner_account and platform == "tiktok":
        owner_account = TIKTOK_FOLLOW_OWNER_FALLBACK
    direction = "follower" if ctx_l == "follower" else "following"
    return owner_account, direction


async def _enqueue_tiktok_profile_targets(
    conn,
    handles,
    source: str,
    priority: int,
    metadata: dict | None = None,
) -> int:
    if not handles:
        return 0
    added = 0
    for raw in handles:
        handle = _tiktok_handle(raw)
        if not handle:
            continue
        try:
            res = await conn.execute(
                """
                INSERT INTO tiktok_spider_queue
                    (platform_user_id, username, source, priority, status, collected_at)
                VALUES ($1, $1, $2, $3, 'pending', now())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    username = COALESCE(tiktok_spider_queue.username, EXCLUDED.username),
                    source = CASE
                        WHEN tiktok_spider_queue.source = 'manual'
                            THEN tiktok_spider_queue.source
                        ELSE EXCLUDED.source
                    END,
                    priority = LEAST(
                        COALESCE(tiktok_spider_queue.priority, EXCLUDED.priority),
                        EXCLUDED.priority
                    ),
                    status = CASE
                        WHEN tiktok_spider_queue.status IN ('completed', 'failed')
                            THEN 'pending'
                        ELSE tiktok_spider_queue.status
                    END,
                    collected_at = now()
                """,
                handle, source[:50], int(priority),
            )
            if res.endswith("1"):
                added += 1
        except Exception:
            logger.debug(
                "enqueue tiktok profile target failed %s metadata=%s",
                handle, metadata,
                exc_info=True,
            )
    return added


def _lemon8_handle(value) -> str | None:
    handle = str(value or "").strip().lstrip("@")
    if not handle or "/" in handle or "?" in handle or "#" in handle:
        return None
    lowered = handle.lower()
    if lowered in {"feed", "foryou", "fashion", "beauty", "food", "travel", "home", "topic", "search"}:
        return None
    return handle if _LEMON8_HANDLE_RE.match(handle) else None


async def _enqueue_lemon8_profile_targets(
    conn,
    handles,
    source: str,
    priority: int,
    metadata: dict | None = None,
) -> int:
    if not handles:
        return 0
    added = 0
    for raw in handles:
        handle = _lemon8_handle(raw)
        if not handle:
            continue
        try:
            res = await conn.execute(
                """
                INSERT INTO lemon8_spider_queue
                    (platform_user_id, source, priority, status, collected_at)
                VALUES ($1, $2, $3, 'pending', now())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    source = CASE
                        WHEN lemon8_spider_queue.source = 'manual'
                            THEN lemon8_spider_queue.source
                        ELSE EXCLUDED.source
                    END,
                    priority = LEAST(
                        COALESCE(lemon8_spider_queue.priority, EXCLUDED.priority),
                        EXCLUDED.priority
                    ),
                    status = CASE
                        WHEN lemon8_spider_queue.status IN ('completed', 'failed')
                            THEN 'pending'
                        ELSE lemon8_spider_queue.status
                    END,
                    collected_at = now()
                """,
                handle, source[:50], int(priority),
            )
            if res.endswith("1"):
                added += 1
        except Exception:
            logger.debug(
                "enqueue lemon8 profile target failed %s metadata=%s",
                handle, metadata,
                exc_info=True,
            )
    return added


def _x_post_url(author: str | None, post_id: str | None, metadata: dict | None = None) -> str | None:
    metadata = metadata if isinstance(metadata, dict) else {}
    for key in ("verify_url", "url", "post_url"):
        url = metadata.get(key)
        if isinstance(url, str) and url.startswith(("https://x.com/", "https://twitter.com/")):
            return url
    author_h = _x_handle(author)
    if author_h and post_id:
        return f"https://x.com/{author_h}/status/{post_id}"
    return None


async def _enqueue_x_profile_targets(conn, handles, source: str, priority: int, metadata: dict | None = None) -> int:
    if not handles:
        return 0
    added = 0
    meta_json = json.dumps(metadata or {})
    for raw in handles:
        handle = _x_handle(raw)
        if not handle:
            continue
        try:
            res = await conn.execute(
                """
                INSERT INTO x_profile_targets (username, source, priority, status, metadata)
                VALUES ($1, $2, $3, 'pending', $4::jsonb)
                ON CONFLICT (username) DO UPDATE SET
                    priority = GREATEST(x_profile_targets.priority, EXCLUDED.priority),
                    source = CASE
                        WHEN x_profile_targets.source = 'manual' THEN x_profile_targets.source
                        ELSE EXCLUDED.source
                    END,
                    status = CASE
                        WHEN x_profile_targets.status IN ('unavailable', 'failed') THEN 'pending'
                        ELSE x_profile_targets.status
                    END,
                    next_visit_at = LEAST(x_profile_targets.next_visit_at, now()),
                    metadata = x_profile_targets.metadata || EXCLUDED.metadata,
                    updated_at = now()
                """,
                handle, source[:64], int(priority), meta_json,
            )
            if res.endswith("1"):
                added += 1
        except Exception:
            logger.debug("enqueue x profile target failed %s", handle, exc_info=True)
    return added


async def _record_x_edge(
    conn,
    *,
    source_username: str | None,
    target_username: str,
    post_id: str | None,
    edge_type: str,
    strength: int,
    evidence_url: str | None,
    metadata: dict | None = None,
) -> None:
    target = _x_handle(target_username)
    if not target:
        return
    source = _x_handle(source_username) if source_username else None
    try:
        await conn.execute(
            """
            INSERT INTO x_edges
                (source_username, target_username, post_id, edge_type, strength, evidence_url, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT DO NOTHING
            """,
            source,
            target,
            str(post_id) if post_id else None,
            str(edge_type or "seen")[:64],
            int(strength),
            evidence_url,
            json.dumps(metadata or {}),
        )
    except Exception:
        logger.debug("record x edge failed %s -> %s", source, target, exc_info=True)


async def _record_x_post_graph(conn, post_id: str, post: dict) -> None:
    author = _x_handle(post.get("author_username"))
    mentions = [_x_handle(m) for m in (post.get("mentions") or [])]
    mentions = [m for m in mentions if m and m != author]
    metadata = post.get("metadata") if isinstance(post.get("metadata"), dict) else {}
    evidence_url = _x_post_url(author, post_id, metadata)
    enqueue = []
    if author:
        enqueue.append(author)
        await _record_x_edge(
            conn,
            source_username=None,
            target_username=author,
            post_id=post_id,
            edge_type="seen_author",
            strength=20,
            evidence_url=evidence_url,
            metadata={"source": "x_post_ingest"},
        )
    enqueue.extend(mentions)
    if enqueue:
        await _enqueue_x_profile_targets(
            conn,
            enqueue,
            "x_post",
            75,
            {"source": "x_posts_ingest", "post_id": post_id},
        )
    for mention in mentions:
        await _record_x_edge(
            conn,
            source_username=author,
            target_username=mention,
            post_id=post_id,
            edge_type="mention",
            strength=70,
            evidence_url=evidence_url,
            metadata={"source": "x_post_mentions"},
        )
    relation_specs = (
        ("reply", post.get("in_reply_to_screen_name") or metadata.get("in_reply_to_screen_name"), 80),
        ("quote", post.get("quoted_author_username") or metadata.get("quoted_author_username"), 75),
        ("repost", post.get("retweeted_author_username") or metadata.get("retweeted_author_username"), 60),
    )
    for edge_type, target, strength in relation_specs:
        target_h = _x_handle(target)
        if target_h:
            await _enqueue_x_profile_targets(
                conn,
                [target_h],
                f"x_{edge_type}",
                max(70, strength),
                {"source": "x_posts_relation", "post_id": post_id, "edge_type": edge_type},
            )
            await _record_x_edge(
                conn,
                source_username=author,
                target_username=target_h,
                post_id=post_id,
                edge_type=edge_type,
                strength=strength,
                evidence_url=evidence_url,
                metadata={"source": "x_post_relation"},
            )


async def _ensure_ig_profile(conn, ppid: str, p: dict) -> str | None:
    """Resolve (or create a minimal stub for) the instagram_profiles row for a
    post's author, keyed on the numeric uid embedded in platform_post_id, and
    return its id (uuid). This is the root-cause fix for the recurring NULL-FK
    bug: this ingest path historically OMITTED profile_id, so ~19k posts landed
    with NULL profile_id and were invisible to every consumer that joins
    instagram_posts.profile_id -> instagram_profiles.id (analyzer timelines,
    /geo). We now attach the FK at insert time.

    A stub is created (ON CONFLICT DO NOTHING) when the author profile hasn't
    been collected yet, so spidered-from-post authors still get a stable FK; the
    real profile collector later enriches the same row (unique platform_user_id).
    Returns None only when the uid can't be recovered (bare media id) — those
    posts remain genuinely unattributable, same as before."""
    uid = _ig_author_uid(ppid)
    if not uid:
        return None
    row = await conn.fetchrow(
        "SELECT id FROM instagram_profiles WHERE platform_user_id = $1", uid
    )
    if row:
        return str(row["id"])
    # No profile row yet — create a minimal stub keyed on the uid. Use any author
    # username the extension happened to send; the profile collector fills the
    # rest later (matched on the unique platform_user_id).
    username = (p.get("author_username") or p.get("username") or None)
    new_id = await conn.fetchval(
        """
        INSERT INTO instagram_profiles (platform_user_id, username)
        VALUES ($1, $2)
        ON CONFLICT (platform_user_id) DO UPDATE SET
            username = COALESCE(instagram_profiles.username, EXCLUDED.username)
        RETURNING id
        """,
        uid, username,
    )
    return str(new_id) if new_id else None


# ---------------------------------------------------------------------------
# structured post + comment metadata (captions/likes/comments threads)
# ---------------------------------------------------------------------------
async def _save_posts(pool, platform, posts) -> int:
    """Upsert post metadata (caption, engagement, hashtags, mentions) into the
    platform's posts table. instagram_posts has the richest schema; threads_posts
    and facebook_posts mirror the essentials. tiktok/lemon8 posts are owned by
    their headless collectors, so we don't double-write them here."""
    if platform not in ("instagram", "threads", "facebook", "x"):
        return 0
    n = 0
    async with pool.acquire() as conn:
        for p in posts:
            ppid = str(p.get("platform_post_id") or "")
            if not ppid:
                continue
            # stamp a canonical, openable verification URL + correct shortcode into
            # metadata (overrides any wrong/long "code" the extension may have sent).
            vurl = _verify_url(platform, ppid)
            if vurl:
                _md = p.get("metadata")
                if not isinstance(_md, dict):
                    _md = {}
                _md["verify_url"] = vurl
                _md["shortcode"] = vurl.rstrip("/").rsplit("/", 1)[-1]
                p["metadata"] = _md
            try:
                if platform == "instagram":
                    # Root-cause fix (NULL-FK bug): resolve/create the author
                    # profile and attach profile_id at insert time so the post is
                    # never orphaned. COALESCE on UPDATE so a later collection that
                    # already set profile_id is never clobbered back to NULL.
                    profile_id = await _ensure_ig_profile(conn, ppid, p)
                    await conn.execute(
                        """
                        INSERT INTO instagram_posts
                          (id, platform_post_id, profile_id, media_type, caption, hashtags, mentions,
                           location_name, location_lat, location_lng, music_title, music_author,
                           likes_count, comments_count, video_duration,
                           platform_created_at, collected_at, metadata)
                        VALUES (gen_random_uuid(),$1,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                                to_timestamp($15), now(), $16::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           profile_id=COALESCE(instagram_posts.profile_id, EXCLUDED.profile_id),
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, hashtags=EXCLUDED.hashtags,
                           mentions=EXCLUDED.mentions, location_name=EXCLUDED.location_name,
                           location_lat=COALESCE(EXCLUDED.location_lat, instagram_posts.location_lat),
                           location_lng=COALESCE(EXCLUDED.location_lng, instagram_posts.location_lng),
                           music_title=COALESCE(EXCLUDED.music_title, instagram_posts.music_title),
                           music_author=COALESCE(EXCLUDED.music_author, instagram_posts.music_author),
                           collected_at=now(), metadata=EXCLUDED.metadata
                        """,
                        ppid, profile_id, p.get("media_type"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        p.get("location"), _num(p.get("location_lat")), _num(p.get("location_lng")),
                        p.get("music_title"), p.get("music_author"),
                        _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("video_duration")), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}),
                    )
                elif platform == "x":
                    await conn.execute(
                        """
                        INSERT INTO x_posts
                          (platform_post_id, author_username, caption, hashtags, mentions,
                           likes_count, comments_count, reposts_count, quote_count, views_count,
                           media_type, platform_created_at, collected_at, metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,to_timestamp($12),now(),$13::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count, reposts_count=EXCLUDED.reposts_count,
                           quote_count=EXCLUDED.quote_count, views_count=EXCLUDED.views_count,
                           hashtags=EXCLUDED.hashtags, mentions=EXCLUDED.mentions,
                           media_type=COALESCE(EXCLUDED.media_type, x_posts.media_type),
                           platform_created_at=COALESCE(EXCLUDED.platform_created_at, x_posts.platform_created_at),
                           collected_at=now(), metadata=x_posts.metadata || EXCLUDED.metadata
                        """,
                        ppid, p.get("author_username"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        _int(p.get("likes_count")), _int(p.get("comments_count")), _int(p.get("reposts_count")),
                        _int(p.get("quote_count")), _int(p.get("views_count")), p.get("media_type"),
                        _num(p.get("taken_at")), json.dumps(p.get("metadata") or {}),
                    )
                    await _record_x_post_graph(conn, ppid, p)
                else:  # threads / facebook
                    table = "threads_posts" if platform == "threads" else "facebook_posts"
                    extra_col = "reposts_count" if platform == "threads" else "shares_count"
                    # Self-heal: the threads/facebook extension scraper sometimes
                    # leaves author_username empty and puts "<handle>\n<time>\n..."
                    # in the caption. Recover the handle so the row isn't orphaned
                    # (was ~56% of threads posts). See _derive_handle_from_caption.
                    if not p.get("author_username"):
                        _h = _derive_handle_from_caption(p.get("caption"))
                        if _h:
                            p["author_username"] = _h
                    await conn.execute(
                        f"""
                        INSERT INTO {table}
                          (platform_post_id, author_username, caption, hashtags, mentions,
                           likes_count, comments_count, {extra_col}, media_type,
                           platform_created_at, collected_at, metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,to_timestamp($10),now(),$11::jsonb)
                        ON CONFLICT (platform_post_id) DO UPDATE SET
                           caption=EXCLUDED.caption, likes_count=EXCLUDED.likes_count,
                           comments_count=EXCLUDED.comments_count,
                           {extra_col}=EXCLUDED.{extra_col}, collected_at=now(),
                           metadata=EXCLUDED.metadata
                        """,
                        ppid, p.get("author_username"), p.get("caption"),
                        p.get("hashtags") or [], p.get("mentions") or [],
                        _int(p.get("likes_count")), _int(p.get("comments_count")),
                        _int(p.get("reposts_count") if platform == "threads" else p.get("shares_count")),
                        p.get("media_type"), _num(p.get("taken_at")),
                        json.dumps(p.get("metadata") or {}),
                    )
                n += 1
            except Exception:
                logger.debug("save post failed %s/%s", platform, ppid, exc_info=True)
    return n


async def _save_comments(pool, platform, post_pid, comments) -> int:
    """Upsert comment threads into instagram_comments, linked to their post."""
    if platform != "instagram":
        return 0
    n = 0
    async with pool.acquire() as conn:
        for c in comments:
            cpid = str(c.get("platform_comment_id") or "")
            if not cpid:
                continue
            try:
                await conn.execute(
                    """
                    INSERT INTO instagram_comments
                      (id, platform_comment_id, post_id, author_username, author_platform_id,
                       text, like_count, parent_comment_id, is_reply, platform_created_at, collected_at)
                    VALUES (gen_random_uuid(),$1,
                       (SELECT id FROM instagram_posts WHERE platform_post_id=$2),
                       $3,$4,$5,$6,$7,$8,to_timestamp($9),now())
                    ON CONFLICT (platform_comment_id) DO UPDATE SET
                       text = EXCLUDED.text, like_count = EXCLUDED.like_count, collected_at = now()
                    """,
                    cpid, str(post_pid or ""), c.get("author_username"), c.get("author_platform_id"),
                    c.get("text"), _int(c.get("like_count")), c.get("parent_comment_id"),
                    bool(c.get("is_reply")), _num(c.get("created_at")),
                )
                n += 1
            except Exception:
                logger.debug("save comment failed %s", cpid, exc_info=True)
    return n


# ---------------------------------------------------------------------------
# universal user registry — every user/id we encounter anywhere (follows graph,
# comment authors, tagged users, post authors, reactors) lands in social_users.
# ---------------------------------------------------------------------------
# A Threads handle IS the same Meta account's Instagram handle. So real people we
# see on Threads (your Following feed, comment authors, etc.) are scrapeable on
# Instagram — which IS target/profile-driven. Cross-seed them into the IG spider
# queue. Gated to non-"foryou" contexts so we don't import algorithmic brand spam.
async def _cross_seed_instagram(conn, usernames, source) -> int:
    clean = sorted({
        (uname or "").strip().lstrip("@")
        for uname in usernames
        if (uname or "").strip().lstrip("@") and len((uname or "").strip().lstrip("@")) <= 30
    })
    if not clean:
        return 0
    try:
        added = int(await conn.fetchval(
            """
            WITH input(username) AS (
                SELECT DISTINCT unnest($1::text[])
            ),
            inserted AS (
                INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                SELECT username, 1, $2
                FROM input
                ON CONFLICT (username) DO NOTHING
                RETURNING 1
            )
            SELECT count(*) FROM inserted
            """,
            clean,
            source,
        ) or 0)
    except Exception:
        logger.debug("cross-seed ig target batch failed", exc_info=True)
        added = 0
        for uname in clean:
            try:
                res = await conn.execute(
                    """
                    INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                    VALUES ($1, 1, $2)
                    ON CONFLICT (username) DO NOTHING
                    """,
                    uname, source,
                )
                if res.endswith("1"):
                    added += 1
            except Exception:
                logger.debug("cross-seed ig target failed %s", uname, exc_info=True)
    if added:
        logger.info("cross-seed instagram <- %s: +%d real handles", source, added)
    return added


_SOCIAL_USERS_UPSERT_SQL = """
INSERT INTO social_users (platform, uid, platform_user_id, username, display_name, profile_photo_url, contexts)
VALUES ($1,$2,$3,$4,$5,$6,$7)
ON CONFLICT (platform, uid) DO UPDATE SET
   last_seen = now(),
   times_seen = social_users.times_seen + 1,
   username = COALESCE(EXCLUDED.username, social_users.username),
   platform_user_id = COALESCE(EXCLUDED.platform_user_id, social_users.platform_user_id),
   display_name = COALESCE(EXCLUDED.display_name, social_users.display_name),
   profile_photo_url = COALESCE(EXCLUDED.profile_photo_url, social_users.profile_photo_url),
   contexts = (SELECT array_agg(DISTINCT c) FROM unnest(social_users.contexts || EXCLUDED.contexts) AS c)
"""

_FOLLOW_EDGES_UPSERT_SQL = """
INSERT INTO follow_edges
    (platform, owner_account, target_uid, direction, target_username, first_seen, last_seen)
VALUES ($1, $2, $3, $4, $5, now(), now())
ON CONFLICT (platform, owner_account, target_uid, direction) DO UPDATE SET
    last_seen = now(),
    target_username = COALESCE(EXCLUDED.target_username, follow_edges.target_username)
"""


async def _record_users(pool, platform, users, context, owner=None) -> int:
    if not users:
        return 0
    rows = []
    context_value = str(context or "seen")[:64]
    cross_handles = []  # threads handles to push into the IG spider queue
    # PER-ACCOUNT follow graph: when the extension sends an owner (the logged-in
    # account) with a follow/follower context, also record a directional edge in
    # follow_edges so each of your accounts' graphs is distinct (multi-account).
    owner_account, direction = _owner_account_for_follow(platform, context, owner)
    owner_account = str(owner_account) if owner_account else None
    for u in users:
        if isinstance(u, str):
            u = {"username": u}
        if not isinstance(u, dict):
            continue
        user_id = u.get("user_id") or u.get("pk") or u.get("id")
        username = (u.get("username") or "").strip().lstrip("@") or None
        uid = str(user_id) if user_id else username
        if not uid:
            continue
        rows.append({
            "uid": uid,
            "user_id": str(user_id) if user_id else None,
            "username": username,
            "display_name": u.get("display_name") or u.get("full_name") or None,
            "profile_photo_url": (
                u.get("profile_pic_url")
                or u.get("profile_photo_url")
                or u.get("avatar_url")
                or None
            ),
        })
    if not rows:
        return 0

    user_args = [
        (
            platform,
            r["uid"],
            r["user_id"],
            r["username"],
            r["display_name"],
            r["profile_photo_url"],
            [context_value],
        )
        for r in rows
    ]
    edge_args = [
        (platform, owner_account, r["uid"], direction, r["username"])
        for r in rows
        if owner_account and direction
    ]
    n = 0
    async with pool.acquire() as conn:
        successful_rows = rows
        try:
            await conn.executemany(_SOCIAL_USERS_UPSERT_SQL, user_args)
            n = len(rows)
        except Exception:
            logger.debug("record users batch failed %s/%d", platform, len(rows), exc_info=True)
            successful_rows = []
            for r, args in zip(rows, user_args, strict=False):
                try:
                    await conn.execute(_SOCIAL_USERS_UPSERT_SQL, *args)
                    successful_rows.append(r)
                    n += 1
                except Exception:
                    logger.debug("record user failed %s", r["uid"], exc_info=True)
        if edge_args:
            try:
                await conn.executemany(_FOLLOW_EDGES_UPSERT_SQL, edge_args)
            except Exception:
                logger.debug("record follow edges batch failed %s/%d", platform, len(edge_args), exc_info=True)
                for args in edge_args:
                    try:
                        await conn.execute(_FOLLOW_EDGES_UPSERT_SQL, *args)
                    except Exception:
                        logger.debug("record follow edge failed %s", args[2], exc_info=True)
        for r in successful_rows:
            username = r["username"]
            try:
                if platform == "x" and username:
                    ctx = context_value.lower()
                    if ctx == "follow":
                        source, priority = "following", 95
                    elif ctx == "follower":
                        source, priority = "follower", 85
                    elif ctx == "author":
                        source, priority = "author", 75
                    else:
                        source, priority = ctx[:64], 60
                    await _enqueue_x_profile_targets(
                        conn,
                        [username],
                        source,
                        priority,
                        {
                            "source": "social_users",
                            "context": context_value,
                            "owner_account": owner_account,
                        },
                    )
                if platform == "tiktok" and username:
                    ctx = context_value.lower()
                    if ctx in {"follow", "following"}:
                        source, priority = "following", 2
                    elif ctx == "follower":
                        source, priority = "follower", 3
                    elif ctx in {"author", "profile", "post"}:
                        source, priority = ctx, 3
                    else:
                        source, priority = ctx[:50], 4
                    await _enqueue_tiktok_profile_targets(
                        conn,
                        [username],
                        source,
                        priority,
                        {
                            "source": "social_users",
                            "context": context_value,
                            "owner_account": owner_account,
                        },
                    )
                if platform == "lemon8" and username:
                    ctx = context_value.lower()
                    if ctx in {"author", "profile", "post"}:
                        source, priority = ctx, 2
                    elif ctx in {"follow", "following"}:
                        source, priority = "following", 2
                    elif ctx == "follower":
                        source, priority = "follower", 3
                    else:
                        source, priority = ctx[:50], 4
                    await _enqueue_lemon8_profile_targets(
                        conn,
                        [username],
                        source,
                        priority,
                        {
                            "source": "social_users",
                            "context": context_value,
                            "owner_account": owner_account,
                        },
                    )
                if platform == "threads" and username and context_value.lower() != "foryou":
                    cross_handles.append(username)
            except Exception:
                logger.debug("post-user enqueue failed %s", r["uid"], exc_info=True)
        if cross_handles:
            await _cross_seed_instagram(conn, cross_handles, "threads:" + (context_value or "feed"))
    return n


async def users_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    n = await _record_users(request.app["pool"], platform, body.get("users") or [],
                            body.get("context") or "seen", owner=body.get("owner"))
    return _cors(web.json_response({"recorded": n}))


async def _record_dms(pool, threads, owner) -> int:
    """Persist Instagram DM threads + messages observed by the extension."""
    if not threads:
        return 0
    owner_id = str((owner or {}).get("id") or "")
    owner_acct = (owner or {}).get("username") or owner_id or None
    n = 0
    async with pool.acquire() as conn:
        for th in threads:
            if not isinstance(th, dict):
                continue
            tid = str(th.get("thread_id") or th.get("thread_v2_id") or "")
            if not tid:
                continue
            users = th.get("users") or []
            umap = {str(u.get("pk") or u.get("id") or ""): (u.get("username") or None) for u in users if isinstance(u, dict)}
            parts = [v or k for k, v in umap.items()]
            try:
                await conn.execute(
                    "INSERT INTO instagram_dm_thread (thread_id, title, participants, owner_account, last_activity, updated_at) "
                    "VALUES ($1,$2,$3,$4, now(), now()) "
                    "ON CONFLICT (thread_id) DO UPDATE SET title=COALESCE(EXCLUDED.title, instagram_dm_thread.title), "
                    "participants=EXCLUDED.participants, owner_account=COALESCE(EXCLUDED.owner_account, instagram_dm_thread.owner_account), updated_at=now()",
                    tid, th.get("thread_title") or th.get("title"), parts, owner_acct)
            except Exception:
                logger.debug("dm thread upsert failed %s", tid, exc_info=True)
            for it in (th.get("items") or []):
                if not isinstance(it, dict):
                    continue
                mid = str(it.get("item_id") or it.get("id") or "")
                if not mid:
                    continue
                sender = str(it.get("user_id") or "")
                # text lives in .text (text items) or nested link/clip/etc — best-effort.
                txt = it.get("text")
                if not txt and isinstance(it.get("link"), dict):
                    txt = it["link"].get("text")
                ts = it.get("timestamp")
                try:
                    ts_s = float(ts) / 1_000_000 if ts and float(ts) > 1e14 else (float(ts) if ts else None)
                except Exception:
                    ts_s = None
                try:
                    await conn.execute(
                        "INSERT INTO instagram_dm (message_id, thread_id, sender_id, sender_username, text, item_type, \"timestamp\", is_from_me, owner_account, collected_at) "
                        "VALUES ($1,$2,$3,$4,$5,$6, CASE WHEN $7::float8 IS NULL THEN NULL ELSE to_timestamp($7) END, $8, $9, now()) "
                        "ON CONFLICT (message_id) DO NOTHING",
                        mid, tid, sender, umap.get(sender), txt, it.get("item_type"),
                        ts_s, bool(owner_id and sender == owner_id), owner_acct)
                    n += 1
                except Exception:
                    logger.debug("dm item upsert failed %s", mid, exc_info=True)
    return n


async def dms_handler(request):
    body = await _safe_json(request)
    pool = request.app["pool"]
    platform = _norm_platform(body.get("platform") or "instagram")
    await _archive_browser_capture(pool, platform, "dms", body)
    try:
        n = await _record_dms(pool, body.get("threads") or [], body.get("owner"))
        return _cors(web.json_response({"recorded": n}))
    except Exception:
        logger.exception("dms handler failed")
        return _cors(web.json_response({"recorded": 0, "error": "db"}, status=500))


async def _dm_probe_log_write(pool, platform, event_type, body, frame_size=None):
    """Best-effort write to dm_probe_log for the dashboard telemetry panel
    (P1.2). Swallows every error — telemetry must never break capture.
    frame_size is a separate arg because the sample handler already knows the
    decoded length, while probes pull it from body.get('frame_size').
    """
    if not pool:
        return
    try:
        size = frame_size
        if size is None:
            raw = body.get("frame_size")
            try:
                size = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                size = None
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dm_probe_log
                    (platform, event_type, url, transport, frame_kind,
                     frame_size, owner_account)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                str(platform)[:64] if platform else "unknown",
                event_type,
                (body.get("url") or None),
                (body.get("transport") or None),
                (body.get("frame_kind") or None),
                size,
                (body.get("owner") or body.get("owner_account") or None),
            )
    except Exception:
        logger.debug("dm_probe_log write failed", exc_info=True)


async def dm_hook_heartbeat_handler(request):
    """Heartbeat from the browser extension's DM WebSocket hook (P1.3).

    The hook lives inside inject.js and is passive/send-nothing (see #35/#38).
    If Instagram or TikTok update their bundle and break the wrapper we have
    no server-side signal today — samples just stop arriving and it's
    indistinguishable from "user isn't DMing". This endpoint records a beat
    per (platform, owner) so:
      (a) src/watchdog/freshness.py can alert on Telegram if the newest
          heartbeat per platform goes stale (no container restart possible —
          the hook lives in the browser), and
      (b) the dashboard telemetry panel can show extension_version and
          time-since-last-beat.
    Best-effort — failures are logged at DEBUG and don't affect return.
    """
    body = await _safe_json(request)
    platform = (body.get("platform") or "").strip()
    if not platform:
        return _cors(web.json_response({"ok": False, "error": "no_platform"}, status=400))
    owner = (body.get("owner") or body.get("owner_account") or "").strip()
    try:
        probes = int(body.get("probes_sent") or 0)
    except (TypeError, ValueError):
        probes = 0
    try:
        samples = int(body.get("samples_shipped") or 0)
    except (TypeError, ValueError):
        samples = 0
    ext_version = (body.get("extension_version") or None)
    ua = request.headers.get("User-Agent") or None

    pool = request.app.get("pool")
    if not pool:
        return _cors(web.json_response({"ok": True, "recorded": False, "telemetry_degraded": True}))
    try:
        async with asyncio.timeout(DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS):
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO dm_hook_heartbeat
                        (platform, owner_account, last_seen, probes_sent,
                         samples_shipped, extension_version, user_agent)
                    VALUES ($1, $2, now(), $3, $4, $5, $6)
                    ON CONFLICT (platform, owner_account) DO UPDATE SET
                        last_seen         = now(),
                        probes_sent       = EXCLUDED.probes_sent,
                        samples_shipped   = EXCLUDED.samples_shipped,
                        extension_version = COALESCE(EXCLUDED.extension_version,
                                                     dm_hook_heartbeat.extension_version),
                        user_agent        = COALESCE(EXCLUDED.user_agent,
                                                     dm_hook_heartbeat.user_agent)
                    """,
                    platform[:64], owner[:128], probes, samples, ext_version, ua,
                )
    except TimeoutError:
        logger.info(
            "dm_hook_heartbeat write timed out after %.2fs platform=%s owner=%s",
            DM_HOOK_HEARTBEAT_WRITE_TIMEOUT_SECONDS,
            platform,
            owner,
        )
        return _cors(web.json_response({
            "ok": True,
            "recorded": False,
            "telemetry_degraded": True,
            "reason": "db_write_timeout",
        }))
    except Exception:
        logger.debug("dm_hook_heartbeat upsert failed", exc_info=True)
        return _cors(web.json_response({
            "ok": True,
            "recorded": False,
            "telemetry_degraded": True,
            "reason": "db_write_failed",
        }))
    return _cors(web.json_response({"ok": True, "recorded": True}))


async def dm_probe_handler(request):
    """One-time investigation probe (#38): the extension's observe-only hooks
    report the transport + format of each platform's DM channel so we can confirm
    the wire format before committing to a decoder/schema. Logged to stderr and
    (P1.2) to dm_probe_log for the dashboard telemetry panel.
    Confirmed so far: TikTok = binary protobuf over wss://im-ws-…/ws/v2; IG is
    binary MQTT over wss://edge-chat.instagram.com/chat — both reasons the
    fetch/XHR JSON observation path can't capture them.
    """
    body = await _safe_json(request)
    logger.info(
        "DM probe: platform=%s transport=%s kind=%s size=%s url=%s",
        body.get("platform"), body.get("transport"), body.get("frame_kind"),
        body.get("frame_size"), body.get("url"),
    )
    pool = request.app.get("pool")
    platform = body.get("platform") or "unknown"
    _schedule_app_task(
        request.app,
        _dm_probe_log_write(pool, platform, "probe", body),
        "dm_probe_log",
    )
    _schedule_app_task(
        request.app,
        _archive_browser_capture(pool, platform, "dm_probe", body),
        "dm_probe_archive",
    )
    return _cors(web.json_response({"ok": True}))


async def dm_sample_handler(request):
    """Save a raw DM-socket frame sample (base64) for decoder development (#35).
    Observe-only: these are bytes the page already received; we send nothing to
    the platform. Written to /tmp/dm_samples/<platform>_<n>.bin so the exact
    MQTT (IG) / protobuf (TikTok) payloads can be inspected offline.

    Rotation (P1.1): after each successful write we prune to the newest
    DM_SAMPLE_CAP_PER_PLATFORM files per platform (by mtime), so this dir
    can't grow unbounded on high-traffic sockets (TikTok emitted +6/hour in
    passive tests). The filename index is derived from `max existing index +
    1`, NOT the file count, so pruning never causes a fresh write to reuse
    an index that still exists on disk. Concurrent writes race-safe via
    O_EXCL retry loop.
    """
    body = await _safe_json(request)
    platform = (body.get("platform") or "unknown").replace("/", "_")[:20]
    b64 = body.get("b64") or ""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return _cors(web.json_response({"ok": False, "error": "bad_b64"}, status=400))
    d = DM_SAMPLE_DIR
    os.makedirs(d, exist_ok=True)
    import glob as _glob
    existing = _glob.glob(f"{d}/{platform}_*.bin")
    # Derive next index from the max existing index across BOTH old 3-digit
    # (`_NNN.bin`) and new 6-digit (`_NNNNNN.bin`) naming — the regex matches
    # any run of digits before `.bin`, so we don't lose track when the format
    # widens.
    max_idx = -1
    _idx_re = re.compile(rf"{re.escape(platform)}_(\d+)\.bin$")
    for p in existing:
        m = _idx_re.search(p)
        if m:
            try:
                max_idx = max(max_idx, int(m.group(1)))
            except ValueError:
                pass
    n = max_idx + 1
    path = None
    for _ in range(5):
        candidate = f"{d}/{platform}_{n:06d}.bin"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            n += 1
            continue
        except Exception:
            logger.exception("dm sample open failed")
            break
        try:
            os.write(fd, raw)
            path = candidate
        except Exception:
            logger.exception("dm sample write failed")
        finally:
            os.close(fd)
        break
    if path is None:
        return _cors(web.json_response({"ok": False, "error": "write_failed"}, status=500))
    logger.info("DM sample saved: %s (%d bytes) url=%s", path, len(raw), body.get("url"))
    archive_body = dict(body)
    archive_body["decoded_bytes"] = len(raw)
    archive_body["debug_sample_path"] = path
    await _archive_browser_capture(
        request.app.get("pool"), platform, "dm_sample", archive_body,
    )
    # Telemetry (P1.2): record the sample event so the dashboard panel can
    # count IG-vs-TikTok samples per 24h and surface last-seen timestamps.
    await _dm_probe_log_write(
        request.app.get("pool"), platform, "sample", body, frame_size=len(raw),
    )
    # Rotate: keep newest DM_SAMPLE_CAP_PER_PLATFORM files per platform by
    # mtime. Uses a fresh glob so we count the file we just wrote.
    try:
        files = sorted(
            _glob.glob(f"{d}/{platform}_*.bin"),
            key=lambda p: os.path.getmtime(p),
        )
        excess = len(files) - DM_SAMPLE_CAP_PER_PLATFORM
        if excess > 0:
            pruned = 0
            for old in files[:excess]:
                try:
                    os.unlink(old)
                    pruned += 1
                except OSError:
                    pass
            if pruned:
                logger.info(
                    "DM sample rotated: pruned %d old %s samples (cap=%d)",
                    pruned, platform, DM_SAMPLE_CAP_PER_PLATFORM,
                )
    except Exception:
        logger.exception("dm sample rotation failed")
    return _cors(web.json_response({"ok": True, "bytes": len(raw)}))


async def dm_frame_handler(request):
    """Capture a DM JSON frame if one is ever observed over a WS (#35). These
    channels almost always send protobuf/MQTT, so this is a best-effort path:
    log the raw frame so it can be inspected. No dedicated table yet — a schema
    is added once the frame shape is confirmed via the probe above.
    """
    body = await _safe_json(request)
    try:
        logger.info("DM JSON frame (%s): %s", body.get("platform"), json.dumps(body.get("frame"))[:1000])
    except Exception:
        logger.info("DM frame observed (unserializable)")
    await _archive_browser_capture(
        request.app.get("pool"), body.get("platform") or "unknown", "dm_frame", body,
    )
    return _cors(web.json_response({"ok": True}))


def _parse_ts_ms(v):
    """Convert an int/str ms-epoch or None to a timezone-aware datetime.
    Falls back to None on garbage so a bad timestamp never fails the upsert."""
    if v is None:
        return None
    try:
        ms = int(v)
        if ms <= 0:
            return None
        # Sanity: TikTok values are 13-digit ms since epoch. Reject > 4102444800000
        # (year ~2100) so a us/ns-scaled leak from the decoder can't corrupt rows.
        if ms > 4102444800000:
            return None
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _conv_participants(conversation_id):
    """`0:1:UID_A:UID_B` -> ['UID_A','UID_B']. None on anything unparseable."""
    if not conversation_id:
        return None
    parts = str(conversation_id).split(":")
    if len(parts) >= 4:
        out = [p for p in parts[2:] if p]
        return out or None
    return None


def _int_or_none(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool_or_none(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


async def dm_decoded_handler(request):
    """Client-decoded DM payload from the extension (Option B of #39).

    The extension's WS hook uses a minimal in-tab protobuf parser to walk
    TikTok frontier frames on wss://im-ws-sg.tiktok.com/ws/v2 (see
    extension/inject.js `_ttDecode`). When it identifies a real message
    frame (method=5, inner has field 500 → field 5 with content JSON), it
    extracts the structured payload and POSTs here. Raw sample capture
    continues in parallel via /social/dm-sample as a schema-drift canary.

    Body shape:
      {
        "platform": "tiktok",
        "owner":    "<owner_uid_or_empty>",
        "threads":  [ {conversation_id, conversation_type, participants,
                       last_activity_ms} ],
        "messages": [ {message_id, conversation_id, sender_uid, sender_secuid,
                       text, aweType, message_type, create_time_ms,
                       client_message_id, is_stranger, raw_content} ]
      }

    Message upsert keyed on message_id (idempotent — the TikTok frontier
    pushes each message ~6× across topic subscriptions).
    """
    body = await _safe_json(request)
    platform = (body.get("platform") or "").strip().lower()
    if platform not in ("tiktok", "instagram"):
        return _cors(web.json_response(
            {"ok": False, "error": "unsupported_platform"}, status=400,
        ))
    owner = (body.get("owner") or "").strip()
    threads = body.get("threads") or []
    messages = body.get("messages") or []
    if not isinstance(threads, list) or not isinstance(messages, list):
        return _cors(web.json_response(
            {"ok": False, "error": "bad_shape"}, status=400,
        ))

    pool = request.app.get("pool")
    if not pool:
        return _cors(web.json_response({"ok": True, "recorded": 0}))
    await _archive_browser_capture(pool, platform, "dm_decoded", body)
    if platform == "tiktok":
        thread_n, msg_n = await _upsert_tt_decoded(pool, owner, threads, messages)
    else:
        thread_n, msg_n = await _upsert_ig_decoded(pool, owner, threads, messages)
    if thread_n or msg_n:
        logger.info("DM decoded[%s]: %d threads, %d messages", platform, thread_n, msg_n)
    return _cors(web.json_response({"ok": True, "threads": thread_n, "messages": msg_n}))


async def _upsert_tt_decoded(pool, owner, threads, messages):
    thread_n = 0
    msg_n = 0
    try:
        async with pool.acquire() as conn:
            for t in threads:
                if not isinstance(t, dict):
                    continue
                cid = (t.get("conversation_id") or "").strip()
                if not cid:
                    continue
                await conn.execute(
                    """
                    INSERT INTO tiktok_dm_thread
                        (conversation_id, conversation_type, participants,
                         owner_account, last_activity)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (conversation_id) DO UPDATE SET
                        conversation_type = COALESCE(EXCLUDED.conversation_type,
                                                     tiktok_dm_thread.conversation_type),
                        participants      = COALESCE(EXCLUDED.participants,
                                                     tiktok_dm_thread.participants),
                        owner_account     = COALESCE(EXCLUDED.owner_account,
                                                     tiktok_dm_thread.owner_account),
                        last_activity     = GREATEST(EXCLUDED.last_activity,
                                                     tiktok_dm_thread.last_activity),
                        updated_at        = now()
                    """,
                    cid,
                    _int_or_none(t.get("conversation_type")),
                    (t.get("participants") or _conv_participants(cid) or None),
                    (t.get("owner_account") or owner or None),
                    _parse_ts_ms(t.get("last_activity_ms")),
                )
                thread_n += 1
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("message_id") or "").strip()
                cid = (m.get("conversation_id") or "").strip()
                if not mid or not cid:
                    continue
                sender_uid = m.get("sender_uid")
                sender_uid_s = str(sender_uid) if sender_uid is not None else None
                is_from_me = bool(owner) and sender_uid_s == owner
                raw = m.get("raw_content")
                if raw is not None and not isinstance(raw, (dict, list)):
                    raw = {"raw": raw}
                await conn.execute(
                    """
                    INSERT INTO tiktok_dm
                        (message_id, conversation_id, sender_uid, sender_secuid,
                         text, awe_type, message_type, "timestamp", is_from_me,
                         owner_account, client_message_id, is_stranger, media_url,
                         raw_content)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT (message_id) DO UPDATE SET
                        text         = COALESCE(EXCLUDED.text, tiktok_dm.text),
                        awe_type     = COALESCE(EXCLUDED.awe_type, tiktok_dm.awe_type),
                        message_type = COALESCE(EXCLUDED.message_type, tiktok_dm.message_type),
                        "timestamp"  = COALESCE(EXCLUDED."timestamp", tiktok_dm."timestamp"),
                        media_url    = COALESCE(EXCLUDED.media_url, tiktok_dm.media_url),
                        raw_content  = COALESCE(EXCLUDED.raw_content, tiktok_dm.raw_content)
                    """,
                    mid, cid, sender_uid_s, m.get("sender_secuid"),
                    m.get("text"),
                    _int_or_none(m.get("aweType")),
                    _int_or_none(m.get("message_type")),
                    _parse_ts_ms(m.get("create_time_ms")),
                    is_from_me,
                    owner or None,
                    m.get("client_message_id"),
                    _bool_or_none(m.get("is_stranger")),
                    (m.get("media_url") or None),
                    json.dumps(raw) if raw is not None else None,
                )
                msg_n += 1
    except Exception:
        logger.exception("tt dm_decoded upsert failed")
        raise
    return thread_n, msg_n


async def _upsert_ig_decoded(pool, owner, threads, messages):
    """Instagram DM upsert into instagram_dm{,_thread}. The wire path is
    intentionally different from TikTok: IG uses MQTT-over-WSS + Thrift on
    edge-chat.instagram.com/chat, AND a GraphQL/direct_v2 HTTP path the
    extension already observes. Both paths funnel through here.

    Scaffolding today — no real MQTT/Thrift decoder in inject.js, so most IG
    decoded payloads will come from the GraphQL harvester (see extension/
    inject.js:harvestIGGraphQL) which extracts inbox/thread structures from
    /api/graphql/ and /graphql/query/ responses.
    """
    thread_n = 0
    msg_n = 0
    try:
        async with pool.acquire() as conn:
            for t in threads:
                if not isinstance(t, dict):
                    continue
                tid = (t.get("thread_id") or t.get("conversation_id") or "").strip()
                if not tid:
                    continue
                await conn.execute(
                    """
                    INSERT INTO instagram_dm_thread
                        (thread_id, title, participants, owner_account, last_activity)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (thread_id) DO UPDATE SET
                        title         = COALESCE(EXCLUDED.title,
                                                 instagram_dm_thread.title),
                        participants  = COALESCE(EXCLUDED.participants,
                                                 instagram_dm_thread.participants),
                        owner_account = COALESCE(EXCLUDED.owner_account,
                                                 instagram_dm_thread.owner_account),
                        last_activity = GREATEST(EXCLUDED.last_activity,
                                                 instagram_dm_thread.last_activity),
                        updated_at    = now()
                    """,
                    tid,
                    (t.get("title") or None),
                    (t.get("participants") or None),
                    (t.get("owner_account") or owner or None),
                    _parse_ts_ms(t.get("last_activity_ms")),
                )
                thread_n += 1
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("message_id") or "").strip()
                tid = (m.get("thread_id") or m.get("conversation_id") or "").strip()
                if not mid or not tid:
                    continue
                sender_id = m.get("sender_id")
                sender_id_s = str(sender_id) if sender_id is not None else None
                # is_from_me: prefer client hint (extension had cookie-derived
                # ds_user_id), fall back to sender_id vs owner match.
                if isinstance(m.get("is_from_me"), bool):
                    is_from_me = m["is_from_me"]
                else:
                    is_from_me = bool(owner) and sender_id_s == owner
                await conn.execute(
                    """
                    INSERT INTO instagram_dm
                        (message_id, thread_id, sender_id, sender_username,
                         text, item_type, "timestamp", is_from_me, owner_account)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (message_id) DO UPDATE SET
                        text            = COALESCE(EXCLUDED.text, instagram_dm.text),
                        item_type       = COALESCE(EXCLUDED.item_type, instagram_dm.item_type),
                        "timestamp"     = COALESCE(EXCLUDED."timestamp", instagram_dm."timestamp"),
                        sender_username = COALESCE(EXCLUDED.sender_username, instagram_dm.sender_username)
                    """,
                    mid, tid, sender_id_s,
                    m.get("sender_username"),
                    m.get("text"),
                    m.get("item_type"),
                    _parse_ts_ms(m.get("timestamp_ms") or m.get("create_time_ms")),
                    is_from_me,
                    owner or None,
                )
                msg_n += 1
    except Exception:
        logger.exception("ig dm_decoded upsert failed")
        raise
    return thread_n, msg_n


async def _save_profile(pool, platform, p) -> bool:
    """Upsert a full profile. Instagram is API-rich; X/Facebook are browser DOM
    best-effort profiles keyed on the visible handle."""
    if platform not in ("instagram", "x", "facebook"):
        return False
    uname = (p.get("username") or "").strip().lstrip("@")
    if not uname:
        return False
    pic = p.get("profile_pic_url")
    if platform == "x":
        async with pool.acquire() as conn:
            # Preserve the exact handle casing already present in x_posts when
            # possible; entity_platform_links for X were historically keyed on
            # x_posts.author_username.
            canonical = await conn.fetchval(
                """
                SELECT author_username
                FROM x_posts
                WHERE lower(author_username) = lower($1)
                ORDER BY collected_at DESC NULLS LAST
                LIMIT 1
                """,
                uname,
            )
            pid = str(p.get("platform_user_id") or p.get("user_id") or canonical or uname).strip().lstrip("@")
            username = str(canonical or uname).strip().lstrip("@")
            await conn.execute(
                """
                INSERT INTO x_profiles
                  (id, platform_user_id, username, display_name, bio, followers_count,
                   following_count, posts_count, is_verified, is_private, profile_pic_url,
                   external_url, location, joined_text, collected_at, updated_at, metadata)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),now(),$14::jsonb)
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=COALESCE(NULLIF(EXCLUDED.username, ''), x_profiles.username),
                   display_name=COALESCE(EXCLUDED.display_name, x_profiles.display_name),
                   bio=COALESCE(EXCLUDED.bio, x_profiles.bio),
                   followers_count=COALESCE(EXCLUDED.followers_count, x_profiles.followers_count),
                   following_count=COALESCE(EXCLUDED.following_count, x_profiles.following_count),
                   posts_count=COALESCE(EXCLUDED.posts_count, x_profiles.posts_count),
                   is_verified=COALESCE(EXCLUDED.is_verified, x_profiles.is_verified),
                   is_private=COALESCE(EXCLUDED.is_private, x_profiles.is_private),
                   profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, x_profiles.profile_pic_url),
                   external_url=COALESCE(EXCLUDED.external_url, x_profiles.external_url),
                   location=COALESCE(EXCLUDED.location, x_profiles.location),
                   joined_text=COALESCE(EXCLUDED.joined_text, x_profiles.joined_text),
                   metadata=x_profiles.metadata || EXCLUDED.metadata,
                   updated_at=now()
                """,
                pid, username, p.get("display_name") or p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("posts_count")),
                bool(p.get("is_verified")) if p.get("is_verified") is not None else None,
                bool(p.get("is_private")) if p.get("is_private") is not None else None,
                pic, p.get("external_url"), p.get("location"), p.get("joined_text"),
                json.dumps(p.get("metadata") or {}),
            )
        await _record_users(pool, platform, [{"user_id": pid, "username": username,
                                              "display_name": p.get("display_name") or p.get("full_name"),
                                              "profile_pic_url": pic}], "profile")
        return True
    if platform == "facebook":
        async with pool.acquire() as conn:
            pid = str(p.get("platform_user_id") or p.get("user_id") or uname).strip().lstrip("@")
            await conn.execute(
                """
                INSERT INTO facebook_profiles
                  (id, platform_user_id, username, display_name, bio, followers_count,
                   following_count, friends_count, is_person, profile_pic_url, external_url,
                   collected_at, updated_at, metadata)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),now(),$11::jsonb)
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=COALESCE(NULLIF(EXCLUDED.username, ''), facebook_profiles.username),
                   display_name=COALESCE(EXCLUDED.display_name, facebook_profiles.display_name),
                   bio=COALESCE(EXCLUDED.bio, facebook_profiles.bio),
                   followers_count=COALESCE(EXCLUDED.followers_count, facebook_profiles.followers_count),
                   following_count=COALESCE(EXCLUDED.following_count, facebook_profiles.following_count),
                   friends_count=COALESCE(EXCLUDED.friends_count, facebook_profiles.friends_count),
                   is_person=COALESCE(EXCLUDED.is_person, facebook_profiles.is_person),
                   profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, facebook_profiles.profile_pic_url),
                   external_url=COALESCE(EXCLUDED.external_url, facebook_profiles.external_url),
                   metadata=facebook_profiles.metadata || EXCLUDED.metadata,
                   updated_at=now()
                """,
                pid, uname, p.get("display_name") or p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("friends_count")),
                bool(p.get("is_person")) if p.get("is_person") is not None else None,
                pic, p.get("external_url"), json.dumps(p.get("metadata") or {}),
            )
        await _record_users(pool, platform, [{"user_id": pid, "username": uname,
                                              "display_name": p.get("display_name") or p.get("full_name"),
                                              "profile_pic_url": pic}], "profile")
        return True
    # Tier 4 change-history: snapshot the row BEFORE the upsert so we can diff
    # old -> new and log bio/username/follower/etc. changes. Best-effort.
    prev_row = None
    if UserChangeTracker is not None:
        try:
            async with pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, full_name, bio, followers_count, following_count, "
                    "posts_count, is_verified, is_private, profile_pic_url, external_url "
                    "FROM instagram_profiles WHERE platform_user_id = $1",
                    str(p.get("user_id") or uname),
                )
        except Exception:
            logger.debug("ig change-tracker prev-row fetch failed for %s", uname, exc_info=True)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO instagram_profiles
                  (id, platform_user_id, username, full_name, bio, followers_count,
                   following_count, posts_count, is_verified, is_private, profile_pic_url,
                   external_url, collected_at, updated_at)
                VALUES (gen_random_uuid(),$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),now())
                ON CONFLICT (platform_user_id) DO UPDATE SET
                   username=EXCLUDED.username, full_name=EXCLUDED.full_name, bio=EXCLUDED.bio,
                   followers_count=EXCLUDED.followers_count, following_count=EXCLUDED.following_count,
                   posts_count=EXCLUDED.posts_count, is_verified=EXCLUDED.is_verified,
                   is_private=EXCLUDED.is_private, profile_pic_url=COALESCE(EXCLUDED.profile_pic_url, instagram_profiles.profile_pic_url),
                   external_url=EXCLUDED.external_url, updated_at=now()
                """,
                str(p.get("user_id") or uname), uname, p.get("full_name"), p.get("bio"),
                _int(p.get("followers_count")), _int(p.get("following_count")), _int(p.get("posts_count")),
                bool(p.get("is_verified")), bool(p.get("is_private")), pic, p.get("external_url"),
            )
    except Exception:
        logger.debug("save profile failed %s", uname, exc_info=True)
    await _track_ig_profile_change(pool, p, uname, prev_row)
    await _record_users(pool, platform, [{"user_id": p.get("user_id"), "username": uname,
                                          "display_name": p.get("full_name"), "profile_pic_url": pic}], "profile")
    return True


async def _track_ig_profile_change(pool, p, uname, prev_row) -> None:
    """Diff the prior instagram_profiles row against the incoming extension
    payload and log field changes into instagram_user_changes (Tier 4). Maps the
    extension's field names onto INSTAGRAM_TRACKED_FIELDS. Best-effort — never
    breaks ingest."""
    if UserChangeTracker is None or INSTAGRAM_TRACKED_FIELDS is None:
        return
    try:
        pk_val = int(p.get("user_id") or 0)
    except (TypeError, ValueError):
        pk_val = 0
    if not pk_val:
        return
    try:
        current_normalized = None
        if prev_row is not None:
            pr = dict(prev_row)
            current_normalized = {
                "username": pr.get("username"), "full_name": pr.get("full_name"),
                "biography": pr.get("bio"), "is_verified": pr.get("is_verified"),
                "is_private": pr.get("is_private"), "profile_pic_url": pr.get("profile_pic_url"),
                "follower_count": pr.get("followers_count"),
                "following_count": pr.get("following_count"),
                "post_count": pr.get("posts_count"), "external_url": pr.get("external_url"),
            }
        new_snapshot = {
            "username": p.get("username"), "full_name": p.get("full_name"),
            "biography": p.get("bio"), "is_verified": bool(p.get("is_verified")),
            "is_private": bool(p.get("is_private")), "profile_pic_url": p.get("profile_pic_url"),
            "follower_count": _int(p.get("followers_count")),
            "following_count": _int(p.get("following_count")),
            "post_count": _int(p.get("posts_count")), "external_url": p.get("external_url"),
        }
        await UserChangeTracker(pool).detect_and_log(
            table="instagram_user_changes", pk_col="user_id", pk_val=pk_val,
            current_row=current_normalized, new_row=new_snapshot,
            fields=INSTAGRAM_TRACKED_FIELDS,
        )
    except Exception:
        logger.debug("ig change-tracker detect_and_log failed for %s", uname, exc_info=True)


async def _record_ig_access(pool, target_username, owner, profile) -> None:
    """Record that ``owner`` (the extension's logged-in IG account) could see
    ``target_username`` into profile_access_{summary,attempts} — the follow-aware
    selector (Phase 0). Because the extension is the live IG path (the headless
    collector is 429'd), this is where the routing data actually accumulates.

    A successful profile ingest means the owner CAN access the target. A private
    profile that still yielded data implies the owner FOLLOWS it (private data is
    only visible to followers). Best-effort — never breaks ingest.
    """
    if ProfileAccessRepository is None or not pool or not target_username:
        return
    account = None
    if isinstance(owner, dict):
        account = (owner.get("username") or owner.get("id") or "") or None
    elif isinstance(owner, str):
        account = owner.strip() or None
    # Fallback label when the extension didn't send which account it used: still
    # records the public/private signal, but won't match a routable cookie
    # account (so it can't mis-route — routing only trusts real account names).
    if not account:
        account = "ig_extension"
    _priv = profile.get("is_private")
    is_private = bool(_priv) if _priv is not None else None
    _fbv = profile.get("followed_by_viewer")
    is_followed = bool(_fbv) if _fbv is not None else bool(is_private)
    try:
        repo = ProfileAccessRepository(pool)
        await repo.record_attempt(
            source="instagram",
            target_id=str(target_username).lstrip("@"),
            account=str(account),
            can_access=True,
            is_public=(None if is_private is None else (not is_private)),
            is_followed=is_followed,
        )
    except Exception:
        logger.debug("ig access-record failed for %s", target_username, exc_info=True)


def _browser_capture_subject(endpoint: str, body: dict) -> str:
    if endpoint == "profile":
        profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
        return str(
            profile.get("username")
            or profile.get("user_id")
            or profile.get("platform_user_id")
            or "profile"
        ).lstrip("@")
    if endpoint == "posts":
        posts = body.get("posts") if isinstance(body.get("posts"), list) else []
        first = posts[0] if posts and isinstance(posts[0], dict) else {}
        return str(first.get("platform_post_id") or first.get("content_id") or f"{len(posts)}_posts")
    if endpoint == "comments":
        return str(body.get("post_id") or "comments")
    if endpoint == "dms":
        owner = body.get("owner") if isinstance(body.get("owner"), dict) else {}
        threads = body.get("threads") if isinstance(body.get("threads"), list) else []
        first = threads[0] if threads and isinstance(threads[0], dict) else {}
        return str(
            owner.get("username")
            or owner.get("id")
            or first.get("thread_id")
            or first.get("thread_v2_id")
            or f"{len(threads)}_threads"
        )
    if endpoint in ("dm_probe", "dm_sample", "dm_frame"):
        parts = [
            body.get("owner") or body.get("owner_account"),
            body.get("transport"),
            body.get("frame_kind"),
            body.get("frame_size"),
        ]
        subject = "_".join(str(p) for p in parts if p not in (None, ""))
        return subject or endpoint
    if endpoint == "dm_decoded":
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        threads = body.get("threads") if isinstance(body.get("threads"), list) else []
        first_msg = messages[0] if messages and isinstance(messages[0], dict) else {}
        first_thread = threads[0] if threads and isinstance(threads[0], dict) else {}
        return str(
            first_msg.get("message_id")
            or first_msg.get("client_message_id")
            or first_thread.get("conversation_id")
            or first_thread.get("thread_id")
            or f"{len(messages)}_messages"
        )
    if endpoint == "strava_streams":
        return str(body.get("activity_id") or body.get("platform_activity_id") or "strava_streams")
    return endpoint


async def _archive_browser_capture(pool, platform: str, endpoint: str, body: dict) -> None:
    """Best-effort vault raw archive for extension/browser evidence.

    Do not call this for credential endpoints. Cookies stay in credential files,
    never in raw payloads or sidecars.
    """
    if endpoint == "cookies":
        return
    if not isinstance(body, dict):
        return
    source = _norm_platform(platform)
    if source not in KNOWN_PLATFORMS:
        return
    subject = _SAFE.sub("_", _browser_capture_subject(endpoint, body))[:96] or endpoint
    artifact_id = f"extension/{endpoint}/{subject}/{time.time_ns()}"
    owner = body.get("owner")
    if isinstance(owner, dict):
        collection_account = owner.get("username") or owner.get("id")
    else:
        collection_account = owner if isinstance(owner, str) else None
    metadata = {
        "ingest_path": "extension",
        "endpoint": endpoint,
        "platform": source,
        "collection_account": collection_account,
        "extension_version": body.get("extension_version"),
        "request_url": body.get("request_url") or body.get("url"),
        "http_status": body.get("http_status"),
        "body_keys": sorted(str(k) for k in body.keys()),
    }
    target_tables = _BROWSER_CAPTURE_TARGET_TABLES.get(endpoint, {}).get(source, [])
    result = write_raw_payload(
        source=source,
        artifact_id=artifact_id,
        payload=body,
        metadata=metadata,
        target_tables=target_tables,
        extension="json.gz" if endpoint in _BROWSER_CAPTURE_COMPRESSED_ENDPOINTS else "json",
    )
    if result.ok:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM dead_letter_queue
                    WHERE source = $1
                      AND entity_id = $2
                      AND status IN ('pending', 'in_progress')
                      AND error_message LIKE 'browser raw archive failed:%'
                    """,
                    source,
                    subject,
                )
        except Exception:
            logger.debug(
                "browser raw archive stale-DLQ cleanup failed platform=%s endpoint=%s subject=%s",
                source,
                endpoint,
                subject,
                exc_info=True,
            )
        return
    logger.warning(
        "browser raw archive failed platform=%s endpoint=%s artifact=%s: %s",
        source,
        endpoint,
        artifact_id,
        result.error,
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                VALUES ($1, $2, $3, $4)
                """,
                source,
                subject,
                artifact_id,
                f"browser raw archive failed: {result.error}"[:500],
            )
    except Exception:
        logger.debug(
            "browser raw archive DLQ insert failed platform=%s endpoint=%s",
            source,
            endpoint,
            exc_info=True,
        )


def _stream_values(container, key: str) -> list:
    """Extract a Strava stream array from web or API-shaped payloads."""
    if not container:
        return []
    if isinstance(container, dict):
        raw = container.get(key)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("data"), list):
            return raw["data"]
    if isinstance(container, list):
        for item in container:
            if not isinstance(item, dict):
                continue
            if item.get("type") == key or item.get("name") == key or item.get("key") == key:
                data = item.get("data")
                return data if isinstance(data, list) else []
    return []


def _json_array_or_none(value: list | None) -> str | None:
    return json.dumps(value) if isinstance(value, list) and value else None


async def _upsert_strava_browser_stream(pool, body: dict) -> dict:
    """Persist a Strava route stream observed by the browser extension."""
    if not pool:
        return {"stored": 0, "point_count": 0, "reason": "no_pool"}
    raw_activity_id = body.get("activity_id") or body.get("platform_activity_id")
    try:
        activity_id = int(raw_activity_id)
    except (TypeError, ValueError):
        return {"stored": 0, "point_count": 0, "reason": "bad_activity_id"}

    streams = body.get("streams") if isinstance(body.get("streams"), (dict, list)) else body
    latlng = _stream_values(streams, "latlng")
    point_count = len(latlng) if isinstance(latlng, list) else 0
    if point_count < 2:
        return {"stored": 0, "point_count": point_count, "reason": "no_route_points"}

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO strava_activities (platform_activity_id, metadata, collected_at)
            VALUES (
                $1,
                jsonb_build_object(
                    'browser_stream_stub', TRUE,
                    'browser_stream_first_seen_at', now(),
                    'browser_stream_request_url', $2::text
                ),
                now()
            )
            ON CONFLICT (platform_activity_id) DO NOTHING
            """,
            activity_id,
            body.get("request_url") or body.get("url"),
        )
        act_row = await conn.fetchrow(
            """
            SELECT id, start_latlng, end_latlng
            FROM strava_activities
            WHERE platform_activity_id = $1
            """,
            activity_id,
        )
        if not act_row:
            return {"stored": 0, "point_count": point_count, "reason": "activity_missing"}

        await conn.execute(
            """
            INSERT INTO strava_gps_streams (
                activity_id, latlng, altitude, distance, time, heartrate,
                cadence, watts, speed, grade_smooth, collected_at
            )
            VALUES (
                $1, $2::jsonb, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb,
                $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb, now()
            )
            ON CONFLICT (activity_id) DO UPDATE SET
                latlng = EXCLUDED.latlng,
                altitude = COALESCE(EXCLUDED.altitude, strava_gps_streams.altitude),
                distance = COALESCE(EXCLUDED.distance, strava_gps_streams.distance),
                time = COALESCE(EXCLUDED.time, strava_gps_streams.time),
                heartrate = COALESCE(EXCLUDED.heartrate, strava_gps_streams.heartrate),
                cadence = COALESCE(EXCLUDED.cadence, strava_gps_streams.cadence),
                watts = COALESCE(EXCLUDED.watts, strava_gps_streams.watts),
                speed = COALESCE(EXCLUDED.speed, strava_gps_streams.speed),
                grade_smooth = COALESCE(EXCLUDED.grade_smooth, strava_gps_streams.grade_smooth),
                collected_at = now()
            """,
            act_row["id"],
            json.dumps(latlng),
            _json_array_or_none(_stream_values(streams, "altitude")),
            _json_array_or_none(_stream_values(streams, "distance")),
            _json_array_or_none(_stream_values(streams, "time")),
            _json_array_or_none(_stream_values(streams, "heartrate")),
            _json_array_or_none(_stream_values(streams, "cadence")),
            _json_array_or_none(_stream_values(streams, "watts")),
            _json_array_or_none(_stream_values(streams, "speed")),
            _json_array_or_none(_stream_values(streams, "grade_smooth")),
        )

        fields = _derive_gps_route_fields(
            act_row["start_latlng"],
            act_row["end_latlng"],
            latlng,
        )
        await conn.execute(
            """
            UPDATE strava_activities
            SET start_latlng = COALESCE(start_latlng, $1),
                end_latlng = COALESCE(end_latlng, $2),
                stream_status = 'ok',
                privacy_zone_start = $3,
                privacy_zone_end = $4,
                truncation_point_start = COALESCE(truncation_point_start, $5),
                truncation_point_end = COALESCE(truncation_point_end, $6),
                summary_polyline = COALESCE(NULLIF(summary_polyline, ''), $7),
                metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'browser_stream_last_seen_at', now(),
                    'browser_stream_extension_version', $8::text,
                    'browser_stream_request_url', $9::text,
                    'browser_stream_point_count', $10::int
                ),
                collected_at = COALESCE(collected_at, now())
            WHERE id = $11
            """,
            fields["start_latlng"],
            fields["end_latlng"],
            fields["privacy_zone_start"],
            fields["privacy_zone_end"],
            fields["truncation_point_start"],
            fields["truncation_point_end"],
            fields["summary_polyline"] or None,
            body.get("extension_version"),
            body.get("request_url") or body.get("url"),
            point_count,
            act_row["id"],
        )
    return {"stored": 1, "point_count": point_count, "activity_id": activity_id}


def _browser_http_status(body: dict) -> int | None:
    for key in ("http_status", "status_code"):
        try:
            value = body.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _browser_account_label(body: dict, *, fallback: str = "extension") -> str:
    owner = body.get("owner") or body.get("owner_account") or body.get("account")
    if isinstance(owner, dict):
        owner = owner.get("username") or owner.get("id")
    text = str(owner or fallback).strip() or fallback
    return text[:128]


async def _record_strava_stream_http_event(pool, body: dict) -> bool:
    status = _browser_http_status(body)
    if status not in {401, 403, 429}:
        return False
    activity_id = str(body.get("activity_id") or body.get("platform_activity_id") or "unknown")
    account = _browser_account_label(body)
    if await _touch_recent_strava_stream_http_event(pool, account, activity_id, status, body):
        return False
    cooldown = None
    dynamic_metadata = {}
    if status == 429:
        state = await record_dynamic_cooldown(
            pool,
            source="strava",
            scope="gps_streams",
            account=account,
            base_seconds=STRAVA_BROWSER_429_COOLDOWN_SECONDS,
            max_seconds=STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS,
            memory_seconds=STRAVA_BROWSER_429_MEMORY_SECONDS,
            write_source_cursor=True,
        )
        cooldown = state.seconds_remaining
        dynamic_metadata = {
            "dynamic_cooldown_service": state.service,
            "dynamic_cooldown_streak": state.streak,
        }
    await record_rate_limit_event(
        pool,
        source="strava",
        account=account,
        scope="browser_strava_streams",
        status_code=status,
        cooldown_seconds=cooldown,
        reason=f"browser Strava stream HTTP {status} for {activity_id}",
        metadata={
            "activity_id": activity_id,
            "request_url": body.get("request_url") or body.get("url"),
            "extension_version": body.get("extension_version"),
            "point_count": body.get("point_count"),
            "browser_capture": True,
            **dynamic_metadata,
        },
    )
    return True


async def _touch_recent_strava_stream_http_event(
    pool,
    account: str,
    activity_id: str,
    status: int,
    body: dict,
) -> bool:
    """Fold duplicate browser stream HTTP failures into the newest recent row."""
    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                """
                WITH latest AS (
                    SELECT id
                    FROM rate_limit_events
                    WHERE source = 'strava'
                      AND account IS NOT DISTINCT FROM $1
                      AND scope = 'browser_strava_streams'
                      AND status_code = $3
                      AND metadata->>'activity_id' = $2
                      AND created_at >= now() - interval '6 hours'
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                UPDATE rate_limit_events r
                SET metadata = r.metadata || jsonb_build_object(
                    'duplicate_suppressed_count',
                    COALESCE((r.metadata->>'duplicate_suppressed_count')::int, 0) + 1,
                    'duplicate_last_seen_at',
                    now(),
                    'latest_request_url',
                    $4::text,
                    'latest_extension_version',
                    $5::text,
                    'latest_point_count',
                    $6::int
                )
                FROM latest
                WHERE r.id = latest.id
                RETURNING 1
                """,
                account,
                activity_id,
                status,
                body.get("request_url") or body.get("url"),
                body.get("extension_version"),
                body.get("point_count"),
            ))
    except Exception:
        logger.debug(
            "recent Strava stream HTTP event touch failed account=%s activity=%s status=%s",
            account,
            activity_id,
            status,
            exc_info=True,
        )
        return False


async def strava_streams_handler(request):
    body = await _safe_json(request)
    body["platform"] = "strava"
    pool = request.app["pool"]
    await _archive_browser_capture(pool, "strava", "strava_streams", body)
    http_event_recorded = await _record_strava_stream_http_event(pool, body)
    result = await _upsert_strava_browser_stream(pool, body)
    if http_event_recorded:
        result["rate_limit_recorded"] = True
    await _record_browser_ingest_event(
        pool,
        "strava",
        "strava_streams",
        str(body.get("activity_id") or body.get("platform_activity_id") or "unknown"),
        observed_count=1 if body.get("activity_id") or body.get("platform_activity_id") else 0,
        stored_count=int(result.get("stored") or 0),
        metadata={
            "point_count": int(result.get("point_count") or 0),
            "reason": result.get("reason"),
            "request_url": body.get("request_url") or body.get("url"),
            "extension_version": body.get("extension_version"),
        },
    )
    status = _strava_stream_response_status(result, http_event_recorded=http_event_recorded)
    return _cors(web.json_response(result, status=status))


def _strava_stream_response_status(result: dict, *, http_event_recorded: bool = False) -> int:
    """Return HTTP status for an accepted browser route-stream observation."""
    if result.get("stored") or http_event_recorded:
        return 200
    reason = result.get("reason")
    if reason == "no_route_points":
        return 200
    if reason == "bad_activity_id":
        return 400
    if reason == "no_pool":
        return 503
    return 422


async def strava_route_queue_handler(request):
    raw_limit = request.query.get("limit", "5")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 5
    account = str(
        request.query.get("account")
        or request.query.get("owner")
        or ""
    ).strip() or None
    cache_key = f"{account or ''}:{limit}"
    now = time.time()
    cached = _STRAVA_ROUTE_QUEUE_RESPONSE_CACHE.get(cache_key)
    try:
        queue = await asyncio.wait_for(
            fetch_strava_route_capture_queue(
                request.app["pool"],
                limit=limit,
                account=account,
                respect_cooldown=True,
            ),
            timeout=max(0.1, STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS),
        )
        _STRAVA_ROUTE_QUEUE_RESPONSE_CACHE[cache_key] = (time.time(), queue)
    except asyncio.TimeoutError:
        if cached and now - cached[0] <= max(STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS, 1.0):
            queue = dict(cached[1])
            queue["stale"] = True
            queue["timeout"] = True
            queue["cache_age_seconds"] = int(now - cached[0])
        else:
            queue = {
                "items": [],
                "timeout": True,
                "reason": "route_queue_timeout",
                "account": account,
            }
        last_warn = _STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST.get(cache_key, 0.0)
        should_warn = now - last_warn >= max(1.0, STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS)
        if should_warn:
            _STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST[cache_key] = now
        log = logger.warning if should_warn else logger.info
        log(
            "strava route queue timed out after %.1fs account=%s limit=%s; returned %d cached/live items",
            max(0.1, STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS),
            account or "",
            limit,
            len(queue.get("items") or []),
        )
    return _cors(web.json_response(queue))


async def strava_route_visit_handler(request):
    body = await _safe_json(request)
    raw_activity_id = body.get("activity_id") or body.get("platform_activity_id")
    activity_id = str(raw_activity_id or "").strip()
    if not activity_id:
        return _cors(web.json_response({"ok": False, "reason": "bad_activity_id"}, status=400))
    await _record_browser_ingest_event(
        request.app["pool"],
        "strava",
        "strava_route_visit",
        activity_id,
        observed_count=1,
        stored_count=0,
        metadata={
            "status": body.get("status") or "observed",
            "url": body.get("url"),
            "activity_url": body.get("activity_url"),
            "owner": body.get("owner") or body.get("owner_account") or body.get("account"),
            "extension_version": body.get("extension_version"),
        },
    )
    return _cors(web.json_response({"ok": True, "activity_id": activity_id}))


async def _browser_content_recovery_hint(pool, platform: str) -> dict:
    if not pool or not platform or platform == "bridge":
        return {}
    now = time.monotonic()
    cached = _BROWSER_CONTENT_HINT_CACHE.get(platform)
    if cached and now - cached[0] < BROWSER_CONTENT_HINT_TTL_SECONDS:
        return dict(cached[1])
    if platform in _BROWSER_CONTENT_HINT_INFLIGHT:
        return {"force_cycle": False, "force_reason": "content_age_check_pending"}
    _BROWSER_CONTENT_HINT_INFLIGHT.add(platform)
    try:
        async with asyncio.timeout(1.0):
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT EXTRACT(EPOCH FROM (NOW() - created_at))::int AS age_seconds
                    FROM browser_ingest_events
                    WHERE platform = $1
                      AND endpoint <> 'browser_heartbeat'
                      AND (
                        observed_count > 0
                        OR stored_count > 0
                        OR (
                          metadata ? 'probe_reason'
                          AND COALESCE(metadata->>'probe_reason', '')
                              NOT IN ('manual_backend_probe', 'forced_recovery_started', 'recoverable_error_shell')
                        )
                      )
                      AND (
                        stored_count > 0
                        OR endpoint IN ('posts', 'profile', 'strava_route_visit', 'strava_streams')
                        OR (
                          metadata ? 'probe_reason'
                          AND COALESCE(metadata->>'probe_reason', '')
                              NOT IN ('manual_backend_probe', 'forced_recovery_started', 'recoverable_error_shell')
                        )
                      )
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    platform,
                )
        age = row["age_seconds"] if row else None
        if age is None or int(age) > BROWSER_CONTENT_STALE_SECONDS:
            hint = {
                "force_cycle": True,
                "force_reason": "browser_content_stale",
                "content_age_seconds": int(age) if age is not None else None,
                "stale_after_seconds": BROWSER_CONTENT_STALE_SECONDS,
            }
            _BROWSER_CONTENT_HINT_CACHE[platform] = (now, hint)
            return dict(hint)
        _BROWSER_CONTENT_HINT_CACHE[platform] = (now, {})
    except TimeoutError:
        return _browser_content_timeout_hint(platform, "content_age_check_timeout")
    except Exception:
        logger.debug("browser content recovery hint failed platform=%s", platform, exc_info=True)
    finally:
        _BROWSER_CONTENT_HINT_INFLIGHT.discard(platform)
    return {}


def _browser_content_timeout_hint(platform: str, reason: str) -> dict:
    if platform in _BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS:
        return {
            "force_cycle": True,
            "force_reason": reason,
            "content_age_seconds": None,
            "stale_after_seconds": BROWSER_CONTENT_STALE_SECONDS,
        }
    return {"force_cycle": False, "force_reason": reason}


def _normalize_extension_version(value) -> str:
    return re.sub(r"^v", "", str(value or "").strip(), flags=re.IGNORECASE)


def _extension_version_at_least(current: str, expected: str) -> bool | None:
    def parse(value: str) -> tuple[int, ...] | None:
        text = _normalize_extension_version(value)
        if not re.fullmatch(r"\d+(?:\.\d+)*", text):
            return None
        return tuple(int(part) for part in text.split("."))

    current_parts = parse(current)
    expected_parts = parse(expected)
    if current_parts is None or expected_parts is None:
        return None
    width = max(len(current_parts), len(expected_parts))
    current_parts = current_parts + (0,) * (width - len(current_parts))
    expected_parts = expected_parts + (0,) * (width - len(expected_parts))
    return current_parts >= expected_parts


def _extension_reload_hint(extension_version) -> dict:
    expected = _normalize_extension_version(UC_EXTENSION_EXPECTED_VERSION)
    current = _normalize_extension_version(extension_version)
    if not expected:
        return {}
    hint = {"expected_extension_version": expected}
    at_least = _extension_version_at_least(current, expected) if current else None
    if current and current != expected and at_least is not True:
        hint.update({
            "reload_extension": True,
            "reload_reason": "extension_version_mismatch",
            "current_extension_version": current,
        })
    return hint


async def browser_heartbeat_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"), allow_diagnostics=True)
    running = bool(body.get("running"))
    url = body.get("url")
    label = body.get("label")
    subject = (
        str(body.get("owner") or body.get("account") or body.get("tab_id") or platform)
        .strip()[:128]
    )
    pool = request.app.get("pool")
    telemetry_degraded = pool is None
    try:
        async with asyncio.timeout(BROWSER_CONTENT_HINT_RESPONSE_TIMEOUT_SECONDS):
            recovery_hint = await _browser_content_recovery_hint(pool, platform)
    except TimeoutError:
        recovery_hint = _browser_content_timeout_hint(platform, "content_age_response_budget_exceeded")
    _schedule_app_task(
        request.app,
        _record_browser_ingest_event(
            pool,
            platform,
            "browser_heartbeat",
            subject,
            observed_count=1,
            stored_count=0,
            metadata={
                "running": running,
                "url": url,
                "label": label,
                "tab_id": body.get("tab_id"),
                "extension_version": body.get("extension_version"),
                "health_status": body.get("health_status"),
                "health_reason": body.get("health_reason"),
                "page_title": body.get("page_title"),
                "text_sample": body.get("text_sample"),
                "content_counts": body.get("content_counts"),
                "cycle_reason": body.get("cycle_reason"),
                "message_type": body.get("message_type"),
                "cycle_targets": body.get("cycle_targets"),
                "cycle_saved": body.get("cycle_saved"),
                "cycle_discovered": body.get("cycle_discovered"),
                "cycle_error": body.get("cycle_error"),
                "cooldown_left_ms": body.get("cooldown_left_ms"),
                "loop_running": body.get("loop_running"),
                "one_shot_running": body.get("one_shot_running"),
                "one_shot_age_ms": body.get("one_shot_age_ms"),
                "scrape_pass_running": body.get("scrape_pass_running"),
                "scrape_pass_age_ms": body.get("scrape_pass_age_ms"),
                "scrape_pass_reason": body.get("scrape_pass_reason"),
                "stale_after_ms": body.get("stale_after_ms"),
                "one_shot_timeout": body.get("one_shot_timeout"),
                "timeout_ms": body.get("timeout_ms"),
                "service_worker_recovery": body.get("service_worker_recovery"),
                "content_age_seconds": body.get("content_age_seconds"),
                "forced_age_ms": body.get("forced_age_ms"),
                "hard_reload_ms": body.get("hard_reload_ms"),
                "revived_content_script": body.get("revived_content_script"),
                "recovery_scheduled": body.get("recovery_scheduled"),
                "recovery_pending": body.get("recovery_pending"),
                "recovery_attempt": body.get("recovery_attempt"),
                "recovery_delay_ms": body.get("recovery_delay_ms"),
                "recovery_limit": body.get("recovery_limit"),
                "recovery_nav": body.get("recovery_nav"),
                "recovery_target_url": body.get("recovery_target_url"),
                "scraper_tabs_seen": body.get("scraper_tabs_seen"),
                "scraper_tabs_sent": body.get("scraper_tabs_sent"),
                "scraper_tabs_failed": body.get("scraper_tabs_failed"),
                "scraper_tabs_canonical": body.get("scraper_tabs_canonical"),
                "scraper_tabs_skipped": body.get("scraper_tabs_skipped"),
                "scraper_heartbeat_error": body.get("scraper_heartbeat_error"),
            },
        ),
        "browser_heartbeat_telemetry",
    )
    return _cors(web.json_response({
        "ok": True,
        "platform": platform,
        "running": running,
        "telemetry_degraded": telemetry_degraded,
        **recovery_hint,
        **_extension_reload_hint(body.get("extension_version")),
    }))


CREDENTIALS_ROOT = os.getenv("CREDENTIALS_ROOT", "/app/credentials")


async def cookies_handler(request):
    """Self-healing sessions: the extension pushes its LIVE logged-in cookies here,
    and we write a Netscape cookies.txt the headless collector auto-discovers. So the
    headless backup never runs on a dead/expired session (which causes 401 retry
    storms that look bot-like). Only instagram for now."""
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    if platform != "instagram":
        return _cors(web.json_response({"ok": False, "reason": "unsupported"}))
    cookies = body.get("cookies") or []
    have = {c.get("name") for c in cookies if isinstance(c, dict)}
    if "sessionid" not in have:  # not logged in — don't clobber a good file with junk
        return _cors(web.json_response({"ok": False, "reason": "no sessionid"}))
    account = re.sub(r"[^A-Za-z0-9_.-]", "_", str(body.get("account") or "extension_live"))[:60]
    lines = ["# Netscape HTTP Cookie File", "# generated by the UnifiedCollector extension", ""]
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        domain = c.get("domain") or ".instagram.com"
        inc_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = int(c.get("expirationDate") or 0)
        lines.append(f"{domain}\t{inc_sub}\t{path}\t{secure}\t{expiry}\t{c['name']}\t{c.get('value','')}")
    try:
        d = Path(CREDENTIALS_ROOT) / "instagram"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (account + ".txt.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, d / (account + ".txt"))
        logger.info("cookies: wrote live session for instagram/%s (%d cookies)", account, len(cookies))
    except Exception as e:
        logger.warning("cookies write failed: %s", e)
        return _cors(web.json_response({"ok": False, "error": str(e)}, status=500))
    return _cors(web.json_response({"ok": True, "account": account}))


async def seed_handler(request):
    """Seed the spider from the user's own followers/following as hop-0 targets."""
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    users = body.get("users") or []
    added = 0
    if platform == "instagram":
        pool = request.app["pool"]
        async with pool.acquire() as conn:
            for u in users:
                uname = (u.get("username") if isinstance(u, dict) else str(u) or "").strip().lstrip("@")
                if not uname:
                    continue
                res = await conn.execute(
                    """
                    INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                    VALUES ($1, 0, 'self') ON CONFLICT (username) DO NOTHING
                    """,
                    uname,
                )
                if res.endswith("1"):
                    added += 1
        await _record_users(request.app["pool"], platform, users, "follow")
    return _cors(web.json_response({"added": added}))


async def profile_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    await _archive_browser_capture(request.app["pool"], platform, "profile", body)
    p = body.get("profile") or {}
    await _save_profile(request.app["pool"], platform, p)
    await _record_browser_ingest_event(
        request.app["pool"],
        platform,
        "profile",
        (p.get("username") or body.get("username") or "").strip().lstrip("@") or None,
        observed_count=1 if p else 0,
        stored_count=1 if p else 0,
    )
    # Follow-aware access recording (Phase 0): the extension just successfully
    # fetched this profile with its logged-in account (body["owner"]) — so that
    # account CAN see the target. Populates profile_access for the selector.
    if platform == "instagram":
        await _record_ig_access(
            request.app["pool"], (p.get("username") or "").strip().lstrip("@"),
            body.get("owner"), p,
        )
    # download the profile photo as a kind=profile media item
    pic = p.get("profile_pic_url")
    uname = (p.get("username") or "").strip().lstrip("@")
    if pic and uname:
        await _ingest(request.app, platform, {"username": uname, "items": [
            {"url": pic, "content_id": str(p.get("user_id") or uname), "content_type": "photo",
             "kind": "profile", "entity_name": uname}]})
    return _cors(web.json_response({"ok": True}))


async def posts_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    posts = body.get("posts") or []
    if not isinstance(posts, list):
        posts = []
    _schedule_app_task(
        request.app,
        _posts_ingest_background(request.app, platform, body),
        "browser_posts_ingest",
    )
    return _cors(web.json_response({
        "ok": True,
        "queued": True,
        "observed": len(posts),
        "saved": 0,
    }))


async def _posts_ingest_background(app, platform: str, body: dict) -> None:
    posts = body.get("posts") or []
    if not isinstance(posts, list):
        posts = []
    sem = app.get("structured_sem")
    if sem is None:
        sem = asyncio.Semaphore(SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY)
        app["structured_sem"] = sem
    async with sem:
        await _archive_browser_capture(app["pool"], platform, "posts", body)
        n = await _save_posts(app["pool"], platform, posts)
        await _record_browser_ingest_event(
            app["pool"],
            platform,
            "posts",
            body.get("username") or body.get("owner"),
            observed_count=len(posts),
            stored_count=n,
        )
        # post authors (threads/facebook carry author_username) count as seen users
        authors = [{"username": p.get("author_username")} for p in posts if isinstance(p, dict) and p.get("author_username")]
        await _record_users(app["pool"], platform, authors, "author")


async def comments_handler(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    await _archive_browser_capture(request.app["pool"], platform, "comments", body)
    comments = body.get("comments") or []
    n = await _save_comments(request.app["pool"], platform, body.get("post_id"), comments)
    await _record_browser_ingest_event(
        request.app["pool"],
        platform,
        "comments",
        body.get("post_id"),
        observed_count=len(comments),
        stored_count=n,
    )
    # every commenter is a user we've seen
    authors = [{"username": c.get("author_username"), "user_id": c.get("author_platform_id")} for c in comments if c.get("author_username") or c.get("author_platform_id")]
    await _record_users(request.app["pool"], platform, authors, "comment")
    return _cors(web.json_response({"saved": n}))


async def ingest(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    return _cors(web.json_response(await _ingest(request.app, platform, body)))


async def ingest_upload(request):
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    _schedule_app_task(
        request.app,
        _ingest_uploaded_media(request.app, platform, body),
        "browser_upload_ingest",
    )
    return _cors(web.json_response(_queued_browser_upload_response(platform, body)))


async def ingest_upload_binary(request):
    body: dict = {}
    file_bytes: bytes | None = None
    file_mime: str | None = None
    file_name: str | None = None
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "metadata":
                try:
                    parsed = json.loads(await part.text())
                except Exception:
                    parsed = {}
                if isinstance(parsed, dict):
                    body = parsed
            elif part.name == "file":
                file_name = part.filename
                file_mime = part.headers.get("Content-Type")
                file_bytes = await part.read(decode=False)
    except Exception as exc:
        logger.warning("browser multipart upload parse failed: %s", exc.__class__.__name__)
        return _cors(web.json_response({"ok": False, "error": "bad_multipart"}, status=400))

    if not isinstance(body, dict):
        body = {}
    platform = _norm_platform(body.get("platform"))
    item = body.get("item") if isinstance(body.get("item"), dict) else {}
    item = dict(item)
    if not file_bytes:
        return _cors(web.json_response({"ok": False, "error": "missing_file", "platform": platform}, status=400))

    item["data_bytes"] = file_bytes
    if file_mime and not item.get("mime_type"):
        item["mime_type"] = file_mime
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    item["meta"] = {
        **meta,
        "browser_upload_transport": "multipart",
        "browser_upload_filename": file_name,
    }
    body["item"] = item
    body["file_size"] = len(file_bytes)
    if file_mime and not body.get("mime_type"):
        body["mime_type"] = file_mime

    _schedule_app_task(
        request.app,
        _ingest_uploaded_media(request.app, platform, body),
        "browser_upload_binary_ingest",
    )
    return _cors(web.json_response(_queued_browser_upload_response(platform, body)))


async def ingest_ig(request):  # /ig/ingest alias
    body = await _safe_json(request)
    return _cors(web.json_response(await _ingest(request.app, "instagram", body)))


async def sw_crash_handler(request):
    """Accept extension MV3 service-worker crash reports (companion to a014dc4).

    background.js POSTs here from its self.addEventListener('error') and
    'unhandledrejection' hooks. Persist to browser_ingest_events under the
    'bridge' diagnostic platform so operators can query recent SW instability
    (e.g. `SELECT metadata FROM browser_ingest_events WHERE endpoint='sw_crash'
    ORDER BY created_at DESC`) and log at WARNING so it shows in docker logs.
    Best-effort — never fails the request.
    """
    body = await _safe_json(request)
    kind = str(body.get("kind") or "sw_crash")[:64]
    message = str(body.get("message") or "")[:512]
    ext_version = str(body.get("extension_version") or "unknown")[:32]
    logger.warning(
        "extension SW crash: kind=%s ext=%s msg=%s",
        kind, ext_version, message,
    )
    pool = request.app.get("pool")
    subject = ext_version[:128]
    _schedule_app_task(
        request.app,
        _record_browser_ingest_event(
            pool,
            "bridge",
            "sw_crash",
            subject,
            observed_count=1,
            stored_count=0,
            metadata=body if isinstance(body, dict) else None,
        ),
        label="sw_crash_record",
    )
    return _cors(web.json_response({"ok": True}, status=202))


# ---------------------------------------------------------------------------
async def _safe_json(request):
    if request.get("_json_cache") is not None:
        return request["_json_cache"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    request["_json_cache"] = body
    return body


async def health(request):
    return _cors(web.json_response({
        "ok": True,
        "db_pool": request.app.get("pool") is not None,
        "startup_error": request.app.get("startup_error"),
        "startup_pending": bool(request.app.get("startup_pending")),
    }))


async def _prepare_db_pool_and_schema(app):
    try:
        async with asyncio.timeout(10):
            app["pool"] = await get_pool()
    except TimeoutError:
        app["startup_error"] = "db_pool_timeout"
        logger.exception("startup DB pool timed out")
    except Exception as exc:
        app["startup_error"] = f"db_pool_error:{exc.__class__.__name__}"
        logger.exception("startup DB pool failed")
    if app.get("pool") is not None:
        try:
            async with asyncio.timeout(SOCIAL_INGEST_STARTUP_DDL_TIMEOUT_SECONDS):
                async with app["pool"].acquire() as conn:
                    await conn.execute(_SPIDER_DDL)
                    await _execute_ddl_script(conn, _X_TARGETS_DDL)
                    await _execute_ddl_script(conn, _TIKTOK_BROWSER_MEDIA_DDL)
                    await _execute_ddl_script(conn, _BROWSER_MEDIA_CANDIDATES_DDL)
            app["startup_error"] = None
        except TimeoutError:
            app["startup_error"] = "startup_ddl_timeout"
            logger.exception("startup DDL timed out")
        except Exception as exc:
            app["startup_error"] = f"startup_ddl_error:{exc.__class__.__name__}"
            logger.exception("startup DDL failed")
    app["startup_pending"] = False


async def _on_startup(app):
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    app["previous_loop_exception_handler"] = previous_exception_handler

    def _connection_lost_exception_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionError) and "unexpected connection_lost()" in str(exc):
            logger.debug("suppressed browser client disconnect noise: %s", exc)
            return
        if previous_exception_handler is not None:
            previous_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_connection_lost_exception_handler)
    app["pool"] = None
    app["startup_error"] = None
    app["startup_pending"] = True
    app["tasks"] = set()
    app["sem"] = asyncio.Semaphore(DL_CONCURRENCY)
    app["upload_sem"] = asyncio.Semaphore(SOCIAL_INGEST_UPLOAD_CONCURRENCY)
    app["structured_sem"] = asyncio.Semaphore(SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY)
    app["session"] = aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    )
    if SOCIAL_INGEST_PREP_DB_ON_STARTUP:
        task = asyncio.create_task(_prepare_db_pool_and_schema(app))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)
    else:
        app["startup_pending"] = False
        app["startup_error"] = "db_lazy_init"


async def _on_cleanup(app):
    for t in list(app.get("tasks", [])):
        t.cancel()
    await app["session"].close()
    await close_pool()
    try:
        asyncio.get_running_loop().set_exception_handler(app.get("previous_loop_exception_handler"))
    except Exception:
        pass


def make_app():
    app = web.Application(
        client_max_size=SOCIAL_INGEST_CLIENT_MAX_MB * 1024 * 1024,
        middlewares=[request_timeout_middleware, db_pool_middleware],
    )
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    # generic multi-platform
    app.router.add_get("/social/targets", get_targets)
    app.router.add_get("/social/ig_cooldown", ig_cooldown)
    app.router.add_post("/social/ingest", ingest)
    app.router.add_post("/social/ingest-upload", ingest_upload)
    app.router.add_post("/social/ingest-upload-binary", ingest_upload_binary)
    app.router.add_post("/social/browser-media-candidates", browser_media_candidates)
    app.router.add_post("/social/discover", discover)
    app.router.add_post("/social/target-status", target_status_handler)
    app.router.add_post("/social/posts", posts_handler)
    app.router.add_post("/social/comments", comments_handler)
    app.router.add_post("/social/users", users_handler)
    app.router.add_post("/social/profile", profile_handler)
    app.router.add_post("/social/seed", seed_handler)
    app.router.add_post("/social/dms", dms_handler)
    app.router.add_post("/social/cookies", cookies_handler)
    app.router.add_post("/social/dm-frame", dm_frame_handler)
    app.router.add_post("/social/dm-sample", dm_sample_handler)
    app.router.add_post("/social/dm-probe", dm_probe_handler)
    app.router.add_post("/social/dm-heartbeat", dm_hook_heartbeat_handler)
    app.router.add_post("/social/dm-decoded", dm_decoded_handler)
    app.router.add_get("/social/x-profile-target", x_profile_target_next)
    app.router.add_post("/social/x-profile-target-result", x_profile_target_result)
    app.router.add_get("/social/browser-revisit-target", browser_revisit_target)
    app.router.add_post("/social/browser-revisit-result", browser_revisit_result)
    app.router.add_get("/social/tiktok-revisit-target", tiktok_revisit_target)
    app.router.add_post("/social/tiktok-revisit-result", tiktok_revisit_result)
    app.router.add_get("/social/strava-route-queue", strava_route_queue_handler)
    app.router.add_post("/social/strava-route-visit", strava_route_visit_handler)
    app.router.add_post("/social/strava-streams", strava_streams_handler)
    app.router.add_post("/social/browser-heartbeat", browser_heartbeat_handler)
    app.router.add_post("/social/sw-crash", sw_crash_handler)
    # instagram back-compat aliases
    app.router.add_get("/ig/targets", get_targets_ig)
    app.router.add_post("/ig/ingest", ingest_ig)
    app.router.add_post("/ig/discover", discover_ig)
    app.router.add_get("/health", health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
