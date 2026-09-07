import asyncio
import contextlib
import html
import io
import json
import logging
import os
import re
import time
import traceback
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# PERF-002 sub-plan 4A — dashboard/api package split status.
#
# Sub-plan 4A steps 1–12 have landed. See docs/plans/perf-file-splits.md.
#
#   1.  api/__init__.py package skeleton (git mv)
#   2.  api/helpers.py — module-level caches, pure helpers, DB pool helpers,
#                        row/cache copy utilities, _dt_for_compare
#   3.  api/health_helpers.py — vault/backup/drive health payload builders
#   4.  api/auth.py — JWT/bcrypt + /auth/* routes
#   5.  api/browser.py — Chrome extension + browser-tab diagnostics used by
#                        the /health and /collectors/source-matrix payloads
#   6.  api/source_matrix.py — payload cache helpers + unavailable-payload
#                              builders (the huge _collectors_source_matrix_payload
#                              builder still lives here — see module docstring)
#   7.  api/telegram_ops.py — /api/telegram/* onboarding-ops routes
#   8.  api/strava.py — /strava/* routes
#   9.  api/youtube.py — /youtube/* routes
#   10. api/whatsapp.py — /whatsapp/* routes + link filter constants
#   11. api/coverage.py — /coverage/collectors
#       api/media.py — /media, /media/stats, /media/browse, /media/{id}/thumbnail,
#                       /media/{id}/file, /media/realtime-feed/*, /media/artifact-audit
#   12. api/rate_limits.py — /rate-limits/recent
#
# Sibling sub-plans (separate work):
#   PERF-003 sub-plan 4B — bridges/ig_ingest.py package split
#   PERF-004 sub-plan 4C — collectors/telegram/__init__.py mixin split
#
# What remains in this file:
# * The FastAPI app object + CORS/static/router assembly.
# * Many domain routes that weren't in a step-5-12 slice (/health, /metrics,
#   /collectors, /collectors/live, /collectors/source-matrix, /collectors/action-queue,
#   /platform/{name}/summary, /social/*, /accounts, /dlq, /graph, /messaging/coverage,
#   /instagram/*, /tiktok/*, /threads/*, /facebook/*, /github/*, /lemon8/*,
#   /beeper/*, /telegram/chats, /telegram/chat/{chat_id}, /api/matrix/*,
#   /worker/health, /schedules, /targets, /runs, /domain-pacing/status,
#   /api-quotas/status, /instagram/dms/*, /tiktok/dms/*, /dm/telemetry,
#   /stories/overview, /ingestion/hourly, /seen/targets, /optional-rollout/status,
#   /recon/*).
# * The huge _collectors_source_matrix_payload builder + row/blocker/section
#   helpers (still tightly coupled to test monkey-patch surface).
# * Miscellaneous DB query helpers still used across those routes.
#
# The <300 LOC target for this file is aspirational — it requires moving all
# remaining routes into per-domain modules following the same pattern. That is
# future work.
# ---------------------------------------------------------------------------

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.backup.db_backup import backup_status
from src.db.connection import get_pool
from src.dashboard.websocket import health_ws
from src.core.strava_route_queue import fetch_strava_route_capture_queue
from src.core.collection_coverage import build_collection_coverage_snapshot
from src.core.seen_targets import (
    list_seen_targets,
    refresh_seen_targets_from_sources,
    seen_target_summary_by_source,
)
from src.core.optional_rollout import optional_rollout_report
from src.core.vault import VAULT_ROOT, vault_artifact_counts, vault_health
from src.core.whatsapp_bridge_health import (
    fetch_whatsapp_bridge_health,
    summarize_whatsapp_bridge_health,
)

logger = logging.getLogger(__name__)

# Caches, TTL config and small pure helpers extracted to api/helpers.py during
# PERF-002 sub-plan 4A step 2. Re-imported here so private call sites and
# tests that reference these names via `src.dashboard.api._foo` keep working.
from src.dashboard.api.helpers import (  # noqa: E402,F401
    _MESSAGING_COVERAGE_CACHE,
    _SOURCE_MEDIA_TOTALS_CACHE,
    _SOURCE_MEDIA_TOTALS_TTL_SECONDS,
    _BEEPER_SUBSOURCE_CONTENT_CACHE,
    _BEEPER_SUBSOURCE_CONTENT_TTL_SECONDS,
    _BEEPER_SUBSOURCE_QUERY_TIMEOUT_SECONDS,
    _BEEPER_SUBSOURCE_TOTAL_TIMEOUT_SECONDS,
    _BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE,
    _BEEPER_SUBSOURCE_MEDIA_TOTALS_TTL_SECONDS,
    _BEEPER_SUBSOURCE_MEDIA_TOTALS_FAILURE_TTL_SECONDS,
    _BEEPER_SUBSOURCE_LIVENESS_CACHE,
    _BEEPER_SUBSOURCE_LIVENESS_TTL_SECONDS,
    _BEEPER_SUBSOURCE_STALE_SECONDS,
    _BEEPER_SUBSOURCE_PREFIX,
    _SOURCE_MATRIX_SECTION_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_LIVENESS_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_DAY_CONTENT_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_MEDIA_TOTALS_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_YOUTUBE_BACKLOG_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_ENABLE_YOUTUBE_BACKLOG,
    _SOURCE_MATRIX_BROWSER_EXTENSION_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_SECTION_CACHE,
    _SOURCE_MATRIX_SECTION_CACHE_TTL_SECONDS,
    _SOURCE_MATRIX_SECTION_STALE_SECONDS,
    _SOURCE_MATRIX_PAYLOAD_CACHE,
    _SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS,
    _SOURCE_MATRIX_PAYLOAD_STALE_SECONDS,
    _SOURCE_MATRIX_PAYLOAD_BUILD_TIMEOUT_SECONDS,
    _SOURCE_MATRIX_PAYLOAD_CACHE_PATH,
    _COLLECTORS_LIVE_CACHE,
    _COLLECTORS_LIVE_CACHE_TTL_SECONDS,
    _COLLECTORS_LIVE_STALE_SECONDS,
    _TELEGRAM_STATS_CACHE,
    _TELEGRAM_STATS_TTL_SECONDS,
    _YOUTUBE_MEDIA_BACKLOG_CACHE,
    _YOUTUBE_MEDIA_BACKLOG_TTL_SECONDS,
    _YOUTUBE_COMPLETENESS_CACHE,
    _YOUTUBE_COMPLETENESS_TTL_SECONDS,
    _tiktok_revisit_claim_timeout_seconds,
    _encode_polyline,
    _jsonb_points,
    _row_get,
    _iso_or_none,
    _safe_row,
    _strava_route_status,
    _estimated_table_rows,
    _DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS,
    _acquire_dashboard_conn,
    _release_dashboard_conn,
    _dt_for_compare,
)

# Constants that remain in __init__.py because they belong to later split
# sub-steps (6–12: source-content/coverage/rate-limits/dashboard-health).
_SOURCE_CONTENT_PART_TIMEOUT_SECONDS = float(os.getenv("SOURCE_CONTENT_PART_TIMEOUT_SECONDS", "0.75"))
_SOURCE_CONTENT_MEDIA_TIMEOUT_SECONDS = float(os.getenv("SOURCE_CONTENT_MEDIA_TIMEOUT_SECONDS", "1.25"))
_SOURCE_CONTENT_SUMMARY_BUDGET_SECONDS = float(os.getenv("SOURCE_CONTENT_SUMMARY_BUDGET_SECONDS", "2.5"))
# Mutable module-level state — reassigned via `global` in callers, so it must
# live in this namespace, not in helpers.py.
_SOURCE_MATRIX_PAYLOAD_BUILD_TASK: asyncio.Task | None = None
_COVERAGE_SNAPSHOT_STALE_SECONDS = int(os.getenv("COVERAGE_SNAPSHOT_STALE_SECONDS", "3600"))
_INGESTION_HOURLY_CACHE: dict[int, dict[str, object]] = {}
_DASHBOARD_HEALTH_SOURCES_TIMEOUT_SECONDS = float(os.getenv("DASHBOARD_HEALTH_SOURCES_TIMEOUT_SECONDS", "10"))
_DASHBOARD_HEALTH_BROWSER_TIMEOUT_SECONDS = float(os.getenv("DASHBOARD_HEALTH_BROWSER_TIMEOUT_SECONDS", "10"))
_RATE_LIMITS_RECENT_CACHE: dict[tuple[int, int], dict[str, object]] = {}
_RATE_LIMITS_RECENT_STALE_SECONDS = int(os.getenv("RATE_LIMITS_RECENT_STALE_SECONDS", "300"))


async def _safe_estimated_table_rows(conn, table: str) -> int:
    try:
        return await _estimated_table_rows(conn, table)
    except Exception:
        return 0


async def _safe_fetch_int(conn, query: str, *args, timeout: float = 8.0, default: int = 0) -> int:
    try:
        return int(await conn.fetchval(query, *args, timeout=timeout) or 0)
    except Exception:
        return default


# Vault / backup / drive health helpers moved to api/health_helpers.py during
# PERF-002 sub-plan 4A step 3. Re-imported for back-compat.
from src.dashboard.api.health_helpers import (  # noqa: E402,F401
    _vault_payload,
    _backup_health_status,
    _normalize_backup_health_payload,
    _vault_health_status,
    _drive_health_status,
)


# Browser / Chrome-extension diagnostics extracted to api/browser.py during
# PERF-002 sub-plan 4A step 5. Re-imported for back-compat with the /health
# and /collectors/source-matrix routes still in __init__.py and with tests
# that reach for these symbols via ``src.dashboard.api``.
from src.dashboard.api.browser import (  # noqa: E402,F401
    router as _browser_router,
    _BROWSER_EXTENSION_QUERY_TIMEOUT_SECONDS,
    _BROWSER_EXTENSION_PAYLOAD_BUDGET_SECONDS,
    _BROWSER_EXTENSION_INGEST_SUMMARY_HOURS,
    _BROWSER_MEDIA_CANDIDATE_SUMMARY_HOURS,
    _BROWSER_EXTENSION_OPTIONAL_QUERY_TIMEOUT_SECONDS,
    _BROWSER_TAB_MAINTENANCE_STATUS_PATH,
    _BROWSER_TAB_AUDIT_RESULT_PATH,
    _BROWSER_TAB_MAINTENANCE_STALE_SECONDS,
    _BROWSER_TAB_MAINTENANCE_RUNNING_STALLED_SECONDS,
    _EXTENSION_RECENT_MISMATCH_SECONDS,
    _OPTIONAL_BROWSER_DIAGNOSTIC_SECTIONS,
    _expected_extension_version,
    _extension_versions_match,
    _extension_reload_target_from_url,
    _extension_management_url,
    _browser_tab_maintenance_payload,
    _browser_tab_audit_platforms,
    _browser_tab_audit_age_seconds,
    _browser_tab_audit_page_errors,
    _browser_extension_add_tab_audit_page_errors,
    _browser_extension_apply_maintenance_ingest_fallback,
    _browser_extension_fallback_payload,
    _browser_ingest_health_from_items,
    _browser_extension_apply_ingest_health,
    _browser_extension_suppress_optional_diagnostics_when_active,
    _browser_extension_fallback_payload_with_fast_ingest,
    _extension_issues_by_source,
    _fresh_current_extension_seen,
    _suppress_shadowed_extension_mismatches,
    _browser_extension_payload,
)

# Source-matrix payload cache + fallback payload helpers extracted to
# api/source_matrix.py during PERF-002 sub-plan 4A step 6.  Step 22 extended
# it with the /collectors/source-matrix route itself; the huge
# _collectors_source_matrix_payload builder + its helpers still live in
# __init__.py and are looked up by source_matrix.py via ``_cfg()``.
from src.dashboard.api.source_matrix import (  # noqa: E402,F401
    router as _source_matrix_router,
    collectors_source_matrix,
    _get_build_task,
    _set_build_task,
)

# Telegram account ops routes extracted to api/telegram_ops.py during
# PERF-002 sub-plan 4A step 7. Re-imports below preserve the public
# ``dashboard_api.X`` surface for tests (TelegramAccountCreate,
# TelegramAccountAuth, _dashboard_auth_sessions, list_telegram_accounts,
# telegram_request_code, telegram_verify_code, delete_telegram_account,
# disable_telegram_account, enable_telegram_account, telegram_stats).
from src.dashboard.api.telegram_ops import (  # noqa: E402,F401
    router as _telegram_ops_router,
    TelegramAccountCreate,
    TelegramAccountAuth,
    _dashboard_auth_sessions,
    list_telegram_accounts,
    telegram_request_code,
    telegram_verify_code,
    delete_telegram_account,
    disable_telegram_account,
    enable_telegram_account,
    telegram_stats,
)

# Strava routes extracted to api/strava.py during PERF-002 sub-plan 4A step 8.
from src.dashboard.api.strava import (  # noqa: E402,F401
    router as _strava_router,
    strava_list_athletes,
    strava_feed_dates,
    strava_feed_activities,
    strava_feed_stats,
    strava_route_capture_queue,
)

# YouTube routes extracted to api/youtube.py during PERF-002 sub-plan 4A step 9.
from src.dashboard.api.youtube import (  # noqa: E402,F401
    router as _youtube_router,
    youtube_completeness,
    list_youtube_channels,
    youtube_channel_detail,
)

# WhatsApp routes + helpers extracted to api/whatsapp.py during PERF-002
# sub-plan 4A step 10.
from src.dashboard.api.whatsapp import (  # noqa: E402,F401
    router as _whatsapp_router,
    _wa_link_filter_values,
    _wa_looks_like_url,
    _wa_link_type_value,
    _wa_link_payload,
    _wa_bridge_base,
    _wa_bridge_post,
    _wa_bridge_get,
    list_wa_users,
    wa_user_history,
    list_wa_chats,
    wa_chat_messages,
    list_wa_links,
    wa_link_stats,
    whatsapp_qr,
    whatsapp_sessions,
    whatsapp_disconnect,
    whatsapp_reconnect,
    whatsapp_fresh_qr,
    whatsapp_pairing_code,
    whatsapp_link_page,
    _should_wait_for_fresh_wa_qr,
    _WA_LINK_TYPE_FILTER_ALIASES,
    _WA_LINK_STATUS_FILTER_ALIASES,
    _WA_LINK_TYPE_VALUES,
    _WA_LINK_TYPE_SQL_VALUES,
    _WA_LINK_TYPE_EXPR,
    _WA_LINK_STATS_TYPE_EXPR,
)

# Coverage route extracted to api/coverage.py during PERF-002 sub-plan 4A step 11.
from src.dashboard.api.coverage import (  # noqa: E402,F401
    router as _coverage_router,
    collectors_coverage,
)

# Media routes extracted to api/media.py during PERF-002 sub-plan 4A step 11.
from src.dashboard.api.media import (  # noqa: E402,F401
    router as _media_router,
    list_media,
    media_stats,
    media_realtime_feed_status,
    media_realtime_feed_deliveries,
    media_artifact_audit,
    browse_media,
    media_thumbnail,
    media_file,
)

# Rate-limits routes extracted to api/rate_limits.py during PERF-002 sub-plan 4A step 12.
from src.dashboard.api.rate_limits import (  # noqa: E402,F401
    router as _rate_limits_router,
    recent_rate_limits,
)

# Operations observability routes (/dlq, /domain-pacing/status, /api-quotas/status)
# extracted to api/ops.py during PERF-002 sub-plan 4A step 14 (cluster 8).
from src.dashboard.api.ops import (  # noqa: E402,F401
    router as _ops_router,
    list_dlq,
    domain_pacing_status,
    _domain_pacing_status_impl,
    api_quotas_status,
)

# Social registry routes (/social/*) extracted to api/social.py during
# PERF-002 sub-plan 4A step 15 (cluster 4).
from src.dashboard.api.social import (  # noqa: E402,F401
    router as _social_router,
    follow_edges_stats,
    social_stats,
    social_network,
    social_users_list,
    social_scrape_config,
    social_page,
    _SOCIAL_HTML,
)

# DM routes (/instagram/dms/*, /tiktok/dms/*, /dm/telemetry) extracted to
# api/dm.py during PERF-002 sub-plan 4A step 16 (cluster 5).
from src.dashboard.api.dm import (  # noqa: E402,F401
    router as _dm_router,
    list_ig_dm_threads,
    ig_dm_thread_messages,
    list_tt_dm_threads,
    tt_dm_thread_messages,
    dm_telemetry,
)

# Targets CRUD routes extracted to api/targets.py during PERF-002 4A step 17
# (cluster 3a).
from src.dashboard.api.targets import (  # noqa: E402,F401
    router as _targets_router,
    TargetRequest,
    _target_already_known,
    create_target,
    delete_target,
    list_targets,
)

# Schedules + runs routes extracted to api/schedules.py during PERF-002 4A
# step 17 (cluster 3b).
from src.dashboard.api.schedules import (  # noqa: E402,F401
    router as _schedules_router,
    ScheduleRequest,
    update_schedule,
    get_run,
    list_schedules,
    create_schedule,
    delete_schedule,
    list_runs,
)

# Accounts overview + platform summary routes extracted to api/accounts.py
# during PERF-002 4A step 18 (cluster 6).
from src.dashboard.api.accounts import (  # noqa: E402,F401
    router as _accounts_router,
    platform_summary,
    accounts_overview,
)

# Backfill-equilibrium, instagram-health, and hourly-ingestion routes extracted
# to api/ingestion.py during PERF-002 4A step 19 (cluster 7).
from src.dashboard.api.ingestion import (  # noqa: E402,F401
    router as _ingestion_router,
    backfill_equilibrium,
    instagram_health,
    _instagram_health_impl,
    _derive_instagram_stuck_stage,
    hourly_ingestion,
)

# Miscellaneous cross-cutting routes (/graph, /messaging/coverage,
# /stories/overview, /worker/health) extracted to api/misc.py during
# PERF-002 4A step 20 (cluster 9).
from src.dashboard.api.misc import (  # noqa: E402,F401
    router as _misc_router,
    social_graph,
    messaging_coverage,
    stories_overview,
    worker_health,
)

# Collector core routes (/collectors, /collectors/live, /collectors/action-queue*,
# /collectors/{source}) extracted to api/collectors_core.py during PERF-002 4A
# step 21 (cluster 2). Route ``/collectors/source-matrix`` still lives in
# __init__.py this iteration — see cluster 1 (source_matrix.py).
from src.dashboard.api.collectors_core import (  # noqa: E402,F401
    router as _collectors_core_router,
    list_collectors,
    collectors_live,
    collectors_action_queue_sync,
    collectors_action_queue,
    collector_detail,
)

# Platform content routes (Matrix + Telegram + TikTok + Threads + GitHub +
# Lemon8 + Beeper) plus /seen/targets, /optional-rollout/status, /recon/*
# extracted to api/platform_content.py during PERF-002 4A step 23
# (clusters 11-14).
from src.dashboard.api.platform_content import (  # noqa: E402,F401
    router as _platform_content_router,
    _matrix_enabled,
    _matrix_disabled_response,
    matrix_sync_state,
    matrix_backfill_state,
    matrix_queue_depths,
    matrix_coverage,
    list_telegram_chats,
    telegram_chat_detail,
    list_tiktok_profiles,
    tiktok_profile_detail,
    list_threads_profiles,
    threads_profile_detail,
    list_github_profiles,
    github_edge_stats,
    github_profile_detail,
    list_github_repos,
    github_repo_detail,
    list_lemon8_profiles,
    lemon8_profile_detail,
    list_beeper_chats,
    beeper_chat_detail,
    seen_targets,
    optional_rollout_status,
    recon_targets,
    recon_observations,
)



_INGESTION_CONTENT_PARTS = [
    ("telegram", "telegram_messages", "collected_at", "messages"),
    ("whatsapp", "whatsapp_messages", "collected_at", "messages"),
    ("beeper", "beeper_shadow_messages", "ingested_at", "messages"),
    ("instagram", "instagram_profiles", "updated_at", "profiles"),
    ("instagram", "instagram_posts", "collected_at", "posts"),
    ("tiktok", "tiktok_profiles", "updated_at", "profiles"),
    ("tiktok", "tiktok_posts", "collected_at", "posts"),
    ("lemon8", "lemon8_profiles", "updated_at", "profiles"),
    ("lemon8", "lemon8_posts", "collected_at", "posts"),
    ("threads", "threads_posts", "collected_at", "posts"),
    ("facebook", "facebook_profiles", "updated_at", "profiles"),
    ("facebook", "facebook_posts", "collected_at", "posts"),
    ("x", "x_profiles", "updated_at", "profiles"),
    ("x", "x_posts", "collected_at", "posts"),
    ("youtube", "youtube_channels", "updated_at", "channels"),
    ("youtube", "youtube_videos", "collected_at", "videos"),
    ("github", "github_users", "collected_at", "users"),
    ("github", "github_repos", "collected_at", "repos"),
    ("github", "github_commits", "collected_at", "commits"),
    ("github", "github_issues", "collected_at", "issues"),
    ("github", "github_issue_comments", "collected_at", "issue comments"),
    ("github", "github_pr_reviews", "collected_at", "PR reviews"),
    ("github", "github_pr_review_comments", "collected_at", "PR review comments"),
    ("website", "website_pages", "collected_at", "pages"),
    ("strava", "strava_athletes", "updated_at", "profiles"),
    ("strava", "strava_activities", "collected_at", "activities"),
    ("search", "search_results", "collected_at", "results"),
]

_SOURCE_METHODS = {
    "telegram": ["native api", "realtime"],
    "whatsapp": ["browser bridge", "realtime"],
    "beeper": ["beeper bridge", "shadow rooms"],
    "instagram": ["chrome extension", "headless cookies"],
    "tiktok": ["chrome extension", "headless cookies"],
    "lemon8": ["chrome extension", "headless cookies"],
    "threads": ["chrome extension"],
    "facebook": ["chrome extension"],
    "x": ["chrome extension"],
    "youtube": ["headless cookies"],
    "website": ["headless crawler"],
    "github": ["api", "headless fallback"],
    "strava": ["api cookies", "browser route capture"],
    "search": ["headless search"],
}


def _beeper_network_label(message_network: str | None, chat_network: str | None = None) -> str:
    for value in (message_network, chat_network):
        text = str(value or "").strip()
        if text and text.lower() != "unknown":
            return text
    return "Unmapped Beeper"


def _beeper_source_key(network: str | None) -> tuple[str, str]:
    label = _beeper_network_label(network)
    if label == "Unmapped Beeper":
        slug = "unmapped"
    else:
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "unmapped"
    return f"{_BEEPER_SUBSOURCE_PREFIX}{slug}", f"Beeper / {label}"


def _source_collection_methods(source: str | None) -> list[str]:
    if source and source.startswith(_BEEPER_SUBSOURCE_PREFIX):
        return ["beeper bridge", "normalized sub-source"]
    return _SOURCE_METHODS.get(source or "", [])


def _copy_cache_value(value):
    # Body moved to api/helpers.py in PERF-002 4A step 6. Re-exported below.
    from src.dashboard.api.helpers import _copy_cache_value as _impl
    return _impl(value)


def _copy_row_map(rows: dict[str, dict]) -> dict[str, dict]:
    from src.dashboard.api.helpers import _copy_row_map as _impl
    return _impl(rows)


def _copy_row_list(rows: list[dict]) -> list[dict]:
    from src.dashboard.api.helpers import _copy_row_list as _impl
    return _impl(rows)


_MEDIA_PRIMARY_SOURCES = {
    "beeper",
    "facebook",
    "instagram",
    "lemon8",
    "search",
    "strava",
    "telegram",
    "threads",
    "tiktok",
    "website",
    "whatsapp",
    "x",
    "youtube",
}
_MEDIA_QUIET_WARN_SECONDS = int(os.getenv("SOURCE_MEDIA_QUIET_WARN_SECONDS", str(24 * 3600)))


def _normalize_rate_limit_source(service: str | None) -> str:
    value = (service or "").lower()
    value = value.replace("_rate_limit", "").replace("_ratelimit", "")
    value = value.replace(" rate limit", "").replace(" ratelimit", "")
    return "".join(ch if ch.isalnum() else " " for ch in value).strip()


async def _existing_public_tables(conn, tables: list[str], *, timeout: float = 8) -> set[str]:
    if not tables:
        return set()
    rows = await conn.fetchval(
        """
        SELECT COALESCE(array_agg(table_name), ARRAY[]::text[])
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY($1::text[])
        """,
        tables,
        timeout=timeout,
    )
    return set(rows or [])


async def _existing_public_columns(conn, table: str, columns: list[str]) -> set[str]:
    if not table or not columns:
        return set()
    rows = await conn.fetchval(
        """
        SELECT COALESCE(array_agg(column_name), ARRAY[]::text[])
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
          AND column_name = ANY($2::text[])
        """,
        table,
        columns,
        timeout=8,
    )
    return set(rows or [])


async def _beeper_subsource_content_summary(
    conn,
    since_sql: str,
    before_sql: str | None = None,
    *,
    timeout: float = _BEEPER_SUBSOURCE_QUERY_TIMEOUT_SECONDS,
) -> dict[str, dict]:
    cache_key = (since_sql, before_sql)
    cached = _BEEPER_SUBSOURCE_CONTENT_CACHE.get(cache_key)
    now_ts = time.time()
    if (
        cached
        and now_ts - float(cached.get("ts") or 0.0) < _BEEPER_SUBSOURCE_CONTENT_TTL_SECONDS
        and isinstance(cached.get("rows"), dict)
    ):
        return _copy_row_map(cached["rows"])  # type: ignore[arg-type]

    existing_tables = await _existing_public_tables(
        conn,
        ["beeper_shadow_messages", "media_items"],
        timeout=timeout,
    )
    before_messages = f" AND ingested_at < {before_sql}" if before_sql else ""
    before_media = f" AND collected_at < {before_sql}" if before_sql else ""
    out: dict[str, dict] = {}

    def merge(source: str, display_name: str, row: dict) -> None:
        cur = out.setdefault(source, {
            "source": source,
            "display_name": display_name,
            "parent_source": "beeper",
            "rollup_exclude": True,
            "records": 0,
            "messages": 0,
            "media_items": 0,
            "latest_record_at": None,
            "latest_media_at": None,
        })
        for key in ("records", "messages", "media_items"):
            cur[key] += int(row.get(key) or 0)
        for key in ("latest_record_at", "latest_media_at"):
            value = row.get(key)
            if value and (cur.get(key) is None or value > cur[key]):
                cur[key] = value

    if "beeper_shadow_messages" in existing_tables:
        rows = await conn.fetch(
            f"""
            SELECT COALESCE(NULLIF(trim(network), ''), 'unknown') AS network,
                   count(*)::bigint AS records,
                   count(*)::bigint AS messages,
                   0::bigint AS media_items,
                   max(ingested_at) AS latest_record_at,
                   NULL::timestamptz AS latest_media_at
            FROM beeper_shadow_messages
            WHERE ingested_at >= {since_sql}
              {before_messages}
            GROUP BY 1
            """,
            timeout=timeout,
        )
        for row in rows:
            source, display_name = _beeper_source_key(row["network"])
            merge(source, display_name, dict(row))

    if "media_items" in existing_tables:
        rows = await conn.fetch(
            f"""
            SELECT COALESCE(
                       NULLIF(trim(metadata->>'network'), ''),
                       NULLIF(split_part(entity_id, '_', 1), ''),
                       'unknown'
                   ) AS network,
                   0::bigint AS records,
                   0::bigint AS messages,
                   count(*)::bigint AS media_items,
                   NULL::timestamptz AS latest_record_at,
                   max(collected_at) AS latest_media_at
            FROM media_items
            WHERE source = 'beeper'
              AND collected_at >= {since_sql}
              {before_media}
            GROUP BY 1
            """,
            timeout=timeout,
        )
        for row in rows:
            source, display_name = _beeper_source_key(row["network"])
            merge(source, display_name, dict(row))
    if len(_BEEPER_SUBSOURCE_CONTENT_CACHE) > 12:
        oldest_key = min(
            _BEEPER_SUBSOURCE_CONTENT_CACHE,
            key=lambda key: float(_BEEPER_SUBSOURCE_CONTENT_CACHE[key].get("ts") or 0.0),
        )
        _BEEPER_SUBSOURCE_CONTENT_CACHE.pop(oldest_key, None)
    _BEEPER_SUBSOURCE_CONTENT_CACHE[cache_key] = {"ts": now_ts, "rows": _copy_row_map(out)}
    return out


async def _source_content_summary(
    conn,
    since_sql: str,
    before_sql: str | None = None,
    *,
    include_media: bool = True,
) -> dict[str, dict]:
    required_tables = [table for _source, table, _column, _label in _INGESTION_CONTENT_PARTS]
    required_tables.append("media_items")
    existing_tables = await _existing_public_tables(conn, required_tables)
    out: dict[str, dict] = {}

    def merge(source: str, row: dict | None) -> None:
        if not source or not row:
            return
        target = out.setdefault(source, {
            "source": source,
            "records": 0,
            "messages": 0,
            "media_items": 0,
            "latest_record_at": None,
            "latest_media_at": None,
            "media_stats_unavailable": False,
        })
        target["records"] = int(target.get("records") or 0) + int(row.get("records") or 0)
        target["messages"] = int(target.get("messages") or 0) + int(row.get("messages") or 0)
        target["media_items"] = int(target.get("media_items") or 0) + int(row.get("media_items") or 0)
        latest_record = row.get("latest_record_at")
        latest_media = row.get("latest_media_at")
        if latest_record and (
            not target.get("latest_record_at") or latest_record > target["latest_record_at"]
        ):
            target["latest_record_at"] = latest_record
        if latest_media and (
            not target.get("latest_media_at") or latest_media > target["latest_media_at"]
        ):
            target["latest_media_at"] = latest_media
        if row.get("media_stats_unavailable"):
            target["media_stats_unavailable"] = True

    row_summary_slow_parts: list[str] = []
    row_parts = []
    for source, table, column, label in _INGESTION_CONTENT_PARTS:
        if table not in existing_tables:
            continue
        row_parts.append(
            f"""
            SELECT '{source}'::text AS source,
                   count(*)::bigint AS records,
                   {("count(*)" if label == "messages" else "0")}::bigint AS messages,
                   max({column}) AS latest_record_at
            FROM {table}
            WHERE {column} >= {since_sql}
              {f"AND {column} < {before_sql}" if before_sql else ""}
            """
        )
    if row_parts:
        try:
            rows = await conn.fetch(
                f"""
                WITH row_parts AS (
                    {" UNION ALL ".join(row_parts)}
                )
                SELECT source,
                       sum(records)::bigint AS records,
                       sum(messages)::bigint AS messages,
                       0::bigint AS media_items,
                       max(latest_record_at) AS latest_record_at,
                       NULL::timestamptz AS latest_media_at
                FROM row_parts
                GROUP BY source
                """,
                timeout=max(1.0, _SOURCE_CONTENT_SUMMARY_BUDGET_SECONDS),
            )
            for row in rows:
                merge(row["source"], dict(row))
        except Exception as exc:  # noqa: BLE001 - keep media stats if row summary lags
            row_summary_slow_parts.append(f"combined_row_summary:{exc.__class__.__name__}")
    if row_summary_slow_parts:
        logger.warning(
            "source content row summary skipped slow parts: %s",
            ", ".join(row_summary_slow_parts[:6]),
        )

    if include_media and "media_items" in existing_tables:
        try:
            rows = await conn.fetch(
                f"""
                SELECT source,
                       0::bigint AS records,
                       0::bigint AS messages,
                       count(*)::bigint AS media_items,
                       NULL::timestamptz AS latest_record_at,
                       max(collected_at) AS latest_media_at
                FROM media_items
                WHERE collected_at >= {since_sql}
                  {f"AND collected_at < {before_sql}" if before_sql else ""}
                GROUP BY source
                """,
                timeout=max(1.0, _SOURCE_CONTENT_MEDIA_TIMEOUT_SECONDS),
            )
            for row in rows:
                merge(row["source"], dict(row))
        except Exception as exc:  # noqa: BLE001 - keep row counts if media stats lag
            logger.warning("source content media summary failed: %s", exc.__class__.__name__)
            for row in out.values():
                row["media_stats_unavailable"] = True
    elif not include_media:
        for row in out.values():
            row["media_stats_unavailable"] = True

    beeper_timeout = max(0.1, min(
        _BEEPER_SUBSOURCE_TOTAL_TIMEOUT_SECONDS,
        max(1.0, _SOURCE_CONTENT_SUMMARY_BUDGET_SECONDS),
    ))
    if beeper_timeout > 0:
        try:
            out.update(await asyncio.wait_for(
                _beeper_subsource_content_summary(conn, since_sql, before_sql),
                timeout=beeper_timeout,
            ))
        except Exception as exc:  # noqa: BLE001 - dashboard should degrade, not 500
            logger.warning("beeper sub-source content summary failed: %s", exc)
    return out


async def _source_rolling_content_summary(conn) -> dict[str, dict]:
    out = await _source_content_summary(conn, "now() - interval '1 hour'")
    existing = await _existing_public_tables(conn, ["browser_ingest_events"])
    if "browser_ingest_events" not in existing:
        return out
    rows = await conn.fetch(
        """
        SELECT platform AS source,
               count(*)::bigint AS requests,
               sum(observed_count)::bigint AS observed_count,
               sum(stored_count)::bigint AS stored_count,
               max(created_at) AS latest_record_at
        FROM browser_ingest_events
        WHERE created_at >= now() - interval '1 hour'
          AND endpoint <> 'browser_heartbeat'
        GROUP BY platform
        """,
        timeout=max(1.0, _SOURCE_CONTENT_SUMMARY_BUDGET_SECONDS),
    )
    for row in rows:
        source = str(row["source"] or "")
        if not source:
            continue
        target = out.setdefault(source, {
            "source": source,
            "records": 0,
            "messages": 0,
            "media_items": 0,
            "latest_record_at": None,
            "latest_media_at": None,
            "media_stats_unavailable": False,
        })
        target["records"] = max(int(target.get("records") or 0), int(row["observed_count"] or 0))
        target["media_items"] = max(int(target.get("media_items") or 0), int(row["stored_count"] or 0))
        target["requests"] = int(row["requests"] or 0)
        latest = row["latest_record_at"]
        if latest and (
            not target.get("latest_record_at") or latest > target["latest_record_at"]
        ):
            target["latest_record_at"] = latest
    return out


async def _beeper_subsource_media_totals(
    conn,
    *,
    timeout: float = _BEEPER_SUBSOURCE_QUERY_TIMEOUT_SECONDS,
) -> dict[str, object]:
    cached_rows = _BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE.get("rows")
    cache_age = time.time() - float(_BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE.get("ts") or 0.0)
    if isinstance(cached_rows, dict):
        ttl = (
            _BEEPER_SUBSOURCE_MEDIA_TOTALS_FAILURE_TTL_SECONDS
            if _BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE.get("failed")
            else _BEEPER_SUBSOURCE_MEDIA_TOTALS_TTL_SECONDS
        )
        if cache_age < ttl:
            return _copy_cache_value(cached_rows)

    existing = await _existing_public_tables(conn, ["media_items"], timeout=timeout)
    if "media_items" not in existing:
        return {}
    try:
        rows = await conn.fetch(
            """
            SELECT COALESCE(
                       NULLIF(trim(metadata->>'network'), ''),
                       NULLIF(split_part(entity_id, '_', 1), ''),
                       'unknown'
                   ) AS network,
                   count(*)::bigint AS total_media_items,
                   COALESCE(sum(file_size), 0)::bigint AS total_media_bytes,
                   max(collected_at) AS latest_media_at
            FROM media_items
            WHERE source = 'beeper'
            GROUP BY 1
            """,
            timeout=timeout,
        )
    except Exception:
        _BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE.update({
            "ts": time.time(),
            "rows": {"__beeper_subsource_stats_unavailable__": True},
            "failed": True,
        })
        raise
    out: dict[str, dict] = {}
    for row in rows:
        source, display_name = _beeper_source_key(row["network"])
        out[source] = {
            **dict(row),
            "source": source,
            "display_name": display_name,
            "parent_source": "beeper",
            "rollup_exclude": True,
        }
    _BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE.update({
        "ts": time.time(),
        "rows": _copy_cache_value(out),
        "failed": False,
    })
    return out


async def _beeper_subsource_liveness(conn) -> list[dict]:
    cached_rows = _BEEPER_SUBSOURCE_LIVENESS_CACHE.get("rows")
    if (
        isinstance(cached_rows, list)
        and time.time() - float(_BEEPER_SUBSOURCE_LIVENESS_CACHE.get("ts") or 0.0)
        < _BEEPER_SUBSOURCE_LIVENESS_TTL_SECONDS
    ):
        return _copy_row_list(cached_rows)  # type: ignore[arg-type]

    existing = await _existing_public_tables(
        conn,
        ["beeper_shadow_messages", "beeper_shadow_chats", "beeper_shadow_participants"],
    )
    networks: dict[str, dict] = {}

    def ensure(network: str | None) -> dict:
        source, display_name = _beeper_source_key(network)
        return networks.setdefault(source, {
            "source": source,
            "display_name": display_name,
            "parent_source": "beeper",
            "rollup_exclude": True,
            "recent_messages": None,
            "chats": 0,
            "people": 0,
            "latest_at": None,
        })

    if "beeper_shadow_messages" in existing and "beeper_shadow_chats" not in existing:
        for row in await conn.fetch(
            """
            SELECT COALESCE(NULLIF(trim(network), ''), 'unknown') AS network,
                   count(*)::bigint AS recent_messages,
                   max(ingested_at) AS latest_at
            FROM beeper_shadow_messages
            WHERE ingested_at >= now() - interval '7 days'
            GROUP BY 1
            """,
            timeout=4,
        ):
            cur = ensure(row["network"])
            cur["recent_messages"] = int(row["recent_messages"] or 0)
            if row["latest_at"] and (cur["latest_at"] is None or row["latest_at"] > cur["latest_at"]):
                cur["latest_at"] = row["latest_at"]

    if "beeper_shadow_chats" in existing:
        for row in await conn.fetch(
            """
            SELECT COALESCE(NULLIF(trim(network), ''), 'unknown') AS network,
                   count(*)::int AS chats,
                   max(last_seen_at) AS latest_at
            FROM beeper_shadow_chats
            GROUP BY 1
            """,
            timeout=20,
        ):
            cur = ensure(row["network"])
            cur["chats"] = max(int(cur["chats"] or 0), int(row["chats"] or 0))
            if row["latest_at"] and (cur["latest_at"] is None or row["latest_at"] > cur["latest_at"]):
                cur["latest_at"] = row["latest_at"]

    if "beeper_shadow_participants" in existing:
        for row in await conn.fetch(
            """
            SELECT COALESCE(NULLIF(trim(network), ''), 'unknown') AS network,
                   count(DISTINCT participant_id)::int AS people
            FROM beeper_shadow_participants
            GROUP BY 1
            """,
            timeout=20,
        ):
            cur = ensure(row["network"])
            cur["people"] = max(int(cur["people"] or 0), int(row["people"] or 0))

    now = datetime.now(timezone.utc)
    out = []
    for row in networks.values():
        latest = row.get("latest_at")
        age_seconds = None
        if isinstance(latest, datetime):
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            age_seconds = max(0, int((now - latest).total_seconds()))
        status = "dead"
        if age_seconds is not None:
            status = "live" if age_seconds <= _BEEPER_SUBSOURCE_STALE_SECONDS else "stale"
        recent_messages = row.get("recent_messages")
        message_detail = (
            f"{int(recent_messages):,} messages in the last 7 days, "
            if recent_messages is not None
            else ""
        )
        out.append({
            "source": row["source"],
            "display_name": row["display_name"],
            "parent_source": "beeper",
            "rollup_exclude": True,
            "status": status,
            "collection_mode": "messaging bridge",
            "freshness_basis": "beeper_shadow_messages.ingested_at by network",
            "age_seconds": age_seconds,
            "stale_after_seconds": _BEEPER_SUBSOURCE_STALE_SECONDS,
            "detail": (
                f"{row['display_name']} via Beeper: "
                f"{message_detail}"
                f"{int(row['chats'] or 0):,} chats, "
                f"{int(row['people'] or 0):,} people. "
                "Message volume is reported in the current-hour and 24-hour columns."
            ),
        })
    _BEEPER_SUBSOURCE_LIVENESS_CACHE.update({"ts": time.time(), "rows": _copy_row_list(out)})
    return out


async def _source_rate_summary(conn, since_sql: str, before_sql: str | None = None) -> dict[str, dict]:
    existing = await _existing_public_tables(
        conn,
        ["rate_limit_events", "strava_activities", "strava_gps_streams"],
    )
    if "rate_limit_events" not in existing:
        return {}
    before_clause = f" AND created_at < {before_sql}" if before_sql else ""
    if {"strava_activities", "strava_gps_streams"}.issubset(existing):
        cleared_by_success_sql = """
                   EXISTS (
                       SELECT 1
                       FROM strava_activities a
                       JOIN strava_gps_streams s ON s.activity_id = a.id
                       WHERE rl.source = 'strava'
                         AND rl.scope IN ('gps_streams', 'browser_strava_streams')
                         AND rl.metadata->>'activity_id' ~ '^[0-9]+$'
                         AND a.platform_activity_id = (rl.metadata->>'activity_id')::bigint
                         AND s.collected_at > rl.created_at
                         AND jsonb_typeof(s.latlng) = 'array'
                         AND jsonb_array_length(s.latlng) > 1
                   )
        """
    else:
        cleared_by_success_sql = "FALSE"
    try:
        query_timeout = max(1.0, float(os.getenv("SOURCE_RATE_SUMMARY_QUERY_TIMEOUT_SECONDS", "4")))
    except (TypeError, ValueError):
        query_timeout = 4.0
    rows = await conn.fetch(
        f"""
        WITH events AS (
            SELECT rl.*,
                   ({cleared_by_success_sql}) AS cleared_by_success
            FROM rate_limit_events rl
            WHERE rl.created_at >= {since_sql}
              {before_clause}
        )
        SELECT source,
               count(*) FILTER (
                   WHERE status_code = 429
                      OR status_code IS NULL
                      OR cooldown_seconds IS NOT NULL
                      OR (
                          source = 'youtube'
                          AND status_code = 403
                          AND reason = 'youtube_api_quota_or_access'
                      )
               )::int AS rate_limits,
               count(*) FILTER (
                   WHERE status_code IS NOT NULL
                     AND NOT (
                         status_code = 429
                         OR cooldown_seconds IS NOT NULL
                         OR (
                             source = 'youtube'
                             AND status_code = 403
                             AND reason = 'youtube_api_quota_or_access'
                         )
                     )
               )::int AS access_errors,
               (array_agg(account ORDER BY created_at DESC))[1] AS latest_account,
               (array_agg(scope ORDER BY created_at DESC))[1] AS latest_scope,
               (array_agg(status_code ORDER BY created_at DESC))[1]::int AS latest_status_code,
               (array_agg(reason ORDER BY created_at DESC))[1] AS latest_reason,
               max(created_at) AS latest_event_at,
               (array_agg(account ORDER BY created_at + COALESCE(cooldown_seconds, 0) * interval '1 second' DESC)
                   FILTER (WHERE NOT cleared_by_success AND cooldown_seconds IS NOT NULL))[1] AS active_account,
               (array_agg(scope ORDER BY created_at + COALESCE(cooldown_seconds, 0) * interval '1 second' DESC)
                   FILTER (WHERE NOT cleared_by_success AND cooldown_seconds IS NOT NULL))[1] AS active_scope,
               (array_agg(status_code ORDER BY created_at + COALESCE(cooldown_seconds, 0) * interval '1 second' DESC)
                   FILTER (WHERE NOT cleared_by_success AND cooldown_seconds IS NOT NULL))[1]::int AS active_status_code,
               (array_agg(reason ORDER BY created_at + COALESCE(cooldown_seconds, 0) * interval '1 second' DESC)
                   FILTER (WHERE NOT cleared_by_success AND cooldown_seconds IS NOT NULL))[1] AS active_reason,
               max(
                   CASE
                       WHEN NOT cleared_by_success
                       THEN created_at + COALESCE(cooldown_seconds, 0) * interval '1 second'
                       ELSE NULL
                   END
               ) AS active_until
        FROM events
        GROUP BY source
        """,
        timeout=query_timeout,
    )
    now_utc = datetime.now(timezone.utc)
    out = {}
    for row in rows:
        d = dict(row)
        active_until = d.get("active_until")
        d["active_now"] = bool(active_until and active_until > now_utc)
        if d["active_now"]:
            d["latest_account"] = d.get("active_account") or d.get("latest_account")
            d["latest_scope"] = d.get("active_scope") or d.get("latest_scope")
            d["latest_status_code"] = d.get("active_status_code")
            d["latest_reason"] = d.get("active_reason") or d.get("latest_reason")
        out[d["source"]] = d
    return out


async def _source_media_totals(conn) -> dict[str, dict]:
    cached_rows = _SOURCE_MEDIA_TOTALS_CACHE.get("rows")
    cache_age = time.time() - float(_SOURCE_MEDIA_TOTALS_CACHE.get("ts") or 0)
    if cached_rows is not None and cache_age < _SOURCE_MEDIA_TOTALS_TTL_SECONDS:
        return cached_rows  # type: ignore[return-value]
    existing = await _existing_public_tables(conn, ["media_source_rollups", "media_items"])
    if "media_source_rollups" in existing:
        rows = await conn.fetch(
            """
            SELECT source,
                   total_media_items,
                   total_media_bytes,
                   latest_media_at
            FROM media_source_rollups
            ORDER BY source
            """,
            timeout=8,
        )
        out = {row["source"]: dict(row) for row in rows}
        try:
            out.update(await asyncio.wait_for(
                _beeper_subsource_media_totals(conn),
                timeout=_BEEPER_SUBSOURCE_TOTAL_TIMEOUT_SECONDS,
            ))
        except Exception as exc:  # noqa: BLE001
            _log_beeper_subsource_media_totals_error(exc)
            out["__beeper_subsource_stats_unavailable__"] = True
        _SOURCE_MEDIA_TOTALS_CACHE.update({"ts": time.time(), "rows": out})
        return out
    if "media_items" not in existing:
        return {}
    try:
        rows = await conn.fetch(
            """
            SELECT source,
                   count(*)::bigint AS total_media_items,
                   COALESCE(sum(file_size), 0)::bigint AS total_media_bytes,
                   max(collected_at) AS latest_media_at
            FROM media_items
            GROUP BY source
            """,
            timeout=20,
        )
    except Exception:
        if cached_rows is not None:
            for row in cached_rows.values():  # type: ignore[union-attr]
                row["stats_stale"] = True
            _SOURCE_MEDIA_TOTALS_CACHE["ts"] = time.time()
            return cached_rows  # type: ignore[return-value]
        raise
    out = {row["source"]: dict(row) for row in rows}
    try:
        out.update(await asyncio.wait_for(
            _beeper_subsource_media_totals(conn),
            timeout=_BEEPER_SUBSOURCE_TOTAL_TIMEOUT_SECONDS,
        ))
    except Exception as exc:  # noqa: BLE001
        _log_beeper_subsource_media_totals_error(exc)
        out["__beeper_subsource_stats_unavailable__"] = True
    _SOURCE_MEDIA_TOTALS_CACHE.update({"ts": time.time(), "rows": out})
    return out


def _log_beeper_subsource_media_totals_error(exc: BaseException) -> None:
    detail = str(exc).strip() or exc.__class__.__name__
    log = logger.info if isinstance(exc, TimeoutError) else logger.warning
    log("beeper sub-source media totals unavailable: %s", detail)


async def _source_matrix_section(
    *,
    section: str,
    label: str,
    errors: list[dict],
    fallback,
    awaitable,
    timeout: float | None = None,
    cache_key: str | None = None,
    cache_ttl: int | None = None,
    stale_ttl: int | None = None,
    prefer_stale_cache: bool = False,
):
    now_ts = time.time()
    cached = _SOURCE_MATRIX_SECTION_CACHE.get(cache_key or "") if cache_key else None
    if cached is not None:
        cache_age = now_ts - float(cached.get("ts") or 0.0)
        max_fresh = _SOURCE_MATRIX_SECTION_CACHE_TTL_SECONDS if cache_ttl is None else cache_ttl
        if cache_age < max_fresh:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return _copy_cache_value(cached.get("value"))
        max_stale = _SOURCE_MATRIX_SECTION_STALE_SECONDS if stale_ttl is None else stale_ttl
        if prefer_stale_cache and cache_age < max_stale:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return _copy_cache_value(cached.get("value"))
    try:
        value = await asyncio.wait_for(awaitable, timeout=timeout or _SOURCE_MATRIX_SECTION_TIMEOUT_SECONDS)
        if cache_key:
            _SOURCE_MATRIX_SECTION_CACHE[cache_key] = {
                "ts": time.time(),
                "value": _copy_cache_value(value),
            }
        return value
    except asyncio.CancelledError as exc:
        logger.warning("source matrix %s cancelled under load", label)
        cached_value = None
        cache_age = None
        if cached is not None:
            cache_age = now_ts - float(cached.get("ts") or 0.0)
            max_stale = _SOURCE_MATRIX_SECTION_STALE_SECONDS if stale_ttl is None else stale_ttl
            if cache_age < max_stale:
                cached_value = _copy_cache_value(cached.get("value"))
        if cached_value is not None:
            errors.append({
                "section": section,
                "error": exc.__class__.__name__,
                "stale_cache": True,
                "cache_age_seconds": int(cache_age or 0),
            })
            return cached_value
        errors.append({"section": section, "error": exc.__class__.__name__})
        return fallback
    except Exception as exc:  # noqa: BLE001 - source matrix should return partial data under load
        log = logger.info if isinstance(exc, TimeoutError) else logger.warning
        log("source matrix %s returned partial data: %s", label, exc.__class__.__name__)
        cached_value = None
        cache_age = None
        if cached is not None:
            cache_age = now_ts - float(cached.get("ts") or 0.0)
            max_stale = _SOURCE_MATRIX_SECTION_STALE_SECONDS if stale_ttl is None else stale_ttl
            if cache_age < max_stale:
                cached_value = _copy_cache_value(cached.get("value"))
        if cached_value is not None:
            errors.append({
                "section": section,
                "error": exc.__class__.__name__,
                "stale_cache": True,
                "cache_age_seconds": int(cache_age or 0),
            })
            return cached_value
        errors.append({"section": section, "error": exc.__class__.__name__})
        return fallback


def _source_matrix_fallback_liveness_rows(detail: str) -> list[dict]:
    from src.core.source_freshness import FRESHNESS, FRESHNESS_BASIS, SOURCE_MODES

    return [
        {
            "source": source,
            "status": "unknown",
            "age_seconds": None,
            "stale_after_seconds": threshold,
            "collection_mode": SOURCE_MODES.get(source, "unknown"),
            "freshness_basis": FRESHNESS_BASIS.get(source),
            "source_health_status": None,
            "source_health_error": detail,
            "source_health_last_success_at": None,
            "source_health_updated_at": None,
            "detail": detail,
        }
        for source, _query, threshold in FRESHNESS
    ]


def _source_matrix_status_from_health(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"running", "ok", "healthy", "idle"}:
        return "live"
    if normalized in {"degraded", "dead", "auth_paused", "stale"}:
        return normalized
    return "unknown"


async def _source_matrix_browser_source_fields(
    conn,
    platforms: list[str] | None = None,
) -> dict[str, dict]:
    selected = platforms or ["instagram", "tiktok", "lemon8", "threads", "facebook", "x", "strava"]
    selected = [str(platform) for platform in selected if platform]
    if not selected:
        return {}
    rows = await conn.fetch(
        """
        WITH selected(platform) AS (
            SELECT unnest($1::text[])
        )
        SELECT selected.platform,
               latest.metadata->>'url' AS browser_url,
               latest.metadata->>'health_status' AS browser_health_status,
               latest.metadata->>'health_reason' AS browser_health_reason
        FROM selected
        JOIN LATERAL (
            SELECT metadata
            FROM browser_ingest_events
            WHERE endpoint = 'browser_heartbeat'
              AND platform = selected.platform
            ORDER BY created_at DESC
            LIMIT 1
        ) latest ON TRUE
        """,
        selected,
    )
    return {str(row["platform"]): dict(row) for row in rows}


def _source_matrix_apply_browser_source_fields(
    source_rows: list[dict],
    browser_fields: dict[str, dict] | None,
) -> None:
    fields = browser_fields or {}
    for row in source_rows:
        source = str(row.get("source") or "")
        browser = fields.get(source)
        if not browser:
            continue
        row.update({
            "browser_url": browser.get("browser_url"),
            "browser_health_status": browser.get("browser_health_status"),
            "browser_health_reason": browser.get("browser_health_reason"),
        })


async def _source_matrix_fallback_liveness_rows_with_source_health(detail: str) -> list[dict]:
    """Cheap fallback liveness from source_health when heavy matrix stats time out.

    The full source matrix touches large per-source tables. During pg_dump or
    other DB pressure those sections can miss the route timeout before any
    payload cache exists. A short source_health-only query is much cheaper and
    lets the dashboard say "running/degraded with last success age" instead of
    rendering every source as blank unknown/zero.
    """
    rows = _source_matrix_fallback_liveness_rows(detail)
    by_source = {row["source"]: row for row in rows}
    pool = await get_pool()
    conn = None
    browser_fields = {}
    try:
        conn = await asyncio.wait_for(pool.acquire(), timeout=1.5)
        try:
            health_rows = await conn.fetch(
                """
                SELECT source, status, last_success_at, last_error, updated_at
                FROM source_health
                """,
                timeout=1.5,
            )
        except Exception:
            return rows
        try:
            browser_fields = await conn.fetch(
                """
                SELECT DISTINCT ON (platform)
                       platform,
                       metadata->>'url' AS browser_url,
                       metadata->>'health_status' AS browser_health_status,
                       metadata->>'health_reason' AS browser_health_reason
                FROM browser_ingest_events
                WHERE endpoint = 'browser_heartbeat'
                  AND platform = ANY($1::text[])
                ORDER BY platform, created_at DESC
                """,
                ["instagram", "tiktok", "lemon8", "threads", "facebook", "x", "strava"],
                timeout=1.0,
            )
            browser_fields = {str(row["platform"]): dict(row) for row in browser_fields}
        except Exception:
            browser_fields = {}
    except Exception:
        return rows
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "source matrix fallback")

    now = datetime.now(timezone.utc)
    for health_row in health_rows:
        health = dict(health_row)
        row = by_source.get(str(health["source"]))
        if row is None:
            continue
        last_success_at = health.get("last_success_at")
        age_seconds = None
        if last_success_at is not None:
            if last_success_at.tzinfo is None:
                last_success_at = last_success_at.replace(tzinfo=timezone.utc)
            age_seconds = int(max(0, (now - last_success_at.astimezone(timezone.utc)).total_seconds()))
        health_status = str(health.get("status") or "")
        health_error = health.get("last_error")
        row.update({
            "status": _source_matrix_status_from_health(health_status),
            "age_seconds": age_seconds,
            "source_health_status": health_status or None,
            "source_health_error": health_error or detail,
            "source_health_last_success_at": last_success_at,
            "source_health_updated_at": health.get("updated_at"),
            "detail": health_error or detail,
        })
    _source_matrix_apply_browser_source_fields(rows, browser_fields)
    return rows


def _collectors_live_fallback_payload(exc: BaseException) -> dict:
    """Return stale/skeleton liveness when dashboard DB acquisition is saturated."""
    detail = f"collector live status unavailable: {exc.__class__.__name__}"
    cached = _COLLECTORS_LIVE_CACHE.get("payload")
    cache_age = time.time() - float(_COLLECTORS_LIVE_CACHE.get("ts") or 0.0)
    if cached is not None and cache_age < _COLLECTORS_LIVE_STALE_SECONDS:
        payload = _copy_cache_value(cached)
        payload["stats_stale"] = True
        payload["stats_error"] = exc.__class__.__name__
        payload["cache_age_seconds"] = int(cache_age)
        return payload
    sources = _source_matrix_fallback_liveness_rows(detail)
    return {
        "total": len(sources),
        "live": 0,
        "degraded": len(sources),
        "sources": sources,
        "whatsapp_bridge_health": None,
        "stats_stale": True,
        "stats_error": exc.__class__.__name__,
    }



def _rate_limits_recent_fallback_payload(hours: int, limit: int, exc: BaseException) -> dict:
    cache_key = (hours, limit)
    cached = _RATE_LIMITS_RECENT_CACHE.get(cache_key)
    cache_age = time.time() - float((cached or {}).get("ts") or 0.0)
    if cached and cache_age < _RATE_LIMITS_RECENT_STALE_SECONDS:
        payload = _copy_cache_value(cached.get("payload"))
        payload["stats_stale"] = True
        payload["stats_error"] = exc.__class__.__name__
        payload["cache_age_seconds"] = int(cache_age)
        return payload
    return {
        "events": [],
        "active": [],
        "active_event_summary": [],
        "cursor_history": [],
        "recent_summary": [],
        "stats_stale": True,
        "stats_error": exc.__class__.__name__,
    }


async def _youtube_media_backlog(conn) -> dict:
    now = time.time()
    cached_row = _YOUTUBE_MEDIA_BACKLOG_CACHE.get("row")
    if (
        cached_row is not None
        and now - float(_YOUTUBE_MEDIA_BACKLOG_CACHE.get("ts") or 0.0) < _YOUTUBE_MEDIA_BACKLOG_TTL_SECONDS
    ):
        out = dict(cached_row)  # type: ignore[arg-type]
        out["stats_stale"] = True
        return out

    existing = await _existing_public_tables(conn, ["youtube_videos"])
    if "youtube_videos" not in existing:
        return {}
    video_cols = await _existing_public_columns(
        conn,
        "youtube_videos",
        ["media_status", "media_skip_reason", "last_media_attempt_at", "duration", "collected_at"],
    )
    media_status_expr = (
        "coalesce(nullif(v.media_status, ''), 'pending')"
        if "media_status" in video_cols
        else "'pending'"
    )
    media_skip_reason_expr = (
        "coalesce(v.media_skip_reason, '')"
        if "media_skip_reason" in video_cols
        else "''"
    )
    last_media_attempt_expr = (
        "v.last_media_attempt_at"
        if "last_media_attempt_at" in video_cols
        else "NULL::timestamptz"
    )
    duration_expr = "upper(coalesce(v.duration, ''))" if "duration" in video_cols else "''"
    collected_at_expr = "v.collected_at" if "collected_at" in video_cols else "NULL::timestamptz"
    try:
        duration_cap_seconds = max(0, int(os.getenv("YOUTUBE_MAX_VIDEO_DURATION_MINUTES", "0")) * 60)
    except (TypeError, ValueError):
        duration_cap_seconds = 0
    try:
        try:
            query_timeout = max(1.0, float(os.getenv("YOUTUBE_MEDIA_BACKLOG_QUERY_TIMEOUT_SECONDS", "6")))
        except (TypeError, ValueError):
            query_timeout = 6.0
        row = await conn.fetchrow(
            f"""
            WITH classified AS (
                SELECT {media_status_expr} AS media_status,
                       {media_skip_reason_expr} AS media_skip_reason,
                       {last_media_attempt_expr} AS last_media_attempt_at,
                       {collected_at_expr} AS collected_at,
                       {duration_expr} AS duration_text,
                       (
                           coalesce((substring({duration_expr} from 'PT([0-9]+)H'))::int, 0) * 3600
                           + coalesce((substring({duration_expr} from '([0-9]+)M'))::int, 0) * 60
                           + coalesce((substring({duration_expr} from '([0-9]+)S'))::int, 0)
                       ) AS duration_seconds
                FROM youtube_videos v
            ),
            missing AS (
                SELECT *
                FROM classified
                WHERE media_status <> 'stored'
            )
            SELECT (SELECT COUNT(*) FROM classified)::bigint AS total_videos,
                   NULL::bigint AS missing_thumbnails,
                   COUNT(*)::bigint AS missing_videos,
                   COUNT(*) FILTER (
                       WHERE duration_text IN ('P0D', 'PT0S')
                   )::bigint AS placeholder_missing_videos,
                   COUNT(*) FILTER (
                       WHERE duration_text NOT IN ('P0D', 'PT0S')
                         AND NOT (
                             $1::int > 0
                             AND media_status = 'skipped'
                             AND media_skip_reason = 'over_duration_cap'
                         )
                         AND ($1::int <= 0 OR duration_seconds <= $1::int OR duration_text = '')
                   )::bigint AS eligible_missing_videos,
                   COUNT(*) FILTER (
                       WHERE duration_text NOT IN ('P0D', 'PT0S')
                         AND NOT (
                             $1::int > 0
                             AND media_status = 'skipped'
                             AND media_skip_reason = 'over_duration_cap'
                         )
                         AND ($1::int <= 0 OR duration_seconds <= $1::int OR duration_text = '')
                         AND last_media_attempt_at IS NULL
                   )::bigint AS eligible_missing_videos_never_attempted,
                   COUNT(*) FILTER (
                       WHERE (
                              $1::int > 0
                              AND media_status = 'skipped'
                              AND media_skip_reason = 'over_duration_cap'
                             )
                          OR (
                              duration_text NOT IN ('P0D', 'PT0S', '')
                              AND $1::int > 0
                              AND duration_seconds > $1::int
                          )
                   )::bigint AS over_duration_missing_videos,
                   COUNT(*) FILTER (
                       WHERE duration_text = ''
                   )::bigint AS unknown_duration_missing_videos,
                   COUNT(*) FILTER (
                       WHERE duration_text NOT IN ('P0D', 'PT0S')
                         AND NOT (
                             $1::int > 0
                             AND media_status = 'skipped'
                             AND media_skip_reason = 'over_duration_cap'
                         )
                         AND ($1::int <= 0 OR duration_seconds <= $1::int OR duration_text = '')
                         AND collected_at >= now() - interval '24 hours'
                   )::bigint AS eligible_missing_videos_touched_24h,
                   COUNT(*) FILTER (
                       WHERE collected_at >= now() - interval '24 hours'
                   )::bigint AS missing_videos_touched_24h
            FROM missing
            """,
            duration_cap_seconds,
            timeout=query_timeout,
        )
    except Exception as exc:
        cached_row = _YOUTUBE_MEDIA_BACKLOG_CACHE.get("row")
        if cached_row is not None:
            out = dict(cached_row)  # type: ignore[arg-type]
            out["stats_stale"] = True
            return out
        logger.warning("youtube media backlog stats unavailable: %s", exc.__class__.__name__)
        out = {
            "stats_unavailable": True,
            "stats_error": exc.__class__.__name__,
            "duration_cap_seconds": duration_cap_seconds,
            "backlog_basis": "youtube_videos.media_status",
            "thumbnail_stats_unavailable": True,
        }
        _YOUTUBE_MEDIA_BACKLOG_CACHE.update({"ts": time.time(), "row": out})
        return out
    out = dict(row) if row else {}
    out["duration_cap_seconds"] = duration_cap_seconds
    out["backlog_basis"] = "youtube_videos.media_status"
    out["thumbnail_stats_unavailable"] = True
    _YOUTUBE_MEDIA_BACKLOG_CACHE.update({"ts": time.time(), "row": out})
    return out


async def _youtube_completeness(conn) -> dict:
    now = time.time()
    cached_payload = _YOUTUBE_COMPLETENESS_CACHE.get("payload")
    if (
        cached_payload is not None
        and now - float(_YOUTUBE_COMPLETENESS_CACHE.get("ts") or 0.0) < _YOUTUBE_COMPLETENESS_TTL_SECONDS
    ):
        out = dict(cached_payload)  # type: ignore[arg-type]
        out["stats_cached"] = True
        return out
    try:
        out = await _youtube_completeness_uncached(conn)
    except Exception as exc:  # noqa: BLE001 - dashboard must not 500 under DB load
        if cached_payload is not None:
            out = dict(cached_payload)  # type: ignore[arg-type]
            out["stats_stale"] = True
            out["stats_error"] = str(exc)[:300] or exc.__class__.__name__
            return out
        return {
            "schema_ready": False,
            "stats_error": str(exc)[:300] or exc.__class__.__name__,
            "videos": {},
            "media_backlog": {},
        }
    _YOUTUBE_COMPLETENESS_CACHE.update({"ts": now, "payload": out})
    return out


async def _youtube_completeness_uncached(conn) -> dict:
    required = [
        "youtube_channels",
        "youtube_videos",
        "media_items",
        "youtube_transcripts",
        "youtube_comments",
        "youtube_community_posts",
        "youtube_edges",
        "youtube_profile_queue",
        "youtube_spider_queue",
        "collection_targets",
    ]
    existing = await _existing_public_tables(conn, required)
    if "youtube_videos" not in existing:
        return {"schema_ready": False, "reason": "youtube_videos_missing"}

    video_cols = await _existing_public_columns(
        conn,
        "youtube_videos",
        [
            "media_status",
            "media_skip_reason",
            "transcript_status",
            "comments_status",
            "last_media_attempt_at",
        ],
    )
    channel_cols = await _existing_public_columns(
        conn,
        "youtube_channels",
        [
            "profile_photo_media_id",
            "external_links",
            "last_video_scan_at",
            "last_community_scan_at",
            "last_skip_reason",
            "last_error",
        ],
    )
    community_cols = await _existing_public_columns(
        conn,
        "youtube_community_posts",
        ["media_status", "media_item_id"],
    ) if "youtube_community_posts" in existing else set()

    video_status_ready = {"media_status", "transcript_status", "comments_status"}.issubset(video_cols)
    channel_profile_ready = {"profile_photo_media_id", "external_links"}.issubset(channel_cols)
    community_ready = {"media_status", "media_item_id"}.issubset(community_cols)
    try:
        duration_cap_seconds = max(0, int(os.getenv("YOUTUBE_MAX_VIDEO_DURATION_MINUTES", "0")) * 60)
    except (TypeError, ValueError):
        duration_cap_seconds = 0

    video_select = """
        WITH video_state AS (
            SELECT v.*,
                   video_mi.id IS NOT NULL AS archived_video_file,
                   thumb_mi.id IS NOT NULL AS archived_thumbnail_file,
                   upper(coalesce(v.duration, '')) AS duration_text,
                   (
                       coalesce((substring(v.duration from 'PT([0-9]+)H'))::int, 0) * 3600
                       + coalesce((substring(v.duration from '([0-9]+)M'))::int, 0) * 60
                       + coalesce((substring(v.duration from '([0-9]+)S'))::int, 0)
                   ) AS duration_seconds
            FROM youtube_videos v
            LEFT JOIN media_items video_mi
                   ON video_mi.source = 'youtube'
                  AND video_mi.content_id = 'video_' || v.platform_video_id
            LEFT JOIN media_items thumb_mi
                   ON thumb_mi.source = 'youtube'
                  AND thumb_mi.content_id = v.platform_video_id
                  AND thumb_mi.content_type = 'thumbnail'
        )
        SELECT COUNT(*)::bigint AS total_videos,
               COUNT(*) FILTER (WHERE archived_video_file)::bigint AS archived_video_files,
               COUNT(*) FILTER (WHERE archived_thumbnail_file)::bigint AS archived_thumbnails,
               COUNT(*) FILTER (WHERE duration_text IN ('P0D', 'PT0S'))::bigint AS live_or_scheduled_placeholders,
               COUNT(*) FILTER (WHERE NOT archived_video_file)::bigint AS missing_videos,
               COUNT(*) FILTER (
                   WHERE NOT archived_video_file
                     AND duration_text IN ('P0D', 'PT0S')
               )::bigint AS placeholder_missing_videos,
               COUNT(*) FILTER (
                   WHERE NOT archived_video_file
                     AND duration_text NOT IN ('P0D', 'PT0S')
                     AND ($1::int <= 0 OR duration_seconds <= $1::int OR duration_text = '')
               )::bigint AS eligible_missing_videos,
               COUNT(*) FILTER (
                   WHERE NOT archived_video_file
                     AND duration_text NOT IN ('P0D', 'PT0S')
                     AND ($1::int <= 0 OR duration_seconds <= $1::int OR duration_text = '')
                     AND v.last_media_attempt_at IS NULL
               )::bigint AS eligible_missing_videos_never_attempted,
               COUNT(*) FILTER (
                   WHERE NOT archived_video_file
                     AND duration_text NOT IN ('P0D', 'PT0S', '')
                     AND $1::int > 0
                     AND duration_seconds > $1::int
               )::bigint AS over_duration_missing_videos
    """
    if video_status_ready:
        video_select += """
               ,COUNT(*) FILTER (WHERE v.media_status='stored')::bigint AS media_status_stored,
               COUNT(*) FILTER (WHERE v.media_status='failed')::bigint AS media_status_failed,
               COUNT(*) FILTER (WHERE v.media_status='skipped')::bigint AS media_status_skipped,
               COUNT(*) FILTER (WHERE v.media_status='pending')::bigint AS media_status_pending,
               COUNT(*) FILTER (WHERE v.transcript_status='stored')::bigint AS transcript_status_stored,
               COUNT(*) FILTER (WHERE v.transcript_status='failed')::bigint AS transcript_status_failed,
               COUNT(*) FILTER (WHERE v.transcript_status='unavailable')::bigint AS transcript_status_unavailable,
               COUNT(*) FILTER (WHERE v.transcript_status='pending')::bigint AS transcript_status_pending,
               COUNT(*) FILTER (WHERE v.comments_status='stored')::bigint AS comments_status_stored,
               COUNT(*) FILTER (WHERE v.comments_status='failed')::bigint AS comments_status_failed,
               COUNT(*) FILTER (WHERE v.comments_status='unavailable')::bigint AS comments_status_unavailable,
               COUNT(*) FILTER (WHERE v.comments_status='pending')::bigint AS comments_status_pending
        """
    video_row = await conn.fetchrow(video_select + " FROM video_state v", duration_cap_seconds, timeout=15)
    video_stats = dict(video_row) if video_row else {}

    out: dict = {
        "schema_ready": video_status_ready and channel_profile_ready,
        "tables": sorted(existing),
        "video_status_columns_ready": video_status_ready,
        "channel_profile_columns_ready": channel_profile_ready,
        "community_columns_ready": community_ready,
        "videos": video_stats,
        "media_backlog": {
            "duration_cap_seconds": duration_cap_seconds,
            "missing_videos": video_stats.get("missing_videos", 0),
            "eligible_missing_videos": video_stats.get("eligible_missing_videos", 0),
            "eligible_missing_videos_never_attempted": video_stats.get(
                "eligible_missing_videos_never_attempted",
                0,
            ),
            "placeholder_missing_videos": video_stats.get("placeholder_missing_videos", 0),
            "over_duration_missing_videos": video_stats.get("over_duration_missing_videos", 0),
        },
    }

    if "youtube_channels" in existing:
        channel_select = """
            SELECT COUNT(*)::bigint AS total_channels,
                   COUNT(*) FILTER (WHERE subscriber_count IS NOT NULL)::bigint AS channels_with_subscriber_count,
                   COUNT(*) FILTER (WHERE video_count IS NOT NULL)::bigint AS channels_with_video_count
        """
        if channel_profile_ready:
            channel_select += """
                   ,COUNT(*) FILTER (WHERE profile_photo_media_id IS NOT NULL)::bigint AS channels_with_profile_photo_media,
                   COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(external_links, '[]'::jsonb)) > 0)::bigint AS channels_with_external_links,
                   COUNT(*) FILTER (WHERE last_video_scan_at IS NOT NULL)::bigint AS channels_video_scanned,
                   COUNT(*) FILTER (WHERE last_community_scan_at IS NOT NULL)::bigint AS channels_community_scanned,
                   COUNT(*) FILTER (WHERE last_skip_reason IS NOT NULL)::bigint AS channels_skipped,
                   COUNT(*) FILTER (WHERE last_error IS NOT NULL)::bigint AS channels_with_error
            """
        channel_row = await conn.fetchrow(channel_select + " FROM youtube_channels", timeout=12)
        out["channels"] = dict(channel_row) if channel_row else {}

    if "youtube_transcripts" in existing:
        row = await conn.fetchrow("SELECT COUNT(*)::bigint AS transcripts FROM youtube_transcripts", timeout=8)
        out["transcripts"] = dict(row) if row else {}
    if "youtube_comments" in existing:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*)::bigint AS comments,
                   COUNT(*) FILTER (WHERE author_channel_id IS NOT NULL)::bigint AS comments_with_author_channel
            FROM youtube_comments
            """,
            timeout=12,
        )
        out["comments"] = dict(row) if row else {}
    if "youtube_community_posts" in existing:
        if community_ready:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::bigint AS community_posts,
                       COUNT(*) FILTER (WHERE has_image)::bigint AS community_posts_with_image,
                       COUNT(*) FILTER (WHERE media_status='stored')::bigint AS community_media_stored,
                       COUNT(*) FILTER (WHERE media_status='failed')::bigint AS community_media_failed,
                       COUNT(*) FILTER (WHERE media_status='pending')::bigint AS community_media_pending
                FROM youtube_community_posts
                """,
                timeout=12,
            )
        else:
            row = await conn.fetchrow("SELECT COUNT(*)::bigint AS community_posts FROM youtube_community_posts", timeout=8)
        out["community"] = dict(row) if row else {}
    if "youtube_edges" in existing:
        rows = await conn.fetch(
            """
            SELECT edge_type, COUNT(*)::bigint AS edges
            FROM youtube_edges
            GROUP BY edge_type
            ORDER BY edges DESC
            LIMIT 20
            """,
            timeout=12,
        )
        out["edges"] = {
            "total_edges": sum(int(r["edges"] or 0) for r in rows),
            "by_type": [dict(r) for r in rows],
        }
    if "youtube_profile_queue" in existing:
        rows = await conn.fetch(
            """
            SELECT status, COUNT(*)::bigint AS profiles
            FROM youtube_profile_queue
            GROUP BY status
            ORDER BY profiles DESC
            """,
            timeout=12,
        )
        out["profile_queue"] = {
            "total_profiles": sum(int(r["profiles"] or 0) for r in rows),
            "by_status": [dict(r) for r in rows],
        }
    if "youtube_spider_queue" in existing:
        rows = await conn.fetch(
            """
            SELECT status, COUNT(*)::bigint AS channels
            FROM youtube_spider_queue
            GROUP BY status
            ORDER BY channels DESC
            """,
            timeout=12,
        )
        out["spider_queue"] = {
            "total_channels": sum(int(r["channels"] or 0) for r in rows),
            "by_status": [dict(r) for r in rows],
        }
    if "collection_targets" in existing:
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE source='youtube')::bigint AS youtube_targets,
                   COUNT(*) FILTER (
                       WHERE source='youtube'
                         AND COALESCE(metadata->>'preserve_on_source_config_sync', 'false')='true'
                   )::bigint AS auto_discovered_targets,
                   COUNT(*) FILTER (WHERE source='youtube' AND status='pending')::bigint AS pending_targets,
                   COUNT(*) FILTER (WHERE source='youtube' AND status='completed')::bigint AS completed_targets,
                   COUNT(*) FILTER (WHERE source='youtube' AND status='error')::bigint AS error_targets
            FROM collection_targets
            """,
            timeout=12,
        )
        out["targets"] = dict(row) if row else {}
    return out


async def _active_rate_limit_cursor_summary(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        rows = await conn.fetch(
            """
            SELECT service, last_processed_id, last_processed_at, status
            FROM service_cursors
            WHERE service ILIKE '%rate_limit'
               OR service ILIKE '%ratelimit'
            ORDER BY last_processed_at DESC NULLS LAST
            """,
            timeout=8,
        )
    except Exception:
        return out
    now_utc = datetime.now(timezone.utc)
    for row in rows:
        d = _rate_limit_cursor_payload(row, now_utc)
        source = _normalize_rate_limit_source(d.get("service"))
        if not source:
            continue
        if source not in out or d["active_now"]:
            out[source] = d
    return out


def _rate_limit_cursor_payload(row, now_utc: datetime) -> dict:
    d = dict(row)
    expiry = None
    streak = None
    raw = str(d.get("last_processed_id") or "")
    if ":" in raw:
        left, right = raw.split(":", 1)
        try:
            expiry = datetime.fromtimestamp(float(left), tz=timezone.utc)
        except Exception:
            expiry = None
        try:
            streak = int(right)
        except Exception:
            streak = None
    d["active_until"] = expiry
    d["streak"] = streak
    d["active_now"] = bool(expiry and expiry > now_utc)
    return d



def _short_age(seconds: object) -> str | None:
    try:
        value = int(seconds or 0)
    except Exception:
        return None
    if value < 0:
        return None
    if value < 90:
        return f"{value}s"
    minutes = value // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_beeper_subsource_row(source_row: dict) -> bool:
    source = str(source_row.get("source") or "")
    return source.startswith(_BEEPER_SUBSOURCE_PREFIX) or source_row.get("parent_source") == "beeper"


def _empty_source_counts() -> dict:
    return {
        "records": 0,
        "messages": 0,
        "media_items": 0,
        "rate_limits": 0,
        "access_errors": 0,
        "latest_record_at": None,
        "latest_media_at": None,
        "latest_event_at": None,
        "media_stats_unavailable": False,
    }


def _merge_source_window(content: dict | None, rate: dict | None) -> dict:
    out = _empty_source_counts()
    if content:
        out.update({
            "records": int(content.get("records") or 0),
            "messages": int(content.get("messages") or 0),
            "media_items": int(content.get("media_items") or 0),
            "latest_record_at": content.get("latest_record_at"),
            "latest_media_at": content.get("latest_media_at"),
            "media_stats_unavailable": bool(content.get("media_stats_unavailable")),
        })
    if rate:
        out.update({
            "rate_limits": int(rate.get("rate_limits") or 0),
            "access_errors": int(rate.get("access_errors") or 0),
            "latest_event_at": rate.get("latest_event_at"),
        })
    return out


def _apply_liveness_floor_to_window(source_row: dict, window: dict, now: datetime | None) -> dict:
    """Avoid fake-zero 24h source activity when exact rollups time out.

    Source liveness is much cheaper than the full per-table count rollup and is
    already required to build the matrix. If the count window has no records but
    liveness proves recent activity inside the window, surface a conservative
    lower bound of one record instead of showing the operator "0".
    """
    if any(int(window.get(key) or 0) for key in ("records", "messages", "media_items")):
        return window
    if source_row.get("status") != "live":
        return window
    age_seconds = _int_or_none(source_row.get("age_seconds"))
    if age_seconds is None or age_seconds < 0 or age_seconds > 86400:
        return window
    out = dict(window)
    out["records"] = 1
    if now is not None:
        out["latest_record_at"] = now - timedelta(seconds=age_seconds)
    out["liveness_floor"] = True
    return out


def _apply_recent_media_floor_to_day_window(day_window: dict, *recent_windows: dict) -> dict:
    """Use exact recent windows as a truthful 24h lower bound when exact 24h media is absent."""
    current_media = sum(int(window.get("media_items") or 0) for window in recent_windows if window)
    day_media = int(day_window.get("media_items") or 0)
    if current_media <= 0 or (day_media > 0 and not day_window.get("media_items_lower_bound")):
        return day_window
    out = dict(day_window)
    out["media_items"] = max(day_media, current_media)
    latest_values = [
        window.get("latest_media_at")
        for window in recent_windows
        if window and window.get("latest_media_at")
    ]
    if out.get("latest_media_at"):
        latest_values.append(out["latest_media_at"])
    if latest_values:
        out["latest_media_at"] = max(latest_values)
    out["media_items_lower_bound"] = True
    if day_window.get("media_stats_unavailable") or any(
        window.get("media_stats_unavailable") for window in recent_windows if window
    ):
        out["media_stats_unavailable"] = True
    return out


def _source_window_totals(rows: list[dict], window_key: str) -> dict:
    out = _empty_source_counts()
    active_sources = 0
    for row in rows:
        if row.get("rollup_exclude"):
            continue
        window = row.get(window_key) or {}
        if any(int(window.get(key) or 0) for key in ("records", "messages", "media_items", "rate_limits", "access_errors")):
            active_sources += 1
        for key in ("records", "messages", "media_items", "rate_limits", "access_errors"):
            out[key] += int(window.get(key) or 0)
        if window.get("media_stats_unavailable"):
            out["media_stats_unavailable"] = True
        if window.get("media_items_lower_bound"):
            out["media_items_lower_bound"] = True
        for key in ("latest_record_at", "latest_media_at", "latest_event_at"):
            value = window.get(key)
            if value and (out.get(key) is None or value > out[key]):
                out[key] = value
    out["active_sources"] = active_sources
    out["total_activity"] = out["records"] + out["media_items"] + out["rate_limits"] + out["access_errors"]
    return out


def _source_matrix_primary_extension_issue(extension_issues: list[dict]) -> dict | None:
    """Pick the issue that best explains missing collection for one source."""
    if not extension_issues:
        return None
    priority = {
        "hook_stale": 0,
        "browser_page_error": 1,
        "browser_content_stale": 1,
        "browser_heartbeat_stale": 2,
        "extension_version_mismatch": 9,
    }
    return min(
        extension_issues,
        key=lambda issue: (
            priority.get(str(issue.get("kind") or ""), 5),
            int(issue.get("age_seconds") or issue.get("content_age_seconds") or 0),
        ),
    )


def _source_matrix_filter_extension_issues_for_current_content(
    extension_issues: list[dict],
    current_content: dict | None,
) -> list[dict]:
    current = current_content or {}
    has_current_content = any(
        int(current.get(key) or 0) > 0
        for key in ("records", "messages", "media_items")
    )
    if not has_current_content:
        return extension_issues
    return [
        issue for issue in extension_issues
        if issue.get("kind") != "browser_content_stale"
    ]


def _source_matrix_filter_extension_blockers_for_current_content(
    extension_issues: list[dict],
    current_content: dict | None,
) -> list[dict]:
    current = current_content or {}
    has_current_content = any(
        int(current.get(key) or 0) > 0
        for key in ("records", "messages", "media_items")
    )
    if not has_current_content:
        return extension_issues
    return [
        issue for issue in extension_issues
        if issue.get("kind") != "extension_version_mismatch"
    ]


def _source_matrix_has_current_content(current_content: dict | None) -> bool:
    current = current_content or {}
    return any(
        int(current.get(key) or 0) > 0
        for key in ("records", "messages", "media_items")
    )


def _source_matrix_x_auth_wall_blocker(browser_url: str) -> dict:
    raw_url = str(browser_url or "").strip()
    location = raw_url or "the X browser tab"
    recovery_url = "https://x.com/home" if "logout=" in raw_url.lower() else location
    return {
        "kind": "auth_wall",
        "severity": "warning",
        "summary": (
            "X browser tab is on the login flow or a session error shell, so the extension is alive "
            "but cannot scrape timeline content."
        ),
        "next_action": (
            f"Open {recovery_url}, log in or restore the X session, then press Scrape now on Social Tabs. "
            f"Last reported tab URL: {location}. "
            "Do not chase Docker logs for this one; the blocker is the interactive browser session."
        ),
    }


def _source_matrix_is_x_auth_wall_url(browser_url: str) -> bool:
    url_lc = str(browser_url or "").lower()
    return (
        "/i/flow/login" in url_lc
        or "/i/jf/onboarding" in url_lc
        or "mode=login" in url_lc
        or "logout=" in url_lc
    )


def _source_matrix_is_x_session_shell(
    browser_url: str,
    browser_health_status: str = "",
    browser_health_reason: str = "",
) -> bool:
    if _source_matrix_is_x_auth_wall_url(browser_url):
        return True
    url_lc = str(browser_url or "").lower()
    if "x.com" not in url_lc and "twitter.com" not in url_lc:
        return False
    status_lc = str(browser_health_status or "").lower()
    reason_lc = str(browser_health_reason or "").lower()
    if status_lc == "external_auth_or_page_shell":
        return True
    return (
        status_lc == "recoverable_error_shell"
        and reason_lc in {
            "try_again_empty_state",
            "something_went_wrong",
            "no_internet_connection",
            "failed_script_url",
            "x_blank_spa_shell",
            "x_no_status_links",
        }
    )


def _source_matrix_has_fresh_browser_content(source_row: dict) -> bool:
    if source_row.get("status") != "live":
        return False
    if source_row.get("browser_content_stale") is True:
        return False
    detail = str(source_row.get("detail") or "")
    return "fresh browser content/probe event is inside the freshness window" in detail


def _source_matrix_blocker(source_row: dict, rate_row: dict | None, cursor_row: dict | None,
                           extension_issues: list[dict]) -> dict:
    source = source_row.get("source")
    status = source_row.get("status")
    bridge_status = source_row.get("bridge_status")
    source_health_error = source_row.get("source_health_error") or ""
    browser_url = str(source_row.get("browser_url") or "")
    browser_health_status = str(source_row.get("browser_health_status") or "")
    browser_health_reason = str(source_row.get("browser_health_reason") or "")
    fresh_browser_content = _source_matrix_has_fresh_browser_content(source_row)
    if source == "x" and _source_matrix_is_x_session_shell(
        browser_url,
        browser_health_status,
        browser_health_reason,
    ):
        return _source_matrix_x_auth_wall_blocker(browser_url)
    if status == "unknown" and str(source_health_error).startswith("source matrix build timed out"):
        return {
            "kind": "stats_unavailable",
            "severity": "ok",
            "summary": source_health_error,
            "next_action": "Wait for the background source-matrix refresh; this is an API stats timeout, not evidence that the collector is degraded.",
        }
    if source == "whatsapp" and bridge_status == "partial":
        return {
            "kind": "whatsapp_partial_pairing",
            "severity": "ok",
            "summary": source_row.get("bridge_detail") or "WhatsApp collection is running with only some bridge slots paired.",
            "next_action": (
                "Collection is still running from the paired WhatsApp bridge. "
                "Open Link WhatsApp and scan the unpaired slot if you expect that second account/device to collect too."
            ),
        }
    if source == "whatsapp" and bridge_status in {"unpaired", "unreachable"}:
        return {
            "kind": "whatsapp_pairing",
            "severity": "error" if bridge_status == "unreachable" else "warning",
            "summary": source_row.get("bridge_detail") or "WhatsApp bridge is not paired.",
            "next_action": "Open Link WhatsApp and scan the QR for any unpaired bridge.",
        }
    if source == "whatsapp" and bridge_status == "paired" and status in {"stale", "unknown", None}:
        bridge_detail = source_row.get("bridge_detail") or "WhatsApp bridge slots are paired and ready."
        freshness_basis = source_row.get("freshness_basis") or "whatsapp_messages.collected_at"
        return {
            "kind": "whatsapp_message_stale",
            "severity": "warning",
            "summary": (
                f"{bridge_detail} However {freshness_basis} has no fresh rows, so the bridge is alive "
                "but WhatsApp message/history sync is not emitting data."
            ),
            "next_action": (
                "Check the WhatsApp bridge HistorySync progress and collector_whatsapp logs. "
                "If it remains at 0 messages after reconnect, unlink/relink that WhatsApp slot from the phone."
            ),
        }
    if cursor_row and cursor_row.get("active_now") and not fresh_browser_content:
        active_until = cursor_row.get("active_until")
        return {
            "kind": "cooldown",
            "severity": "warning",
            "summary": (
                f"Active collector cooldown"
                f"{' until ' + active_until.isoformat() if active_until else ''}"
                f"{' after streak ' + str(cursor_row.get('streak')) if cursor_row.get('streak') else ''}."
            ),
            "next_action": "Let the cooldown expire; do not force this source unless it is Tier 1 emergency data.",
        }
    if rate_row and rate_row.get("active_now") and not fresh_browser_content:
        active_until = rate_row.get("active_until")
        scope = " / ".join(str(v) for v in (rate_row.get("latest_account"), rate_row.get("latest_scope")) if v)
        return {
            "kind": "cooldown",
            "severity": "warning",
            "summary": (
                f"Recent HTTP pressure is cooling down"
                f"{' for ' + scope if scope else ''}"
                f"{' until ' + active_until.isoformat() if active_until else ''}."
            ),
            "next_action": "Wait for the scoped backoff before retrying that path.",
        }
    if rate_row and int(rate_row.get("access_errors") or 0) > 0 and status != "live":
        status_code = rate_row.get("latest_status_code")
        latest_is_429 = _int_or_none(status_code) == 429
        if latest_is_429 and not extension_issues:
            scope = " / ".join(
                str(v) for v in (rate_row.get("latest_account"), rate_row.get("latest_scope")) if v
            )
            return {
                "kind": "rate_limit_recent",
                "severity": "warning",
                "summary": (
                    f"Recent HTTP 429 pressure"
                    f"{' for ' + scope if scope else ''}: "
                    f"{rate_row.get('latest_reason') or 'rate limit detected'}."
                ),
                "next_action": "Let the collector backoff continue; do not treat this as a bad login unless 401/403 errors appear.",
            }
        if not latest_is_429:
            return {
                "kind": "auth_or_access",
                "severity": "error",
                "summary": rate_row.get("latest_reason") or f"Latest HTTP access event was {status_code}.",
                "next_action": "Refresh auth cookies/session or inspect the account-specific scraper log.",
            }
    if browser_health_status == "recoverable_error_shell":
        if source == "x" and _source_matrix_is_x_session_shell(
            browser_url,
            browser_health_status,
            browser_health_reason,
        ):
            return _source_matrix_x_auth_wall_blocker(browser_url)
        platform = str(source or "this platform")
        reason = browser_health_reason or "recoverable page shell"
        return {
            "kind": "browser_page_error",
            "severity": "warning",
            "summary": (
                f"{platform}: browser tab is alive, but the page is showing {reason} instead of usable content."
            ),
            "next_action": (
                f"Focus or refresh the {platform} browser tab, then press Scrape now on Social Tabs. "
                "Reload the unpacked extension only if normal browser heartbeats or bundle version are stale."
            ),
        }
    issue = _source_matrix_primary_extension_issue(extension_issues)
    if issue:
        age_text = _short_age(issue.get("age_seconds"))
        endpoint = issue.get("endpoint")
        version = issue.get("extension_version") or "unknown"
        expected = issue.get("expected_version") or "current"
        detail = issue.get("detail") or "Chrome extension issue detected."
        reload_url = issue.get("reload_url")
        manage_url = _extension_management_url(issue.get("extension_id"))
        if issue.get("kind") == "extension_version_mismatch":
            scope = f"on {endpoint}" if endpoint else "from hook"
            detail = f"{detail} Saw v{version} {scope}; expected v{expected}."
            if issue.get("needs_new_event"):
                detail = (
                    f"Last browser signal {scope} was from old v{version}; expected v{expected}. "
                    "No newer signal has arrived yet to prove the reload took."
                )
        elif issue.get("kind") == "browser_content_stale":
            heartbeat_age = _short_age(issue.get("heartbeat_age_seconds"))
            content_age = _short_age(issue.get("content_age_seconds"))
            stale_after = _short_age(issue.get("stale_after_seconds"))
            platform = str(issue.get("platform") or source or "this platform")
            detail = (
                f"{platform}: browser heartbeat is fresh"
                f"{' (' + heartbeat_age + ' old)' if heartbeat_age else ''}, "
                f"but useful content is stale"
                f"{' (' + content_age + ' old)' if content_age else ''}."
                f"{' Expected within ' + stale_after + '.' if stale_after else ''}"
            )
        elif issue.get("kind") == "browser_page_error":
            heartbeat_age = _short_age(issue.get("heartbeat_age_seconds"))
            platform = str(issue.get("platform") or source or "this platform")
            health_reason = issue.get("health_reason") or "recoverable error shell"
            counts = issue.get("content_counts")
            detail = (
                f"{platform}: browser tab is alive"
                f"{' (' + heartbeat_age + ' heartbeat)' if heartbeat_age else ''}, "
                f"but the page is showing {health_reason} instead of usable content."
            )
            if counts:
                detail = f"{detail} Content counts: {counts}."
        if age_text:
            detail = f"{detail} Last seen {age_text} ago."
        if issue.get("kind") == "hook_stale":
            platform = str(issue.get("platform") or source or "this platform").lower()
            if platform == "instagram":
                hook_target = "the Instagram Direct inbox tab"
            elif platform == "tiktok":
                hook_target = "the TikTok Messages tab"
            else:
                hook_target = "the platform messaging tab"
            if manage_url:
                next_action = (
                    f"Open or focus {hook_target}, then check for a fresh DM hook heartbeat. "
                    f"If normal browser heartbeats are stale too, open {manage_url}, press Reload on UnifiedCollector Bridge, "
                    "then hard-refresh the scraper tabs."
                )
                if reload_url:
                    next_action += f" The in-extension reload shortcut is {reload_url}, but use Chrome's Reload button if the worker is stale."
            elif reload_url:
                next_action = (
                    f"Open or focus {hook_target}, then check for a fresh DM hook heartbeat. "
                    f"If normal browser heartbeats are stale too, open {reload_url} to reload the extension "
                    "and hard-refresh the scraper tabs."
                )
            else:
                next_action = (
                    f"Open or focus {hook_target}, then check for a fresh DM hook heartbeat. "
                    "Reload the unpacked extension only if normal browser heartbeats are stale too."
                )
        elif issue.get("kind") == "browser_content_stale":
            platform = str(issue.get("platform") or source or "this platform")
            target_url = issue.get("url")
            next_action = (
                f"The extension is alive, so do not reload it first. The ingest bridge will ask the {platform} tab "
                "to run one forced scrape pass on the next heartbeat. If this row stays degraded after a few minutes, "
                f"focus or refresh the tab{(' at ' + target_url) if target_url else ''}, then press Scrape now on Social Tabs."
            )
        elif issue.get("kind") == "browser_page_error":
            platform = str(issue.get("platform") or source or "this platform")
            target_url = issue.get("url")
            next_action = (
                f"Focus or refresh the {platform} browser tab"
                f"{(' at ' + target_url) if target_url else ''}, clear the visible page error/login shell, "
                "then press Scrape now on Social Tabs. Reload the unpacked extension only if heartbeats or bundle version are stale."
            )
        elif manage_url:
            tab_action = (
                "refresh or reopen the platform tab so it emits one fresh signal"
                if issue.get("needs_new_event")
                else "refresh or reopen every tab for this platform"
            )
            next_action = (
                f"Open {manage_url}, press Reload on UnifiedCollector Bridge, then open the extension Social Tabs page "
                f"and press Test ingest. After that, {tab_action}. "
                "If it still reports the old version, fully close Chrome and check the unpacked extension path."
            )
            if reload_url:
                next_action += f" The in-extension reload shortcut is {reload_url}, but use Chrome's Reload button if the worker is stale."
        elif reload_url:
            tab_action = (
                "refresh or reopen the platform tab so it emits one fresh signal"
                if issue.get("needs_new_event")
                else "refresh or reopen every tab for this platform"
            )
            next_action = (
                f"Open {reload_url} to reload the extension, then {tab_action}. "
                "If it still reports the old version, use chrome://extensions to reload the unpacked extension "
                "and check the unpacked extension path."
            )
        elif issue.get("needs_new_event"):
            next_action = (
                "Refresh or reopen the platform tab so the current extension emits one fresh signal. "
                "If it still reports the old version, reload the unpacked extension and close duplicate platform tabs/windows."
            )
        else:
            next_action = (
                "Reload the unpacked Chrome extension, then refresh or reopen every tab for this platform. "
                "If it still reports the old version, close duplicate platform tabs/windows."
            )
        return {
            "kind": issue.get("kind") or "extension_issue",
            "severity": "warning",
            "summary": detail,
            "next_action": next_action,
        }
    source_health_error_lc = str(source_health_error).lower()
    detail_lc = str(source_row.get("detail") or "").lower()
    browser_stall_text = f"{source_health_error_lc}\n{detail_lc}"
    browser_stall_marker = source_health_error_lc.startswith("browser capture stalled:") or (
        "browser content progress is" in source_health_error_lc
    )
    stale_browser_marker = browser_stall_marker and (
        "watchdog" in source_health_error_lc
    )
    stale_browser_content_now = bool(source_row.get("browser_content_stale")) or status != "live"
    if stale_browser_content_now and (
        browser_stall_marker
        or "browser content progress is" in detail_lc
    ):
        platform = str(source or "this platform")
        if source == "x" and _source_matrix_is_x_session_shell(
            browser_url,
            browser_health_status,
            browser_health_reason,
        ):
            return _source_matrix_x_auth_wall_blocker(browser_url)
        return {
            "kind": "browser_capture_stalled",
            "severity": "warning",
            "summary": source_health_error or source_row.get("detail") or "Browser content capture has stalled.",
            "next_action": (
                f"Focus or refresh the {platform} browser tab, then press Scrape now on Social Tabs. "
                "Reload the unpacked extension only if heartbeats or bundle version are stale too; "
                "Docker collector logs are secondary for this browser-tab stall."
            ),
        }
    stale_watchdog_degraded_cleared = (
        status == "live"
        and source_row.get("source_health_status") == "degraded"
        and (
            stale_browser_marker
            or (
                source_health_error_lc.startswith("stale ")
                and "watchdog" in source_health_error_lc
            )
        )
    )
    if source_row.get("source_health_status") in {"dead", "auth_paused", "degraded"} and not stale_watchdog_degraded_cleared:
        return {
            "kind": source_row.get("source_health_status"),
            "severity": "error" if source_row.get("source_health_status") == "dead" else "warning",
            "summary": source_row.get("source_health_error") or source_row.get("detail") or "source_health reports trouble.",
            "next_action": "Check the source-specific Docker logs and account/session state.",
        }
    if _is_beeper_subsource_row(source_row) and status in {"stale", "dead", "unknown", None}:
        return {
            "kind": "quiet_beeper_subsource",
            "severity": "ok",
            "summary": source_row.get("detail") or "This Beeper network has no recent messages.",
            "next_action": "No action unless you expected new messages in this Beeper network.",
        }
    if status not in {"live"}:
        return {
            "kind": status or "unknown",
            "severity": "warning",
            "summary": source_row.get("detail") or "No fresh source row is available.",
            "next_action": "Check whether the scraper tab, cookies, or source account are still valid.",
        }
    return {
        "kind": "none",
        "severity": "ok",
        "summary": "Collecting normally.",
        "next_action": "No operator action.",
    }


def _activity_last_seen_at(age_seconds: int | float | None, now: datetime | None = None) -> datetime | None:
    """Wall-clock timestamp of the newest source-specific activity row.

    Complements ``latest_media_at`` (newest media_items row for the source).
    Both are useful for operators: ``activity_last_seen_at`` is the true source
    liveness signal (message table row / posts row / profile update) that
    compute_liveness uses; ``latest_media_at`` is the newest DOWNLOADED media
    file, which for text-heavy realtime sources (whatsapp/beeper) can be days
    or weeks old even while messages are flowing every minute. Exposing them
    as separate fields keeps the freshness UI from having to guess.
    """
    if age_seconds is None:
        return None
    try:
        seconds = int(age_seconds)
    except (TypeError, ValueError):
        return None
    ref = now or datetime.now(timezone.utc)
    return ref - timedelta(seconds=seconds)


def _source_media_freshness(source: str | None, current_window: dict, day_window: dict,
                            media_total: dict | None, now: datetime | None = None) -> dict:
    """Report media freshness separately from source liveness.

    A source can have fresh rows while media downloads are quiet. Keeping this
    out of ``blocker`` avoids false outages while still making stale media
    visible in the operator matrix.
    """
    current_items = int((current_window or {}).get("media_items") or 0)
    day_items = int((day_window or {}).get("media_items") or 0)
    total = media_total or {}
    stats_unavailable = bool(total.get("stats_unavailable"))
    total_items = int(total.get("total_media_items") or 0)
    latest = total.get("latest_media_at")
    expected = source in _MEDIA_PRIMARY_SOURCES or bool(source and source.startswith(_BEEPER_SUBSOURCE_PREFIX))
    beeper_subsource = bool(source and source.startswith(_BEEPER_SUBSOURCE_PREFIX))
    beeper_message_activity = beeper_subsource and any(
        int((window or {}).get(key) or 0) > 0
        for window in (current_window, day_window)
        for key in ("records", "messages")
    )
    now = now or datetime.now(timezone.utc)

    latest_age_seconds = None
    if isinstance(latest, datetime):
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        latest_age_seconds = max(0, int((now - latest).total_seconds()))

    if not expected:
        return {
            "status": "not_primary",
            "severity": "ok",
            "expected": False,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": "Media is not the primary collection signal for this source.",
            "next_action": "Judge freshness from rows/events for this source.",
        }
    if current_items > 0:
        return {
            "status": "fresh",
            "severity": "ok",
            "expected": True,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": f"Stored {current_items:,} media file(s) this hour.",
            "next_action": "No media action.",
        }
    if day_items > 0:
        return {
            "status": "recent",
            "severity": "ok",
            "expected": True,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": f"Stored {day_items:,} media file(s) in the last 24h; none this hour.",
            "next_action": "No action unless this source should be media-heavy right now.",
        }
    if stats_unavailable:
        return {
            "status": "unknown",
            "severity": "warning",
            "expected": True,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": "Media totals are temporarily unavailable under DB load; not claiming this source has zero media.",
            "next_action": "Refresh after DB load drops or inspect media rollups directly before treating this as a collection gap.",
        }
    if beeper_message_activity:
        return {
            "status": "quiet",
            "severity": "ok",
            "expected": True,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": "Messages are flowing in this Beeper network; no recent media files is not a collection failure.",
            "next_action": "No media action unless you expected attachments in this specific network.",
        }
    if total_items <= 0:
        return {
            "status": "none_yet",
            "severity": "warning",
            "expected": True,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": "No media files captured yet for this source.",
            "next_action": "Check whether this scraper has media extraction enabled.",
        }

    age_text = _short_age(latest_age_seconds)
    if latest_age_seconds is not None and latest_age_seconds < 86_400:
        return {
            "status": "recent",
            "severity": "ok",
            "expected": True,
            "current_hour_items": current_items,
            "last_24h_items": day_items,
            "latest_age_seconds": latest_age_seconds,
            "summary": f"Latest media was {age_text} ago; hourly/24h counters may be partial under DB load.",
            "next_action": "No media action unless recent browser ingest keeps seeing candidates without stored files.",
        }

    quiet = latest_age_seconds is None or latest_age_seconds >= _MEDIA_QUIET_WARN_SECONDS
    return {
        "status": "quiet" if quiet else "idle",
        "severity": "warning" if quiet else "ok",
        "expected": True,
        "current_hour_items": current_items,
        "last_24h_items": day_items,
        "latest_age_seconds": latest_age_seconds,
        "summary": (
            f"No media files in the last 24h; latest media was {age_text} ago."
            if age_text
            else "No media files in the last 24h; latest media time is unknown."
        ),
        "next_action": (
            "If this source should contain media, inspect the scraper tab/session and media download logs."
            if quiet
            else "No action unless media is expected this hour."
        ),
    }


def _source_operator_status(source_row: dict, blocker: dict) -> dict:
    """Human-facing status for the matrix.

    ``status`` remains the raw freshness state for API compatibility. This
    display status prevents quiet Beeper networks from looking broken simply
    because no messages arrived recently.
    """
    status = source_row.get("status") or "unknown"
    if blocker.get("kind") == "quiet_beeper_subsource":
        return {"status_label": "quiet", "status_severity": "ok"}
    if blocker.get("kind") == "stats_unavailable":
        return {"status_label": "unknown", "status_severity": "ok"}
    blocker_severity = blocker.get("severity")
    if blocker_severity in {"error", "warning"} and blocker.get("kind") != "none":
        if blocker_severity == "error":
            return {"status_label": "blocked", "status_severity": "error"}
        return {"status_label": "degraded", "status_severity": "warning"}
    if status == "live":
        severity = "ok"
    elif status in {"dead", "unpaired", "unreachable"}:
        severity = "error"
    else:
        severity = "warning"
    return {"status_label": status, "status_severity": severity}


def _source_matrix_row(source_row: dict, current_content: dict | None, current_rate: dict | None,
                       day_content: dict | None, day_rate: dict | None, media_total: dict | None,
                       cursor_row: dict | None, extension_issues: list[dict],
                       now: datetime | None = None, media_backlog: dict | None = None,
                       rolling_content: dict | None = None) -> dict:
    source = source_row.get("source")
    total_media = media_total or {}
    current_window = _merge_source_window(current_content, current_rate)
    day_window = _merge_source_window(day_content, day_rate)
    day_window = _apply_liveness_floor_to_window(source_row, day_window, now)
    day_window = _apply_recent_media_floor_to_day_window(day_window, current_window)
    extension_issues = _source_matrix_filter_extension_issues_for_current_content(
        extension_issues,
        current_window,
    )
    blocker_issues = _source_matrix_filter_extension_blockers_for_current_content(
        extension_issues,
        current_window,
    )
    blocker = _source_matrix_blocker(source_row, day_rate, cursor_row, blocker_issues)
    effective_source_row = source_row
    if (
        blocker.get("kind") == "cooldown"
        and source in {"instagram", "tiktok", "lemon8", "threads", "facebook", "x"}
        and int(current_window.get("media_items") or 0) > 0
    ):
        blocker = {"kind": "none", "severity": "ok", "summary": None, "next_action": "No action."}
        effective_source_row = {**source_row, "status": "live"}
    if (
        blocker.get("kind") in {"auth_wall", "browser_capture_stalled"}
        and _source_matrix_has_current_content(current_window)
    ):
        blocker = {"kind": "none", "severity": "ok", "summary": None, "next_action": "No action."}
        effective_source_row = {**source_row, "status": "live"}
    media_freshness = _source_media_freshness(source, current_window, day_window, total_media, now)
    if (
        blocker.get("kind") == "quiet_beeper_subsource"
        and media_freshness.get("severity") != "ok"
    ):
        media_freshness = {
            **media_freshness,
            "status": "quiet",
            "severity": "ok",
            "summary": "No recent messages in this Beeper network; media silence is expected.",
            "next_action": "No media action unless you expected this network to receive files.",
        }
    backlog = dict(media_backlog or {})
    if (
        blocker.get("kind") == "none"
        and source == "youtube"
        and int(backlog.get("eligible_missing_videos", backlog.get("missing_videos") or 0) or 0) > 0
    ):
        missing = int(backlog.get("eligible_missing_videos", backlog.get("missing_videos") or 0) or 0)
        total_missing = int(backlog.get("missing_videos") or 0)
        recent = int(backlog.get("eligible_missing_videos_touched_24h", backlog.get("missing_videos_touched_24h") or 0) or 0)
        never_attempted = int(backlog.get("eligible_missing_videos_never_attempted") or 0)
        over_cap = int(backlog.get("over_duration_missing_videos") or 0)
        placeholders = int(backlog.get("placeholder_missing_videos") or 0)
        duration_cap_seconds = int(backlog.get("duration_cap_seconds") or 0)
        excluded_parts = []
        if over_cap:
            cap_label = f">{duration_cap_seconds // 60}m" if duration_cap_seconds else "over cap"
            excluded_parts.append(f"{over_cap:,} {cap_label}")
        if placeholders:
            excluded_parts.append(f"{placeholders:,} live/scheduled")
        excluded = f" ({'; '.join(excluded_parts)} excluded from action)" if excluded_parts else ""
        total_note = f" out of {total_missing:,} missing total" if total_missing and total_missing != missing else ""
        blocker = {
            "kind": "media_backlog",
            "severity": "warning",
            "summary": (
                f"{missing:,} eligible YouTube video row(s) still have no archived video file"
                + total_note
                + excluded
                + (f"; {never_attempted:,} have never had a video download attempt" if never_attempted else "")
                + (f"; {recent:,} eligible rows were touched in the last 24h." if recent else ".")
            ),
            "next_action": (
                "Let collector_youtube video backfill run; if stored media stays at 0, "
                "check yt-dlp/cookie logs for duration, auth, or format skips."
            ),
        }
    day_rate = day_rate or {}
    cursor_row = cursor_row or {}
    rate_active_until = cursor_row.get("active_until") or day_rate.get("active_until")
    active_until_dt = _dt_for_compare(rate_active_until)
    ref_now = now or datetime.now(timezone.utc)
    rate_active = bool(active_until_dt and active_until_dt > ref_now)
    day_rate_has_active_fields = any(
        key in day_rate
        for key in ("active_account", "active_scope", "active_status_code", "active_reason")
    )
    if rate_active and day_rate.get("active_now") and day_rate_has_active_fields:
        latest_status_code = day_rate.get("active_status_code")
        latest_account = day_rate.get("active_account") or day_rate.get("latest_account")
        latest_scope = day_rate.get("active_scope") or day_rate.get("latest_scope")
        latest_reason = day_rate.get("active_reason") or day_rate.get("latest_reason")
    elif rate_active and cursor_row.get("active_now") and not day_rate.get("active_now"):
        latest_status_code = None
        latest_account = day_rate.get("latest_account")
        latest_scope = day_rate.get("latest_scope")
        latest_reason = day_rate.get("latest_reason")
    else:
        latest_status_code = day_rate.get("latest_status_code")
        latest_account = day_rate.get("latest_account")
        latest_scope = day_rate.get("latest_scope")
        latest_reason = day_rate.get("latest_reason")
    rolling_window = rolling_content or {}
    return {
        "source": source,
        "display_name": source_row.get("display_name"),
        "parent_source": source_row.get("parent_source"),
        "rollup_exclude": bool(source_row.get("rollup_exclude")),
        "status": source_row.get("status"),
        **_source_operator_status(effective_source_row, blocker),
        "collection_mode": source_row.get("collection_mode"),
        "collection_methods": _source_collection_methods(source),
        "freshness_basis": source_row.get("freshness_basis"),
        "age_seconds": source_row.get("age_seconds"),
        "activity_last_seen_at": _activity_last_seen_at(source_row.get("age_seconds"), now),
        "stale_after_seconds": source_row.get("stale_after_seconds"),
        "detail": source_row.get("detail"),
        "source_health_status": source_row.get("source_health_status"),
        "source_health_error": source_row.get("source_health_error"),
        "source_health_last_success_at": source_row.get("source_health_last_success_at"),
        "source_health_updated_at": source_row.get("source_health_updated_at"),
        "browser_url": source_row.get("browser_url"),
        "bridge_status": source_row.get("bridge_status"),
        "bridge_detail": source_row.get("bridge_detail"),
        "current_hour": current_window,
        "stored_rolling_60m": int(rolling_window.get("media_items") or 0),
        "observed_rolling_60m": int(rolling_window.get("records") or 0),
        "requests_rolling_60m": int(rolling_window.get("requests") or rolling_window.get("records") or 0),
        "latest_content_rolling_60m_at": (
            rolling_window.get("latest_media_at") or rolling_window.get("latest_record_at")
        ),
        "last_24h": day_window,
        "total_media_items": int(total_media.get("total_media_items") or 0),
        "total_media_bytes": int(total_media.get("total_media_bytes") or 0),
        "latest_media_at": total_media.get("latest_media_at"),
        "media_stats_unavailable": bool(total_media.get("stats_unavailable")),
        "media_stats_error": total_media.get("stats_error"),
        "media_freshness": media_freshness,
        "media_backlog": backlog,
        "rate_limit": {
            "active_now": rate_active,
            "active_until": rate_active_until,
            "streak": cursor_row.get("streak"),
            "latest_status_code": latest_status_code,
            "latest_account": latest_account,
            "latest_scope": latest_scope,
            "latest_reason": latest_reason,
        },
        "extension_issues": extension_issues,
        "blocker": blocker,
    }


def _empty_run_ingestion() -> dict:
    return {
        "records": 0,
        "messages": 0,
        "media_items": 0,
        "rate_limits": 0,
        "access_errors": 0,
        "latest_at": None,
        "window_seconds": None,
    }


def _collection_run_payload(row) -> dict:
    data = dict(row)
    data["finished_at"] = data.get("finished_at") or data.get("completed_at")
    data["errors"] = int(data.get("errors") if data.get("errors") is not None else data.get("items_failed") or 0)
    data["items_collected"] = int(data.get("items_collected") or 0)
    return data


async def _enrich_runs_with_ingestion(conn, runs: list[dict]) -> list[dict]:
    if not runs:
        return runs

    def _floor_hour(value: datetime | None) -> datetime | None:
        if not value:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)

    now_utc = datetime.now(timezone.utc)
    started_hours = [_floor_hour(row.get("started_at")) for row in runs]
    started_hours = [hour for hour in started_hours if hour is not None]
    if not started_hours:
        return runs
    start_bound = min(started_hours)
    end_bound = (now_utc + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    required_tables = [table for _source, table, _column, _label in _INGESTION_CONTENT_PARTS]
    required_tables.extend(["media_items", "rate_limit_events"])
    existing_tables = await _existing_public_tables(conn, required_tables)
    raw_parts = [
        f"""
        SELECT '{source}'::text AS source,
               date_trunc('hour', {column}) AS hour,
               count(t.*)::bigint AS records,
               {("count(t.*)" if label == "messages" else "0")}::bigint AS messages,
               0::bigint AS media_items,
               0::bigint AS rate_limits,
               0::bigint AS access_errors,
               max(t.{column}) AS latest_at
        FROM {table} t
        WHERE t.{column} >= $1
          AND t.{column} < $2
        GROUP BY date_trunc('hour', {column})
        """
        for source, table, column, label in _INGESTION_CONTENT_PARTS
        if table in existing_tables
    ]
    if "media_items" in existing_tables:
        raw_parts.append(
            """
            SELECT m.source,
                   date_trunc('hour', m.collected_at) AS hour,
                   0::bigint AS records,
                   0::bigint AS messages,
                   count(m.*)::bigint AS media_items,
                   0::bigint AS rate_limits,
                   0::bigint AS access_errors,
                   max(m.collected_at) AS latest_at
            FROM media_items m
            WHERE m.collected_at >= $1
              AND m.collected_at < $2
            GROUP BY m.source, date_trunc('hour', m.collected_at)
            """
        )
    if "rate_limit_events" in existing_tables:
        raw_parts.append(
            """
            SELECT rl.source,
                   date_trunc('hour', rl.created_at) AS hour,
                   0::bigint AS records,
                   0::bigint AS messages,
                   0::bigint AS media_items,
                   count(rl.*) FILTER (
                       WHERE rl.status_code = 429
                          OR rl.status_code IS NULL
                          OR rl.cooldown_seconds IS NOT NULL
                          OR (
                              rl.source = 'youtube'
                              AND rl.status_code = 403
                              AND rl.reason = 'youtube_api_quota_or_access'
                          )
                   )::bigint AS rate_limits,
                   count(rl.*) FILTER (
                       WHERE rl.status_code IS NOT NULL
                         AND NOT (
                             rl.status_code = 429
                             OR rl.cooldown_seconds IS NOT NULL
                             OR (
                                 rl.source = 'youtube'
                                 AND rl.status_code = 403
                                 AND rl.reason = 'youtube_api_quota_or_access'
                             )
                         )
                   )::bigint AS access_errors,
                   max(rl.created_at) AS latest_at
            FROM rate_limit_events rl
            WHERE rl.created_at >= $1
              AND rl.created_at < $2
            GROUP BY rl.source, date_trunc('hour', rl.created_at)
            """
        )
    hourly: dict[tuple[str, datetime], dict] = {}
    if raw_parts:
        rows = await conn.fetch(
            f"""
            WITH raw AS (
                {" UNION ALL ".join(raw_parts)}
            )
            SELECT source,
                   hour,
                   COALESCE(sum(raw.records), 0)::bigint AS records,
                   COALESCE(sum(raw.messages), 0)::bigint AS messages,
                   COALESCE(sum(raw.media_items), 0)::bigint AS media_items,
                   COALESCE(sum(raw.rate_limits), 0)::bigint AS rate_limits,
                   COALESCE(sum(raw.access_errors), 0)::bigint AS access_errors,
                   max(raw.latest_at) AS latest_at
            FROM raw
            GROUP BY source, hour
            """,
            start_bound,
            end_bound,
            timeout=30,
        )
        hourly = {
            (row["source"], _floor_hour(row["hour"])): dict(row)
            for row in rows
            if row.get("source") and _floor_hour(row["hour"]) is not None
        }
    for run in runs:
        summary = _empty_run_ingestion()
        started = _floor_hour(run.get("started_at"))
        ended = _floor_hour(run.get("finished_at") or now_utc)
        if started and ended:
            cursor = started
            while cursor <= ended:
                item = hourly.get((run.get("source"), cursor))
                if item:
                    for key in ("records", "messages", "media_items", "rate_limits", "access_errors"):
                        summary[key] += int(item.get(key) or 0)
                    latest = item.get("latest_at")
                    if latest and (summary.get("latest_at") is None or latest > summary["latest_at"]):
                        summary["latest_at"] = latest
                cursor += timedelta(hours=1)
            end_actual = run.get("finished_at") or now_utc
            if end_actual and run.get("started_at"):
                summary["window_seconds"] = int((end_actual - run["started_at"]).total_seconds())
        summary["basis"] = "source_hour"
        summary["exact_window"] = False
        for key in ("records", "messages", "media_items", "rate_limits", "access_errors"):
            summary[key] = int(summary.get(key) or 0)
        if summary.get("window_seconds") is not None:
            summary["window_seconds"] = int(summary["window_seconds"])
        run["ingestion"] = summary
        run["ingestion_items"] = (
            summary["records"] + summary["media_items"] + summary["rate_limits"] + summary["access_errors"]
        )
        run["items_label"] = "targets_rearmed"
    return runs



async def _with_bridge_overrides(sources: list[dict]) -> tuple[list[dict], dict | None]:
    """Overlay process-level bridge state on top of DB freshness.

    A WhatsApp row can be stale because the bridge is not paired. Reporting that
    as generic "stale" hides the one operator action that matters: scan a QR.
    """
    bridge_summary = None
    try:
        try:
            bridge_timeout = max(0.25, float(os.getenv("WA_BRIDGE_HEALTH_TIMEOUT_SECONDS", "5.0")))
        except (TypeError, ValueError):
            bridge_timeout = 5.0
        states = await fetch_whatsapp_bridge_health(timeout=bridge_timeout)
        bridge_summary = {
            "summary": summarize_whatsapp_bridge_health(states),
            "bridges": states,
        }
    except Exception as exc:  # noqa: BLE001 - dashboard health must not raise
        bridge_summary = {
            "summary": {
                "status": "unreachable",
                "detail": f"WhatsApp bridge health check failed: {exc}",
                "ready_count": 0,
                "reachable_count": 0,
                "total": 2,
            },
            "bridges": [],
        }

    summary = bridge_summary.get("summary") or {}
    bridge_status = summary.get("status")
    if bridge_status and bridge_status != "paired":
        for source in sources:
            if source.get("source") == "whatsapp":
                source["bridge_status"] = bridge_status
                source["bridge_detail"] = summary.get("detail")
                source["whatsapp_bridges"] = bridge_summary.get("bridges", [])
                if bridge_status in {"unpaired", "unreachable"}:
                    source["status"] = bridge_status
                elif bridge_status == "partial" and int(summary.get("ready_count") or 0) > 0:
                    source["status"] = "live"
                elif bridge_status != "partial" and source.get("status") == "live":
                    source["status"] = "degraded"
                source["detail"] = summary.get("detail") or source.get("detail")
                break
    elif bridge_status == "paired":
        for source in sources:
            if source.get("source") == "whatsapp":
                source["bridge_status"] = bridge_status
                source["bridge_detail"] = summary.get("detail")
                source["whatsapp_bridges"] = bridge_summary.get("bridges", [])
                break

    return sources, bridge_summary

app = FastAPI(title="UnifiedCollector Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8700"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard" / "frontend" / "dist"

# Auth (JWT / bcrypt / /auth routes) extracted to api/auth.py during PERF-002
# sub-plan 4A step 4. Re-import public names for back-compat with tests and
# route sites that reference Depends(require_role(...)).
from src.dashboard.api.auth import (  # noqa: E402,F401
    JWT_SECRET,
    JWT_EXPIRY_HOURS,
    ADMIN_USERNAME,
    ADMIN_PASSWORD,
    security,
    _ROLE_RANK,
    _AUTH_DISABLED,
    get_current_user,
    require_role,
    LoginRequest,
    router as _auth_router,
)

app.include_router(_auth_router)
# Package-split routers included as they become non-empty (PERF-002 4A).
app.include_router(_browser_router)          # step 5 — currently empty placeholder
app.include_router(_source_matrix_router)    # step 6 — currently empty placeholder
app.include_router(_telegram_ops_router)     # step 7 — /api/telegram/*
app.include_router(_strava_router)           # step 8 — /strava/*
app.include_router(_youtube_router)          # step 9 — /youtube/*
app.include_router(_whatsapp_router)         # step 10 — /whatsapp/*
app.include_router(_coverage_router)         # step 11 — /coverage/*
app.include_router(_media_router)            # step 11 — /media/*
app.include_router(_rate_limits_router)      # step 12 — /rate-limits/*
app.include_router(_ops_router)              # step 14 — /dlq, /domain-pacing/*, /api-quotas/*
app.include_router(_social_router)           # step 15 — /social/*
app.include_router(_dm_router)               # step 16 — /instagram/dms/*, /tiktok/dms/*, /dm/telemetry
app.include_router(_targets_router)          # step 17 — /targets/*
app.include_router(_schedules_router)        # step 17 — /schedules/*, /runs/*
app.include_router(_accounts_router)         # step 18 — /platform/{name}/summary, /accounts
app.include_router(_ingestion_router)        # step 19 — /api/backfill-equilibrium, /instagram/health, /ingestion/hourly
app.include_router(_misc_router)             # step 20 — /graph, /messaging/coverage, /stories/overview, /worker/health
app.include_router(_collectors_core_router)  # step 21 — /collectors, /collectors/live, /collectors/action-queue*, /collectors/{source}
app.include_router(_platform_content_router) # step 23 — matrix/telegram/tiktok/threads/github/lemon8/beeper + seen/rollout/recon


@app.exception_handler(Exception)
async def verbose_exception_handler(request: Request, exc: Exception):
    """Surface RAW errors so localhost can diagnose (no localized 500 mask).

    Always logs the full method/path/exception/traceback at ERROR level. When
    DASHBOARD_AUTH_DISABLED (localhost single-user), the JSON body includes the
    exception type, message, and traceback tail so the operator sees exactly
    what broke. On a network-exposed deployment (auth ON) the body stays generic
    to avoid leaking internals, but the server log still has the full trace.
    """
    # Let FastAPI's own HTTPException handling pass through unchanged.
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    tb = traceback.format_exc()
    logger.error(
        "Unhandled error: %s %s -> %s: %s\n%s",
        request.method, request.url.path, type(exc).__name__, exc, tb,
    )
    if _AUTH_DISABLED:
        return JSONResponse(
            status_code=500,
            content={
                "error": type(exc).__name__,
                "detail": str(exc),
                "path": request.url.path,
                "method": request.method,
                "traceback": tb.splitlines()[-12:],
            },
        )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.get("/health")
async def health(include_sources: bool = False, include_storage: bool = False):
    vault = {
        "available": None,
        "writable": None,
        "mode": "skipped_by_config",
        "status": "skipped_by_config",
    }
    backups = {
        "status": "skipped_by_config",
        "mode": "skipped_by_config",
    }
    sources = []
    whatsapp_bridge_health = None
    browser_extension = None
    health_section_errors = []
    try:
        db_probe_timeout = max(5.0, _DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS)
        pool = await asyncio.wait_for(get_pool(), timeout=db_probe_timeout)
        conn = await _acquire_dashboard_conn(pool)
        try:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=min(db_probe_timeout, 10.0))
            if include_storage:
                try:
                    vault = _vault_payload()
                    vault.update(await vault_artifact_counts(conn, timeout=5))
                except Exception as exc:
                    vault = {
                        "available": None,
                        "writable": None,
                        "mode": "error",
                        "counts_error": exc.__class__.__name__,
                    }
            if include_sources:
                try:
                    from src.core.source_freshness import compute_liveness
                    sources = await asyncio.wait_for(
                        compute_liveness(conn),
                        timeout=_DASHBOARD_HEALTH_SOURCES_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError as exc:
                    logger.debug("source liveness health section cancelled: %s", exc.__class__.__name__)
                    cached_matrix = _load_source_matrix_payload_cache()
                    if cached_matrix is not None:
                        _cached_ts, cached_payload = cached_matrix
                        sources = cached_payload.get("sources") or []
                        whatsapp_bridge_health = cached_payload.get("whatsapp_bridge_health")
                    else:
                        health_section_errors.append({
                            "source": "source_liveness",
                            "status": "unknown",
                            "message": f"source liveness unavailable: {exc.__class__.__name__}",
                        })
                except Exception as exc:
                    logger.debug("source liveness health section failed: %s", exc)
                    cached_matrix = _load_source_matrix_payload_cache()
                    if cached_matrix is not None:
                        _cached_ts, cached_payload = cached_matrix
                        sources = cached_payload.get("sources") or []
                        whatsapp_bridge_health = cached_payload.get("whatsapp_bridge_health")
                    else:
                        health_section_errors.append({
                            "source": "source_liveness",
                            "status": "unknown",
                            "message": f"source liveness unavailable: {exc.__class__.__name__}",
                        })
                try:
                    browser_extension = await asyncio.wait_for(
                        _browser_extension_payload(conn),
                        timeout=_DASHBOARD_HEALTH_BROWSER_TIMEOUT_SECONDS,
                    )
                    _browser_extension_suppress_optional_diagnostics_when_active(browser_extension)
                except asyncio.CancelledError as exc:
                    logger.debug("browser extension health section cancelled: %s", exc.__class__.__name__)
                    browser_extension = _browser_extension_fallback_payload(exc.__class__.__name__)
                    if not browser_extension.get("maintenance_ingest_fallback"):
                        health_section_errors.append({
                            "source": "browser_extension",
                            "status": "unknown",
                            "message": f"browser extension diagnostics unavailable: {exc.__class__.__name__}",
                        })
                except Exception as exc:
                    logger.debug("browser extension health section failed: %s", exc)
                    browser_extension = _browser_extension_fallback_payload(exc.__class__.__name__)
                    if not browser_extension.get("maintenance_ingest_fallback"):
                        health_section_errors.append({
                            "source": "browser_extension",
                            "status": "unknown",
                            "message": f"browser extension diagnostics unavailable: {exc.__class__.__name__}",
                        })
        finally:
            await _release_dashboard_conn(pool, conn, "health")
        db_status = "healthy"
        db_health_status = "ok"
    except TimeoutError:
        db_status = "error: timeout"
        db_health_status = "error"
    except Exception as e:
        db_status = f"error: {e}"
        db_health_status = "error"

    if include_sources and sources and whatsapp_bridge_health is None:
        sources, whatsapp_bridge_health = await _with_bridge_overrides(sources)

    drive_ok = True
    vault_ok = True
    backups_ok = True
    if include_storage:
        from src.core.drive_check import check_drive
        drive_ok = check_drive()
        try:
            backups = _normalize_backup_health_payload(backup_status(), include_storage=True)
        except Exception as exc:
            backups = _normalize_backup_health_payload(
                {"status": "error", "error": exc.__class__.__name__},
                include_storage=True,
            )
        vault["status"] = _vault_health_status(vault, include_storage=True)
        vault_ok = vault["status"] == "ok"
        backups_ok = backups.get("status") in {"backup_ok", "backup_running", "backup_disabled"}
    else:
        backups = _normalize_backup_health_payload(backups, include_storage=False)
        vault["status"] = _vault_health_status(vault, include_storage=False)
    source_issues = [s for s in sources if s.get("status") not in {"live"}] if include_sources else []
    if include_sources:
        source_issues.extend(health_section_errors)
    browser_ingest = {}
    if isinstance(browser_extension, dict):
        browser_ingest = browser_extension.get("ingest_health") or {}
    browser_ingest_active = bool(browser_ingest.get("active")) or bool(
        browser_ingest.get("active_platforms")
    )
    browser_extension_issues = []
    if isinstance(browser_extension, dict):
        browser_extension_issues = [
            issue for issue in (browser_extension.get("issues") or [])
            if str((issue or {}).get("severity") or "error").lower() not in {"ok", "info", "warning"}
        ]
    health_degrading_source_issues = source_issues
    if browser_ingest_active:
        health_degrading_source_issues = [
            issue for issue in source_issues
            if str(issue.get("source") or "") != "source_liveness"
        ]
    drive_status = _drive_health_status(drive_ok, include_storage=include_storage)
    health_status = "ok"
    if db_health_status == "error" or backups.get("status") == "error":
        health_status = "error"
    elif vault.get("status") == "blocked" or drive_status == "blocked":
        health_status = "blocked"
    elif not (drive_ok and vault_ok and backups_ok) or health_degrading_source_issues or browser_extension_issues:
        health_status = "degraded"

    payload = {
        "status": health_status,
        "database": db_status,
        "drive": ("mounted" if drive_ok else "missing") if include_storage else "skipped",
        "drive_status": drive_status,
        "database_status": db_health_status,
        "vault": vault,
        "backups": backups,
    }
    if include_sources:
        payload.update({
            "sources": sources,
            "source_issues": source_issues,
            "whatsapp_bridge_health": whatsapp_bridge_health,
            "browser_extension": browser_extension,
        })
    return payload


@app.get("/metrics")
async def metrics():
    """Prometheus text-format metrics (P2-2).

    Dependency-free: renders the exposition format by hand from DB queries, so
    no prometheus_client install / extra port is needed — reuses the dashboard
    web server. Scrape with a standard Prometheus job pointed at :8700/metrics.
    """
    pool = await get_pool()
    lines: list[str] = []

    def emit(name: str, value, help_text: str, mtype: str = "gauge", labels: str = ""):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
        suffix = "{" + labels + "}" if labels else ""
        lines.append(f"{name}{suffix} {value}")

    try:
        vault = _vault_payload()
        emit("uc_vault_available", 1 if vault["available"] else 0,
             "Whether the collector vault root exists", "gauge")
        emit("uc_vault_writable", 1 if vault["writable"] else 0,
             "Whether the collector vault root is writable", "gauge")
        if vault["free_bytes"] is not None:
            emit("uc_vault_free_bytes", vault["free_bytes"],
                 "Free bytes on the collector vault filesystem", "gauge")
        if vault["total_bytes"] is not None:
            emit("uc_vault_total_bytes", vault["total_bytes"],
                 "Total bytes on the collector vault filesystem", "gauge")

        async with pool.acquire() as conn:
            try:
                vault.update(await vault_artifact_counts(conn, timeout=5))
                emit("uc_vault_sidecar_failures", vault["sidecar_failures"],
                     "Total vault sidecar write failures in the dead-letter queue", "gauge")
                emit("uc_vault_artifacts_queued", vault["artifacts_queued"],
                     "Vault sidecar dead-letter queue rows", "gauge")
                emit("uc_vault_artifacts_partial", vault["artifacts_partial"],
                     "Media rows with failed vault sidecar metadata", "gauge")
                emit("uc_vault_artifacts_quarantined", vault.get("artifacts_quarantined", 0),
                     "Media rows with reviewed bad vault artifacts excluded from active partial health", "gauge")
                emit("uc_vault_artifacts_missing_sidecar_estimate", vault.get("artifacts_missing_sidecar", 0),
                     "Estimated media rows with no successful occurrence sidecar metadata", "gauge")
            except Exception:
                pass

            # Per-source media item counts.
            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS n FROM media_items GROUP BY source"
            )
            first = True
            for r in rows:
                emit("uc_media_items_total", r["n"],
                     "Total media items collected per source" if first else "",
                     "counter", labels=f'source="{r["source"]}"')
                first = False

            # Items collected in the last hour (throughput proxy).
            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS n FROM media_items "
                "WHERE collected_at > NOW() - INTERVAL '1 hour' GROUP BY source"
            )
            first = True
            for r in rows:
                emit("uc_media_items_last_hour", r["n"],
                     "Media items collected in the last hour per source" if first else "",
                     "gauge", labels=f'source="{r["source"]}"')
                first = False

            # Seconds since last successful collection per source (staleness).
            rows = await conn.fetch(
                "SELECT source, EXTRACT(EPOCH FROM (NOW() - MAX(collected_at)))::int AS age "
                "FROM media_items GROUP BY source"
            )
            first = True
            for r in rows:
                emit("uc_source_last_success_age_seconds", r["age"] or 0,
                     "Seconds since last collected item per source" if first else "",
                     "gauge", labels=f'source="{r["source"]}"')
                first = False

            # Spider queue depth per source (pending discovery backlog).
            try:
                rows = await conn.fetch(
                    "SELECT source, COUNT(*) AS n FROM spider_queue "
                    "WHERE status = 'pending' GROUP BY source"
                )
                first = True
                for r in rows:
                    emit("uc_spider_queue_pending", r["n"],
                         "Pending spider-queue entries per source" if first else "",
                         "gauge", labels=f'source="{r["source"]}"')
                    first = False
            except Exception:
                pass  # spider_queue may be source-specific; non-fatal

            # Telegram spider queue (separate table).
            try:
                n = await conn.fetchval(
                    "SELECT COUNT(*) FROM telegram_spider_queue WHERE status = 'pending'"
                )
                emit("uc_spider_queue_pending", n or 0, "", "gauge",
                     labels='source="telegram"')
            except Exception:
                pass

            # DLQ depth (unretried failures).
            dlq = await conn.fetchval("SELECT COUNT(*) FROM dead_letter_queue")
            emit("uc_dlq_total", dlq or 0, "Dead-letter-queue entries", "gauge")

            # Recent collection_runs by status (last 24h).
            rows = await conn.fetch(
                "SELECT status, COUNT(*) AS n FROM collection_runs "
                "WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status"
            )
            first = True
            for r in rows:
                emit("uc_collection_runs_24h", r["n"],
                     "Collection runs in the last 24h by status" if first else "",
                     "gauge", labels=f'status="{r["status"]}"')
                first = False

            # Worker liveness: seconds since last health report.
            age = await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (NOW() - last_processed_at))::int "
                "FROM service_cursors WHERE service = '_worker'"
            )
            emit("uc_worker_health_age_seconds", age if age is not None else -1,
                 "Seconds since the worker last reported health (-1 = never)", "gauge")

            # P2-4: permanently-dead sources (watchdog gave up). Alert on > 0.
            try:
                rows = await conn.fetch(
                    "SELECT source, crash_count FROM source_health WHERE status = 'dead'"
                )
                emit("uc_source_dead_total", len(rows),
                     "Number of sources the watchdog permanently gave up on", "gauge")
                for r in rows:
                    emit("uc_source_dead", 1, "", "gauge",
                         labels=f'source="{r["source"]}"')
            except Exception:
                pass  # source_health table may not exist on older deploys

            # Per-source error rate (DLQ entries vs total items).
            try:
                rows = await conn.fetch(
                    "SELECT d.source, d.n AS errors, COALESCE(m.n, 0) AS total "
                    "FROM (SELECT source, COUNT(*) AS n FROM dead_letter_queue GROUP BY source) d "
                    "LEFT JOIN (SELECT source, COUNT(*) AS n FROM media_items GROUP BY source) m "
                    "USING (source)"
                )
                first = True
                for r in rows:
                    total = r["total"] + r["errors"]
                    rate = r["errors"] / total if total > 0 else 0
                    emit("uc_error_rate", f"{rate:.4f}",
                         "Error rate per source (DLQ / total attempts)" if first else "",
                         "gauge", labels=f'source="{r["source"]}"')
                    first = False
            except Exception:
                pass

            # Per-source collection cycle duration (avg of last 24h runs).
            try:
                rows = await conn.fetch(
                    "SELECT source, "
                    "  AVG(EXTRACT(EPOCH FROM (completed_at - started_at)))::int AS avg_secs, "
                    "  MAX(EXTRACT(EPOCH FROM (completed_at - started_at)))::int AS max_secs "
                    "FROM collection_runs "
                    "WHERE status = 'completed' AND completed_at > NOW() - INTERVAL '24 hours' "
                    "GROUP BY source"
                )
                first = True
                for r in rows:
                    emit("uc_cycle_duration_avg_seconds", r["avg_secs"] or 0,
                         "Average collection cycle duration (last 24h)" if first else "",
                         "gauge", labels=f'source="{r["source"]}"')
                    emit("uc_cycle_duration_max_seconds", r["max_secs"] or 0,
                         "Max collection cycle duration (last 24h)" if first else "",
                         "gauge", labels=f'source="{r["source"]}"')
                    first = False
            except Exception:
                pass

            # Pending collection targets per source.
            try:
                rows = await conn.fetch(
                    "SELECT source, status, COUNT(*) AS n "
                    "FROM collection_targets GROUP BY source, status"
                )
                first = True
                for r in rows:
                    emit("uc_targets", r["n"],
                         "Collection targets by source and status" if first else "",
                         "gauge", labels=f'source="{r["source"]}",status="{r["status"]}"')
                    first = False
            except Exception:
                pass

            # Per-account quota usage (issue #8: observability gap).
            try:
                rows = await conn.fetch(
                    "SELECT platform, account, requests_today, requests_hour, "
                    "  hour_bucket, day "
                    "FROM account_quota_usage "
                    "WHERE day >= (NOW() AT TIME ZONE 'Asia/Singapore')::date"
                )
                first = True
                for r in rows:
                    labels = f'platform="{r["platform"]}",account="{r["account"]}"'
                    emit("uc_account_requests_today", r["requests_today"],
                         "Per-account requests today (SGT day)" if first else "",
                         "gauge", labels=labels)
                    emit("uc_account_requests_hour", r["requests_hour"],
                         "Per-account requests in current hour" if first else "",
                         "gauge", labels=labels)
                    first = False
            except Exception:
                pass

            # Per-source account cooldown / health from source_health.
            try:
                rows = await conn.fetch(
                    "SELECT source, status, crash_count, "
                    "  EXTRACT(EPOCH FROM (NOW() - updated_at))::int AS age_seconds "
                    "FROM source_health"
                )
                first = True
                for r in rows:
                    labels = f'source="{r["source"]}",status="{r["status"]}"'
                    emit("uc_source_health_age_seconds", r["age_seconds"],
                         "Seconds since source_health was last updated" if first else "",
                         "gauge", labels=labels)
                    emit("uc_source_crash_count", r["crash_count"],
                         "Crash count per source" if first else "",
                         "gauge", labels=f'source="{r["source"]}"')
                    first = False
            except Exception:
                pass
    except Exception as e:  # pragma: no cover - defensive
        emit("uc_metrics_scrape_error", 1, f"Metrics scrape failed: {e}")

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n")


# /api/backfill-equilibrium route extracted to api/ingestion.py during
# PERF-002 4A step 19 (cluster 7).

# /collectors, /collectors/live routes extracted to api/collectors_core.py
# during PERF-002 4A step 21 (cluster 2).

def _source_matrix_unavailable_payload(*args, **kwargs):
    # Body moved to src/dashboard/api/source_matrix.py during PERF-002 4A step 6.
    # Import lazily to break the circular dependency; source_matrix.py imports
    # __init__.py names via _cfg() when it needs them at call time.
    from src.dashboard.api.source_matrix import _source_matrix_unavailable_payload as _impl
    return _impl(*args, **kwargs)


async def _source_matrix_unavailable_payload_with_fast_health(*args, **kwargs):
    from src.dashboard.api.source_matrix import _source_matrix_unavailable_payload_with_fast_health as _impl
    return await _impl(*args, **kwargs)


def _source_matrix_payload_stale_limit_seconds() -> float:
    from src.dashboard.api.source_matrix import _source_matrix_payload_stale_limit_seconds as _impl
    return _impl()


def _load_source_matrix_payload_cache(now: float | None = None):
    from src.dashboard.api.source_matrix import _load_source_matrix_payload_cache as _impl
    return _impl(now)


def _load_persisted_source_matrix_payload(now: float | None = None):
    from src.dashboard.api.source_matrix import _load_persisted_source_matrix_payload as _impl
    return _impl(now)


def _persist_source_matrix_payload(ts: float, payload: dict) -> None:
    from src.dashboard.api.source_matrix import _persist_source_matrix_payload as _impl
    return _impl(ts, payload)


# /collectors/source-matrix route extracted to api/source_matrix.py during
# PERF-002 4A step 22 (cluster 1). The huge _collectors_source_matrix_payload
# builder + support helpers still live in this file (accessed by source_matrix.py
# via _cfg lookup); moving the builder is a separate follow-up.

# /collectors/action-queue routes extracted to api/collectors_core.py during
# PERF-002 4A step 21 (cluster 2).

async def _collectors_source_matrix_payload():
    """Operator matrix: source status, collection method, volume, and blocker."""
    from src.core.source_freshness import compute_liveness
    pool = await get_pool()
    errors = []
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
    except Exception as exc:  # noqa: BLE001 - source matrix must stay usable during DB pressure
        logger.warning("source matrix DB acquire failed: %s", exc.__class__.__name__)
        errors.append({"section": "db_acquire", "error": exc.__class__.__name__})
        live_sources = _source_matrix_fallback_liveness_rows(
            "source matrix could not acquire a DB connection quickly; showing known source skeleton until load drops"
        )
        whatsapp_bridge_health = None
        beeper_subsources = []
        current_content = {}
        current_rate = {}
        rolling_content = {}
        previous_content = {}
        previous_rate = {}
        day_content = {}
        day_rate = {}
        media_totals = {"__stats_unavailable__": True}
        youtube_media_backlog = {}
        active_cursors = {}
        browser_extension = _browser_extension_fallback_payload("db_acquire_failed")
    else:
        liveness_fallback = _source_matrix_fallback_liveness_rows(
            "source liveness query timed out; showing known source skeleton until DB load drops"
        )
        live_sources = await _source_matrix_section(
            section="source_liveness",
            label="source liveness",
            errors=errors,
            fallback=liveness_fallback,
            awaitable=compute_liveness(conn),
            cache_key="source_liveness",
            timeout=_SOURCE_MATRIX_LIVENESS_TIMEOUT_SECONDS,
        )
        live_sources, whatsapp_bridge_health = await _source_matrix_section(
            section="bridge_overrides",
            label="bridge overrides",
            errors=errors,
            fallback=(live_sources, None),
            awaitable=_with_bridge_overrides(live_sources),
            cache_key="bridge_overrides",
            cache_ttl=15,
            timeout=3,
        )
        browser_platforms = [
            str(row.get("source"))
            for row in live_sources
            if str(row.get("source") or "") in {"instagram", "tiktok", "lemon8", "threads", "facebook", "x", "strava"}
            and row.get("status") != "live"
        ]
        browser_source_fields = await _source_matrix_section(
            section="browser_source_fields",
            label="browser source fields",
            errors=errors,
            fallback={},
            awaitable=_source_matrix_browser_source_fields(conn, browser_platforms),
            cache_key="browser_source_fields",
            cache_ttl=15,
            timeout=1.5,
        )
        _source_matrix_apply_browser_source_fields(live_sources, browser_source_fields)
        if any(
            error["section"] == "source_liveness" and not error.get("stale_cache")
            for error in errors
        ):
            beeper_subsources = []
            current_content = {}
            current_rate = {}
            rolling_content = {}
            previous_content = {}
            previous_rate = {}
            day_content = {}
            day_rate = {}
            media_totals = {"__stats_unavailable__": True}
            youtube_media_backlog = {}
            active_cursors = {}
            browser_extension = _browser_extension_fallback_payload("source_liveness_unavailable")
        else:
            beeper_subsources = await _source_matrix_section(
                section="beeper_subsource_liveness",
                label="beeper sub-source liveness",
                errors=errors,
                fallback=[],
                awaitable=_beeper_subsource_liveness(conn),
                cache_key="beeper_subsource_liveness",
                timeout=3,
            )
            current_content = await _source_matrix_section(
                section="current_content",
                label="current content summary",
                errors=errors,
                fallback={},
                awaitable=_source_content_summary(conn, "date_trunc('hour', now())"),
                cache_key="current_content",
                cache_ttl=15,
                timeout=3,
            )
            current_rate = await _source_matrix_section(
                section="current_rate",
                label="current rate summary",
                errors=errors,
                fallback={},
                awaitable=_source_rate_summary(conn, "date_trunc('hour', now())"),
                cache_key="current_rate",
                cache_ttl=15,
                timeout=3,
            )
            rolling_content = await _source_matrix_section(
                section="rolling_content",
                label="rolling content summary",
                errors=errors,
                fallback={},
                awaitable=_source_rolling_content_summary(conn),
                cache_key="rolling_content",
                cache_ttl=15,
                timeout=3,
            )
            previous_content = await _source_matrix_section(
                section="previous_hour_content",
                label="previous-hour content summary",
                errors=errors,
                fallback={},
                awaitable=_source_content_summary(
                    conn,
                    "date_trunc('hour', now()) - interval '1 hour'",
                    "date_trunc('hour', now())",
                ),
                cache_key="previous_hour_content",
                cache_ttl=60,
                timeout=3,
            )
            previous_rate = await _source_matrix_section(
                section="previous_hour_rate",
                label="previous-hour rate summary",
                errors=errors,
                fallback={},
                awaitable=_source_rate_summary(
                    conn,
                    "date_trunc('hour', now()) - interval '1 hour'",
                    "date_trunc('hour', now())",
                ),
                cache_key="previous_hour_rate",
                cache_ttl=60,
                timeout=3,
            )
            day_content = await _source_matrix_section(
                section="day_content",
                label="24h content summary",
                errors=errors,
                fallback={},
                awaitable=_source_content_summary(
                    conn,
                    "now() - interval '24 hours'",
                    include_media=False,
                ),
                cache_key="day_content",
                cache_ttl=120,
                prefer_stale_cache=True,
                timeout=_SOURCE_MATRIX_DAY_CONTENT_TIMEOUT_SECONDS,
            )
            day_rate = await _source_matrix_section(
                section="day_rate",
                label="24h rate summary",
                errors=errors,
                fallback={},
                awaitable=_source_rate_summary(conn, "now() - interval '24 hours'"),
                cache_key="day_rate",
                cache_ttl=120,
                timeout=3,
            )
            media_totals = await _source_matrix_section(
                section="media_totals",
                label="media totals",
                errors=errors,
                fallback={"__stats_unavailable__": True},
                awaitable=_source_media_totals(conn),
                cache_key="media_totals",
                cache_ttl=120,
                prefer_stale_cache=True,
                timeout=_SOURCE_MATRIX_MEDIA_TOTALS_TIMEOUT_SECONDS,
            )
            if _SOURCE_MATRIX_ENABLE_YOUTUBE_BACKLOG:
                youtube_media_backlog = await _source_matrix_section(
                    section="youtube_media_backlog",
                    label="youtube media backlog",
                    errors=errors,
                    fallback={},
                    awaitable=_youtube_media_backlog(conn),
                    cache_key="youtube_media_backlog",
                    cache_ttl=300,
                    timeout=_SOURCE_MATRIX_YOUTUBE_BACKLOG_TIMEOUT_SECONDS,
                )
            else:
                cached_backlog = _YOUTUBE_MEDIA_BACKLOG_CACHE.get("row")
                youtube_media_backlog = dict(cached_backlog) if isinstance(cached_backlog, dict) else {
                    "stats_unavailable": True,
                    "stats_error": "skipped_on_live_source_matrix",
                }
                youtube_media_backlog["stats_stale"] = True
            active_cursors = await _source_matrix_section(
                section="active_cursors",
                label="active cursor summary",
                errors=errors,
                fallback={},
                awaitable=_active_rate_limit_cursor_summary(conn),
                cache_key="active_cursors",
                cache_ttl=30,
            )
            browser_extension = await _source_matrix_section(
                section="browser_extension",
                label="browser extension summary",
                errors=errors,
                fallback=_browser_extension_fallback_payload("TimeoutError"),
                awaitable=_browser_extension_payload(conn),
                cache_key="browser_extension",
                cache_ttl=15,
                stale_ttl=3600,
                timeout=_SOURCE_MATRIX_BROWSER_EXTENSION_TIMEOUT_SECONDS,
            )
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "source matrix")

    if str((browser_extension.get("ingest_health") or {}).get("state") or "") == "unknown":
        fast_browser_extension = await _browser_extension_fallback_payload_with_fast_ingest("TimeoutError")
        if (fast_browser_extension.get("ingest_health") or {}).get("active"):
            browser_extension = fast_browser_extension
        else:
            browser_extension = _browser_extension_apply_maintenance_ingest_fallback(browser_extension)

    generated_at = datetime.now(timezone.utc)
    live_sources = [*live_sources, *beeper_subsources]
    media_totals_unavailable = bool(media_totals.get("__stats_unavailable__"))
    media_total_unavailable_row = {
        "stats_unavailable": True,
        "stats_error": next(
            (
                error.get("error")
                for error in errors
                if error.get("section") in {"media_totals", "source_liveness"}
            ),
            "unavailable",
        ),
    }
    extension_by_source = _extension_issues_by_source(browser_extension)
    rows = [
        _source_matrix_row(
            source_row,
            current_content.get(source_row["source"]),
            current_rate.get(source_row["source"]),
            day_content.get(source_row["source"]),
            day_rate.get(source_row["source"]),
            (
                media_totals.get(source_row["source"])
                or (
                    {
                        "stats_unavailable": True,
                        "stats_error": "beeper_subsource_timeout",
                    }
                    if source_row.get("parent_source") == "beeper"
                    and media_totals.get("__beeper_subsource_stats_unavailable__")
                    else None
                )
                or (media_total_unavailable_row if media_totals_unavailable else None)
            ),
            active_cursors.get(source_row["source"]),
            extension_by_source.get(source_row["source"], []),
            generated_at,
            youtube_media_backlog if source_row["source"] == "youtube" else None,
            rolling_content.get(source_row["source"]),
        )
        for source_row in live_sources
    ]
    for row in rows:
        source = row["source"]
        row["last_complete_hour"] = _merge_source_window(
            previous_content.get(source),
            previous_rate.get(source),
        )
        row["last_24h"] = _apply_recent_media_floor_to_day_window(
            row["last_24h"],
            row["current_hour"],
            row["last_complete_hour"],
        )
    severity_rank = {"error": 0, "warning": 1, "ok": 2}
    rows.sort(key=lambda r: (
        severity_rank.get((r.get("blocker") or {}).get("severity"), 3),
        r.get("source") or "",
    ))
    current_hour_started_at = generated_at.replace(minute=0, second=0, microsecond=0)
    previous_hour_started_at = current_hour_started_at - timedelta(hours=1)
    return {
        "generated_at": generated_at,
        "current_hour_started_at": current_hour_started_at,
        "last_complete_hour_started_at": previous_hour_started_at,
        "summary": {
            "current_hour": {
                **_source_window_totals(rows, "current_hour"),
                "started_at": current_hour_started_at,
                "elapsed_seconds": int((generated_at - current_hour_started_at).total_seconds()),
            },
            "last_complete_hour": {
                **_source_window_totals(rows, "last_complete_hour"),
                "started_at": previous_hour_started_at,
                "elapsed_seconds": 3600,
            },
            "last_24h": {
                **_source_window_totals(rows, "last_24h"),
                "started_at": generated_at - timedelta(hours=24),
                "elapsed_seconds": 86400,
            },
        },
        "sources": rows,
        "whatsapp_bridge_health": whatsapp_bridge_health,
        "browser_extension": {
            "expected_version": browser_extension.get("expected_version"),
            "extension_id": browser_extension.get("extension_id"),
            "reload_url": browser_extension.get("reload_url"),
            "maintenance": browser_extension.get("maintenance"),
            "ingest_health": browser_extension.get("ingest_health"),
            "issues": browser_extension.get("issues", []),
        },
        "errors": errors,
    }


_PLATFORM_POSTS = {
    "instagram": "instagram_posts", "tiktok": "tiktok_posts", "lemon8": "lemon8_posts",
    "youtube": "youtube_videos", "threads": "threads_posts", "facebook": "facebook_posts",
    "x": "x_posts", "strava": "strava_activities", "search": "search_results",
    "website": "website_pages", "github": "github_commits",
}
_PLATFORM_MESSAGES = {
    "telegram": ("telegram_messages", "collected_at"),
    "whatsapp": ("whatsapp_messages", "collected_at"),
    "beeper": ("beeper_shadow_messages", "ingested_at"),
}


def _normalize_beeper_network(message_network: str | None, chat_network: str | None = None) -> str:
    return _beeper_network_label(message_network, chat_network)


def _messaging_policy(native_source: str | None) -> str:
    if native_source:
        return f"{native_source} native is canonical; Beeper is a mirror/backstop"
    return "Beeper is canonical until a native collector exists"


_LATEST_ACTIVITY_QUERIES = {
    "telegram": ("SELECT max(collected_at) FROM telegram_messages", "telegram messages"),
    "whatsapp": ("SELECT max(collected_at) FROM whatsapp_messages", "whatsapp messages"),
    "beeper": ("SELECT max(ingested_at) FROM beeper_shadow_messages", "beeper messages"),
    "instagram": ("SELECT max(collected_at) FROM instagram_posts", "instagram posts"),
    "tiktok": ("SELECT max(collected_at) FROM tiktok_posts", "tiktok posts"),
    "lemon8": ("SELECT max(collected_at) FROM lemon8_posts", "lemon8 posts"),
    "threads": ("SELECT max(collected_at) FROM threads_posts", "threads posts"),
    "facebook": ("SELECT max(collected_at) FROM facebook_posts", "facebook posts"),
    "x": ("SELECT max(collected_at) FROM x_posts", "x posts"),
    "youtube": ("SELECT max(collected_at) FROM youtube_videos", "youtube videos"),
    "website": ("SELECT max(collected_at) FROM website_pages", "website pages"),
    "github": (
        """
        SELECT max(ts)
        FROM (
            SELECT max(collected_at) AS ts FROM github_users
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_repos
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_commits
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_issues
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_issue_comments
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_pr_reviews
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_pr_review_comments
            UNION ALL
            SELECT max(collected_at) AS ts FROM github_edges
        ) progress
        """,
        "GitHub profile, repo, commit, issue, PR review, comment, or edge rows",
    ),
    "strava": (
        """
        SELECT max(ts)
        FROM (
            SELECT max(collected_at) AS ts FROM strava_activities
            UNION ALL
            SELECT max(collected_at) AS ts FROM strava_gps_streams
            UNION ALL
            SELECT max(collected_at) AS ts FROM media_items WHERE source='strava'
        ) progress
        """,
        "strava activity, GPS stream, or media rows",
    ),
    "search": ("SELECT max(collected_at) FROM search_results", "search results"),
}


# /platform/{name}/summary route extracted to api/accounts.py during
# PERF-002 4A step 18 (cluster 6).

# /social/follow-edges/stats route extracted to api/social.py during PERF-002
# 4A step 15 (cluster 4).
# Cookie-authenticated sources (session cookies under /app/credentials/<source>/).
# Cookie-authenticated sources + their session-cookie name(s). lemon8 is dropped
# (extension-based, no cookies); github uses a token, not cookies.
_COOKIE_SOURCES = {
    "instagram": ("sessionid",),
    "tiktok": ("sessionid", "sessionid_ss"),
    "strava": ("_strava4_session",),
    "youtube": ("__Secure-3PSID", "SID", "LOGIN_INFO"),
}


def _audit_cookie_file(path: Path, session_keys) -> dict | None:
    """Parse a Netscape cookie file: age, session-cookie presence, expiry."""
    import time as _t
    try:
        size = path.stat().st_size
        age_days = round((_t.time() - path.stat().st_mtime) / 86400, 1)
    except Exception:
        return None
    if size == 0:
        return {"file": path.name, "age_days": age_days, "has_session": False,
                "expiry_days": None, "reason": "empty file"}
    session_name = None
    exp = None
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            parts = line.strip().split("\t")
            if len(parts) >= 7 and parts[5] in session_keys and parts[6]:
                session_name = parts[5]
                try:
                    exp = float(parts[4])
                except Exception:
                    exp = None
                break
    except Exception:
        pass
    if not session_name:
        return {"file": path.name, "age_days": age_days, "has_session": False,
                "expiry_days": None, "reason": "no session cookie"}
    expiry_days = None
    reason = None
    if exp and exp > 0:
        expiry_days = round((exp - _t.time()) / 86400, 1)
        if expiry_days < 0:
            reason = "cookie expired"
    return {"file": path.name, "age_days": age_days, "has_session": True,
            "expiry_days": expiry_days, "reason": reason}


# /accounts route extracted to api/accounts.py during PERF-002 4A step 18
# (cluster 6).

async def _realtime_feed_status_from_redis() -> dict:
    try:
        import redis.asyncio as aioredis
        from src.notifications import realtime_feed
    except Exception as exc:  # noqa: BLE001 - Redis is optional for dashboard visibility.
        return {"available": False, "error": exc.__class__.__name__}

    client = None
    try:
        client = aioredis.from_url(
            realtime_feed._redis_url(),  # noqa: SLF001 - shared env parsing, no secrets returned.
            decode_responses=True,
            socket_timeout=1.5,
            socket_connect_timeout=1.5,
        )
        await client.ping()
        queue_depth = int(await client.llen(realtime_feed._queue_key()) or 0)  # noqa: SLF001
        deferred_burst = int(await client.get(realtime_feed.DEFERRED_KEY_DEFAULT) or 0)
        failed_depth = int(await client.llen(realtime_feed.FAILED_KEY_DEFAULT) or 0)
        local_fallback_total = int(await client.get(realtime_feed.LOCAL_FALLBACK_TOTAL_KEY) or 0)
        raw_by_source = await client.hgetall(realtime_feed.LOCAL_FALLBACK_BY_SOURCE_KEY)
        by_source = {
            str(source): int(count or 0)
            for source, count in (raw_by_source or {}).items()
        }
        raw_by_reason = await client.hgetall(realtime_feed.LOCAL_FALLBACK_BY_REASON_KEY)
        by_reason = {
            str(reason): int(count or 0)
            for reason, count in (raw_by_reason or {}).items()
        }
        raw_by_source_reason = await client.hgetall(realtime_feed.LOCAL_FALLBACK_BY_SOURCE_REASON_KEY)
        by_source_reason: dict[str, dict[str, int]] = {}
        for field, count in (raw_by_source_reason or {}).items():
            source_name, sep, reason = str(field or "").partition(":")
            if not sep:
                continue
            by_source_reason.setdefault(source_name, {})[reason] = int(count or 0)
        raw_source_counters = await client.hgetall(realtime_feed.SOURCE_COUNTER_TOTALS_KEY)
        source_counters = realtime_feed.source_counters_from_hash(raw_source_counters)
        last_raw = await client.get(realtime_feed.LOCAL_FALLBACK_LAST_KEY)
        try:
            last = json.loads(last_raw) if last_raw else None
        except json.JSONDecodeError:
            last = None
        return {
            "available": True,
            "queue_depth": queue_depth,
            "skipped_burst": deferred_burst,
            "deferred_burst": deferred_burst,
            "failed_depth": failed_depth,
            "local_fallback_total": local_fallback_total,
            "local_fallback_by_source": by_source,
            "local_fallback_by_reason": by_reason,
            "local_fallback_by_source_reason": by_source_reason,
            "local_fallback_last": last,
            "source_counters": source_counters,
        }
    except Exception as exc:  # noqa: BLE001 - dashboard should degrade, not fail.
        return {"available": False, "error": exc.__class__.__name__}
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


async def _realtime_delivery_ledger_status() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.realtime_media_deliveries')")
        if not exists:
            return {"available": False, "reason": "table_missing"}
        status_rows = await conn.fetch(
            """
            SELECT status, count(*)::int AS count
            FROM realtime_media_deliveries
            WHERE updated_at >= NOW() - INTERVAL '24 hours'
            GROUP BY status
            ORDER BY status
            """,
            timeout=3,
        )
        source_rows = await conn.fetch(
            """
            SELECT source,
                   count(*)::int AS total,
                   count(*) FILTER (WHERE status = 'enqueued')::int AS enqueued,
                   count(*) FILTER (WHERE status = 'delivered')::int AS delivered,
                   count(*) FILTER (WHERE status = 'skipped')::int AS skipped,
                   count(*) FILTER (WHERE status = 'deduped')::int AS deduped,
                   count(*) FILTER (WHERE status = 'too_large')::int AS too_large,
                   count(*) FILTER (WHERE status = 'failed')::int AS failed,
                   max(updated_at) AS latest_at
            FROM realtime_media_deliveries
            WHERE updated_at >= NOW() - INTERVAL '24 hours'
            GROUP BY source
            ORDER BY latest_at DESC NULLS LAST
            LIMIT 30
            """,
            timeout=3,
        )
        latest_rows = await conn.fetch(
            """
            SELECT source, content_id, status, reason, file_size, content_type,
                   target_name, updated_at
            FROM realtime_media_deliveries
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            timeout=3,
        )
        reason_rows = await conn.fetch(
            """
            SELECT COALESCE(telegram_result->>'fallback_bucket', reason, 'unknown') AS reason,
                   count(*)::int AS count
            FROM realtime_media_deliveries
            WHERE updated_at >= NOW() - INTERVAL '24 hours'
              AND (
                telegram_result ? 'fallback_bucket'
                OR reason IN ('local_media_text_fallback', 'telegram_too_large', 'telegram_send_failed')
              )
            GROUP BY 1
            ORDER BY count DESC, reason
            """,
            timeout=3,
        )
    return {
        "available": True,
        "window_hours": 24,
        "status_counts": {row["status"]: int(row["count"] or 0) for row in status_rows},
        "reason_counts": {row["reason"]: int(row["count"] or 0) for row in reason_rows},
        "by_source": [dict(row) for row in source_rows],
        "latest": [dict(row) for row in latest_rows],
    }
# /instagram/health + /ingestion/hourly routes and their helpers extracted
# to api/ingestion.py during PERF-002 4A step 19 (cluster 7).

def _jsonish(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value if isinstance(value, dict) else {}


# /domain-pacing/status and /api-quotas/status routes + their impl functions
# extracted to api/ops.py during PERF-002 4A step 14 (cluster 8).


# Social registry routes (/social/stats, /social/network, /social/users,
# /social/scrape-config, /social HTML page) extracted to api/social.py
# during PERF-002 4A step 15 (cluster 4).


# /dlq route extracted to api/ops.py during PERF-002 4A step 14 (cluster 8).


# TargetRequest, _target_already_known, POST/DELETE/GET /targets, PUT /schedules/{source}
# and GET /runs/{run_id} extracted to api/targets.py + api/schedules.py during
# PERF-002 4A step 17 (cluster 3).

# /collectors/{source} route extracted to api/collectors_core.py during
# PERF-002 4A step 21 (cluster 2).

# /graph and /messaging/coverage routes extracted to api/misc.py during
# PERF-002 4A step 20 (cluster 9).

# ── Media browser ──


# /stories/overview route extracted to api/misc.py during PERF-002 4A step 20.

def _parse_media_uuid(media_id: str) -> _uuid.UUID:
    """Validate the ``media_id`` path param as a UUID.

    ``media_items.id`` is a UUID but the endpoint was previously typed ``int``,
    so every ``Number(item.id)`` from the frontend turned into ``NaN`` and the
    request got a 422 -- which is what the "broken image" tiles in the media
    browser actually were.
    """
    try:
        return _uuid.UUID(media_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Invalid media id")


def _is_relative_to_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_media_roots() -> list[Path]:
    """Roots that are allowed to serve media_items.file_path values.

    Legacy collectors store paths under COLLECTOR_DRIVE_PATH. New vault-backed
    media blobs are keyed by sha256 under VAULT_ROOT/media. Both locations map
    to the same external vault in production, but containers can see them as
    distinct mount points.
    """
    from src.core.drive_check import DRIVE_PATH as _DRIVE_PATH

    roots = [
        Path(_DRIVE_PATH).resolve(),
        (Path(VAULT_ROOT) / "media").resolve(),
    ]
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _resolve_media_path(file_path_str: str) -> Path:
    """Resolve a media_items.file_path, constrained to configured media roots.

    Raises HTTPException (403/404) on traversal or a missing file. Shared by the
    thumbnail + file endpoints so a poisoned file_path can't serve host files.
    """
    if not file_path_str:
        raise HTTPException(status_code=404, detail="Media path missing")
    file_path = Path(file_path_str).resolve()
    if not any(_is_relative_to_path(file_path, root) for root in _allowed_media_roots()):
        raise HTTPException(status_code=403, detail="Path outside media roots")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return file_path


def _thumbnail_placeholder(label: str, detail: str = "") -> Response:
    safe_label = html.escape((label or "media").upper()[:24])
    safe_detail = html.escape((detail or "preview unavailable")[:64])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" viewBox="0 0 300 300" role="img" aria-label="{safe_label}">
<rect width="300" height="300" fill="#111827"/>
<rect x="18" y="18" width="264" height="264" rx="10" fill="#1f2937" stroke="#374151" stroke-width="2"/>
<circle cx="150" cy="122" r="34" fill="#4b5563"/>
<path d="M142 104 L172 122 L142 140 Z" fill="#e5e7eb"/>
<text x="150" y="190" text-anchor="middle" fill="#f9fafb" font-family="Arial, sans-serif" font-size="24" font-weight="700">{safe_label}</text>
<text x="150" y="218" text-anchor="middle" fill="#9ca3af" font-family="Arial, sans-serif" font-size="13">{safe_detail}</text>
</svg>"""
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300"},
    )




# ── WhatsApp: Users ──



# ── WhatsApp: Chats & messages (Baileys bridge → RabbitMQ → collector) ──
#
# Same two-pane pattern as /instagram/dms/threads + /instagram/dms/thread/{id}
# but backed by whatsapp_chats + whatsapp_messages + whatsapp_users. The path
# param on the message endpoint is the platform_chat_id (JID e.g.
# 6591234567@s.whatsapp.net or 120363xxx@g.us) — chat_id in the DB is a uuid
# so we look up by JID and rewrite to the fk before fetching messages.
#
# media_id is joined from media_items on file_path = media_url so the frontend
# can reuse /media/{id}/thumbnail + /media/{id}/file (already drive-confined)
# instead of a new WhatsApp-specific media proxy.



# ── Instagram: DMs (captured ban-safely by the extension observing direct_v2) ──

# DM routes (/instagram/dms/*, /tiktok/dms/*, /dm/telemetry) extracted to
# api/dm.py during PERF-002 4A step 16 (cluster 5).

# ── WhatsApp: Links ──





# /worker/health route extracted to api/misc.py during PERF-002 4A step 20.

# /schedules (GET/POST), /schedules/{source} DELETE, /targets GET, /runs GET
# extracted to api/schedules.py + api/targets.py during PERF-002 4A step 17.

# ── Strava following-feed endpoints ──
#
# Powers the dashboard /strava/feed page. All endpoints are read-only and
# require viewer role. Backed by the strava_activities table, which is
# populated by both the API path (collect_athlete_profile/_collect_activities_api)
# and the cookie path (fetch_feed_for_date / backfill_feed_history).






@app.websocket("/ws/health")
async def ws_health(ws):
    await health_ws(ws)


# Matrix / Telegram / TikTok / Threads / GitHub / Lemon8 / Beeper content routes
# plus /seen/targets, /optional-rollout/status, and /recon/* routes extracted to
# api/platform_content.py during PERF-002 4A step 23 (clusters 11-14).

if DIST_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")

    _DIST_ROOT = DIST_DIR.resolve()

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # Resolve the requested file under DIST_DIR and refuse anything
        # that escapes the SPA root via traversal.
        try:
            candidate = (DIST_DIR / path).resolve()
            candidate.relative_to(_DIST_ROOT)
        except (ValueError, OSError):
            return FileResponse(str(DIST_DIR / "index.html"))
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(DIST_DIR / "index.html"))
