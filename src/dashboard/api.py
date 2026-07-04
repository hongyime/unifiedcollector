import asyncio
import io
import logging
import os
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.db.connection import get_pool
from src.dashboard.websocket import health_ws

logger = logging.getLogger(__name__)

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
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "healthy"
    except Exception as e:
        db_status = f"error: {e}"

    from src.core.drive_check import check_drive
    drive_ok = check_drive()

    return {
        "status": "ok" if db_status == "healthy" and drive_ok else "degraded",
        "database": db_status,
        "drive": "mounted" if drive_ok else "missing",
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
        async with pool.acquire() as conn:
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
    return [dict(r) for r in rows]


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
async def social_graph(source: str = "github", depth: int = 2,
                       _user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        edges = await conn.fetch(
            "SELECT source_user, target_user, edge_type FROM graph_edges LIMIT 5000"
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


# ── Media browser ──

@app.get("/media/browse")
async def browse_media(
    source: str | None = None,
    entity: str | None = None,
    content_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    _user: dict = Depends(require_role("viewer")),
):
    pool = await get_pool()
    offset = (page - 1) * page_size
    conditions = []
    params = []
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

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM media_items {where}", *params,
        )
        rows = await conn.fetch(
            f"SELECT id, source, entity_name, content_type, filename, file_path, "
            f"file_size, sha256, collected_at "
            f"FROM media_items {where} ORDER BY collected_at DESC "
            f"LIMIT ${idx} OFFSET ${idx + 1}",
            *params, page_size, offset,
        )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [dict(r) for r in rows],
    }


@app.get("/media/{media_id}/thumbnail")
async def media_thumbnail(media_id: int, _user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, content_type FROM media_items WHERE id = $1", media_id,
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
        raise HTTPException(status_code=404, detail="File not found on disk")

    if row["content_type"] in ("video", "audio", "document"):
        raise HTTPException(status_code=400, detail="Thumbnails only for images")

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
async def media_file(media_id: int, _user: dict = Depends(require_role("viewer"))):
    """Stream the raw media file with the correct Content-Type.

    Handles every content_type (video/audio/pdf/document/image), unlike the
    thumbnail endpoint which is images-only. FileResponse adds Accept-Ranges +
    honours Range requests automatically, so <video>/<audio> seeking works.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, filename FROM media_items WHERE id = $1", media_id,
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
    sql = (
        "SELECT act.platform_activity_id, act.name, act.type, act.sport_type, "
        "       act.distance, act.moving_time, act.elapsed_time, "
        "       act.total_elevation_gain, act.start_date, "
        "       a.platform_athlete_id, a.username, a.firstname, a.lastname, a.profile "
        "FROM strava_activities act "
        "LEFT JOIN strava_athletes a ON a.id = act.athlete_id "
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
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    d = dict(row) if row else {}
    for k in ("earliest", "latest"):
        if d.get(k):
            d[k] = d[k].isoformat()
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
  <div class="card"><h2>Bridge 1 <span id="t1"></span></h2><div class="qrbox" id="q1"><span class="spinner"></span></div><div class="status" id="s1">Loading&hellip;</div></div>
  <div class="card"><h2>Bridge 2 <span id="t2"></span></h2><div class="qrbox" id="q2"><span class="spinner"></span></div><div class="status" id="s2">Loading&hellip;</div></div>
 </div>
 <div class="steps">
  <b>On your phone:</b> WhatsApp &rarr; Settings &rarr; <b>Linked Devices</b> &rarr; <b>Link a Device</b> &rarr; point the camera at a QR above.<br>
  The panel turns <span class="connected">green</span> automatically once linked. No need to refresh the page.
 </div>
<script>
async function poll(b){
  const sEl=document.getElementById('s'+b), qEl=document.getElementById('q'+b), tEl=document.getElementById('t'+b);
  try{
    const r=await fetch('/whatsapp/qr/'+b,{cache:'no-store'}); const d=await r.json();
    if(d.ready||d.status==='connected'){
      qEl.innerHTML='&#10003;'; qEl.style.background='#0b3d24'; qEl.style.color='#22c55e'; qEl.style.fontSize='90px';
      sEl.innerHTML='<span class="dot ok"></span> <span class="connected">Connected</span>';
      tEl.innerHTML='<span class="dot ok"></span>';
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
