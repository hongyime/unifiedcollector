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
