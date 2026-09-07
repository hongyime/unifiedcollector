"""Observability routes: /health, /metrics, /ws/health, plus the global
verbose exception handler.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 26
so that ``__init__.py`` collapses to a thin app-assembly module.

The plan's baseline recommendation was to keep /health and /metrics in
__init__.py because they are app-level probes. The extraction here is the
"bonus" step called out in the plan ("< 300 was the plan's aspiration").
"""
from __future__ import annotations

import asyncio
import logging
import sys
import traceback

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.backup.db_backup import backup_status
from src.core.vault import vault_artifact_counts
from src.db.connection import get_pool
from src.dashboard.websocket import health_ws
from src.dashboard.api.auth import _AUTH_DISABLED
from src.dashboard.api.helpers import (
    _DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS,
    _acquire_dashboard_conn,
    _release_dashboard_conn,
)
from src.dashboard.api.browser import (
    _browser_extension_payload,
    _browser_extension_fallback_payload,
    _browser_extension_suppress_optional_diagnostics_when_active,
)
from src.dashboard.api.health_helpers import (
    _vault_payload,
    _normalize_backup_health_payload,
    _vault_health_status,
    _drive_health_status,
)

logger = logging.getLogger(__name__)


def _p(name: str, default=None):
    """Look up a name on the parent ``dashboard_api`` module at call time."""
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return default
    return getattr(root, name, default)


router = APIRouter()


async def verbose_exception_handler(request: Request, exc: Exception):
    """Surface RAW errors so localhost can diagnose (no localized 500 mask).

    Always logs the full method/path/exception/traceback at ERROR level. When
    DASHBOARD_AUTH_DISABLED (localhost single-user), the JSON body includes the
    exception type, message, and traceback tail so the operator sees exactly
    what broke. On a network-exposed deployment (auth ON) the body stays generic
    to avoid leaking internals, but the server log still has the full trace.
    """
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


@router.get("/health")
async def health(include_sources: bool = False, include_storage: bool = False):
    _with_bridge_overrides = _p("_with_bridge_overrides")
    _load_source_matrix_payload_cache = _p("_load_source_matrix_payload_cache")
    _bx_payload = _p("_browser_extension_payload", _browser_extension_payload)
    _bx_fallback = _p("_browser_extension_fallback_payload", _browser_extension_fallback_payload)
    _bx_suppress = _p(
        "_browser_extension_suppress_optional_diagnostics_when_active",
        _browser_extension_suppress_optional_diagnostics_when_active,
    )
    _vault_payload_fn = _p("_vault_payload", _vault_payload)
    _normalize_backup_fn = _p("_normalize_backup_health_payload", _normalize_backup_health_payload)
    _vault_health_fn = _p("_vault_health_status", _vault_health_status)
    _drive_health_fn = _p("_drive_health_status", _drive_health_status)
    _get_pool = _p("get_pool", get_pool)
    _acquire = _p("_acquire_dashboard_conn", _acquire_dashboard_conn)
    _release = _p("_release_dashboard_conn", _release_dashboard_conn)
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
        pool = await asyncio.wait_for(_get_pool(), timeout=db_probe_timeout)
        conn = await _acquire(pool)
        try:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=min(db_probe_timeout, 10.0))
            if include_storage:
                try:
                    vault = _vault_payload_fn()
                    vault.update(await vault_artifact_counts(conn, timeout=5))
                except Exception as exc:
                    vault = {
                        "available": None,
                        "writable": None,
                        "mode": "error",
                        "counts_error": exc.__class__.__name__,
                    }
            if include_sources:
                _DASHBOARD_HEALTH_SOURCES_TIMEOUT_SECONDS = _p("_DASHBOARD_HEALTH_SOURCES_TIMEOUT_SECONDS", 10.0)
                _DASHBOARD_HEALTH_BROWSER_TIMEOUT_SECONDS = _p("_DASHBOARD_HEALTH_BROWSER_TIMEOUT_SECONDS", 10.0)
                try:
                    from src.core.source_freshness import compute_liveness
                    sources = await asyncio.wait_for(
                        compute_liveness(conn),
                        timeout=_DASHBOARD_HEALTH_SOURCES_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError as exc:
                    logger.debug("source liveness health section cancelled: %s", exc.__class__.__name__)
                    cached_matrix = _load_source_matrix_payload_cache() if _load_source_matrix_payload_cache else None
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
                    cached_matrix = _load_source_matrix_payload_cache() if _load_source_matrix_payload_cache else None
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
                        _bx_payload(conn),
                        timeout=_DASHBOARD_HEALTH_BROWSER_TIMEOUT_SECONDS,
                    )
                    _bx_suppress(browser_extension)
                except asyncio.CancelledError as exc:
                    logger.debug("browser extension health section cancelled: %s", exc.__class__.__name__)
                    browser_extension = _bx_fallback(exc.__class__.__name__)
                    if not browser_extension.get("maintenance_ingest_fallback"):
                        health_section_errors.append({
                            "source": "browser_extension",
                            "status": "unknown",
                            "message": f"browser extension diagnostics unavailable: {exc.__class__.__name__}",
                        })
                except Exception as exc:
                    logger.debug("browser extension health section failed: %s", exc)
                    browser_extension = _bx_fallback(exc.__class__.__name__)
                    if not browser_extension.get("maintenance_ingest_fallback"):
                        health_section_errors.append({
                            "source": "browser_extension",
                            "status": "unknown",
                            "message": f"browser extension diagnostics unavailable: {exc.__class__.__name__}",
                        })
        finally:
            await _release(pool, conn, "health")
        db_status = "healthy"
        db_health_status = "ok"
    except TimeoutError:
        db_status = "error: timeout"
        db_health_status = "error"
    except Exception as e:
        db_status = f"error: {e}"
        db_health_status = "error"

    if include_sources and sources and whatsapp_bridge_health is None and _with_bridge_overrides is not None:
        sources, whatsapp_bridge_health = await _with_bridge_overrides(sources)

    drive_ok = True
    vault_ok = True
    backups_ok = True
    if include_storage:
        from src.core.drive_check import check_drive
        drive_ok = check_drive()
        try:
            backups = _normalize_backup_fn(backup_status(), include_storage=True)
        except Exception as exc:
            backups = _normalize_backup_fn(
                {"status": "error", "error": exc.__class__.__name__},
                include_storage=True,
            )
        vault["status"] = _vault_health_fn(vault, include_storage=True)
        vault_ok = vault["status"] == "ok"
        backups_ok = backups.get("status") in {"backup_ok", "backup_running", "backup_disabled"}
    else:
        backups = _normalize_backup_fn(backups, include_storage=False)
        vault["status"] = _vault_health_fn(vault, include_storage=False)
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
    drive_status = _drive_health_fn(drive_ok, include_storage=include_storage)
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


@router.get("/metrics")
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
        _vault_payload_fn = _p("_vault_payload", _vault_payload)
        vault = _vault_payload_fn()
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

            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS n FROM media_items GROUP BY source"
            )
            first = True
            for r in rows:
                emit("uc_media_items_total", r["n"],
                     "Total media items collected per source" if first else "",
                     "counter", labels=f'source="{r["source"]}"')
                first = False

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
                pass

            try:
                n = await conn.fetchval(
                    "SELECT COUNT(*) FROM telegram_spider_queue WHERE status = 'pending'"
                )
                emit("uc_spider_queue_pending", n or 0, "", "gauge",
                     labels='source="telegram"')
            except Exception:
                pass

            dlq = await conn.fetchval("SELECT COUNT(*) FROM dead_letter_queue")
            emit("uc_dlq_total", dlq or 0, "Dead-letter-queue entries", "gauge")

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

            age = await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (NOW() - last_processed_at))::int "
                "FROM service_cursors WHERE service = '_worker'"
            )
            emit("uc_worker_health_age_seconds", age if age is not None else -1,
                 "Seconds since the worker last reported health (-1 = never)", "gauge")

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
                pass

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

    return PlainTextResponse("\n".join(lines) + "\n")


@router.websocket("/ws/health")
async def ws_health(ws):
    await health_ws(ws)
