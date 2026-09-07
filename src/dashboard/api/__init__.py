"""FastAPI application assembly for the collector dashboard.

This module is intentionally thin: it constructs the ``app`` object, wires
middleware and static files, includes every per-domain router, and re-imports
symbols from the split modules so test monkey-patches on ``dashboard_api.X``
continue to work.

All routes live in per-domain modules under ``src/dashboard/api/``. See
``docs/plans/perf-file-splits.md`` for the PERF-002 sub-plan 4A history and
``.agents/JOURNAL.md`` for the extraction log.

Modules and what they contain:

* ``auth.py`` — /auth/* routes, JWT/bcrypt config, ``require_role``.
* ``browser.py`` — Chrome extension diagnostics used by /health + source-matrix.
* ``source_matrix.py`` — /collectors/source-matrix route + payload cache.
* ``telegram_ops.py`` — /api/telegram/* onboarding-ops routes.
* ``strava.py`` — /strava/* routes.
* ``youtube.py`` — /youtube/* routes.
* ``whatsapp.py`` — /whatsapp/* routes.
* ``coverage.py`` — /coverage/collectors.
* ``media.py`` — /media, /media/browse, /media/{id}/*, /media/realtime-feed/*.
* ``rate_limits.py`` — /rate-limits/recent.
* ``ops.py`` — /dlq, /domain-pacing/*, /api-quotas/*.
* ``social.py`` — /social/*.
* ``dm.py`` — /instagram/dms/*, /tiktok/dms/*, /dm/telemetry.
* ``targets.py`` — /targets/*.
* ``schedules.py`` — /schedules/*, /runs/*.
* ``accounts.py`` — /platform/{name}/summary, /accounts.
* ``ingestion.py`` — /api/backfill-equilibrium, /instagram/health, /ingestion/hourly.
* ``misc.py`` — /graph, /messaging/coverage, /stories/overview, /worker/health.
* ``collectors_core.py`` — /collectors, /collectors/live, /collectors/action-queue*.
* ``platform_content.py`` — /api/matrix/*, /telegram/chats, /tiktok/*, /threads/*,
                              /github/*, /lemon8/*, /beeper/*, /seen/*, /recon/*.
* ``observability.py`` — /health, /metrics, /ws/health + exception handler.
* ``helpers.py`` — module-level caches, TTL config, pure utilities, DB pool helpers.
* ``health_helpers.py`` — vault/backup/drive health payload builders.
* ``_shared.py`` — big helper block (content/rate summaries, source-matrix helpers,
                    builder, bridge-override merge, platform constants, cookie
                    audit, realtime status, media resolution).

The ``_SOURCE_MATRIX_PAYLOAD_BUILD_TASK`` module-level global stays here because
tests bind to it and callers mutate it via ``global``. ``source_matrix.py``'s
route accesses it through ``sys.modules["src.dashboard.api"]``.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-imports of leaf helper modules — keeps ``dashboard_api._X`` a valid
# monkey-patch target for tests written before the package split.
# ---------------------------------------------------------------------------

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

from src.core.vault import VAULT_ROOT, vault_health, vault_artifact_counts  # noqa: F401 - re-export for tests
from src.core.whatsapp_bridge_health import (  # noqa: F401 - re-export for tests
    fetch_whatsapp_bridge_health,
    summarize_whatsapp_bridge_health,
)
from src.core.strava_route_queue import fetch_strava_route_capture_queue  # noqa: F401 - re-export for tests
from src.core.collection_coverage import build_collection_coverage_snapshot  # noqa: F401 - re-export for tests
from src.core.seen_targets import (  # noqa: F401 - re-export for tests
    list_seen_targets,
    refresh_seen_targets_from_sources,
    seen_target_summary_by_source,
)
from src.core.optional_rollout import optional_rollout_report  # noqa: F401 - re-export for tests
from src.backup.db_backup import backup_status  # noqa: F401 - re-export for tests
from src.dashboard.websocket import health_ws  # noqa: F401 - re-export for tests
from src.db.connection import get_pool  # noqa: F401 - re-export for tests

# Mutable module-level task handle. Kept here because callers rebind via
# ``global`` and tests set ``dashboard_api._SOURCE_MATRIX_PAYLOAD_BUILD_TASK = ...``.
# ``source_matrix.py`` reads/writes this attribute via ``sys.modules``.
_SOURCE_MATRIX_PAYLOAD_BUILD_TASK: "asyncio.Task | None" = None


# Bulk-imported helper block. ``_shared.py`` holds the big helper set
# (content/rate summaries, source-matrix helpers + builder, cookie audit,
# realtime redis/ledger, media path resolution). Copy every public and
# private name into this module's namespace so tests can patch
# ``dashboard_api._X`` transparently.
from src.dashboard.api import _shared as _sh  # noqa: E402
for _n in dir(_sh):
    if not _n.startswith("__"):
        globals()[_n] = getattr(_sh, _n)
del _sh, _n

# Per-domain routers.
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
from src.dashboard.api.source_matrix import (  # noqa: E402,F401
    router as _source_matrix_router,
    collectors_source_matrix,
    _get_build_task,
    _set_build_task,
)
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
from src.dashboard.api.strava import (  # noqa: E402,F401
    router as _strava_router,
    strava_list_athletes,
    strava_feed_dates,
    strava_feed_activities,
    strava_feed_stats,
    strava_route_capture_queue,
)
from src.dashboard.api.youtube import (  # noqa: E402,F401
    router as _youtube_router,
    youtube_completeness,
    list_youtube_channels,
    youtube_channel_detail,
)
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
from src.dashboard.api.coverage import (  # noqa: E402,F401
    router as _coverage_router,
    collectors_coverage,
)
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
from src.dashboard.api.rate_limits import (  # noqa: E402,F401
    router as _rate_limits_router,
    recent_rate_limits,
)
from src.dashboard.api.ops import (  # noqa: E402,F401
    router as _ops_router,
    list_dlq,
    domain_pacing_status,
    _domain_pacing_status_impl,
    api_quotas_status,
)
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
from src.dashboard.api.dm import (  # noqa: E402,F401
    router as _dm_router,
    list_ig_dm_threads,
    ig_dm_thread_messages,
    list_tt_dm_threads,
    tt_dm_thread_messages,
    dm_telemetry,
)
from src.dashboard.api.targets import (  # noqa: E402,F401
    router as _targets_router,
    TargetRequest,
    _target_already_known,
    create_target,
    delete_target,
    list_targets,
)
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
from src.dashboard.api.accounts import (  # noqa: E402,F401
    router as _accounts_router,
    platform_summary,
    accounts_overview,
)
from src.dashboard.api.ingestion import (  # noqa: E402,F401
    router as _ingestion_router,
    backfill_equilibrium,
    instagram_health,
    _instagram_health_impl,
    _derive_instagram_stuck_stage,
    hourly_ingestion,
)
from src.dashboard.api.misc import (  # noqa: E402,F401
    router as _misc_router,
    social_graph,
    messaging_coverage,
    stories_overview,
    worker_health,
)
from src.dashboard.api.collectors_core import (  # noqa: E402,F401
    router as _collectors_core_router,
    list_collectors,
    collectors_live,
    collectors_action_queue_sync,
    collectors_action_queue,
    collector_detail,
)
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
from src.dashboard.api.observability import (  # noqa: E402,F401
    router as _observability_router,
    verbose_exception_handler,
    health,
    metrics,
    ws_health,
)


# ---------------------------------------------------------------------------
# App assembly.
# ---------------------------------------------------------------------------

app = FastAPI(title="UnifiedCollector Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8700"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard" / "frontend" / "dist"

app.include_router(_auth_router)
app.include_router(_browser_router)
app.include_router(_source_matrix_router)
app.include_router(_telegram_ops_router)
app.include_router(_strava_router)
app.include_router(_youtube_router)
app.include_router(_whatsapp_router)
app.include_router(_coverage_router)
app.include_router(_media_router)
app.include_router(_rate_limits_router)
app.include_router(_ops_router)
app.include_router(_social_router)
app.include_router(_dm_router)
app.include_router(_targets_router)
app.include_router(_schedules_router)
app.include_router(_accounts_router)
app.include_router(_ingestion_router)
app.include_router(_misc_router)
app.include_router(_collectors_core_router)
app.include_router(_platform_content_router)
app.include_router(_observability_router)
app.add_exception_handler(Exception, verbose_exception_handler)


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
