import asyncio
import html
import io
import json
import logging
import os
import time
import traceback
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from src.core.vault import vault_artifact_counts, vault_health

logger = logging.getLogger(__name__)
_MESSAGING_COVERAGE_CACHE: dict[str, object] = {"ts": 0.0, "rows": None}


def _encode_polyline(points, precision: int = 5) -> str:
    """Encode GPS points into a compact map thumbnail polyline."""
    if not points:
        return ""
    max_points = 600
    if len(points) > max_points:
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points)]
    factor = 10 ** precision

    def _enc(v: int) -> str:
        v = v << 1 if v >= 0 else ~(v << 1)
        out = []
        while v >= 0x20:
            out.append(chr((0x20 | (v & 0x1F)) + 63))
            v >>= 5
        out.append(chr(v + 63))
        return "".join(out)

    prev_lat = prev_lng = 0
    result = []
    for pt in points:
        if not pt or len(pt) != 2:
            continue
        lat_i = int(round(float(pt[0]) * factor))
        lng_i = int(round(float(pt[1]) * factor))
        result.append(_enc(lat_i - prev_lat))
        result.append(_enc(lng_i - prev_lng))
        prev_lat, prev_lng = lat_i, lng_i
    return "".join(result)


def _jsonb_points(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return value if isinstance(value, list) else []


def _strava_route_status(row: dict) -> dict[str, str | None]:
    """Explain why a Strava activity does or does not have a renderable route."""
    if row.get("summary_polyline"):
        return {"route_status": "mapped", "route_status_detail": "GPS route available"}

    stream_status = row.get("stream_status")
    if stream_status == "truncated_empty":
        return {
            "route_status": "privacy_zone",
            "route_status_detail": "Strava returned an empty GPS stream, usually because the activity route is hidden.",
        }
    if stream_status == "incomplete":
        return {
            "route_status": "no_gps",
            "route_status_detail": "Strava returned the activity without GPS stream points.",
        }
    if stream_status == "ok_unverifiable":
        return {
            "route_status": "unverifiable",
            "route_status_detail": "Older activity could not be rechecked, so the collector will not keep retrying it.",
        }

    cooldown_until = row.get("gps_rate_limit_until")
    if isinstance(cooldown_until, datetime):
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        if cooldown_until > datetime.now(timezone.utc):
            reason = row.get("gps_rate_limit_reason") or "GPS stream fetch hit HTTP 429"
            return {
                "route_status": "rate_limited",
                "route_status_detail": f"{reason}; retry after {cooldown_until.isoformat()}",
            }

    if row.get("start_latlng"):
        return {
            "route_status": "start_only",
            "route_status_detail": "Collector has a start coordinate, but no route line yet.",
        }
    if row.get("gps_rate_limit_at"):
        return {
            "route_status": "recent_429",
            "route_status_detail": row.get("gps_rate_limit_reason") or "Recent GPS stream request hit HTTP 429.",
        }
    return {
        "route_status": "queued",
        "route_status_detail": "GPS stream has not reached a definitive result yet and remains eligible for backfill.",
    }


async def _estimated_table_rows(conn, table: str) -> int:
    value = await conn.fetchval(
        "SELECT GREATEST(0, reltuples)::bigint FROM pg_class WHERE oid = $1::regclass",
        table,
    )
    return int(value or 0)


def _vault_payload() -> dict:
    health = vault_health()
    return {
        "root": str(health.root),
        "available": health.available,
        "writable": health.writable,
        "free_bytes": health.free_bytes,
        "total_bytes": health.total_bytes,
        "error": health.error,
        "sidecar_failures": 0,
        "artifacts_queued": 0,
        "artifacts_partial": 0,
    }

app = FastAPI(title="UnifiedCollector Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8700"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard" / "frontend" / "dist"

JWT_SECRET = os.getenv("DASHBOARD_JWT_SECRET", "")
if not JWT_SECRET or JWT_SECRET == "changeme-in-production":
    # Fail closed: never allow the dashboard to run with a known/empty signing key.
    raise RuntimeError(
        "DASHBOARD_JWT_SECRET env var is not set (or still default). "
        "Generate one: python -c 'import secrets;print(secrets.token_urlsafe(48))'"
    )
JWT_EXPIRY_HOURS = int(os.getenv("DASHBOARD_JWT_EXPIRY_HOURS", "8"))
ADMIN_USERNAME = os.getenv("DASHBOARD_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")

security = HTTPBearer(auto_error=False)

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

# Localhost convenience: when DASHBOARD_AUTH_DISABLED is truthy, every request is
# treated as an authenticated admin. Intended for single-user localhost-only
# deployments where prompting for a bearer token is pure friction. Leave UNSET
# (or false) for any network-exposed deployment -- the JWT flow stays fully intact.
_AUTH_DISABLED = os.getenv("DASHBOARD_AUTH_DISABLED", "").lower() in ("1", "true", "yes", "on")


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


async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if _AUTH_DISABLED:
        return {"username": "localhost", "role": "admin"}
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    username = payload.get("sub")
    role = payload.get("role", "viewer")
    if not username or role not in _ROLE_RANK:
        raise HTTPException(status_code=401, detail="Malformed token")
    return {"username": username, "role": role}


def require_role(min_role: str):
    if min_role not in _ROLE_RANK:
        raise ValueError(f"Unknown role: {min_role}")
    threshold = _ROLE_RANK[min_role]

    async def check(user: dict = Depends(get_current_user)):
        if _ROLE_RANK.get(user["role"], -1) < threshold:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user
    return check


@app.get("/health")
async def health():
    pool = await get_pool()
    vault = _vault_payload()
    backups = backup_status()
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            try:
                vault.update(await vault_artifact_counts(conn, timeout=3))
            except Exception as exc:
                vault["counts_error"] = exc.__class__.__name__
        db_status = "healthy"
    except Exception as e:
        db_status = f"error: {e}"

    from src.core.drive_check import check_drive
    drive_ok = check_drive()
    vault_ok = (
        vault.get("available") is True
        and vault.get("writable") is True
        and int(vault.get("artifacts_queued") or 0) == 0
        and int(vault.get("artifacts_partial") or 0) == 0
    )
    backups_ok = backups.get("status") == "ok"

    return {
        "status": "ok" if db_status == "healthy" and drive_ok and vault_ok and backups_ok else "degraded",
        "database": db_status,
        "drive": "mounted" if drive_ok else "missing",
        "vault": vault,
        "backups": backups,
    }


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


@app.get("/api/backfill-equilibrium")
async def backfill_equilibrium(_user: dict = Depends(require_role("viewer"))):
    """Read-only backfill drain state for the dashboard.

    Queue tables tell us how far each platform is from historical equilibrium:
    pending/processing rows are backlog; terminal rows are drained work. Beeper
    and Matrix expose explicit backfill-state tables, so include those too.
    """
    pool = await get_pool()
    generated_at = datetime.now(timezone.utc).isoformat()

    queue_tables = {
        "github": "github_spider_queue",
        "instagram": "instagram_spider_queue",
        "lemon8": "lemon8_spider_queue",
        "strava": "strava_spider_queue",
        "telegram": "telegram_spider_queue",
        "tiktok": "tiktok_spider_queue",
        "youtube": "youtube_spider_queue",
    }
    terminal_statuses = {"completed", "done", "failed", "unresolvable"}
    completed_statuses = {"completed", "done"}
    running_statuses = {"pending", "processing", "in_progress"}

    def pct(num: int, den: int) -> float | None:
        return None if not den else round((num / den) * 100.0, 2)

    async def table_exists(conn, name: str) -> bool:
        return bool(await conn.fetchval("SELECT to_regclass($1)", f"public.{name}"))

    async def status_counts(conn, table: str, platform: str) -> dict:
        if not await table_exists(conn, table):
            return {"platform": platform, "queue_table": table, "missing_table": True}
        approximate = False
        row_estimate = int(await conn.fetchval(
            "SELECT COALESCE(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass($1)",
            f"public.{table}",
        ) or 0)
        if row_estimate > 500_000:
            # Large queues are usually dominated by pending rows. Exact pending
            # counts seq-scan the table and can hang the dashboard. Count the
            # small non-pending buckets exactly via the status index and infer an
            # estimated pending/total from pg_class.reltuples.
            counts = {}
            for status in ("completed", "done", "failed", "unresolvable", "processing", "in_progress"):
                n = await conn.fetchval(f"SELECT COUNT(*)::int FROM {table} WHERE status = $1", status)
                if n:
                    counts[status] = int(n)
            counts["pending"] = max(row_estimate - sum(counts.values()), 0)
            approximate = True
        else:
            rows = await conn.fetch(f"SELECT status, COUNT(*)::int AS n FROM {table} GROUP BY status")
            counts = {str(r["status"] or "unknown"): int(r["n"] or 0) for r in rows}
        total = sum(counts.values())
        queue_depth = sum(counts.get(s, 0) for s in running_statuses)
        completed = sum(counts.get(s, 0) for s in completed_statuses)
        terminal = sum(counts.get(s, 0) for s in terminal_statuses)
        failed = counts.get("failed", 0) + counts.get("unresolvable", 0)
        return {
            "platform": platform,
            "queue_table": table,
            "status_counts": counts,
            "total": total,
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0) + counts.get("in_progress", 0),
            "queue_depth": queue_depth,
            "completed": completed,
            "failed": failed,
            "terminal": terminal,
            "completed_pct": pct(completed, total),
            "terminal_pct": pct(terminal, total),
            "backfill_complete_pct": pct(terminal, total),
            "approximate": approximate,
        }

    async def target_counts(conn) -> dict[str, dict]:
        if not await table_exists(conn, "collection_targets"):
            return {}
        rows = await conn.fetch(
            "SELECT source, status, COUNT(*)::int AS n "
            "FROM collection_targets GROUP BY source, status"
        )
        out: dict[str, dict] = {}
        for r in rows:
            src = str(r["source"])
            out.setdefault(src, {"target_total": 0, "target_status_counts": {}})
            out[src]["target_status_counts"][str(r["status"] or "unknown")] = int(r["n"] or 0)
            out[src]["target_total"] += int(r["n"] or 0)
        return out

    platforms: dict[str, dict] = {}
    async with pool.acquire() as conn:
        for platform, table in queue_tables.items():
            platforms[platform] = await status_counts(conn, table, platform)

        if await table_exists(conn, "spider_queue"):
            rows = await conn.fetch(
                "SELECT platform, status, COUNT(*)::int AS n "
                "FROM spider_queue GROUP BY platform, status"
            )
            generic: dict[str, dict[str, int]] = {}
            for r in rows:
                generic.setdefault(str(r["platform"]), {})[str(r["status"])] = int(r["n"] or 0)
            for platform, counts in generic.items():
                total = sum(counts.values())
                queue_depth = sum(counts.get(s, 0) for s in running_statuses)
                completed = sum(counts.get(s, 0) for s in completed_statuses)
                terminal = sum(counts.get(s, 0) for s in terminal_statuses)
                platforms.setdefault(platform, {"platform": platform})["generic_spider_queue"] = {
                    "status_counts": counts,
                    "total": total,
                    "queue_depth": queue_depth,
                    "completed": completed,
                    "terminal": terminal,
                    "completed_pct": pct(completed, total),
                    "terminal_pct": pct(terminal, total),
                }

        if await table_exists(conn, "beeper_shadow_sync_state"):
            row = await conn.fetchrow(
                "SELECT COUNT(*)::int AS total, "
                "(COUNT(*) FILTER (WHERE backfill_complete))::int AS complete "
                "FROM beeper_shadow_sync_state"
            )
            total = int(row["total"] or 0)
            complete = int(row["complete"] or 0)
            platforms["beeper"] = {
                "platform": "beeper",
                "queue_table": "beeper_shadow_sync_state",
                "total": total,
                "completed": complete,
                "queue_depth": max(total - complete, 0),
                "backfill_complete": complete,
                "backfill_running": max(total - complete, 0),
                "backfill_complete_pct": pct(complete, total),
            }
        if await table_exists(conn, "matrix_backfill_state"):
            row = await conn.fetchrow(
                "SELECT COUNT(*)::int AS total, "
                "(COUNT(*) FILTER (WHERE done))::int AS complete "
                "FROM matrix_backfill_state"
            )
            total = int(row["total"] or 0)
            complete = int(row["complete"] or 0)
            platforms["matrix"] = {
                "platform": "matrix",
                "queue_table": "matrix_backfill_state",
                "total": total,
                "completed": complete,
                "queue_depth": max(total - complete, 0),
                "backfill_complete": complete,
                "backfill_running": max(total - complete, 0),
                "backfill_complete_pct": pct(complete, total),
            }

        for platform, target in (await target_counts(conn)).items():
            platforms.setdefault(platform, {"platform": platform}).update(target)

    rows = sorted(platforms.values(), key=lambda r: r.get("platform", ""))
    totals = {
        "platforms": len(rows),
        "queue_depth": sum(int(r.get("queue_depth") or 0) for r in rows),
        "completed": sum(int(r.get("completed") or 0) for r in rows),
        "total": sum(int(r.get("total") or 0) for r in rows),
    }
    totals["backfill_complete_pct"] = pct(totals["completed"], totals["total"])
    return {"generated_at": generated_at, "totals": totals, "platforms": rows}


@app.get("/collectors")
async def list_collectors(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service, last_processed_id, last_processed_at, status "
            "FROM service_cursors ORDER BY service"
        )
    return [dict(r) for r in rows]


@app.get("/collectors/live")
async def collectors_live(_user: dict = Depends(require_role("viewer"))):
    """REAL per-source liveness from data freshness + source_health.

    service_cursors.status (used by /collectors) flips to 'idle' between cycles for
    healthy long-sleep collectors and is 'never' for realtime feeds, so counting
    status=='running' made healthy collectors look down (the "9/12" confusion). This
    reports true live/stale/degraded/dead per source from the actual data tables.
    """
    from src.core.source_freshness import compute_liveness
    pool = await get_pool()
    async with pool.acquire() as conn:
        sources = await compute_liveness(conn)
    live = sum(1 for s in sources if s["status"] == "live")
    return {"total": len(sources), "live": live, "sources": sources}


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
    "github": ("SELECT max(collected_at) FROM github_commits", "github commits"),
    "strava": ("SELECT max(collected_at) FROM strava_activities", "strava activities"),
    "search": ("SELECT max(collected_at) FROM search_results", "search results"),
}


@app.get("/platform/{name}/summary")
async def platform_summary(name: str, _user: dict = Depends(require_role("viewer"))):
    """Everything collected for ONE platform: recent media (what was just scraped),
    counts (media/users/posts/messages), per-account follow graph, and live status.
    Powers the per-platform dashboard sections."""
    name = (name or "").lower()
    pool = await get_pool()
    out: dict = {"platform": name}
    async with pool.acquire() as conn:
        if name == "discord":
            out["source_mode"] = "beeper shadow"
            try:
                media_row = await conn.fetchrow(
                    """
                    SELECT count(*) AS media_count,
                           max(collected_at) AS media_last
                    FROM media_items
                    WHERE source = 'beeper'
                      AND filename LIKE 'beeper_discord_%'
                    """,
                    timeout=8,
                )
                out["media_count"] = int((media_row and media_row["media_count"]) or 0)
                out["media_last"] = media_row["media_last"] if media_row else None
                out["media_recent"] = [dict(r) for r in await conn.fetch(
                    """
                    SELECT id, entity_name, content_type, filename, collected_at
                    FROM media_items
                    WHERE source = 'beeper'
                      AND filename LIKE 'beeper_discord_%'
                    ORDER BY collected_at DESC
                    LIMIT 24
                    """,
                    timeout=8,
                )]
            except Exception:
                out["media_count"] = 0
                out["media_last"] = None
                out["media_recent"] = []
            try:
                out["posts_count"] = int(await conn.fetchval(
                    "SELECT count(*) FROM beeper_shadow_chats WHERE network = 'Discord'",
                    timeout=6,
                ) or 0)
                out["posts_label"] = "chats"
                msg_row = await conn.fetchrow(
                    """
                    SELECT count(*) AS messages,
                           (
                             SELECT "timestamp"
                             FROM beeper_shadow_messages
                             WHERE network = 'Discord'
                               AND "timestamp" IS NOT NULL
                             ORDER BY "timestamp" DESC
                             LIMIT 1
                           ) AS messages_last
                    FROM beeper_shadow_messages
                    WHERE network = 'Discord'
                      AND message_id IS NOT NULL
                    """,
                    timeout=12,
                )
                out["messages_count"] = int((msg_row and msg_row["messages"]) or 0)
                out["messages_last"] = msg_row["messages_last"] if msg_row else None
                out["users_count"] = int(await conn.fetchval(
                    """
                    SELECT count(DISTINCT NULLIF(sender_id, ''))
                    FROM beeper_shadow_messages
                    WHERE network = 'Discord'
                      AND sender_id IS NOT NULL
                      AND sender_id <> ''
                    """,
                    timeout=20,
                ) or 0)
            except Exception:
                out["users_count"] = 0
                out["posts_count"] = 0
                out["posts_label"] = "chats"
                out["messages_count"] = 0
            out["follow_edges"] = []
            try:
                from src.core.source_freshness import compute_liveness
                live = {s["source"]: s for s in await compute_liveness(conn)}
                b = live.get("beeper")
                if b:
                    out["live"] = b["status"]
                    out["age_seconds"] = b["age_seconds"]
                    out["stale_after_seconds"] = b.get("stale_after_seconds")
            except Exception:
                pass
            return out

        try:
            out["media_count"] = int(await conn.fetchval(
                "SELECT count(*) FROM media_items WHERE source=$1", name, timeout=8) or 0)
            out["media_last"] = await conn.fetchval(
                "SELECT max(collected_at) FROM media_items WHERE source=$1", name, timeout=8)
            out["media_recent"] = [dict(r) for r in await conn.fetch(
                "SELECT id, entity_name, content_type, filename, collected_at "
                "FROM media_items WHERE source=$1 ORDER BY collected_at DESC LIMIT 24", name, timeout=8)]
        except Exception:
            out.setdefault("media_count", 0)
            out.setdefault("media_recent", [])
        query_spec = _LATEST_ACTIVITY_QUERIES.get(name)
        if query_spec:
            query, basis = query_spec
            try:
                out["last_activity"] = await conn.fetchval(query, timeout=8)
                out["activity_basis"] = basis
            except Exception:
                out["last_activity"] = out.get("media_last")
                out["activity_basis"] = "media"
        try:
            if name == "whatsapp":
                out["users_count"] = int(await conn.fetchval(
                    "SELECT count(*) FROM whatsapp_users", timeout=6) or 0)
            elif name == "telegram":
                row = await conn.fetchrow(
                    """
                    SELECT count(*) FILTER (WHERE COALESCE(is_bot, false) = false) AS people,
                           count(*) FILTER (WHERE is_bot = true) AS bots
                    FROM telegram_users
                    """,
                    timeout=6,
                )
                out["users_count"] = int((row and row["people"]) or 0)
                out["bots_count"] = int((row and row["bots"]) or 0)
            elif name == "beeper":
                out["users_count"] = int(await conn.fetchval(
                    "SELECT count(DISTINCT NULLIF(sender_id, '')) FROM beeper_shadow_messages",
                    timeout=6,
                ) or 0)
            else:
                out["users_count"] = int(await conn.fetchval(
                    "SELECT count(*) FROM social_users WHERE platform=$1", name, timeout=6) or 0)
        except Exception:
            out["users_count"] = 0
        # Whole-table counts via the planner estimate (instant) — count(*) on
        # 747k-row telegram_messages timed the endpoint out.
        pt = _PLATFORM_POSTS.get(name)
        if pt:
            try:
                out["posts_count"] = int(await conn.fetchval(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname=$1", pt) or 0)
                out["posts_label"] = pt.replace("_", " ")
            except Exception:
                pass
        mt = _PLATFORM_MESSAGES.get(name)
        if mt:
            tbl, col = mt
            try:
                out["messages_count"] = int(await conn.fetchval(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname=$1", tbl) or 0)
                out["messages_last"] = await conn.fetchval(f"SELECT max({col}) FROM {tbl}", timeout=6)
            except Exception:
                pass
        try:
            out["follow_edges"] = [dict(r) for r in await conn.fetch(
                "SELECT owner_account, count(*) FILTER (WHERE direction='follower') AS followers, "
                "count(*) FILTER (WHERE direction='following') AS following "
                "FROM follow_edges WHERE platform=$1 GROUP BY owner_account", name)]
        except Exception:
            out["follow_edges"] = []
        # Live status: canonical per-source data freshness, not source_health's
        # coarse running/idle flag.
        try:
            from src.core.source_freshness import compute_liveness
            live = {s["source"]: s for s in await compute_liveness(conn)}
            cur = live.get(name)
            if cur:
                out["live"] = cur["status"]
                out["age_seconds"] = cur["age_seconds"]
                out["stale_after_seconds"] = cur.get("stale_after_seconds")
        except Exception:
            pass
    return out


@app.get("/social/follow-edges/stats")
async def follow_edges_stats(_user: dict = Depends(require_role("viewer"))):
    """Per-account follow graph (from follow_edges): how many followers/following
    were captured for EACH of your owned accounts, distinctly. Populated by the
    extension self-seed (per account, as you rotate logins) + the headless path."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                SELECT platform, owner_account,
                       count(*) FILTER (WHERE direction = 'follower')  AS followers,
                       count(*) FILTER (WHERE direction = 'following') AS following,
                       max(last_seen) AS last_seen
                FROM follow_edges
                GROUP BY platform, owner_account
                ORDER BY platform, owner_account
                """
            )
        except Exception:
            rows = []
    return [dict(r) for r in rows]


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


@app.get("/accounts")
async def accounts_overview(_user: dict = Depends(require_role("viewer"))):
    """Unified cross-platform account/session state — telegram accounts, whatsapp
    bridge devices, and cookie-auth sources — with health, so one panel covers all
    platforms instead of a telegram-only view.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            tg = [dict(r) for r in await conn.fetch(
                "SELECT name, phone, status, last_connected_at, last_error "
                "FROM telegram_user_accounts ORDER BY name")]
        except Exception:
            tg = []
        health = {}
        try:
            for r in await conn.fetch("SELECT source, status, last_success_at, last_error FROM source_health"):
                health[r["source"]] = dict(r)
        except Exception:
            pass

    # WhatsApp bridges (health) — concurrent, off the event loop.
    async def _wa_health(bridge: str) -> dict:
        base = _wa_bridge_base(bridge)
        import urllib.request

        def _do():
            with urllib.request.urlopen(f"{base}/health", timeout=6) as r:
                return __import__("json").loads(r.read().decode())
        try:
            h = await asyncio.to_thread(_do)
            return {"session": bridge, "ready": bool(h.get("whatsapp_ready")),
                    "status": "connected" if h.get("whatsapp_ready") else "awaiting_scan"}
        except Exception as exc:  # noqa: BLE001
            return {"session": bridge, "ready": False, "status": "unreachable", "error": str(exc)}

    wa = await asyncio.gather(_wa_health("1"), _wa_health("2"))

    # Persisted per-account validity (collector-tested each cycle).
    async with pool.acquire() as conn:
        try:
            cs_rows = {
                (r["platform"], r["account"]): (r["status"], r["reason"])
                for r in await conn.fetch("SELECT platform, account, status, reason FROM cookie_status")
            }
        except Exception:
            cs_rows = {}

    stale_days = int(os.getenv("COOKIE_STALE_DAYS", "30"))
    cred_dir = Path(os.getenv("COLLECTOR_CREDENTIALS_DIR", "/app/credentials"))
    cookies = []
    for src, keys in _COOKIE_SOURCES.items():
        base = cred_dir / src
        try:
            files = sorted(p for p in base.iterdir()
                           if p.suffix == ".txt" and p.name.lower() != "readme.txt")
        except Exception:
            files = []
        for p in files:
            a = _audit_cookie_file(p, keys)
            if not a:
                continue
            acct = p.stem
            if acct.startswith(src + "_"):
                acct = acct[len(src) + 1:]
            live_status, live_reason = cs_rows.get((src, acct), (None, None))
            # needs-refresh reason: persisted 'dead' (401) wins, then file signals.
            reason = None
            if live_status == "dead":
                reason = live_reason or "session dead (401)"
            elif a.get("reason"):
                reason = a["reason"]
            elif a.get("age_days") is not None and a["age_days"] > stale_days:
                reason = f"stale ({a['age_days']:.0f}d)"
            cookies.append({
                "source": src, "account": acct, "file": a["file"],
                "age_days": a["age_days"], "expiry_days": a.get("expiry_days"),
                "has_session": a["has_session"], "live_status": live_status,
                "needs_refresh": reason is not None, "reason": reason,
                "health": (health.get(src, {}) or {}).get("status", "unknown"),
            })

    return {
        "telegram": tg,
        "whatsapp": list(wa),
        "cookies": cookies,
        "health": {k: v.get("status") for k, v in health.items()},
    }


@app.get("/media")
async def list_media(source: str | None = None, limit: int = 50,
                     _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT id, source, entity_name, content_type, filename, file_size, collected_at "
                "FROM media_items WHERE source = $1 ORDER BY collected_at DESC LIMIT $2",
                source, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, source, entity_name, content_type, filename, file_size, collected_at "
                "FROM media_items ORDER BY collected_at DESC LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


@app.get("/media/stats")
async def media_stats(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source,
                   COUNT(*) AS total_items,
                   COALESCE(SUM(file_size), 0) AS total_bytes,
                   MAX(collected_at) AS last_collected
            FROM media_items
            GROUP BY source
            ORDER BY source
            """
        )
        try:
            from src.core.source_freshness import compute_liveness
            live = {s["source"]: s for s in await compute_liveness(conn)}
        except Exception:
            live = {}
        out = []
        now = datetime.now(timezone.utc)
        for r in rows:
            d = dict(r)
            query_spec = _LATEST_ACTIVITY_QUERIES.get(d["source"])
            cur = live.get(d["source"])
            if cur:
                d["live"] = cur["status"]
                d["age_seconds"] = cur["age_seconds"]
                d["stale_after_seconds"] = cur.get("stale_after_seconds")
                if cur["age_seconds"] is not None:
                    d["last_activity"] = now - timedelta(seconds=cur["age_seconds"])
                else:
                    d["last_activity"] = d.get("last_collected")
            else:
                d["last_activity"] = d.get("last_collected")
            d["activity_basis"] = query_spec[1] if query_spec else "media"
            out.append(d)
    return out


@app.get("/ingestion/hourly")
async def hourly_ingestion(hours: int = 12, _user: dict = Depends(require_role("viewer"))):
    """Hour-by-hour real ingestion from source tables, not collection_runs.

    collection_runs records scheduler trigger/rearm events. This endpoint reads
    the actual rows operators care about: posts/messages/activities/etc, media
    files, and rate-limit events.
    """
    hours = max(1, min(hours, 72))
    pool = await get_pool()
    content_parts = [
        ("telegram", "telegram_messages", "collected_at", "messages"),
        ("whatsapp", "whatsapp_messages", "collected_at", "messages"),
        ("beeper", "beeper_shadow_messages", "ingested_at", "messages"),
        ("instagram", "instagram_posts", "collected_at", "posts"),
        ("tiktok", "tiktok_posts", "collected_at", "posts"),
        ("lemon8", "lemon8_posts", "collected_at", "posts"),
        ("threads", "threads_posts", "collected_at", "posts"),
        ("facebook", "facebook_posts", "collected_at", "posts"),
        ("x", "x_posts", "collected_at", "posts"),
        ("youtube", "youtube_videos", "collected_at", "videos"),
        ("github", "github_commits", "collected_at", "commits"),
        ("website", "website_pages", "collected_at", "pages"),
        ("strava", "strava_activities", "collected_at", "activities"),
        ("search", "search_results", "collected_at", "results"),
    ]
    required_tables = [table for _source, table, _column, _label in content_parts]
    required_tables.extend(["media_items", "rate_limit_events"])
    async with pool.acquire() as conn:
        existing_tables = set(await conn.fetchval(
            """
            SELECT COALESCE(array_agg(table_name), ARRAY[]::text[])
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY($1::text[])
            """,
            required_tables,
            timeout=8,
        ))
    raw_parts = [
        f"""
        SELECT '{source}'::text AS source,
               date_trunc('hour', {column}) AS hour,
               count(*)::bigint AS records,
               0::bigint AS media_items,
               {("count(*)" if label == "messages" else "0")}::bigint AS messages,
               0::bigint AS rate_limits,
               0::bigint AS access_errors
        FROM {table}
        WHERE {column} >= now() - ($1 || ' hours')::interval
        GROUP BY date_trunc('hour', {column})
        """
        for source, table, column, label in content_parts
        if table in existing_tables
    ]
    if "media_items" in existing_tables:
        raw_parts.append(
            """
            SELECT source,
                   date_trunc('hour', collected_at) AS hour,
                   0::bigint AS records,
                   count(*)::bigint AS media_items,
                   0::bigint AS messages,
                   0::bigint AS rate_limits,
                   0::bigint AS access_errors
            FROM media_items
            WHERE collected_at >= now() - ($1 || ' hours')::interval
            GROUP BY source, date_trunc('hour', collected_at)
            """
        )
    if "rate_limit_events" in existing_tables:
        raw_parts.append(
            """
            SELECT source,
                   date_trunc('hour', created_at) AS hour,
                   0::bigint AS records,
                   0::bigint AS media_items,
                   0::bigint AS messages,
                   count(*) FILTER (WHERE status_code = 429)::bigint AS rate_limits,
                   count(*) FILTER (WHERE status_code IS DISTINCT FROM 429)::bigint AS access_errors
            FROM rate_limit_events
            WHERE created_at >= now() - ($1 || ' hours')::interval
            GROUP BY source, date_trunc('hour', created_at)
            """
        )
    if not raw_parts:
        return []
    sql = f"""
        WITH raw AS (
            {" UNION ALL ".join(raw_parts)}
        )
        SELECT source, hour,
               sum(records)::bigint AS records,
               sum(media_items)::bigint AS media_items,
               sum(messages)::bigint AS messages,
               sum(rate_limits)::bigint AS rate_limits,
               sum(access_errors)::bigint AS access_errors
        FROM raw
        GROUP BY source, hour
        ORDER BY hour DESC, source
    """
    labels = {source: label for source, _table, _column, label in content_parts}
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, str(hours), timeout=30)
    out = []
    for row in rows:
        d = dict(row)
        d["record_label"] = labels.get(d["source"], "records")
        out.append(d)
    return out


@app.get("/rate-limits/recent")
async def recent_rate_limits(hours: int = 24, limit: int = 100,
                             _user: dict = Depends(require_role("viewer"))):
    hours = max(1, min(hours, 168))
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_exists = bool(await conn.fetchval("SELECT to_regclass('public.rate_limit_events') IS NOT NULL", timeout=8))
        if table_exists:
            events = [dict(r) for r in await conn.fetch(
                """
                SELECT id, source, account, scope, status_code, cooldown_seconds,
                       reason, metadata, created_at
                FROM rate_limit_events
                WHERE created_at >= now() - ($1 || ' hours')::interval
                ORDER BY created_at DESC
                LIMIT $2
                """,
                str(hours), limit,
                timeout=15,
            )]
            for event in events:
                if isinstance(event.get("metadata"), str):
                    try:
                        event["metadata"] = json.loads(event["metadata"])
                    except Exception:
                        event["metadata"] = {}
            recent_summary = [dict(r) for r in await conn.fetch(
                """
                SELECT source, account, scope,
                       (array_agg(status_code ORDER BY created_at DESC))[1]::int AS status_code,
                       count(*)::int AS count,
                       (array_agg(cooldown_seconds ORDER BY created_at DESC))[1]::int AS cooldown_seconds,
                       (array_agg(reason ORDER BY created_at DESC))[1] AS reason,
                       min(created_at) AS first_seen_at,
                       max(created_at) AS last_seen_at,
                       max(created_at + COALESCE(cooldown_seconds, 0) * interval '1 second') AS active_until
                FROM rate_limit_events
                WHERE created_at >= now() - ($1 || ' hours')::interval
                GROUP BY source, account, scope
                ORDER BY last_seen_at DESC
                LIMIT 24
                """,
                str(hours),
                timeout=15,
            )]
            now_utc = datetime.now(timezone.utc)
            for row in recent_summary:
                active_until = row.get("active_until")
                row["active_now"] = bool(active_until and active_until > now_utc)
        else:
            events = []
            recent_summary = []
        active = []
        try:
            for r in await conn.fetch(
                """
                SELECT service, last_processed_id, last_processed_at, status
                FROM service_cursors
                WHERE service ILIKE '%rate_limit'
                   OR service ILIKE '%ratelimit'
                ORDER BY last_processed_at DESC NULLS LAST
                """,
                timeout=8,
            ):
                d = dict(r)
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
                d["active_now"] = bool(expiry and expiry > datetime.now(timezone.utc))
                active.append(d)
        except Exception:
            active = []
    return {"events": events, "active": active, "recent_summary": recent_summary}


# ---------------------------------------------------------------------------
# Social registry browser (social_users + post/comment tables)
# ---------------------------------------------------------------------------
@app.get("/social/stats")
async def social_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT platform, count(*) AS users, count(*) FILTER (WHERE profile_photo_url IS NOT NULL) AS with_photo FROM social_users GROUP BY platform ORDER BY 2 DESC"
        )
        content = {}
        for label, q in (
            ("instagram_posts", "SELECT count(*) FROM instagram_posts"),
            ("instagram_comments", "SELECT count(*) FROM instagram_comments"),
            ("threads_posts", "SELECT count(*) FROM threads_posts"),
            ("facebook_posts", "SELECT count(*) FROM facebook_posts"),
        ):
            try:
                content[label] = await conn.fetchval(q)
            except Exception:
                content[label] = None
    return {"users": [dict(r) for r in users], "content": content}


@app.get("/social/network")
async def social_network(_user: dict = Depends(require_role("viewer"))):
    """Your REAL captured network per platform, from social_users contexts.

    The Targets table is a tiny manual seed list; the actual follow graph lives
    here (e.g. tens of thousands of instagram users, thousands you follow). This
    surfaces following/followers so the Targets page can show your real reach
    instead of implying "2 strava targets" is your whole network.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT platform,
                   count(*)                                          AS total,
                   count(*) FILTER (WHERE 'follow'    = ANY(contexts)) AS following,
                   count(*) FILTER (WHERE 'follower'  = ANY(contexts)) AS followers
            FROM social_users
            GROUP BY platform
            ORDER BY total DESC
            """
        )
    return [dict(r) for r in rows]


@app.get("/social/users")
async def social_users_list(platform: str | None = None, q: str | None = None,
                            limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 200))
    clauses, args = [], []
    if platform:
        args.append(platform); clauses.append(f"platform = ${len(args)}")
    if q:
        args.append(f"%{q}%"); clauses.append(f"(username ILIKE ${len(args)} OR display_name ILIKE ${len(args)})")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    args.extend([limit, offset])
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT platform, uid, username, display_name, profile_photo_url, times_seen, contexts, last_seen "
            f"FROM social_users {where} ORDER BY times_seen DESC, last_seen DESC LIMIT ${len(args)-1} OFFSET ${len(args)}",
            *args,
        )
    return [dict(r) for r in rows]


@app.get("/social", response_class=HTMLResponse)
async def social_page():
    return HTMLResponse(_SOCIAL_HTML)


_SOCIAL_HTML = """<!doctype html><html><head><meta charset=utf-8><title>Social registry</title>
<style>body{font:13px system-ui;background:#0f1117;color:#e6e8ee;margin:0;padding:16px}
h1{font-size:16px}.stats{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 16px}
.chip{background:#171a23;border:1px solid #2a2f3e;border-radius:8px;padding:8px 12px}
.chip b{color:#818cf8}input,select{background:#1e2230;color:#e6e8ee;border:1px solid #2a2f3e;border-radius:6px;padding:6px 8px;font:inherit}
table{border-collapse:collapse;width:100%;margin-top:12px}td,th{border-bottom:1px solid #2a2f3e;padding:6px 8px;text-align:left}
img{width:32px;height:32px;border-radius:50%;object-fit:cover;background:#222}.muted{color:#8b91a3}</style></head>
<body><h1>🧑‍🤝‍🧑 Social registry</h1><div class=stats id=stats></div>
<div><select id=platform><option value="">all platforms</option></select>
<input id=q placeholder="search username / name" oninput="load()"></div>
<table><thead><tr><th></th><th>platform</th><th>username</th><th>name</th><th>seen</th><th>contexts</th></tr></thead><tbody id=rows></tbody></table>
<script>
async function stats(){const s=await (await fetch('/social/stats')).json();
document.getElementById('stats').innerHTML=s.users.map(u=>`<div class=chip><b>${u.platform}</b> ${u.users} users · ${u.with_photo} 📷</div>`).join('')+
Object.entries(s.content).map(([k,v])=>`<div class=chip>${k}: <b>${v??'—'}</b></div>`).join('');
const sel=document.getElementById('platform');s.users.forEach(u=>{const o=document.createElement('option');o.value=u.platform;o.textContent=u.platform;sel.appendChild(o)});}
async function load(){const p=document.getElementById('platform').value,q=document.getElementById('q').value;
const r=await (await fetch(`/social/users?limit=100&platform=${encodeURIComponent(p)}&q=${encodeURIComponent(q)}`)).json();
document.getElementById('rows').innerHTML=r.map(u=>`<tr><td>${u.profile_photo_url?`<img src="${u.profile_photo_url}">`:''}</td>
<td>${u.platform}</td><td>${u.username||'<span class=muted>—</span>'}</td><td>${u.display_name||''}</td>
<td>${u.times_seen}</td><td class=muted>${(u.contexts||[]).join(', ')}</td></tr>`).join('');}
document.getElementById('platform').onchange=load;stats().then(load);
</script></body></html>"""


@app.get("/dlq")
async def list_dlq(source: str | None = None, limit: int = 50,
                   _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT * FROM dead_letter_queue WHERE source = $1 ORDER BY created_at DESC LIMIT $2",
                source, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM dead_letter_queue ORDER BY created_at DESC LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


class ScheduleRequest(BaseModel):
    source: str
    interval_hours: int = 24
    enabled: bool = True


class TargetRequest(BaseModel):
    source: str
    target: str
    priority: int = 0


class LoginRequest(BaseModel):
    username: str
    password: str


# ── Auth ──

@app.post("/auth/login")
async def login(req: LoginRequest):
    import secrets as _secrets
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash, role FROM dashboard_users WHERE username = $1",
            req.username,
        )

    role = None
    if row:
        try:
            stored_hash = row["password_hash"]
            if isinstance(stored_hash, str):
                stored_hash = stored_hash.encode()
            if bcrypt.checkpw(req.password.encode(), stored_hash):
                role = row["role"]
        except (ValueError, TypeError):
            role = None
    elif (
        ADMIN_PASSWORD
        and req.username == ADMIN_USERNAME
        and _secrets.compare_digest(req.password, ADMIN_PASSWORD)
    ):
        role = "admin"

    if role is None:
        # Constant-ish failure path — sleep a bit so success/fail paths are similar.
        import asyncio as _asyncio
        await _asyncio.sleep(0.25)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = jwt.encode(
        {
            "sub": req.username,
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"token": token, "username": req.username, "role": role}


@app.get("/auth/me")
async def auth_me(user: dict = Depends(require_role("viewer"))):
    return user


# ── Targets CRUD ──


async def _target_already_known(conn, source: str, target_id: str) -> dict | None:
    """Return {discovered_via, last_seen} if target already in spider data, else None."""
    queries = {
        'github': "SELECT login AS hit FROM github_users WHERE login=$1 OR platform_user_id::text=$1 LIMIT 1",
        'instagram': "SELECT username AS hit FROM instagram_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'telegram': "SELECT username AS hit FROM telegram_users WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'lemon8': "SELECT username AS hit FROM lemon8_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'strava': "SELECT platform_athlete_id::text AS hit FROM strava_athletes WHERE platform_athlete_id::text=$1 LIMIT 1",
        'tiktok': "SELECT username AS hit FROM tiktok_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'whatsapp': "SELECT platform_user_id AS hit FROM whatsapp_users WHERE platform_user_id=$1 LIMIT 1",
        'website': "SELECT domain AS hit FROM website_targets WHERE domain=$1 LIMIT 1",
    }
    q = queries.get(source)
    if not q:
        return None  # youtube, search - no profile table or no dedupe applicable
    try:
        row = await conn.fetchrow(q, target_id)
    except Exception:
        return None  # table missing - don't block
    if not row:
        return None
    try:
        parent = await conn.fetchval(
            "SELECT parent_node_id FROM spider_queue WHERE platform=$1 AND node_id=$2 AND parent_node_id IS NOT NULL LIMIT 1",
            source, target_id,
        )
    except Exception:
        parent = None
    try:
        last_seen = await conn.fetchval(
            "SELECT last_attempted_at FROM spider_queue WHERE platform=$1 AND node_id=$2 LIMIT 1",
            source, target_id,
        )
    except Exception:
        last_seen = None
    return {'discovered_via': parent, 'last_seen': last_seen.isoformat() if last_seen else None}


@app.post("/targets")
async def create_target(
    req: TargetRequest,
    force: bool = False,
    _user: dict = Depends(require_role("operator")),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not force:
            already = await _target_already_known(conn, req.source, req.target)
            if already is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        'code': 'already_discovered',
                        'source': req.source,
                        'target_id': req.target,
                        **already,
                    },
                )
        priority = req.priority + 5 if force else req.priority
        await conn.execute(
            "INSERT INTO collection_targets (source, target_id, priority) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            req.source, req.target, priority,
        )
    return {"status": "ok", "source": req.source, "target": req.target, "forced": force}


@app.delete("/targets/{target_id}")
async def delete_target(target_id: int, _user: dict = Depends(require_role("admin"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM collection_targets WHERE id = $1", target_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Target not found")
    return {"status": "deleted"}


# ── Schedules ──

@app.put("/schedules/{source}")
async def update_schedule(source: str, req: ScheduleRequest, _user: dict = Depends(require_role("operator"))):
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    next_run = now + timedelta(hours=req.interval_hours)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO collection_schedules (source, interval_hours, enabled, next_run) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (source) DO UPDATE "
            "SET interval_hours = $2, enabled = $3, next_run = $4",
            source, req.interval_hours, req.enabled, next_run,
        )
    return {"status": "ok", "source": source}


# ── Run detail ──

@app.get("/runs/{run_id}")
async def get_run(run_id: int, _user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM collection_runs WHERE id = $1", run_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return dict(row)


# ── Per-collector deep view ──

@app.get("/collectors/{source}")
async def collector_detail(source: str, _user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        cursor = await conn.fetchrow(
            "SELECT * FROM service_cursors WHERE service = $1", source,
        )
        media_count = await conn.fetchval(
            "SELECT COUNT(*) FROM media_items WHERE source = $1", source,
        )
        error_count = await conn.fetchval(
            "SELECT COUNT(*) FROM dead_letter_queue WHERE source = $1", source,
        )
        recent = await conn.fetch(
            "SELECT id, entity_name, content_type, filename, file_size, collected_at "
            "FROM media_items WHERE source = $1 ORDER BY collected_at DESC LIMIT 10",
            source,
        )
    return {
        "source": source,
        "cursor": dict(cursor) if cursor else None,
        "media_count": media_count,
        "error_count": error_count,
        "recent_items": [dict(r) for r in recent],
    }


# ── Social Graph ──

@app.get("/graph")
async def social_graph(
    source: str = Query("instagram", description="Platform source"),
    limit: int = Query(5000, description="Max edges to return", ge=1, le=50000),
    _user: dict = Depends(require_role("viewer"))
):
    # Relationship edges live in TWO tables depending on the collector:
    #   * graph_edges  (source, source_user, target_user, edge_type) — whatsapp
    #   * follow_edges (platform, owner_account, target_uid/username, direction)
    #     — instagram (and any future follow-graph collector)
    # Query the right one for the requested platform and normalise follow_edges
    # into the (source_user, target_user, edge_type) shape the frontend expects.
    pool = await get_pool()
    async with pool.acquire() as conn:
        edges = await conn.fetch(
            "SELECT source_user, target_user, edge_type FROM graph_edges WHERE source = $1 LIMIT $2",
            source, limit,
        )
        if not edges:
            edges = await conn.fetch(
                """
                SELECT owner_account AS source_user,
                       COALESCE(target_username, target_uid) AS target_user,
                       direction AS edge_type
                FROM follow_edges
                WHERE platform = $1
                LIMIT $2
                """,
                source, limit,
            )
    nodes = set()
    edge_list = []
    for e in edges:
        nodes.add(e["source_user"])
        nodes.add(e["target_user"])
        edge_list.append(dict(e))
    return {
        "nodes": [{"id": n} for n in nodes],
        "edges": edge_list,
    }


@app.get("/messaging/coverage")
async def messaging_coverage(_user: dict = Depends(require_role("viewer"))):
    """Native-vs-Beeper messaging coverage.

    Native Telegram/WhatsApp tables are canonical. Beeper is a mirror for those
    networks and the canonical source for networks with no native collector.
    """
    cache_ttl = 300
    now = time.time()
    cached = _MESSAGING_COVERAGE_CACHE.get("rows")
    if cached is not None and now - float(_MESSAGING_COVERAGE_CACHE.get("ts") or 0) < cache_ttl:
        return cached

    pool = await get_pool()
    native_networks = {
        "Telegram": {
            "native_source": "telegram",
            "canonical_source": "native",
            "policy": "native primary, Beeper mirror",
        },
        "WhatsApp": {
            "native_source": "whatsapp",
            "canonical_source": "native",
            "policy": "native primary, Beeper mirror",
        },
    }
    async with pool.acquire() as conn:
        native = {
            "telegram": {
                "messages": await _estimated_table_rows(conn, "telegram_messages"),
                "chats": await conn.fetchval("SELECT COUNT(*) FROM telegram_chats"),
                "people": await conn.fetchval(
                    "SELECT COUNT(*) FROM telegram_users WHERE NOT COALESCE(is_bot, false)"
                ),
                "last_message": await conn.fetchval(
                    "SELECT platform_created_at FROM telegram_messages "
                    "ORDER BY platform_created_at DESC NULLS LAST LIMIT 1"
                ),
            },
            "whatsapp": {
                "messages": await conn.fetchval("SELECT COUNT(*) FROM whatsapp_messages"),
                "chats": await conn.fetchval("SELECT COUNT(*) FROM whatsapp_chats"),
                "people": await conn.fetchval("SELECT COUNT(*) FROM whatsapp_users"),
                "last_message": await conn.fetchval(
                    'SELECT "timestamp" FROM whatsapp_messages ORDER BY "timestamp" DESC NULLS LAST LIMIT 1'
                ),
            },
        }
        beeper_message_rows = await conn.fetch(
            """
            WITH networks AS (
                SELECT DISTINCT network FROM beeper_shadow_chats
                UNION
                SELECT DISTINCT network FROM beeper_shadow_messages
            )
            SELECT n.network,
                   (
                       SELECT COUNT(*)::bigint
                       FROM beeper_shadow_messages m
                       WHERE m.network = n.network
                   ) AS messages,
                   (
                       SELECT MAX(timestamp)
                       FROM beeper_shadow_messages m
                       WHERE m.network = n.network
                   ) AS last_message
            FROM networks n
            ORDER BY n.network
            """,
            timeout=30,
        )
        beeper_chat_rows = await conn.fetch(
            """
            SELECT network, COUNT(*) AS chats, MAX(last_seen_at) AS last_seen
            FROM beeper_shadow_chats
            GROUP BY network
            """,
            timeout=30,
        )
        beeper_people_rows = await conn.fetch(
            """
            SELECT network, COUNT(DISTINCT participant_id)::int AS people
            FROM beeper_shadow_participants
            GROUP BY network
            """,
            timeout=30,
        )
    beeper_messages = {r["network"]: dict(r) for r in beeper_message_rows}
    beeper_chats = {r["network"]: dict(r) for r in beeper_chat_rows}
    beeper_people = {r["network"]: dict(r) for r in beeper_people_rows}
    networks = sorted(
        set(native_networks) | set(beeper_messages) | set(beeper_chats),
        key=lambda n: (0 if n in native_networks else 1, n.lower()),
    )
    out = []
    for net in networks:
        row = native_networks.get(net, {
            "native_source": None,
            "canonical_source": "beeper",
            "policy": "Beeper only",
        })
        src = row["native_source"]
        native_stats = native.get(src or "", {"messages": 0, "chats": 0, "people": 0, "last_message": None})
        mirror_messages = beeper_messages.get(net, {})
        mirror_chats = beeper_chats.get(net, {})
        mirror_people = beeper_people.get(net, {})
        d = {
            **row,
            "network": net,
            "beeper_network": net,
            "native_messages": native_stats.get("messages") or 0,
            "native_chats": native_stats.get("chats") or 0,
            "native_people": native_stats.get("people") or 0,
            "native_last_message": native_stats.get("last_message"),
            "beeper_messages": mirror_messages.get("messages") or 0,
            "beeper_chats": mirror_chats.get("chats") or 0,
            "beeper_people": mirror_people.get("people") or 0,
            "beeper_last_message": mirror_messages.get("last_message") or mirror_chats.get("last_seen"),
        }
        for key in ("native_last_message", "beeper_last_message"):
            if d[key]:
                d[key] = d[key].isoformat()
        out.append(d)
    _MESSAGING_COVERAGE_CACHE["ts"] = now
    _MESSAGING_COVERAGE_CACHE["rows"] = out
    return out


# ── Media browser ──

@app.get("/media/browse")
async def browse_media(
    source: str | None = None,
    entity: str | None = None,
    content_type: str | None = None,
    kind: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    _user: dict = Depends(require_role("viewer")),
):
    """Paginated media browse.

    Count strategy: an exact ``SELECT COUNT(*) FROM media_items`` on a 500k+
    row table hit the 60s asyncpg ``command_timeout`` and hung the endpoint
    (bare count ~10s, source-filtered ~4s cache-warm and much worse cold, ILIKE
    entity match worse still). Instead we ask the query planner for its own
    rowcount estimate via ``EXPLAIN (FORMAT JSON)`` -- it comes from pg_stats
    (MCV frequencies + reltuples), does not touch the heap, returns in <1 ms,
    and matches the true count within a fraction of a percent for this table
    provided ANALYZE is up to date. ``total_estimated`` is always ``True`` so
    the UI can prefix "~" if it wants to be honest about approximation. The
    item query is strictly index-backed via ``idx_media_collected`` (backward
    index scan + LIMIT → <1 ms).
    """
    pool = await get_pool()
    offset = (page - 1) * page_size
    conditions = []
    params: list = []
    idx = 1

    def _escape_like(s: str) -> str:
        # Escape LIKE wildcards so user input matches literally.
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    if source:
        conditions.append(f"source = ${idx}")
        params.append(source)
        idx += 1
    if entity:
        conditions.append(f"entity_name ILIKE ${idx} ESCAPE '\\'")
        params.append(f"%{_escape_like(entity)}%")
        idx += 1
    if content_type:
        conditions.append(f"content_type = ${idx}")
        params.append(content_type)
        idx += 1
    if kind:
        # Accept a single kind or a comma list (e.g. "story,highlight").
        kinds = [k.strip() for k in kind.split(",") if k.strip()]
        if kinds:
            conditions.append(f"kind = ANY(${idx}::text[])")
            params.append(kinds)
            idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async def _planner_estimate(c) -> int:
        plan = await c.fetchval(
            f"EXPLAIN (FORMAT JSON) SELECT 1 FROM media_items {where}",
            *params,
        )
        if isinstance(plan, str):
            import json as _json
            plan = _json.loads(plan)
        try:
            return int(plan[0]["Plan"]["Plan Rows"])
        except (KeyError, IndexError, TypeError, ValueError):
            return 0

    async with pool.acquire() as conn:
        total = await _planner_estimate(conn)
        rows = await conn.fetch(
            f"SELECT id, source, entity_name, content_type, kind, filename, file_path, "
            f"file_size, sha256, collected_at "
            f"FROM media_items {where} ORDER BY collected_at DESC "
            f"LIMIT ${idx} OFFSET ${idx + 1}",
            *params, page_size, offset,
        )
    return {
        "total": total,
        "total_estimated": True,
        "page": page,
        "page_size": page_size,
        "items": [dict(r) for r in rows],
    }


@app.get("/stories/overview")
async def stories_overview(
    limit: int = Query(300, ge=1, le=2000),
    _user: dict = Depends(require_role("viewer")),
):
    """Ephemeral media (media_items.kind story/highlight) grouped per account,
    with overall stats — powers the Stories dashboard page. Stories live under
    the `kind` column (not content_type), across any source that captures them
    (instagram today; whatsapp status / telegram stories / tiktok as they land).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE kind='story')      AS stories,
                   count(*) FILTER (WHERE kind='highlight')  AS highlights,
                   count(DISTINCT entity_name)               AS accounts,
                   count(DISTINCT source)                     AS sources,
                   max(collected_at)                          AS newest
            FROM media_items
            WHERE kind IN ('story','highlight')
            """
        )
        rows = await conn.fetch(
            """
            SELECT source, entity_name,
                   count(*) FILTER (WHERE kind='story')     AS story_count,
                   count(*) FILTER (WHERE kind='highlight') AS highlight_count,
                   count(*)                                  AS total,
                   max(collected_at)                         AS newest
            FROM media_items
            WHERE kind IN ('story','highlight')
            GROUP BY source, entity_name
            ORDER BY max(collected_at) DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return {"stats": dict(stats) if stats else {}, "accounts": [dict(r) for r in rows]}


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


@app.get("/media/{media_id}/thumbnail")
async def media_thumbnail(media_id: str, _user: dict = Depends(require_role("viewer"))):
    mid = _parse_media_uuid(media_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, content_type FROM media_items WHERE id = $1", mid,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Media not found")

    file_path = Path(row["file_path"]).resolve()
    # Constrain access to the configured drive root so a poisoned file_path
    # column can't make us serve arbitrary host files.
    from src.core.drive_check import DRIVE_PATH as _DRIVE_PATH
    drive_root = Path(_DRIVE_PATH).resolve()
    try:
        file_path.relative_to(drive_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside media root")
    if not file_path.is_file():
        return _thumbnail_placeholder("missing", "file not on disk")

    if row["content_type"] in ("video", "audio", "document"):
        return _thumbnail_placeholder(row["content_type"], file_path.suffix.lstrip(".") or "stored file")

    try:
        from PIL import Image
        img = Image.open(file_path)
        img.thumbnail((300, 300))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/jpeg")
    except Exception:
        return FileResponse(str(file_path))


def _resolve_media_path(file_path_str: str) -> Path:
    """Resolve a media_items.file_path, constrained to the drive root.

    Raises HTTPException (403/404) on traversal or a missing file. Shared by the
    thumbnail + file endpoints so a poisoned file_path can't serve host files.
    """
    file_path = Path(file_path_str).resolve()
    from src.core.drive_check import DRIVE_PATH as _DRIVE_PATH
    drive_root = Path(_DRIVE_PATH).resolve()
    try:
        file_path.relative_to(drive_root)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path outside media root")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found on disk")
    return file_path


@app.get("/media/{media_id}/file")
async def media_file(media_id: str, _user: dict = Depends(require_role("viewer"))):
    """Stream the raw media file with the correct Content-Type.

    Handles every content_type (video/audio/pdf/document/image), unlike the
    thumbnail endpoint which is images-only. FileResponse adds Accept-Ranges +
    honours Range requests automatically, so <video>/<audio> seeking works.
    """
    mid = _parse_media_uuid(media_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, filename FROM media_items WHERE id = $1", mid,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Media not found")

    file_path = _resolve_media_path(row["file_path"])
    import mimetypes
    media_type = mimetypes.guess_type(row["filename"] or file_path.name)[0] \
        or "application/octet-stream"
    # inline so the browser renders PDFs/video in-tab instead of force-downloading.
    return FileResponse(
        str(file_path),
        media_type=media_type,
        content_disposition_type="inline",
    )


# ── WhatsApp: Users ──

@app.get("/whatsapp/users")
async def list_wa_users(search: str | None = None, limit: int = 50,
                         _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if search:
            esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_users "
                "WHERE platform_user_id ILIKE $1 ESCAPE '\\' OR name ILIKE $1 ESCAPE '\\' "
                "OR pushname ILIKE $1 ESCAPE '\\' "
                "ORDER BY updated_at DESC NULLS LAST LIMIT $2",
                f"%{esc}%", limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_users ORDER BY updated_at DESC NULLS LAST LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


@app.get("/whatsapp/users/{jid}/history")
async def wa_user_history(jid: str, limit: int = 100,
                           _user: dict = Depends(require_role("viewer"))):
    """Message history for one WhatsApp user (by platform_user_id / JID).

    There is no separate wa_user_history table; we return the user's recent
    messages joined through whatsapp_users.id -> whatsapp_messages.sender_id.
    """
    limit = max(1, min(limit, 1000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.* FROM whatsapp_messages m "
            "JOIN whatsapp_users u ON u.id = m.sender_id "
            "WHERE u.platform_user_id = $1 "
            "ORDER BY m.collected_at DESC LIMIT $2",
            jid, limit,
        )
    return [dict(r) for r in rows]


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

@app.get("/whatsapp/chats")
async def list_wa_chats(limit: int = 100,
                        _user: dict = Depends(require_role("viewer"))):
    """Recent WhatsApp chats with last-message preview and unread count.

    Ordered by newest activity (max(chat.updated_at, latest message timestamp))
    so an idle group with a fresh reply floats to the top. `message_count` is
    the total, `last_text` is the preview (truncated) of the latest message.
    """
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('whatsapp_chats')") is None:
            return []
        # whatsapp_chats is tiny (~100 rows) vs. whatsapp_messages (~46k). Driving
        # from the chats side and doing per-chat LATERAL lookups turns each
        # last-message fetch into a single-tuple hit on
        # idx_wa_messages_chat_ts (chat_id, timestamp DESC). Beats DISTINCT ON
        # over the whole messages table (which forced a full seq scan +
        # external-merge sort, ~4s).
        #
        # Deliberately NO per-chat message_count here: a count(*) LATERAL adds
        # ~500ms for 101 chats (indexed but still walks every leaf per chat) —
        # not worth the wall-clock. participant_count comes from the chats row
        # and is enough for the sidebar; the detail view shows the loaded
        # message run itself.
        rows = await conn.fetch(
            """
            SELECT c.platform_chat_id,
                   c.name,
                   c.is_group,
                   c.chat_type,
                   c.participant_count,
                   c.updated_at,
                   lm."timestamp"          AS last_message_ts,
                   lm.text                 AS last_text,
                   lm.from_me              AS last_from_me,
                   lm.media_mime_type      AS last_media_mime
            FROM whatsapp_chats c
            LEFT JOIN LATERAL (
                SELECT "timestamp", text, from_me, media_mime_type
                FROM whatsapp_messages
                WHERE chat_id = c.id
                ORDER BY "timestamp" DESC NULLS LAST
                LIMIT 1
            ) lm ON true
            ORDER BY COALESCE(lm."timestamp", c.updated_at) DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/whatsapp/chat/{jid:path}")
async def wa_chat_messages(jid: str, limit: int = 200,
                            _user: dict = Depends(require_role("viewer"))):
    """Messages for one WhatsApp chat (chronological, oldest first).

    `jid` is the whatsapp_chats.platform_chat_id (uses :path so JIDs containing
    slashes — e.g. broadcast lists — are accepted). Joins whatsapp_users for
    sender display name + phone_number, and media_items for a stable media_id
    the frontend can pass to /media/{id}/thumbnail.
    """
    limit = max(1, min(limit, 2000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('whatsapp_chats')") is None:
            return {"chat": None, "messages": []}
        chat = await conn.fetchrow(
            """
            SELECT platform_chat_id, name, is_group, chat_type,
                   participant_count, description, updated_at
            FROM whatsapp_chats WHERE platform_chat_id = $1
            """,
            jid,
        )
        if chat is None:
            return {"chat": None, "messages": []}
        # Message-slice-then-join with scalar chat_id lookup: joining
        # whatsapp_chats inside the WHERE stops the planner from using
        # idx_wa_messages_chat_ts for ORDER BY (it can only use it when
        # chat_id is bound to a scalar). With the scalar the ORDER BY DESC
        # + LIMIT is a single index-range walk, ~200ms even for a 3.5k-msg
        # channel. Reversed to chronological for display.
        rows = await conn.fetch(
            """
            WITH msgs AS (
                SELECT m.*
                FROM whatsapp_messages m
                WHERE m.chat_id = (
                    SELECT id FROM whatsapp_chats WHERE platform_chat_id = $1
                )
                ORDER BY m."timestamp" DESC NULLS LAST
                LIMIT $2
            )
            SELECT m.platform_message_id,
                   m.from_me,
                   m.text,
                   m.media_url,
                   m.media_mime_type,
                   m.media_size,
                   m.thumbnail_url,
                   m."timestamp",
                   m.is_deleted,
                   m.deleted_at,
                   m.quoted_text,
                   m.forward_from_name,
                   u.platform_user_id  AS sender_jid,
                   u.pushname          AS sender_pushname,
                   u.name              AS sender_name,
                   u.phone_number      AS sender_phone,
                   mi.id::text         AS media_id
            FROM msgs m
            LEFT JOIN whatsapp_users u ON u.id = m.sender_id
            LEFT JOIN media_items mi ON mi.source = 'whatsapp'
                                    AND mi.file_path = m.media_url
            ORDER BY m."timestamp" DESC NULLS LAST
            """,
            jid, limit,
        )
    # Reverse to chronological (oldest first) for the chat UI. Cap individual
    # message text at 1500 chars to bound the response body — the whatsapp_
    # messages p95 text length is 638 chars, so 1500 is a comfortable ceiling
    # for anything a person actually typed. A handful of forwards / bot
    # dumps are 6–38KB each; multiplied by 200 rows those blow the payload
    # past 1.4 MB and take stdlib json.dumps 5+ seconds to encode. Rows
    # carrying truncated text set text_truncated so the frontend can offer
    # a "show full" affordance later.
    _TEXT_CAP = 1500
    messages = []
    for r in reversed(rows):
        d = dict(r)
        text = d.get("text")
        if text is not None and len(text) > _TEXT_CAP:
            d["text"] = text[:_TEXT_CAP]
            d["text_truncated"] = True
            d["text_full_length"] = len(text)
        messages.append(d)
    return {"chat": dict(chat), "messages": messages}


# ── Instagram: DMs (captured ban-safely by the extension observing direct_v2) ──

@app.get("/instagram/dms/threads")
async def list_ig_dm_threads(owner: str | None = None, limit: int = 100,
                             _user: dict = Depends(require_role("viewer"))):
    """DM threads with a message count and last-activity, newest first.

    instagram_dm_thread may be empty until the extension observes IG DMs in a
    logged-in tab; return [] cleanly if the table doesn't exist yet.
    """
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('instagram_dm_thread')") is None:
            return []
        params: list = []
        where = ""
        if owner:
            where = "WHERE t.owner_account = $1"
            params.append(owner)
        rows = await conn.fetch(
            f"""
            SELECT t.thread_id, t.title, t.participants, t.owner_account,
                   t.last_activity,
                   COALESCE(m.cnt, 0)   AS message_count,
                   m.last_ts            AS last_message_ts
            FROM instagram_dm_thread t
            LEFT JOIN (
                SELECT thread_id, count(*) AS cnt, max("timestamp") AS last_ts
                FROM instagram_dm GROUP BY thread_id
            ) m ON m.thread_id = t.thread_id
            {where}
            ORDER BY COALESCE(t.last_activity, m.last_ts) DESC NULLS LAST
            LIMIT ${len(params) + 1}
            """,
            *params, limit,
        )
    return [dict(r) for r in rows]


@app.get("/instagram/dms/thread/{thread_id}")
async def ig_dm_thread_messages(thread_id: str, limit: int = 200,
                                _user: dict = Depends(require_role("viewer"))):
    """Messages for one IG DM thread, chronological (oldest first) for display."""
    limit = max(1, min(limit, 2000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('instagram_dm')") is None:
            return {"thread": None, "messages": []}
        thread = await conn.fetchrow(
            "SELECT * FROM instagram_dm_thread WHERE thread_id = $1", thread_id,
        )
        rows = await conn.fetch(
            'SELECT message_id, sender_id, sender_username, text, item_type, '
            '"timestamp", is_from_me, owner_account '
            'FROM instagram_dm WHERE thread_id = $1 '
            'ORDER BY "timestamp" ASC NULLS LAST LIMIT $2',
            thread_id, limit,
        )
    return {
        "thread": dict(thread) if thread else None,
        "messages": [dict(r) for r in rows],
    }


@app.get("/tiktok/dms/threads")
async def list_tt_dm_threads(owner: str | None = None, limit: int = 100,
                             _user: dict = Depends(require_role("viewer"))):
    """TikTok DM threads with per-thread message count + last-activity, newest
    first. Mirrors /instagram/dms/threads. tiktok_dm{,_thread} are populated
    by the extension's client-side decoder POSTing to /social/dm-decoded (see
    src/db/migrations/add_tiktok_dm.sql for the field-number derivation).

    Returns [] cleanly if the table doesn't exist yet — the migration ships
    together with the code that writes to it, but a partial-boot / lock-
    deferred migration is a real state the dashboard shouldn't 500 on.
    """
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_dm_thread')") is None:
            return []
        params: list = []
        where = ""
        if owner:
            where = "WHERE t.owner_account = $1"
            params.append(owner)
        rows = await conn.fetch(
            f"""
            SELECT t.conversation_id      AS thread_id,
                   t.conversation_type,
                   t.participants,
                   t.owner_account,
                   t.last_activity,
                   COALESCE(m.cnt, 0)     AS message_count,
                   m.last_ts              AS last_message_ts
            FROM tiktok_dm_thread t
            LEFT JOIN (
                SELECT conversation_id, count(*) AS cnt, max("timestamp") AS last_ts
                FROM tiktok_dm GROUP BY conversation_id
            ) m ON m.conversation_id = t.conversation_id
            {where}
            ORDER BY COALESCE(t.last_activity, m.last_ts) DESC NULLS LAST
            LIMIT ${len(params) + 1}
            """,
            *params, limit,
        )
    return [dict(r) for r in rows]


@app.get("/tiktok/dms/thread/{thread_id}")
async def tt_dm_thread_messages(thread_id: str, limit: int = 200,
                                _user: dict = Depends(require_role("viewer"))):
    """Messages for one TikTok DM thread, chronological (oldest first) for
    display. Returns awe_type / message_type in the JSON so a caller can
    tell text-message rows apart from other content kinds without querying
    raw_content."""
    limit = max(1, min(limit, 2000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_dm')") is None:
            return {"thread": None, "messages": []}
        thread = await conn.fetchrow(
            "SELECT conversation_id AS thread_id, conversation_type, participants, "
            "       owner_account, last_activity "
            "FROM tiktok_dm_thread WHERE conversation_id = $1",
            thread_id,
        )
        rows = await conn.fetch(
            'SELECT message_id, sender_uid AS sender_id, sender_secuid, '
            '       text, awe_type, message_type, "timestamp", is_from_me, '
            '       owner_account, client_message_id, is_stranger, media_url '
            'FROM tiktok_dm WHERE conversation_id = $1 '
            'ORDER BY "timestamp" ASC NULLS LAST LIMIT $2',
            thread_id, limit,
        )
    return {
        "thread": dict(thread) if thread else None,
        "messages": [dict(r) for r in rows],
    }


@app.get("/dm/telemetry")
async def dm_telemetry(_user: dict = Depends(require_role("viewer"))):
    """Passive DM probe/sample telemetry for the dashboard panel (P1.2 + P1.3).

    Returns per-platform counts of probes and samples the extension's
    observe-only WS hook has emitted, so we can tell at a glance whether real
    DM frames have arrived (particularly Instagram, which has been stuck at
    keepalive-class 1–4 byte frames while TikTok has been streaming 1KB
    protobuf samples on every DM). P1.3 also folds in dm_hook_heartbeat so
    the panel surfaces "last time the extension WS hook checked in" —
    critical for knowing when an IG/TikTok bundle change has silently broken
    the wrapper.

    Empty result if the tables don't exist yet (boot before migration).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('dm_probe_log')") is None:
            return {"platforms": [], "generated_at": datetime.now(timezone.utc).isoformat()}
        rows = await conn.fetch(
            """
            SELECT
                platform,
                event_type,
                COUNT(*)                                                            AS all_time,
                COUNT(*) FILTER (WHERE seen_at > now() - interval '24 hours')       AS last_24h,
                COUNT(*) FILTER (WHERE seen_at > now() - interval '1 hour')         AS last_1h,
                MAX(seen_at)                                                        AS last_seen,
                MAX(frame_size)                                                     AS max_frame_size,
                MIN(frame_size) FILTER (WHERE frame_size > 0)                       AS min_frame_size
            FROM dm_probe_log
            GROUP BY platform, event_type
            ORDER BY platform, event_type
            """
        )
        heartbeat_rows = []
        if await conn.fetchval("SELECT to_regclass('dm_hook_heartbeat')") is not None:
            heartbeat_rows = await conn.fetch(
                """
                SELECT platform,
                       MAX(last_seen)                                                AS last_seen,
                       SUM(probes_sent)                                              AS probes_sent,
                       SUM(samples_shipped)                                          AS samples_shipped,
                       (ARRAY_AGG(extension_version ORDER BY last_seen DESC))[1]     AS extension_version,
                       COUNT(*) FILTER (WHERE owner_account <> '')                   AS owner_count
                FROM dm_hook_heartbeat
                GROUP BY platform
                """
            )
    # Pivot to per-platform record for easy frontend rendering.
    per_platform: dict[str, dict] = {}
    def _empty_bucket():
        return {"all_time": 0, "last_24h": 0, "last_1h": 0, "last_seen": None,
                "max_frame_size": None, "min_frame_size": None}
    for r in rows:
        p = per_platform.setdefault(r["platform"], {
            "platform": r["platform"],
            "probe":  _empty_bucket(),
            "sample": _empty_bucket(),
            "hook":   None,
        })
        bucket = "sample" if r["event_type"] == "sample" else "probe"
        p[bucket] = {
            "all_time":       int(r["all_time"] or 0),
            "last_24h":       int(r["last_24h"] or 0),
            "last_1h":        int(r["last_1h"] or 0),
            "last_seen":      r["last_seen"].isoformat() if r["last_seen"] else None,
            "max_frame_size": int(r["max_frame_size"]) if r["max_frame_size"] is not None else None,
            "min_frame_size": int(r["min_frame_size"]) if r["min_frame_size"] is not None else None,
        }
    for h in heartbeat_rows:
        p = per_platform.setdefault(h["platform"], {
            "platform": h["platform"],
            "probe":  _empty_bucket(),
            "sample": _empty_bucket(),
            "hook":   None,
        })
        p["hook"] = {
            "last_seen":         h["last_seen"].isoformat() if h["last_seen"] else None,
            "probes_sent":       int(h["probes_sent"] or 0),
            "samples_shipped":   int(h["samples_shipped"] or 0),
            "extension_version": h["extension_version"],
            "owner_count":       int(h["owner_count"] or 0),
        }
    return {
        "platforms": list(per_platform.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── WhatsApp: Links ──

@app.get("/whatsapp/links")
async def list_wa_links(link_type: str | None = None, status: str | None = None,
                        limit: int = 100,
                        _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 1000))
    pool = await get_pool()
    conditions = []
    params = []
    idx = 1
    if link_type:
        conditions.append(f"link_type = ${idx}")
        params.append(link_type)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with pool.acquire() as conn:
        # wa_discovered_links is an optional feature table; return [] if absent
        # rather than surfacing a 500 for a table that was never created.
        if await conn.fetchval("SELECT to_regclass('wa_discovered_links')") is None:
            return []
        rows = await conn.fetch(
            f"SELECT * FROM wa_discovered_links {where} ORDER BY discovered_at DESC LIMIT ${idx}",
            *params, limit,
        )
    return [dict(r) for r in rows]


@app.get("/whatsapp/links/stats")
async def wa_link_stats(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('wa_discovered_links')") is None:
            return []
        rows = await conn.fetch(
            "SELECT link_type, status, COUNT(*) AS count "
            "FROM wa_discovered_links GROUP BY link_type, status ORDER BY count DESC"
        )
    return [dict(r) for r in rows]


@app.get("/worker/health")
async def worker_health(_user: dict = Depends(require_role("viewer"))):
    from src.worker import get_worker_health
    return get_worker_health()


@app.get("/schedules")
async def list_schedules(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM collection_schedules ORDER BY source"
        )
    return [dict(r) for r in rows]


@app.post("/schedules")
async def create_schedule(req: ScheduleRequest, _user: dict = Depends(require_role("operator"))):
    from datetime import datetime, timedelta, timezone
    pool = await get_pool()
    now = datetime.now(timezone.utc)
    next_run = now + timedelta(hours=req.interval_hours)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO collection_schedules (source, interval_hours, enabled, next_run) "
            "VALUES ($1, $2, true, $3) "
            "ON CONFLICT (source) DO UPDATE "
            "SET interval_hours = $2, enabled = true, next_run = $3",
            req.source, req.interval_hours, next_run,
        )
    return {"status": "ok", "source": req.source, "interval_hours": req.interval_hours}


@app.delete("/schedules/{source}")
async def delete_schedule(source: str, _user: dict = Depends(require_role("admin"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM collection_schedules WHERE source = $1", source,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"No schedule for {source}")
    return {"status": "deleted", "source": source}


@app.get("/targets")
async def list_targets(source: str | None = None,
                        _user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT * FROM collection_targets WHERE source = $1 ORDER BY priority DESC",
                source,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM collection_targets ORDER BY source, priority DESC"
            )
    return [dict(r) for r in rows]


@app.get("/runs")
async def list_runs(source: str | None = None, limit: int = 20,
                     _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT * FROM collection_runs WHERE source = $1 "
                "ORDER BY started_at DESC LIMIT $2",
                source, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


# ── Strava following-feed endpoints ──
#
# Powers the dashboard /strava/feed page. All endpoints are read-only and
# require viewer role. Backed by the strava_activities table, which is
# populated by both the API path (collect_athlete_profile/_collect_activities_api)
# and the cookie path (fetch_feed_for_date / backfill_feed_history).

@app.get("/strava/athletes")
async def strava_list_athletes(
    limit: int = 200,
    _user: dict = Depends(require_role("viewer")),
):
    """List athletes with at least one activity, ordered by activity count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.platform_athlete_id, a.username, a.firstname, a.lastname,
                   a.profile, COUNT(act.id) AS activity_count
            FROM strava_athletes a
            LEFT JOIN strava_activities act ON act.athlete_id = a.id
            GROUP BY a.id
            ORDER BY activity_count DESC, a.platform_athlete_id ASC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/strava/feed/dates")
async def strava_feed_dates(
    athlete_id: int | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    _user: dict = Depends(require_role("viewer")),
):
    """List dates with activity counts for a given athlete (or all)."""
    pool = await get_pool()
    where = ["start_date IS NOT NULL"]
    args: list = []
    if athlete_id is not None:
        args.append(int(athlete_id))
        where.append(
            f"athlete_id = (SELECT id FROM strava_athletes WHERE platform_athlete_id = ${len(args)})"
        )
    if from_:
        try:
            args.append(datetime.fromisoformat(from_))
            where.append(f"start_date >= ${len(args)}")
        except Exception:
            raise HTTPException(status_code=400, detail="bad 'from' date")
    if to:
        try:
            args.append(datetime.fromisoformat(to))
            where.append(f"start_date <= ${len(args)}")
        except Exception:
            raise HTTPException(status_code=400, detail="bad 'to' date")
    sql = (
        "SELECT DATE(start_date) AS date, COUNT(*) AS count "
        "FROM strava_activities "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY DATE(start_date) ORDER BY DATE(start_date) DESC LIMIT 365"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [{"date": r["date"].isoformat(), "count": r["count"]} for r in rows]


@app.get("/strava/feed/activities")
async def strava_feed_activities(
    date: str,
    athlete_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    _user: dict = Depends(require_role("viewer")),
):
    """Activities on a given UTC date for an athlete (or all)."""
    try:
        day = datetime.fromisoformat(date).date()
    except Exception:
        raise HTTPException(status_code=400, detail="bad 'date'")
    pool = await get_pool()
    where = ["DATE(act.start_date) = $1"]
    args: list = [day]
    if athlete_id is not None:
        args.append(int(athlete_id))
        where.append(
            f"act.athlete_id = (SELECT id FROM strava_athletes WHERE platform_athlete_id = ${len(args)})"
        )
    args.append(int(limit))
    args.append(int(offset))
    # NB: summary_polyline / start_latlng / distance_unit / stream_status feed the
    # dashboard map thumbnail. Older scrapes sometimes populated the full GPS
    # stream without backfilling summary_polyline, so we also fetch latlng and
    # derive the compact thumbnail line below when needed.
    sql = (
        "SELECT act.platform_activity_id, act.name, act.type, act.sport_type, "
        "       act.distance, act.distance_unit, act.moving_time, act.elapsed_time, "
        "       act.total_elevation_gain, act.average_speed, act.start_date, "
        "       act.summary_polyline, act.start_latlng, act.stream_status, s.latlng AS gps_latlng, "
        "       rl.created_at AS gps_rate_limit_at, rl.cooldown_until AS gps_rate_limit_until, "
        "       rl.reason AS gps_rate_limit_reason, rl.context AS gps_rate_limit_context, "
        "       a.platform_athlete_id, a.username, a.firstname, a.lastname, a.profile "
        "FROM strava_activities act "
        "LEFT JOIN strava_athletes a ON a.id = act.athlete_id "
        "LEFT JOIN strava_gps_streams s ON s.activity_id = act.id "
        "LEFT JOIN LATERAL ( "
        "    SELECT created_at, "
        "           created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS cooldown_until, "
        "           reason, metadata->>'context' AS context "
        "    FROM rate_limit_events "
        "    WHERE source = 'strava' "
        "      AND scope = 'gps_streams' "
        "      AND metadata->>'activity_id' = act.platform_activity_id::text "
        "    ORDER BY created_at DESC "
        "    LIMIT 1 "
        ") rl ON TRUE "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY act.start_date DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out = []
    for r in rows:
        d = dict(r)
        if d.get("start_date"):
            d["start_date"] = d["start_date"].isoformat()
        if not d.get("summary_polyline"):
            points = _jsonb_points(d.pop("gps_latlng", None))
            if len(points) > 1:
                d["summary_polyline"] = _encode_polyline(points)
                d["stream_status"] = d.get("stream_status") or "ok"
        else:
            d.pop("gps_latlng", None)
        d.update(_strava_route_status(d))
        for key in ("gps_rate_limit_at", "gps_rate_limit_until"):
            if d.get(key):
                d[key] = d[key].isoformat()
        out.append(d)
    return out


@app.get("/strava/feed/stats")
async def strava_feed_stats(
    athlete_id: int | None = None,
    _user: dict = Depends(require_role("viewer")),
):
    """Summary stats for an athlete's activities (or all)."""
    pool = await get_pool()
    where = ["1 = 1"]
    args: list = []
    if athlete_id is not None:
        args.append(int(athlete_id))
        where.append(
            f"athlete_id = (SELECT id FROM strava_athletes WHERE platform_athlete_id = ${len(args)})"
        )
    sql = (
        "SELECT COUNT(*) AS total_activities, "
        "       COALESCE(SUM(distance), 0) AS total_distance, "
        "       COALESCE(SUM(moving_time), 0) AS total_moving_time, "
        "       COALESCE(SUM(total_elevation_gain), 0) AS total_elevation_gain, "
        "       MIN(start_date) AS earliest, "
        "       MAX(start_date) AS latest "
        f"FROM strava_activities WHERE {' AND '.join(where)}"
    )
    coverage_sql = (
        "WITH base AS ( "
        "  SELECT *, "
        "         COALESCE((summary_polyline IS NOT NULL AND summary_polyline <> '') OR stream_status = 'ok', FALSE) AS is_mapped, "
        "         (COALESCE(metadata, '{}'::jsonb) ? 'browser_stream_last_seen_at') AS is_browser_captured "
        "  FROM strava_activities "
        f"  WHERE {' AND '.join(where)} "
        ") "
        "SELECT COUNT(*)::int AS total, "
        "       COUNT(*) FILTER (WHERE is_mapped)::int AS mapped, "
        "       COUNT(*) FILTER (WHERE is_browser_captured)::int AS browser_captured, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status = 'truncated_empty')::int AS privacy_zone, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status = 'incomplete')::int AS no_gps, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status = 'ok_unverifiable')::int AS unverifiable, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status IS NULL AND start_latlng IS NOT NULL)::int AS start_only, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status IS NULL AND start_latlng IS NULL)::int AS queued "
        "FROM base"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        coverage = await conn.fetchrow(coverage_sql, *args)
        recent_429_events = int(await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM rate_limit_events
            WHERE source = 'strava'
              AND scope = 'gps_streams'
              AND status_code = 429
              AND created_at >= date_trunc('hour', now())
            """
        ) or 0)
        active_cooldown = await conn.fetchrow(
            """
            SELECT created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS cooldown_until,
                   reason
            FROM rate_limit_events
            WHERE source = 'strava'
              AND scope = 'gps_streams'
              AND status_code = 429
              AND cooldown_seconds IS NOT NULL
              AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > now()
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        latest_browser_capture_at = await conn.fetchval(
            """
            SELECT max(created_at)
            FROM browser_ingest_events
            WHERE platform = 'strava'
              AND endpoint = 'strava_streams'
            """
        )
    d = dict(row) if row else {}
    for k in ("earliest", "latest"):
        if d.get(k):
            d[k] = d[k].isoformat()
    c = dict(coverage) if coverage else {}
    total = int(c.get("total") or 0)
    mapped = int(c.get("mapped") or 0)
    d["route_coverage"] = {
        "total": total,
        "mapped": mapped,
        "queued": int(c.get("queued") or 0),
        "start_only": int(c.get("start_only") or 0),
        "privacy_zone": int(c.get("privacy_zone") or 0),
        "no_gps": int(c.get("no_gps") or 0),
        "unverifiable": int(c.get("unverifiable") or 0),
        "browser_captured": int(c.get("browser_captured") or 0),
        "completion_pct": round((mapped / total) * 100, 1) if total else 0.0,
        "recent_gps_429_events": recent_429_events,
        "active_gps_cooldown_until": (
            active_cooldown["cooldown_until"].isoformat()
            if active_cooldown and active_cooldown["cooldown_until"]
            else None
        ),
        "active_gps_cooldown_reason": active_cooldown["reason"] if active_cooldown else None,
        "latest_browser_capture_at": latest_browser_capture_at.isoformat() if latest_browser_capture_at else None,
    }
    return d


@app.websocket("/ws/health")
async def ws_health(ws):
    await health_ws(ws)


# ── Matrix collector (Wave 1 Phase 3) ──
#
# Read-only views over matrix_sync_state, matrix_backfill_state, the
# decryption / media backlogs, and cross-source coverage. Every endpoint
# is gated on MATRIX_COLLECTOR_ENABLED — when disabled they short-circuit
# with a 503 rather than running queries against tables that may not be
# populated. No writes from this section.

def _matrix_enabled() -> bool:
    return os.getenv("MATRIX_COLLECTOR_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _matrix_disabled_response():
    raise HTTPException(
        status_code=503,
        detail={"enabled": False, "reason": "matrix collector disabled"},
    )


@app.get("/api/matrix/sync-state")
async def matrix_sync_state(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, next_batch, last_sync_at "
                "FROM matrix_sync_state ORDER BY last_sync_at DESC NULLS LAST LIMIT 1"
            )
    except Exception as e:
        logger.warning("matrix_sync_state query failed: %s", e)
        return {"user_id": None, "next_batch": None, "last_sync_at": None}
    if not row:
        return {"user_id": None, "next_batch": None, "last_sync_at": None}
    return dict(row)


@app.get("/api/matrix/backfill-state")
async def matrix_backfill_state(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            summary = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total_rooms,
                       COUNT(*) FILTER (WHERE done = TRUE) AS done,
                       COUNT(*) FILTER (WHERE done = FALSE) AS pending,
                       COUNT(*) FILTER (WHERE last_error IS NOT NULL AND done = FALSE) AS errored,
                       COALESCE(SUM(events_fetched), 0) AS events_total
                  FROM matrix_backfill_state
                """
            )
    except Exception as e:
        logger.warning("matrix_backfill_state query failed: %s", e)
        return {"total_rooms": 0, "done": 0, "pending": 0, "errored": 0, "events_total": 0}
    return dict(summary) if summary else {
        "total_rooms": 0, "done": 0, "pending": 0, "errored": 0, "events_total": 0,
    }


@app.get("/api/matrix/queue-depths")
async def matrix_queue_depths(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            undecrypted = await conn.fetchval(
                "SELECT COUNT(*) FROM matrix_events "
                "WHERE is_encrypted = TRUE AND is_decrypted = FALSE"
            )
            pending_media = await conn.fetchval(
                "SELECT COUNT(*) FROM matrix_events "
                "WHERE media_mxc IS NOT NULL "
                "AND media_local_path IS NULL "
                "AND (is_encrypted = FALSE OR is_decrypted = TRUE)"
            )
    except Exception as e:
        logger.warning("matrix_queue_depths query failed: %s", e)
        return {"undecrypted": 0, "pending_media": 0}
    return {"undecrypted": int(undecrypted or 0), "pending_media": int(pending_media or 0)}


@app.get("/api/matrix/coverage")
async def matrix_coverage(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    from src.core.matrix_dedupe_queries import coverage_overlap_summary
    pool = await get_pool()
    return await coverage_overlap_summary(pool)


# -----------------------------------------------------------------------------
# Telegram account management (item 4.7)
# -----------------------------------------------------------------------------

class TelegramAccountCreate(BaseModel):
    phone: str
    name: str | None = None


class TelegramAccountAuth(BaseModel):
    phone: str
    code: str
    password: str | None = None  # For 2FA


# In-memory auth state for dashboard onboarding (similar to bot flow)
_dashboard_auth_sessions: dict[str, dict] = {}


@app.get("/api/telegram/accounts")
async def list_telegram_accounts(_user: dict = Depends(require_role("viewer"))):
    """List all onboarded Telegram accounts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, phone, status, owner_bot, created_at, last_connected_at, last_error
            FROM telegram_user_accounts
            ORDER BY created_at DESC
            """
        )
    return [
        {
            "name": r["name"],
            "phone": r["phone"][:4] + "****" + r["phone"][-2:] if r["phone"] else None,
            "phone_full": r["phone"],  # Only for admin, could filter
            "status": r["status"],
            "owner_bot": r["owner_bot"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_connected_at": r["last_connected_at"].isoformat() if r["last_connected_at"] else None,
            "last_error": r["last_error"],
        }
        for r in rows
    ]


@app.post("/api/telegram/accounts/request-code")
async def telegram_request_code(
    body: TelegramAccountCreate,
    _user: dict = Depends(require_role("admin")),
):
    """Step 1: Request verification code for a new phone number."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import FloodWaitError

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        raise HTTPException(500, "TELEGRAM_API_ID/API_HASH not configured")

    phone = body.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must include country code (e.g. +6591234567)")

    # Check if already registered
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT name FROM telegram_user_accounts WHERE phone = $1",
            phone,
        )
        if existing:
            raise HTTPException(400, f"Phone already registered as '{existing}'")

    try:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        sent_code = await client.send_code_request(phone)

        # Store session for next step
        _dashboard_auth_sessions[phone] = {
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "api_id": api_id,
            "api_hash": api_hash,
            "name": body.name,
        }

        return {"status": "code_sent", "phone": phone}

    except FloodWaitError as e:
        raise HTTPException(429, f"Rate limited. Wait {e.seconds} seconds.")
    except Exception as e:
        logger.error("telegram_request_code failed: %s", e)
        raise HTTPException(500, f"Failed to send code: {type(e).__name__}")


@app.post("/api/telegram/accounts/verify-code")
async def telegram_verify_code(
    body: TelegramAccountAuth,
    _user: dict = Depends(require_role("admin")),
):
    """Step 2: Verify code and complete sign-in (handles 2FA if needed)."""
    from telethon.errors import (
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        PasswordHashInvalidError,
    )

    session = _dashboard_auth_sessions.get(body.phone)
    if not session:
        raise HTTPException(400, "No pending auth for this phone. Call request-code first.")

    client = session["client"]
    phone_code_hash = session["phone_code_hash"]

    try:
        if body.password:
            # 2FA step
            await client.sign_in(password=body.password)
        else:
            # Code verification step
            await client.sign_in(body.phone, body.code, phone_code_hash=phone_code_hash)

        # Success — save to DB
        me = await client.get_me()
        session_string = client.session.save()

        name = session.get("name") or me.username or f"user_{me.id}"
        if me.first_name and not session.get("name"):
            name = me.first_name.lower().replace(" ", "_")[:32]

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Handle name collision
            base_name = name
            suffix = 0
            while True:
                existing = await conn.fetchval(
                    "SELECT 1 FROM telegram_user_accounts WHERE name = $1",
                    name,
                )
                if not existing:
                    break
                suffix += 1
                name = f"{base_name}_{suffix}"

            await conn.execute(
                """
                INSERT INTO telegram_user_accounts
                    (name, api_id, api_hash, phone, session_string, owner_bot, status, last_connected_at)
                VALUES ($1, $2, $3, $4, $5, 'dashboard', 'active', NOW())
                """,
                name,
                session["api_id"],
                session["api_hash"],
                body.phone,
                session_string,
            )

            # Notify collector
            await conn.execute("SELECT pg_notify('telegram_account_added', $1)", name)

        # Cleanup
        del _dashboard_auth_sessions[body.phone]

        return {
            "status": "success",
            "name": name,
            "display_name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
        }

    except SessionPasswordNeededError:
        return {"status": "2fa_required", "phone": body.phone}

    except PhoneCodeInvalidError:
        raise HTTPException(400, "Invalid code")

    except PhoneCodeExpiredError:
        del _dashboard_auth_sessions[body.phone]
        raise HTTPException(400, "Code expired. Request a new one.")

    except PasswordHashInvalidError:
        raise HTTPException(400, "Incorrect 2FA password")

    except Exception as e:
        logger.error("telegram_verify_code failed: %s", e)
        raise HTTPException(500, f"Verification failed: {type(e).__name__}")


@app.delete("/api/telegram/accounts/{name}")
async def delete_telegram_account(
    name: str,
    _user: dict = Depends(require_role("admin")),
):
    """Remove a Telegram account."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM telegram_user_accounts WHERE name = $1",
            name,
        )
        if result == "DELETE 0":
            raise HTTPException(404, "Account not found")

    return {"status": "deleted", "name": name}


@app.post("/api/telegram/accounts/{name}/disable")
async def disable_telegram_account(
    name: str,
    _user: dict = Depends(require_role("admin")),
):
    """Disable a Telegram account (stops collection but keeps session)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE telegram_user_accounts SET status = 'disabled' WHERE name = $1",
            name,
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "Account not found")

    return {"status": "disabled", "name": name}


@app.post("/api/telegram/accounts/{name}/enable")
async def enable_telegram_account(
    name: str,
    _user: dict = Depends(require_role("admin")),
):
    """Re-enable a disabled Telegram account."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE telegram_user_accounts SET status = 'active' WHERE name = $1 AND status = 'disabled'",
            name,
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "Account not found or not disabled")

        # Notify collector
        await conn.execute("SELECT pg_notify('telegram_account_added', $1)", name)

    return {"status": "enabled", "name": name}


@app.get("/api/telegram/stats")
async def telegram_stats(_user: dict = Depends(require_role("viewer"))):
    """Aggregate Telegram collection stats for the dashboard Telegram section.

    Returns totals (messages, users, chats, reactions), top chats by message
    count, and recent ingest activity, so the UI can show "users, links" and
    overall health at a glance. Each count is guarded so a missing table never
    500s the whole panel.
    """
    pool = await get_pool()
    out: dict = {"totals": {}, "top_chats": [], "recent": {}}
    async with pool.acquire() as conn:
        async def _count(sql: str) -> int:
            try:
                v = await conn.fetchval(sql)
                return int(v or 0)
            except Exception:
                return 0

        out["totals"] = {
            "messages": await _count("SELECT COUNT(*) FROM telegram_messages"),
            "users": await _count("SELECT COUNT(*) FROM telegram_users"),
            "chats": await _count("SELECT COUNT(*) FROM telegram_chats"),
            "reactions": await _count("SELECT COUNT(*) FROM telegram_reactions"),
            "accounts": await _count("SELECT COUNT(*) FROM telegram_user_accounts"),
            "spider_queue": await _count("SELECT COUNT(*) FROM telegram_spider_queue"),
        }
        out["recent"] = {
            "messages_24h": await _count(
                "SELECT COUNT(*) FROM telegram_messages "
                "WHERE collected_at > now() - interval '24 hours'"
            ),
            "messages_1h": await _count(
                "SELECT COUNT(*) FROM telegram_messages "
                "WHERE collected_at > now() - interval '1 hour'"
            ),
        }
        try:
            rows = await conn.fetch(
                "SELECT c.title, c.username, COUNT(m.*) AS messages "
                "FROM telegram_chats c "
                "LEFT JOIN telegram_messages m ON m.chat_id = c.id "
                "GROUP BY c.id, c.title, c.username "
                "ORDER BY messages DESC LIMIT 10"
            )
            out["top_chats"] = [dict(r) for r in rows]
        except Exception:
            out["top_chats"] = []
    return out


@app.get("/whatsapp/qr/{bridge}")
async def whatsapp_qr(bridge: str):
    """Proxy the live QR / status from a wa-bridge.

    bridge is '1' or '2'. Returns {status, qr, ready, error}. The bridge
    regenerates the QR on its own cadence; the client polls this endpoint and
    re-renders, so a QR never goes stale on screen. Unauthenticated like the
    bridge's own /qr route (link page is behind the dashboard already).
    """
    import urllib.request

    if bridge not in ("1", "2"):
        raise HTTPException(400, "bridge must be 1 or 2")
    base = os.getenv(
        f"WA_BRIDGE_{bridge}_URL",
        f"http://wa-bridge-{bridge}:3001",
    )
    out = {"bridge": bridge, "status": "unknown", "qr": "", "ready": False, "error": None}
    try:
        # /health tells us if already paired; /qr gives the code when waiting
        with urllib.request.urlopen(f"{base}/health", timeout=8) as r:
            health = __import__("json").loads(r.read().decode())
        out["ready"] = bool(health.get("whatsapp_ready"))
        out["status"] = health.get("status", "unknown")
        if out["ready"]:
            out["status"] = "connected"
            return out
        with urllib.request.urlopen(f"{base}/qr", timeout=8) as r:
            qrd = __import__("json").loads(r.read().decode())
        out["status"] = qrd.get("status", out["status"])
        raw_qr = qrd.get("qr", "")
        if raw_qr:
            # Convert the raw Baileys QR string to a base64-encoded PNG so the
            # browser can use it directly as <img src="data:image/png;base64,…">
            import base64
            import io

            import qrcode  # noqa: PLC0415

            buf = io.BytesIO()
            qrcode.make(raw_qr).save(buf, "PNG")
            out["qr"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        else:
            out["qr"] = ""
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["status"] = "unreachable"
    return out


def _wa_bridge_base(bridge: str) -> str:
    if bridge not in ("1", "2"):
        raise HTTPException(400, "bridge must be 1 or 2")
    return os.getenv(f"WA_BRIDGE_{bridge}_URL", f"http://wa-bridge-{bridge}:3001")


async def _wa_bridge_post(bridge: str, path: str) -> dict:
    """POST to a wa-bridge control route (disconnect/reconnect), off the event loop."""
    import urllib.request

    base = _wa_bridge_base(bridge)

    def _do():
        req = urllib.request.Request(f"{base}/{path}", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return __import__("json").loads(r.read().decode())

    try:
        body = await asyncio.to_thread(_do)
        return {"bridge": bridge, "ok": True, **body}
    except Exception as exc:  # noqa: BLE001
        return {"bridge": bridge, "ok": False, "error": str(exc)}


async def _wa_bridge_get(bridge: str, path: str, timeout: int = 5) -> dict:
    """GET a wa-bridge read-only route (session/qr), off the event loop.

    Returns {ok: True, ...body} on 2xx; {ok: False, error: <str>} on transport
    or non-2xx. Never raises — the dashboard renders 'unreachable' cleanly."""
    import urllib.request
    base = _wa_bridge_base(bridge)

    def _do():
        with urllib.request.urlopen(f"{base}/{path}", timeout=timeout) as r:
            return __import__("json").loads(r.read().decode())

    try:
        body = await asyncio.to_thread(_do)
        return {"bridge": bridge, "ok": True, **body}
    except Exception as exc:  # noqa: BLE001
        return {"bridge": bridge, "ok": False, "error": str(exc)}


@app.get("/whatsapp/sessions")
async def whatsapp_sessions(_user: dict = Depends(require_role("viewer"))):
    """Identity of each linked WhatsApp session (phone number, push name,
    connection state) for both bridges. Bryan uses this after a QR scan to
    verify WHICH account got linked to WHICH bridge slot — the dashboard
    previously only exposed 'bridge 1' vs 'bridge 2' labels with no way to
    tell them apart.

    Sourced from each bridge's GET /session (added in src/bridges/whatsapp/
    src/index.ts). Called in parallel; unreachable bridges return
    ``ok: false`` with an error string rather than 500ing the endpoint."""
    results = await asyncio.gather(
        _wa_bridge_get("1", "session"),
        _wa_bridge_get("2", "session"),
    )
    return {"sessions": list(results)}


@app.post("/whatsapp/{bridge}/disconnect")
async def whatsapp_disconnect(bridge: str, _user: dict = Depends(require_role("viewer"))):
    """Unpair a wa-bridge device (logout) so it can be re-scanned as a new device."""
    return await _wa_bridge_post(bridge, "disconnect")


@app.post("/whatsapp/{bridge}/reconnect")
async def whatsapp_reconnect(bridge: str, _user: dict = Depends(require_role("viewer"))):
    """Soft-reconnect a wa-bridge (keeps creds — no re-scan)."""
    return await _wa_bridge_post(bridge, "reconnect")


@app.get("/whatsapp/link")
async def whatsapp_link_page():
    """Self-contained QR linking page with auto-refresh.

    Polls /whatsapp/qr/{1,2} every 3s, re-renders the QR image, and flips each
    panel to a green 'Connected' state the moment the bridge reports
    whatsapp_ready. No build step -- inline HTML so it ships without rebuilding
    the SPA bundle.
    """
    from fastapi.responses import HTMLResponse

    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Link WhatsApp</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;color:#e9edef;margin:0;padding:24px}
 h1{font-size:20px;font-weight:600;margin:0 0 4px}
 p.sub{color:#8696a0;margin:0 0 24px;font-size:14px}
 .grid{display:flex;gap:24px;flex-wrap:wrap}
 .card{background:#111b21;border:1px solid #222d34;border-radius:12px;padding:20px;width:320px}
 .card h2{font-size:16px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
 .qrbox{width:280px;height:280px;display:flex;align-items:center;justify-content:center;background:#fff;border-radius:8px;margin:0 auto}
 .qrbox img{width:264px;height:264px;image-rendering:pixelated}
 .status{margin-top:14px;font-size:13px;text-align:center;color:#8696a0}
 .identity{margin-top:10px;text-align:center}
 .identity .phone{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:15px;color:#e9edef;font-weight:600}
 .identity .name{font-size:13px;color:#8696a0;margin-top:2px}
 .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
 .dot.wait{background:#f0b232}.dot.ok{background:#22c55e}.dot.err{background:#ef4444}
 .connected{color:#22c55e;font-weight:600}
 .spinner{display:inline-block;width:12px;height:12px;border:2px solid #2a3942;border-top-color:#00a884;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px}
 @keyframes spin{to{transform:rotate(360deg)}}
 .steps{color:#8696a0;font-size:13px;line-height:1.7;margin:20px 0 0;max-width:680px}
 code{background:#202c33;padding:2px 6px;border-radius:4px}
</style></head>
<body>
 <h1>Link WhatsApp accounts</h1>
 <p class="sub">Two independent account slots. Link either one in any order. QR refreshes automatically &mdash; just leave this open.</p>
 <div class="grid">
  <div class="card"><h2>Bridge 1 <span id="t1"></span></h2><div class="qrbox" id="q1"><span class="spinner"></span></div><div class="status" id="s1">Loading&hellip;</div><div class="identity" id="i1"></div></div>
  <div class="card"><h2>Bridge 2 <span id="t2"></span></h2><div class="qrbox" id="q2"><span class="spinner"></span></div><div class="status" id="s2">Loading&hellip;</div><div class="identity" id="i2"></div></div>
 </div>
 <div class="steps">
  <b>On your phone:</b> WhatsApp &rarr; Settings &rarr; <b>Linked Devices</b> &rarr; <b>Link a Device</b> &rarr; point the camera at a QR above.<br>
  The panel turns <span class="connected">green</span> automatically once linked. No need to refresh the page.
 </div>
<script>
async function pollSession(b){
  // Fetch the paired-account identity (phone, push name) for this bridge and
  // paint it under the QR box. Called on each poll tick so a new pairing
  // shows up within one poll cycle. Uses /whatsapp/sessions which fans out
  // to both bridges — cheap, and matches what the bridge itself reports.
  try{
    const r=await fetch('/whatsapp/sessions',{cache:'no-store'});
    if(!r.ok) return;
    const d=await r.json();
    const iEl=document.getElementById('i'+b);
    if(!iEl) return;
    const idx = b === '1' ? 0 : 1;
    const s = (d.sessions||[])[idx];
    if(!s || !s.ok){ iEl.innerHTML=''; return; }
    if(s.connected && s.phone_number){
      const parts = [];
      parts.push('<div class="phone">+' + s.phone_number + '</div>');
      if(s.push_name) parts.push('<div class="name">' + s.push_name + '</div>');
      iEl.innerHTML = parts.join('');
    } else if(!s.connected){
      iEl.innerHTML='';
    }
  }catch(e){/* ignore */}
}
async function poll(b){
  const sEl=document.getElementById('s'+b), qEl=document.getElementById('q'+b), tEl=document.getElementById('t'+b);
  try{
    const r=await fetch('/whatsapp/qr/'+b,{cache:'no-store'}); const d=await r.json();
    if(d.ready||d.status==='connected'){
      qEl.innerHTML='&#10003;'; qEl.style.background='#0b3d24'; qEl.style.color='#22c55e'; qEl.style.fontSize='90px';
      sEl.innerHTML='<span class="dot ok"></span> <span class="connected">Connected</span>';
      tEl.innerHTML='<span class="dot ok"></span>';
      pollSession(b);          // populate phone + push_name
      setTimeout(()=>poll(b),15000);   // slow poll once connected
      return;
    }
    if(d.qr){
      qEl.innerHTML='<img src="'+d.qr+'" width="264" height="264" style="image-rendering:pixelated">';
      qEl.style.background='#fff';
      sEl.innerHTML='<span class="dot wait"></span> Waiting for scan&hellip; (auto-refreshing)';
      tEl.innerHTML='<span class="dot wait"></span>';
    } else if(d.status==='unreachable'){
      qEl.innerHTML='&#9888;'; qEl.style.background='#3d1414'; qEl.style.color='#ef4444'; qEl.style.fontSize='60px';
      sEl.innerHTML='<span class="dot err"></span> Bridge unreachable: '+(d.error||'');
      tEl.innerHTML='<span class="dot err"></span>';
    } else {
      sEl.innerHTML='<span class="dot wait"></span> '+(d.status||'starting')+'&hellip;';
    }
  }catch(e){ sEl.textContent='poll error: '+e; }
  setTimeout(()=>poll(b),3000);
}
poll('1'); poll('2');
</script>
</body></html>"""
    return HTMLResponse(html)


# ── Telegram: chats + messages (realtime MTProto + full-history backfill) ──
#
# Rich per-chat detail page (dashboard /telegram/chats). Mirrors the shape of
# /instagram/dms/{threads,thread} and /tiktok/dms/{threads,thread}: a list
# endpoint keyed on the human-visible platform id, and a detail endpoint that
# returns {chat, messages}. `platform_chat_id` (varchar UNIQUE on
# telegram_chats) is used as the URL key rather than the internal UUID so the
# URL is stable, greppable, and matches how the collector logs identify chats
# ("-1001234567890"). Both endpoints degrade to an empty payload when the
# tables aren't present yet (fresh boot / partial migration) rather than
# 500ing the whole dashboard.

@app.get("/telegram/chats")
async def list_telegram_chats(owner: str | None = None, limit: int = 100,
                              _user: dict = Depends(require_role("viewer"))):
    """Recent Telegram chats, newest activity first.

    Sort key is telegram_chats.updated_at — the collector bumps this on every
    fresh MTProto chat snapshot, so it tracks activity closely enough that we
    don't have to aggregate 1.2M+ telegram_messages rows on every dashboard
    load. LIMIT ($1) is hard-capped at 500. ~6k chats × unsorted-column sort
    is still sub-50ms in practice; if we ever need strict "last message ts"
    ordering we can add a materialised chat-summary view or a compound index,
    but that's a new migration and today's cost doesn't justify one.

    `owner` is accepted for signature-parity with the IG/TT endpoints but the
    Telegram collector shares one pool of chats across all 4 accounts, so
    filtering by owner would require joining telegram_chat_members. Keeping
    the parameter reserved so callers don't need to switch shape later.
    """
    _ = owner  # reserved for future per-owner filtering via telegram_chat_members
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('telegram_chats')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT c.platform_chat_id,
                   c.title,
                   c.username,
                   c.type,
                   c.description,
                   c.members_count,
                   c.updated_at,
                   c.collected_at
            FROM telegram_chats c
            ORDER BY c.updated_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/telegram/chat/{chat_id}")
async def telegram_chat_detail(chat_id: str, limit: int = 200,
                               _user: dict = Depends(require_role("viewer"))):
    """Chat metadata + newest N messages for one Telegram chat.

    `chat_id` is `telegram_chats.platform_chat_id` (e.g. "-1001234567890"),
    matching how the IG/TT DM endpoints key on their platform thread ids.

    Messages come back newest-first (`platform_created_at DESC`) up to LIMIT
    (default 200, hard-capped at 1000). Sender identity is folded in via
    telegram_users; the deletion signal is surfaced through
    `metadata->>'deleted'` (the collector's partial index makes the flip
    cheap on write and the accessor is a plain jsonb lookup on read).

    Media rows are joined in from `media_items` (`source='telegram'`) using
    the (entity_id, content_id) unique index — telegram's message id lives in
    the second colon-separated segment of `platform_message_id`. Only the
    media UUID is returned; the frontend already has /media/<uuid>/thumbnail
    and /media/<uuid>/file wired up, so no extra API surface is needed.
    """
    limit = max(1, min(limit, 1000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('telegram_chats')") is None:
            return {"chat": None, "messages": []}
        chat_row = await conn.fetchrow(
            """
            SELECT id,
                   platform_chat_id,
                   title,
                   username,
                   type,
                   description,
                   members_count,
                   updated_at,
                   collected_at
            FROM telegram_chats
            WHERE platform_chat_id = $1
            """,
            chat_id,
        )
        if not chat_row:
            return {"chat": None, "messages": []}
        chat = dict(chat_row)
        chat_uuid = chat.pop("id")
        # Total messages in this chat (uses idx_tg_messages_chat). Cheap even
        # for the busiest chats (~63k rows top-end today).
        chat["message_count"] = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM telegram_messages WHERE chat_id = $1",
                chat_uuid,
            )
            or 0
        )
        rows = await conn.fetch(
            """
            WITH picked AS (
                -- Filter+sort on ids only (32B rows) so Postgres doesn't have
                -- to materialise every 500B-wide text/caption/metadata row
                -- just to pick the top-N. This turns a ~200s cold-cache
                -- worst-case (busiest 63k-msg chat) into a bounded scan.
                SELECT id
                FROM telegram_messages
                WHERE chat_id = $1
                ORDER BY platform_created_at DESC NULLS LAST, collected_at DESC
                LIMIT $3
            )
            SELECT m.platform_message_id,
                   m.text,
                   m.caption,
                   m.media_type,
                   m.media_file_id,
                   m.is_edited,
                   m.edit_date,
                   m.reply_to_message_id,
                   m.platform_created_at,
                   m.collected_at,
                   (m.metadata->>'deleted' = 'true' IS TRUE)  AS is_deleted,
                   m.metadata->>'deleted_at'                  AS deleted_at,
                   u.platform_user_id                 AS sender_platform_id,
                   u.username                         AS sender_username,
                   u.first_name                       AS sender_first_name,
                   u.last_name                        AS sender_last_name,
                   mi.id                              AS media_item_id
            FROM picked p
            JOIN telegram_messages m ON m.id = p.id
            LEFT JOIN telegram_users u ON u.id = m.sender_id
            LEFT JOIN media_items mi
                   ON mi.source = 'telegram'
                  AND mi.entity_id = $2
                  AND mi.content_id = split_part(m.platform_message_id, ':', 2)
            ORDER BY m.platform_created_at DESC NULLS LAST, m.collected_at DESC
            """,
            chat_uuid, chat_id, limit,
        )
    return {
        "chat": chat,
        "messages": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# TikTok feed (profiles + posts)
# ---------------------------------------------------------------------------
# Two-endpoint pair mirroring /telegram/chats + /telegram/chat/{id}. The left
# pane picks a tiktok_profiles row; the right pane pulls that profile's newest
# ~200 posts with the media_items UUID joined in so the existing
# /media/<uuid>/thumbnail + /media/<uuid>/file endpoints render the video
# thumbnail without a new proxy. Sort keys stay on indexed columns
# (idx_tt_posts_profile, tiktok_profiles.pkey) so both queries hold sub-100ms
# on the current 186-profile / 10.8k-post dataset.

@app.get("/tiktok/profiles")
async def list_tiktok_profiles(limit: int = 100,
                               _user: dict = Depends(require_role("viewer"))):
    """TikTok profiles, biggest audience first.

    Sort key is followers_count DESC with an updated_at tie-break so
    freshly-scraped accounts float ahead of stale duplicates at the same
    follower tier. No index on followers_count, but the profile table is
    small (186 rows today, headroom to ~10k) and the sort is trivial.

    Also folds a last_post_at + posts_collected pair into the row — cheap
    thanks to idx_tt_posts_profile — so the picker can show "N posts,
    last N days ago" without a second round trip per profile.
    """
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_profiles')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT p.platform_user_id,
                   p.username,
                   p.nickname,
                   p.avatar_url,
                   p.bio,
                   p.followers_count,
                   p.following_count,
                   p.heart_count,
                   p.video_count,
                   p.is_verified,
                   p.updated_at,
                   p.collected_at,
                   (SELECT MAX(create_time) FROM tiktok_posts WHERE profile_id = p.id) AS last_post_at,
                   (SELECT COUNT(*)         FROM tiktok_posts WHERE profile_id = p.id) AS posts_collected
            FROM tiktok_profiles p
            ORDER BY p.followers_count DESC NULLS LAST,
                     p.updated_at      DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/tiktok/profile/{username}")
async def tiktok_profile_detail(username: str, limit: int = 200,
                                _user: dict = Depends(require_role("viewer"))):
    """Profile metadata + newest N posts for one TikTok account.

    Posts filter by profile_id (idx_tt_posts_profile) then sort by
    create_time DESC with a collected_at tie-break for the pre-2019 posts
    whose exact upload timestamp is fuzzy. media_items joined in on
    (source='tiktok', content_id=platform_post_id) — the source unique
    index makes the join O(N posts). Only the media UUID + content_type
    come back; the frontend already knows how to hit
    /media/<uuid>/thumbnail and /media/<uuid>/file.

    post_url prefers media_items.source_url (the collector's authoritative
    build) and falls back to the standard TikTok share pattern for posts
    scraped before the source_url contract landed.
    """
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_profiles')") is None:
            return {"profile": None, "posts": []}
        profile_row = await conn.fetchrow(
            """
            SELECT id,
                   platform_user_id,
                   username,
                   nickname,
                   avatar_url,
                   bio,
                   followers_count,
                   following_count,
                   heart_count,
                   video_count,
                   digg_count,
                   is_verified,
                   is_private,
                   updated_at,
                   collected_at
            FROM tiktok_profiles
            WHERE username = $1
            """,
            username,
        )
        if not profile_row:
            return {"profile": None, "posts": []}
        profile = dict(profile_row)
        profile_uuid = profile.pop("id")
        rows = await conn.fetch(
            """
            SELECT p.platform_post_id,
                   p.title,
                   p.description,
                   p.video_url,
                   p.cover_image_url,
                   p.hashtags,
                   p.view_count,
                   p.like_count,
                   p.comment_count,
                   p.share_count,
                   p.duration,
                   p.music_title,
                   p.music_author,
                   p.create_time,
                   p.collected_at,
                   mi.id                     AS media_item_id,
                   mi.content_type           AS media_content_type,
                   mi.source_url             AS media_source_url
            FROM tiktok_posts p
            LEFT JOIN media_items mi
                   ON mi.source = 'tiktok'
                  AND mi.content_id = p.platform_post_id
            WHERE p.profile_id = $1
            ORDER BY p.create_time DESC NULLS LAST, p.collected_at DESC
            LIMIT $2
            """,
            profile_uuid, limit,
        )
        posts = []
        for r in rows:
            d = dict(r)
            d["post_url"] = d.pop("media_source_url") or (
                f"https://www.tiktok.com/@{username}/video/{d['platform_post_id']}"
            )
            posts.append(d)
    return {"profile": profile, "posts": posts}


# ---------------------------------------------------------------------------
# Threads feed (profiles + posts)
# ---------------------------------------------------------------------------

@app.get("/threads/profiles")
async def list_threads_profiles(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """Threads profiles derived from collected posts."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('threads_posts')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT p.author_username AS username,
                   COUNT(p.id) AS posts_collected,
                   MAX(p.platform_created_at) AS last_post_at,
                   (SELECT profile_photo_url FROM social_users WHERE platform='threads' AND username=p.author_username ORDER BY times_seen DESC LIMIT 1) AS avatar_url
            FROM threads_posts p
            GROUP BY p.author_username
            ORDER BY posts_collected DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]

@app.get("/threads/profile/{username}")
async def threads_profile_detail(username: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Posts for one Threads account."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('threads_posts')") is None:
            return {"profile": None, "posts": []}
            
        profile_info = await conn.fetchrow(
            """
            SELECT p.author_username AS username,
                   COUNT(p.id) AS posts_collected,
                   MAX(p.platform_created_at) AS last_post_at,
                   (SELECT profile_photo_url FROM social_users WHERE platform='threads' AND username=p.author_username ORDER BY times_seen DESC LIMIT 1) AS avatar_url
            FROM threads_posts p
            WHERE p.author_username = $1
            GROUP BY p.author_username
            """,
            username
        )
        if not profile_info:
            return {"profile": None, "posts": []}
            
        posts = await conn.fetch(
            """
            SELECT p.platform_post_id,
                   p.caption,
                   p.hashtags,
                   p.likes_count,
                   p.comments_count,
                   p.reposts_count,
                   p.media_type,
                   p.platform_created_at,
                   p.collected_at,
                   mi.id AS media_item_id,
                   mi.content_type AS media_content_type
            FROM threads_posts p
            LEFT JOIN media_items mi
                   ON mi.source = 'threads'
                  AND mi.entity_id = p.author_username
                  AND mi.content_id = p.platform_post_id
            WHERE p.author_username = $1
            ORDER BY p.platform_created_at DESC NULLS LAST, p.collected_at DESC
            LIMIT $2
            """,
            username, limit,
        )
        
    out_posts = []
    for r in posts:
        d = dict(r)
        d["post_url"] = f"https://www.threads.net/@{username}/post/{d['platform_post_id']}"
        out_posts.append(d)
        
    return {"profile": dict(profile_info), "posts": out_posts}


# ---------------------------------------------------------------------------
# YouTube feed (channels + videos)
# ---------------------------------------------------------------------------

@app.get("/youtube/channels")
async def list_youtube_channels(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """YouTube channels and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('youtube_channels')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT c.id,
                   c.platform_channel_id,
                   c.title,
                   c.custom_url,
                   c.thumbnail_url,
                   c.description,
                   c.subscriber_count,
                   c.video_count,
                   c.view_count,
                   c.updated_at,
                   (SELECT COUNT(*) FROM youtube_videos WHERE channel_id = c.id) AS videos_collected,
                   (SELECT MAX(platform_published_at) FROM youtube_videos WHERE channel_id = c.id) AS last_video_at
            FROM youtube_channels c
            ORDER BY c.subscriber_count DESC NULLS LAST, c.updated_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]

@app.get("/youtube/channel/{channel_id}")
async def youtube_channel_detail(channel_id: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Videos for one YouTube channel."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('youtube_channels')") is None:
            return {"channel": None, "videos": []}
            
        channel_row = await conn.fetchrow(
            """
            SELECT c.id,
                   c.platform_channel_id,
                   c.title,
                   c.custom_url,
                   c.thumbnail_url,
                   c.description,
                   c.subscriber_count,
                   c.video_count,
                   c.view_count,
                   c.updated_at
            FROM youtube_channels c
            WHERE c.platform_channel_id = $1
            """,
            channel_id
        )
        if not channel_row:
            return {"channel": None, "videos": []}
            
        channel = dict(channel_row)
        channel_uuid = channel.pop("id")
            
        videos = await conn.fetch(
            """
            SELECT v.platform_video_id,
                   v.title,
                   v.description,
                   v.view_count,
                   v.like_count,
                   v.comment_count,
                   v.duration,
                   v.platform_published_at,
                   v.collected_at,
                   mi.id AS media_item_id,
                   mi.content_type AS media_content_type
            FROM youtube_videos v
            LEFT JOIN media_items mi
                   ON mi.source = 'youtube'
                  AND mi.content_id = v.platform_video_id
            WHERE v.channel_id = $1
            ORDER BY v.platform_published_at DESC NULLS LAST, v.collected_at DESC
            LIMIT $2
            """,
            channel_uuid, limit,
        )
        
    out_videos = []
    for r in videos:
        d = dict(r)
        d["video_url"] = f"https://www.youtube.com/watch?v={d['platform_video_id']}"
        out_videos.append(d)
        
    return {"channel": channel, "videos": out_videos}


# ---------------------------------------------------------------------------
# GitHub feed (repos + commits)
# ---------------------------------------------------------------------------

@app.get("/github/profiles")
async def list_github_profiles(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """GitHub owners first, with repo totals.

    The dashboard is profile-first because a repo-first picker hides the useful
    question: whose GitHub footprint changed recently?
    """
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return []
        rows = await conn.fetch(
            """
            WITH recent_commits AS MATERIALIZED (
                SELECT repo_id, collected_at
                FROM github_commits
                WHERE collected_at IS NOT NULL
                ORDER BY collected_at DESC
                LIMIT 5000
            )
            SELECT split_part(r.full_name, '/', 1) AS owner,
                   COUNT(DISTINCT r.id) AS repos_collected,
                   COALESCE(MAX(r.stargazers_count), 0)::bigint AS stargazers_count,
                   COALESCE(MAX(r.forks_count), 0)::bigint AS forks_count,
                   MAX(r.platform_updated_at) AS updated_at,
                   MAX(rc.collected_at) AS collected_at,
                   COUNT(*) AS commits_loaded
            FROM recent_commits rc
            JOIN github_repos r ON r.id = rc.repo_id
            WHERE r.full_name IS NOT NULL AND r.full_name <> ''
            GROUP BY split_part(r.full_name, '/', 1)
            ORDER BY MAX(rc.collected_at) DESC NULLS LAST,
                     COUNT(*) DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/github/profile/{owner}")
async def github_profile_detail(owner: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Repos + recent commits for one GitHub owner."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return {"profile": None, "repos": [], "commits": []}

        profile_row = await conn.fetchrow(
            """
            SELECT split_part(full_name, '/', 1) AS owner,
                   COUNT(*) AS repos_collected,
                   COALESCE(SUM(stargazers_count), 0)::bigint AS stargazers_count,
                   COALESCE(SUM(forks_count), 0)::bigint AS forks_count,
                   MAX(platform_updated_at) AS updated_at,
                   MAX(collected_at) AS collected_at
            FROM github_repos
            WHERE split_part(full_name, '/', 1) = $1
            GROUP BY split_part(full_name, '/', 1)
            """,
            owner,
        )
        if not profile_row:
            return {"profile": None, "repos": [], "commits": []}

        repos = await conn.fetch(
            """
            SELECT id,
                   platform_repo_id,
                   name,
                   full_name,
                   description,
                   language,
                   stargazers_count,
                   forks_count,
                   open_issues_count,
                   platform_updated_at
            FROM github_repos
            WHERE split_part(full_name, '/', 1) = $1
            ORDER BY stargazers_count DESC NULLS LAST,
                     platform_updated_at DESC NULLS LAST
            LIMIT 2000
            """,
            owner,
        )
        repo_ids = [r["id"] for r in repos]
        commits = []
        if repo_ids:
            commits = await conn.fetch(
                """
                SELECT c.sha,
                       c.author_name,
                       c.author_login,
                       c.message,
                       c.date,
                       c.files_changed,
                       c.insertions,
                       c.deletions,
                       c.collected_at,
                       r.full_name
                FROM github_commits c
                JOIN github_repos r ON r.id = c.repo_id
                WHERE c.repo_id = ANY($1::uuid[])
                ORDER BY c.date DESC NULLS LAST, c.collected_at DESC
                LIMIT $2
                """,
                repo_ids, limit,
            )

    profile = dict(profile_row)
    out_commits = []
    for r in commits:
        d = dict(r)
        full_name = d.pop("full_name")
        d["repo_full_name"] = full_name
        d["commit_url"] = f"https://github.com/{full_name}/commit/{d['sha']}"
        out_commits.append(d)
    if out_commits:
        profile["last_commit_at"] = out_commits[0].get("date")
        profile["commits_loaded"] = len(out_commits)

    out_repos = []
    for r in repos:
        d = dict(r)
        d.pop("id", None)
        out_repos.append(d)
    return {"profile": profile, "repos": out_repos, "commits": out_commits}


@app.get("/github/repos")
async def list_github_repos(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """GitHub repos and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return []
        # Sort + LIMIT the repos FIRST (CTE), then attach commit counts via
        # LATERAL for only the surviving rows. The naive form put the COUNT/MAX
        # correlated subqueries in the top-level target list, which the planner
        # evaluated across far more than $1 rows over the 7.9M-row commits table
        # (~29s, tripping the 60s command_timeout intermittently). CTE-first ->
        # 100 lateral lookups on idx_gh_commits_repo -> <1s.
        rows = await conn.fetch(
            """
            WITH top AS (
                SELECT id, platform_repo_id, name, full_name, description,
                       language, stargazers_count, forks_count,
                       open_issues_count, platform_updated_at
                FROM github_repos
                ORDER BY stargazers_count DESC NULLS LAST, platform_updated_at DESC
                LIMIT $1
            )
            SELECT t.id, t.platform_repo_id, t.name, t.full_name, t.description,
                   t.language, t.stargazers_count, t.forks_count,
                   t.open_issues_count, t.platform_updated_at,
                   cc.commits_collected, cc.last_commit_at
            FROM top t
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS commits_collected, MAX(date) AS last_commit_at
                FROM github_commits gc WHERE gc.repo_id = t.id
            ) cc ON TRUE
            ORDER BY t.stargazers_count DESC NULLS LAST, t.platform_updated_at DESC
            """,
            limit,
        )
    return [dict(r) for r in rows]

@app.get("/github/repo/{full_name:path}")
async def github_repo_detail(full_name: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Commits for one GitHub repo."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return {"repo": None, "commits": []}
            
        repo_row = await conn.fetchrow(
            """
            SELECT r.id,
                   r.platform_repo_id,
                   r.name,
                   r.full_name,
                   r.description,
                   r.language,
                   r.stargazers_count,
                   r.forks_count,
                   r.open_issues_count,
                   r.platform_updated_at
            FROM github_repos r
            WHERE r.full_name = $1
            """,
            full_name
        )
        if not repo_row:
            return {"repo": None, "commits": []}
            
        repo = dict(repo_row)
        repo_uuid = repo.pop("id")
            
        commits = await conn.fetch(
            """
            SELECT c.sha,
                   c.author_name,
                   c.author_login,
                   c.message,
                   c.date,
                   c.files_changed,
                   c.insertions,
                   c.deletions,
                   c.collected_at
            FROM github_commits c
            WHERE c.repo_id = $1
            ORDER BY c.date DESC NULLS LAST, c.collected_at DESC
            LIMIT $2
            """,
            repo_uuid, limit,
        )
        
    out_commits = []
    for r in commits:
        d = dict(r)
        d["commit_url"] = f"https://github.com/{full_name}/commit/{d['sha']}"
        out_commits.append(d)
        
    return {"repo": repo, "commits": out_commits}


# ---------------------------------------------------------------------------
# Lemon8 feed (profiles + posts)
# ---------------------------------------------------------------------------

@app.get("/lemon8/profiles")
async def list_lemon8_profiles(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """Lemon8 profiles and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('lemon8_profiles')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT p.id,
                   p.platform_user_id,
                   p.username,
                   p.nickname,
                   p.avatar_url,
                   p.bio,
                   p.followers_count,
                   p.following_count,
                   p.like_count,
                   p.updated_at,
                   (SELECT COUNT(*) FROM lemon8_posts WHERE profile_id = p.id) AS posts_collected,
                   (SELECT MAX(platform_created_at) FROM lemon8_posts WHERE profile_id = p.id) AS last_post_at
            FROM lemon8_profiles p
            ORDER BY p.followers_count DESC NULLS LAST, p.updated_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]

@app.get("/lemon8/profile/{username}")
async def lemon8_profile_detail(username: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Posts for one Lemon8 account."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('lemon8_profiles')") is None:
            return {"profile": None, "posts": []}
            
        profile_row = await conn.fetchrow(
            """
            SELECT p.id,
                   p.platform_user_id,
                   p.username,
                   p.nickname,
                   p.avatar_url,
                   p.bio,
                   p.followers_count,
                   p.following_count,
                   p.like_count,
                   p.updated_at
            FROM lemon8_profiles p
            WHERE p.username = $1
            """,
            username
        )
        if not profile_row:
            return {"profile": None, "posts": []}
            
        profile = dict(profile_row)
        profile_uuid = profile.pop("id")
            
        posts = await conn.fetch(
            """
            SELECT p.platform_post_id,
                   p.title,
                   p.description,
                   p.music_title,
                   p.like_count,
                   p.comment_count,
                   p.share_count,
                   p.platform_created_at,
                   p.collected_at,
                   mi.id AS media_item_id,
                   mi.content_type AS media_content_type
            FROM lemon8_posts p
            LEFT JOIN media_items mi
                   ON mi.source = 'lemon8'
                  AND mi.content_id = p.platform_post_id
            WHERE p.profile_id = $1
            ORDER BY p.platform_created_at DESC NULLS LAST, p.collected_at DESC
            LIMIT $2
            """,
            profile_uuid, limit,
        )
        
    out_posts = []
    for r in posts:
        d = dict(r)
        d["post_url"] = f"https://www.lemon8-app.com/{username}/post/{d['platform_post_id']}"
        out_posts.append(d)
        
    return {"profile": profile, "posts": out_posts}


# ---------------------------------------------------------------------------
# Beeper feed (chats + messages)
# ---------------------------------------------------------------------------

@app.get("/beeper/chats")
async def list_beeper_chats(
    limit: int = 100,
    network: str | None = None,
    _user: dict = Depends(require_role("viewer")),
):
    """Beeper chats and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('beeper_shadow_chats')") is None:
            return []
        where = ""
        args: list = [limit]
        if network:
            args.append(network)
            where = "WHERE c.network = $2"
        rows = await conn.fetch(
            f"""
            SELECT c.chat_id,
                   c.local_chat_id,
                   c.network,
                   c.title,
                   c.img_url,
                   c.chat_type,
                   (c.chat_type = 'dm') AS is_direct,
                   c.account_id,
                   c.last_seen_at,
                   (SELECT COUNT(*) FROM beeper_shadow_messages WHERE chat_id = c.chat_id) AS messages_collected,
                   (SELECT MAX(timestamp) FROM beeper_shadow_messages WHERE chat_id = c.chat_id) AS last_message_at
            FROM beeper_shadow_chats c
            {where}
            ORDER BY c.last_seen_at DESC NULLS LAST
            LIMIT $1
            """,
            *args,
        )
    return [dict(r) for r in rows]

@app.get("/beeper/chat/{chat_id:path}")
async def beeper_chat_detail(chat_id: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Messages for one Beeper chat."""
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('beeper_shadow_chats')") is None:
            return {"chat": None, "messages": []}
            
        chat_row = await conn.fetchrow(
            """
            SELECT c.chat_id,
                   c.local_chat_id,
                   c.network,
                   c.title,
                   c.img_url,
                   c.chat_type,
                   (c.chat_type = 'dm') AS is_direct,
                   c.account_id,
                   c.last_seen_at
            FROM beeper_shadow_chats c
            WHERE c.chat_id = $1
            """,
            chat_id
        )
        if not chat_row:
            return {"chat": None, "messages": []}
            
        chat = dict(chat_row)
            
        messages = await conn.fetch(
            """
            SELECT m.message_id,
                   m.network,
                   m.sender_id,
                   m.sender_name,
                   m.text,
                   m.timestamp,
                   m.sort_key,
                   -- beeper_shadow_messages has no is_media/media_url/media_type
                   -- columns; media is inferred from msg_type + attachments and
                   -- previewed through the media_items map attached below.
                   (m.msg_type IN ('IMAGE','VIDEO','STICKER','FILE','VOICE','AUDIO')
                    OR (m.attachments IS NOT NULL
                        AND m.attachments::text NOT IN ('null','[]','{}'))) AS is_media,
                   NULL::text AS media_url,
                   m.msg_type AS media_type,
                   m.is_deleted,
                   m.deleted_at,
                   m.ingested_at
            FROM beeper_shadow_messages m
            WHERE m.chat_id = $1
            ORDER BY m.timestamp DESC NULLS LAST
            LIMIT $2
            """,
            chat_id, limit,
        )

        # The beeper collector keys media as content_id = "{message_id}_{att}"
        # (see BeeperMediaArchiver), so an exact content_id = message_id join
        # never matched — that's why the page showed no thumbnails despite
        # ~11.8k beeper media rows on disk. Recover the message_id via the
        # leading split_part segment, but ONLY for this chat's message ids
        # (= ANY) so it's a single ~0.9s scan instead of a 7s DISTINCT ON over
        # all beeper media on every open.
        mids = [m["message_id"] for m in messages]
        media_map: dict[str, str] = {}
        if mids:
            media_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (split_part(content_id, '_', 1))
                       split_part(content_id, '_', 1) AS mid,
                       id AS media_item_id
                FROM media_items
                WHERE source = 'beeper'
                  AND split_part(content_id, '_', 1) = ANY($1::text[])
                ORDER BY split_part(content_id, '_', 1), collected_at
                """,
                mids,
            )
            media_map = {r["mid"]: r["media_item_id"] for r in media_rows}

    out_messages = []
    for r in messages:
        d = dict(r)
        d["media_item_id"] = media_map.get(d["message_id"])
        out_messages.append(d)

    # Reverse so oldest is first for chat UI
    out_messages.reverse()

    return {"chat": chat, "messages": out_messages}


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
