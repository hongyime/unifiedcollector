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

# Constants moved to api/_shared.py during PERF-002 4A step 24.
# Only the mutable ``_SOURCE_MATRIX_PAYLOAD_BUILD_TASK`` stays here because
# callers rebind it via ``global`` and tests write to
# ``dashboard_api._SOURCE_MATRIX_PAYLOAD_BUILD_TASK`` — the source_matrix.py
# route accesses this attribute via ``sys.modules["src.dashboard.api"]``.
_SOURCE_MATRIX_PAYLOAD_BUILD_TASK: asyncio.Task | None = None


# Big helper block extracted to api/_shared.py during PERF-002 4A step 24.
# Re-import all names into __init__.py's namespace so tests that patch
# ``dashboard_api.X`` and route sites that reference ``_X`` keep working.
from src.dashboard.api import _shared as _sh  # noqa: E402
for _n in dir(_sh):
    if not _n.startswith('__'):
        globals()[_n] = getattr(_sh, _n)
del _sh, _n

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

# Observability routes (/health, /metrics, /ws/health) + exception handler
# moved to api/observability.py during PERF-002 4A step 26.
from src.dashboard.api.observability import (  # noqa: E402,F401
    router as _observability_router,
    verbose_exception_handler,
    health,
    metrics,
    ws_health,
)
app.include_router(_observability_router)    # step 26 — /health, /metrics, /ws/health
app.add_exception_handler(Exception, verbose_exception_handler)


# /health, /metrics routes + verbose_exception_handler moved to
# api/observability.py during PERF-002 4A step 26.

# /api/backfill-equilibrium route extracted to api/ingestion.py during
# PERF-002 4A step 19 (cluster 7).

# /collectors, /collectors/live routes extracted to api/collectors_core.py
# during PERF-002 4A step 21 (cluster 2).

# Remaining helpers (source_matrix builder + shims, platform constants,
# cookie audit, realtime status, media resolution) moved to api/_shared.py
# during PERF-002 4A step 25.







# /ws/health websocket route moved to api/observability.py during
# PERF-002 4A step 26.

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
