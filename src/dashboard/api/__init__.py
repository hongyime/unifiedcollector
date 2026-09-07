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

# Remaining helpers (source_matrix builder + shims, platform constants,
# cookie audit, realtime status, media resolution) moved to api/_shared.py
# during PERF-002 4A step 25.







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
