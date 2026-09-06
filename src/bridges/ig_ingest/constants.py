"""Module-level configuration constants for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 (see
``docs/plans/perf-file-splits.md`` §4B). Every value here is *immutable* —
env-parsed once at import time. Mutable module state (dedup caches, lock
holders, refresh timestamps) stays in ``__init__.py`` because those objects
are shared across handlers and must be a single object identity.

Consumers should import specific names (`from .constants import PORT`) or
use the star re-export from ``__init__.py`` for backward compatibility with
tests that reach for `src.bridges.ig_ingest.<CONSTANT>`.
"""
import os
import re


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
try:
    SOCIAL_INGEST_HEARTBEAT_CONCURRENCY = max(
        1,
        int(os.getenv("SOCIAL_INGEST_HEARTBEAT_CONCURRENCY", "16")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_HEARTBEAT_CONCURRENCY = 16
try:
    SOCIAL_INGEST_WRITE_CONCURRENCY = max(
        1,
        int(os.getenv("SOCIAL_INGEST_WRITE_CONCURRENCY", "4")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_WRITE_CONCURRENCY = 4
try:
    SOCIAL_INGEST_REVISIT_CONCURRENCY = max(
        1,
        int(os.getenv("SOCIAL_INGEST_REVISIT_CONCURRENCY", "2")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_REVISIT_CONCURRENCY = 2
try:
    SOCIAL_INGEST_DM_SAMPLE_CONCURRENCY = max(
        1,
        int(os.getenv("SOCIAL_INGEST_DM_SAMPLE_CONCURRENCY", "1")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_DM_SAMPLE_CONCURRENCY = 1
try:
    SOCIAL_INGEST_LANE_WAIT_SECONDS = max(
        0.0,
        float(os.getenv("SOCIAL_INGEST_LANE_WAIT_SECONDS", "0.25")),
    )
except (TypeError, ValueError):
    SOCIAL_INGEST_LANE_WAIT_SECONDS = 0.25
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
X_ZERO_PROGRESS_PROBES = {
    "no_dom_media_candidates",
    "try_again_empty_state",
    "x_blank_spa_shell",
    "x_no_status_links",
}
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

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_THREADS_SYNTHETIC_MEDIA_ID = re.compile(r"^(?:img|vid)_[a-z0-9]+$", re.IGNORECASE)

# Platforms the bridge may push. Each may carry its own famous-cap / hop config.
# Only instagram currently spiders (followers/following graph); the others scrape
# whatever the open page exposes, so they have no spider table.
KNOWN_PLATFORMS = {"instagram", "tiktok", "lemon8", "x", "threads", "facebook", "strava"}
BROWSER_DIAGNOSTIC_PLATFORMS = {"bridge"}
_BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS = {"x", "facebook", "tiktok", "lemon8", "threads"}

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
STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS = float(os.getenv("STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS", "30.0"))
STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS = float(os.getenv("STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS", "2.0"))
STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS = float(os.getenv("STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS", "600.0"))

CREDENTIALS_ROOT = os.getenv("CREDENTIALS_ROOT", "/app/credentials")
