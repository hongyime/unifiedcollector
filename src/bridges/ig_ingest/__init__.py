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
import logging
import os
import re
import time

import aiohttp
from aiohttp import web

from src.db.connection import get_pool, close_pool
from src.core.priority_hints import refresh_collector_priority_hints as refresh_collector_priority_hints
from src.core.proximity import refresh_account_proximity_cache as refresh_account_proximity_cache
from src.core.recovery_profiles import mark_recovery_observation as mark_recovery_observation
from src.core.dynamic_cooldown import record_dynamic_cooldown as record_dynamic_cooldown
from src.core.strava_route_queue import fetch_strava_route_capture_queue as fetch_strava_route_capture_queue
from src.core.vault import (
    VAULT_ROOT as VAULT_ROOT,
    assert_media_write_allowed as assert_media_write_allowed,
    verify_media_item_db_consistency as verify_media_item_db_consistency,
    write_media_sidecar as write_media_sidecar,
    write_raw_payload as write_raw_payload,
)
from src.core.collection_surface import normalize_collection_surface as normalize_collection_surface

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

# Config constants + CORS helper + middlewares live in dedicated modules
# (PERF-003 split, docs/plans/perf-file-splits.md §4B). Re-export the full
# constant surface here so tests and downstream imports that still reach for
# ``src.bridges.ig_ingest.<CONSTANT>`` keep working.
from .constants import *  # noqa: E402,F401,F403 -- re-export
from .constants import (
    BRIDGE_MEDIA_ALLOWED_HOSTS as BRIDGE_MEDIA_ALLOWED_HOSTS,
    BROWSER_CONTENT_HINT_TTL_SECONDS as BROWSER_CONTENT_HINT_TTL_SECONDS,
    BROWSER_CONTENT_STALE_SECONDS as BROWSER_CONTENT_STALE_SECONDS,
    BROWSER_DIAGNOSTIC_PLATFORMS as BROWSER_DIAGNOSTIC_PLATFORMS,
    BROWSER_HEARTBEAT_ONLY_PLATFORMS as BROWSER_HEARTBEAT_ONLY_PLATFORMS,
    BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS as BROWSER_TELEMETRY_WRITE_TIMEOUT_SECONDS,
    DL_CONCURRENCY,
    KNOWN_PLATFORMS as KNOWN_PLATFORMS,
    MEDIA_ROOT as MEDIA_ROOT,
    PORT,
    SOCIAL_INGEST_HEARTBEAT_CONCURRENCY,
    SOCIAL_INGEST_PREP_DB_ON_STARTUP,
    SOCIAL_INGEST_REVISIT_CONCURRENCY,
    SOCIAL_INGEST_STARTUP_DDL_TIMEOUT_SECONDS,
    SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY,
    SOCIAL_INGEST_UPLOAD_CONCURRENCY,
    SOCIAL_INGEST_WRITE_CONCURRENCY,
    STRAVA_BROWSER_429_COOLDOWN_SECONDS as STRAVA_BROWSER_429_COOLDOWN_SECONDS,
    STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS as STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS,
    STRAVA_BROWSER_429_MEMORY_SECONDS as STRAVA_BROWSER_429_MEMORY_SECONDS,
    TIKTOK_FOLLOW_OWNER_FALLBACK as TIKTOK_FOLLOW_OWNER_FALLBACK,
    UC_EXTENSION_EXPECTED_VERSION,
    X_ZERO_PROGRESS_PROBES as X_ZERO_PROGRESS_PROBES,
)
from .constants import (  # noqa: E402  -- explicit for the private names not in *
    _SAFE as _SAFE,
    _THREADS_SYNTHETIC_MEDIA_ID as _THREADS_SYNTHETIC_MEDIA_ID,
    _BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS as _BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS,
)
from .cors import _cors, handle_options  # noqa: E402,F401
from .middleware import (  # noqa: E402,F401
    _social_ingest_lane_for_path,
    _request_timeout_seconds,
    _STRUCTURED_CAPTURE_PATHS,
    _HEARTBEAT_LANE_PATHS,
    _REVISIT_LANE_PATHS,
    _WRITE_LANE_PATHS,
    _FAST_LANE_PATHS,
    request_timeout_middleware,
    lane_isolation_middleware,
    db_pool_middleware,
)


# 2-hop spider (instagram only): the extension scrapes a target's media AND, when
# the target's hop < MAX_HOP, crawls its followers/following and POSTs them to
# discover; we store them at hop+1 in instagram_spider_targets (a channel SEPARATE
# from collection_targets so the .targets file-sync never wipes them). Famous
# accounts (follower_count > cap) are dropped — we want your network, not celebs.
# NOTE: IG_SPIDER_*, SOCIAL_TARGET_*, X_PROFILE_TARGET_*, TIKTOK_FOLLOW_OWNER_FALLBACK,
# STRAVA_ROUTE_QUEUE_RESPONSE_* — all moved to .constants (imported at top).
# _SOCIAL_TARGET_* mutable state moved to .targets per split step 4.
_STRAVA_ROUTE_QUEUE_RESPONSE_CACHE: dict[str, tuple[float, dict]] = {}
_STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST: dict[str, float] = {}

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






















# ---------------------------------------------------------------------------
# CORS helpers moved to .cors (imported at top of module).
# Middleware and path classifiers moved to .middleware (imported at top).
# ---------------------------------------------------------------------------


# Pool bootstrap moved to .pool per split step 3.
from .pool import (  # noqa: E402,F401
    _PoolRef,
    _startup_state,
    _set_startup_error,
    _set_startup_pending,
    _ensure_app_pool,
    _schedule_app_task,
)


# db_pool_middleware moved to .middleware (imported at top of module).


# Targets + discover moved to .targets and .discover per split step 4.
from .targets import (  # noqa: E402,F401
    _refresh_target_side_caches,
    _targets_for,
    _cached_targets_for,
    get_targets,
    get_targets_ig,
    _SOCIAL_TARGET_CACHE_REFRESH_LAST,
    _SOCIAL_TARGET_CACHE_REFRESH_LOCK,
    _SOCIAL_TARGET_CACHE_REFRESH_TASKS,
    _SOCIAL_TARGET_RESPONSE_CACHE,
    _SOCIAL_TARGET_RESPONSE_LOCKS,
)
from .discover import _discover, discover, discover_ig  # noqa: E402,F401


# Ingest + upload handlers moved to .ingest per split step 5.
from .ingest import (  # noqa: E402,F401
    ingest,
    ingest_upload,
    ingest_upload_binary,
    ingest_ig,
    browser_media_candidates,
)


# X profile + revisit handlers moved to .x_profile / .revisit per split step 6.
from .x_profile import x_profile_target_next, x_profile_target_result  # noqa: E402,F401
from .revisit import (  # noqa: E402,F401
    _browser_revisit_platform,
    tiktok_revisit_target,
    tiktok_revisit_result,
    browser_revisit_target,
    browser_revisit_result,
)


# Strava route capture moved to .strava per split step 8.
from .strava import (  # noqa: E402,F401
    strava_streams_handler,
    strava_route_queue_handler,
    strava_route_visit_handler,
    _upsert_strava_browser_stream,
    _record_empty_strava_browser_stream,
    _browser_http_status,
    _browser_account_label,
    _record_strava_stream_http_event,
    _touch_recent_strava_stream_http_event,
    _strava_stream_response_status,
)


# Telemetry moved to .telemetry per split step 9.
from .telemetry import browser_heartbeat_handler, sw_crash_handler  # noqa: E402,F401

# Cooldown moved to .cooldown per split step 10.
from .cooldown import ig_cooldown  # noqa: E402,F401


# Core helpers moved to sibling modules per split step 11.
from ._util import (  # noqa: E402,F401
    _int,
    _num,
    _safe_json,
    _url_hash,
    _item_meta,
    _top_reject_reason,
    _reject_count,
    _inspect_media_payload,
    _merge_reject_stats,
    _stream_values,
    _json_array_or_none,
    _queued_browser_upload_response,
)
from ._ids import (  # noqa: E402,F401
    _execute_ddl_script,
    _ig_date_from_id,
    _shortcode_from_id,
    _verify_url,
    _date_prefix,
    _norm_platform,
    _explicitly_verified_capture,
    _IG_EPOCH,
    _IG_LONG,
    _SC_ALPHABET,
    _VERIFIED_CAPTURE_KEYS,
    _VERIFIED_FILTER_PLATFORMS,
)
from ._media import (  # noqa: E402,F401
    BridgeMediaRejected,
    _download_headers,
    _is_allowed_media_host,
    _validate_media_url,
    _threads_media_content_base_id,
    _media_store_content_id,
    _write_extension_media_artifacts_sync,
    _candidate_asset_role,
    _tiktok_asset_role,
    _candidate_dimensions,
    _candidate_is_video,
    _tiktok_candidate_is_video,
    _looks_like_tiny_thumbnail,
    _classify_tiktok_candidate_result,
    _classify_browser_candidate_result,
    _tiktok_candidate_post_url,
    _canonical_browser_revisit_url,
    _browser_candidate_post_url,
    _browser_revisit_priority,
    _prepare_tiktok_browser_candidate,
    _record_tiktok_browser_candidate,
    _prepare_browser_media_candidate,
    _record_browser_media_candidate,
    _TIKTOK_BROWSER_MEDIA_CANDIDATE_UPSERT_SQL,
    _TIKTOK_BROWSER_REVISIT_UPSERT_SQL,
    _BROWSER_MEDIA_CANDIDATE_UPSERT_SQL,
    _BROWSER_MEDIA_REVISIT_UPSERT_SQL,
)
from ._download import (  # noqa: E402,F401
    _download_and_save,
    _drain,
    BRIDGE_MEDIA_MAX_BYTES,
)
from ._events import (  # noqa: E402,F401
    _record_browser_ingest_event,
    _write_browser_ingest_event,
    _browser_event_marks_source_success,
    _browser_event_duplicate_count,
)
from ._browser_ingest import (  # noqa: E402,F401
    _ingest,
    _ingest_uploaded_media,
    _record_browser_media_candidate_batch,
)
from ._handles import (  # noqa: E402,F401
    _derive_handle_from_caption,
    _ig_author_uid,
    _x_handle,
    _tiktok_handle,
    _lemon8_handle,
    _owner_account_for_follow,
    _executemany_with_fallback,
    _enqueue_tiktok_profile_targets,
    _enqueue_lemon8_profile_targets,
    _x_post_url,
    _enqueue_x_profile_targets,
    _record_x_edge,
    _record_x_post_graph,
    _ensure_ig_profile,
    _HANDLE_RE,
    _X_HANDLE_RE,
    _LEMON8_HANDLE_RE,
    _TIKTOK_HANDLE_RE,
)
from ._posts import (  # noqa: E402,F401
    _save_posts,
    _save_comments,
    _cross_seed_instagram,
    _record_users,
    _SOCIAL_USERS_UPSERT_SQL,
    _FOLLOW_EDGES_UPSERT_SQL,
    record_users,
    save_comments,
    save_posts,
)
from ._profiles import (  # noqa: E402,F401
    _save_profile,
    _record_profile_capability_receipts,
    _track_ig_profile_change,
    _record_ig_access,
    save_profile,
)
from ._capture import (  # noqa: E402,F401
    _archive_browser_capture,
    _browser_capture_subject,
    _BROWSER_CAPTURE_TARGET_TABLES,
    _BROWSER_CAPTURE_COMPRESSED_ENDPOINTS,
)
from ._hints import (  # noqa: E402,F401
    _browser_content_recovery_hint,
    _browser_content_timeout_hint,
    _BROWSER_CONTENT_HINT_CACHE,
    _BROWSER_CONTENT_HINT_INFLIGHT,
)
from ._legacy import (  # noqa: E402,F401
    _legacy_target_status_handler,
    _legacy_users_handler,
    _legacy_seed_handler,
    _legacy_profile_handler,
    _legacy_posts_handler,
    _posts_ingest_background,
    _legacy_comments_handler,
)


# Targets endpoints moved to .targets per split step 4.


# x_profile_target_next moved to .revisit / .x_profile per split step 6.


# ig_cooldown moved to .cooldown per split step 10.


























































































# browser_media_candidates moved to .ingest per split step 5.


# tiktok_revisit_target moved to .revisit / .x_profile per split step 6.















































































# strava_streams_handler moved to .strava per split step 8.




# strava_route_queue_handler moved to .strava per split step 8.






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


# browser_heartbeat_handler moved to .cookies / .telemetry per split step 9.












# ingest moved to .ingest per split step 5.


# sw_crash_handler moved to .cookies / .telemetry per split step 9.




async def health(request):
    state = _startup_state(request.app)
    pool_ready = bool(request.app.get("pool"))
    pending = bool(state.get("pending"))
    startup_error = state.get("error")
    # Health honesty (REL-001 / INTR-001 / LOGIC-002 fix): a green /health while
    # db_pool is absent AND the startup task has given up (pending=False) is
    # what left this container running dark for 42h before manual detection.
    # Report 503 in that hard-broken state so Docker's healthcheck flips the
    # container to unhealthy and the watchdog / operator notices. If startup
    # is still trying (pending=True), stay 200 to avoid restart storms during
    # normal boot delays.
    ok = pool_ready or pending
    status = 200 if ok else 503
    return _cors(web.json_response({
        "ok": ok,
        "db_pool": pool_ready,
        "startup_error": startup_error,
        "startup_pending": pending,
        "lanes": {
            "heartbeat": {"concurrency": SOCIAL_INGEST_HEARTBEAT_CONCURRENCY},
            "write": {"concurrency": SOCIAL_INGEST_WRITE_CONCURRENCY},
            "revisit": {"concurrency": SOCIAL_INGEST_REVISIT_CONCURRENCY},
        },
    }, status=status))


async def _prepare_db_pool_and_schema(app):
    # CONC-001 / REL-001 fix: retry the initial pool build on transient
    # startup failures instead of giving up permanently. Postgres slow-start
    # during host boot was the observed trigger: the first attempt raced the
    # DB service_healthy signal and hit `CannotConnectNowError: the database
    # system is starting up`, then TimeoutError from the async-with wrapper.
    # We keep `startup_pending=True` for the whole retry window so the /health
    # endpoint stays 200 during a legitimate startup delay and only flips to
    # 503 after we truly give up.
    holder = app.get("pool")
    total_budget = float(os.getenv("SOCIAL_INGEST_STARTUP_POOL_BUDGET_SECONDS", "300"))
    per_attempt = float(os.getenv("SOCIAL_INGEST_STARTUP_POOL_ATTEMPT_SECONDS", "10"))
    backoff = 3.0
    max_backoff = 30.0
    deadline = time.monotonic() + total_budget
    pool = None
    _set_startup_pending(app, True)
    last_error = None
    while pool is None and time.monotonic() < deadline:
        try:
            async with asyncio.timeout(per_attempt):
                pool = await get_pool()
            if isinstance(holder, _PoolRef):
                holder.pool = pool
            else:
                app["pool"] = pool
            break
        except TimeoutError:
            last_error = "db_pool_timeout"
            logger.warning("startup DB pool attempt timed out; retrying")
        except Exception as exc:
            last_error = f"db_pool_error:{exc.__class__.__name__}"
            logger.warning("startup DB pool attempt failed (%s); retrying", exc.__class__.__name__)
        if pool is None:
            sleep_for = min(backoff, max(1.0, deadline - time.monotonic()))
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 1.5, max_backoff)
    if pool is None:
        _set_startup_error(app, last_error or "db_pool_unavailable")
        _set_startup_pending(app, False)
        logger.error("startup DB pool gave up after %ss (last=%s)", total_budget, last_error)
        return
    try:
        async with asyncio.timeout(SOCIAL_INGEST_STARTUP_DDL_TIMEOUT_SECONDS):
            async with pool.acquire() as conn:
                await conn.execute(_SPIDER_DDL)
                await _execute_ddl_script(conn, _X_TARGETS_DDL)
                await _execute_ddl_script(conn, _TIKTOK_BROWSER_MEDIA_DDL)
                await _execute_ddl_script(conn, _BROWSER_MEDIA_CANDIDATES_DDL)
        _set_startup_error(app, None)
    except TimeoutError:
        _set_startup_error(app, "startup_ddl_timeout")
        logger.exception("startup DDL timed out")
    except Exception as exc:
        _set_startup_error(app, f"startup_ddl_error:{exc.__class__.__name__}")
        logger.exception("startup DDL failed")
    _set_startup_pending(app, False)


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
    app["pool"] = _PoolRef()
    app["startup_state"] = {"error": None, "pending": True}
    app["tasks"] = set()
    app["sem"] = asyncio.Semaphore(DL_CONCURRENCY)
    app["upload_admission_sem"] = asyncio.Semaphore(SOCIAL_INGEST_UPLOAD_CONCURRENCY)
    app["structured_sem"] = asyncio.Semaphore(SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY)
    app["heartbeat_lane_sem"] = asyncio.Semaphore(SOCIAL_INGEST_HEARTBEAT_CONCURRENCY)
    app["write_lane_sem"] = asyncio.Semaphore(SOCIAL_INGEST_WRITE_CONCURRENCY)
    app["revisit_lane_sem"] = asyncio.Semaphore(SOCIAL_INGEST_REVISIT_CONCURRENCY)
    app["session"] = aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    )
    if SOCIAL_INGEST_PREP_DB_ON_STARTUP:
        task = asyncio.create_task(_prepare_db_pool_and_schema(app))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)
    else:
        _set_startup_pending(app, False)
        _set_startup_error(app, "db_lazy_init")


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
    """Back-compat shim — the factory now lives in ``.app``. Import there to
    keep touches to ``__init__.py`` small."""
    from .app import build_app
    return build_app()


# Compatibility exports for callers that historically imported generic route
# handlers from this package while the implementation now lives in routes/.
from .routes.generic import (  # noqa: E402,F401
    comments_handler,
    posts_handler,
    profile_handler,
    seed_handler,
    target_status_handler,
    users_handler,
)


# Module-level app instance so downstream code and tests can do
# `from src.bridges.ig_ingest import app`. Route registration happens
# eagerly at import time; startup hooks (DB pool bootstrap) only fire
# when `web.run_app(app)` is actually called (see __main__.py).
app = make_app()


if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)