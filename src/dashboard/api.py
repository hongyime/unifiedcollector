import io
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
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


async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> dict:
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


@app.get("/collectors")
async def list_collectors(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service, last_processed_id, last_processed_at, status "
            "FROM service_cursors ORDER BY service"
        )
    return [dict(r) for r in rows]


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


class LabelRequest(BaseModel):
    label: str


class MergeRequest(BaseModel):
    source_id: str
    target_id: str


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

@app.post("/targets")
async def create_target(req: TargetRequest, _user: dict = Depends(require_role("operator"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO collection_targets (source, target, priority) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            req.source, req.target, req.priority,
        )
    return {"status": "ok", "source": req.source, "target": req.target}


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


# ── WhatsApp: Faces ──

@app.get("/whatsapp/faces")
async def list_faces(limit: int = 50, _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label, occurrence_count, created_at, last_seen "
            "FROM wa_face_identities ORDER BY occurrence_count DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/whatsapp/faces/{identity_id}")
async def get_face(identity_id: str, _user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        identity = await conn.fetchrow(
            "SELECT * FROM wa_face_identities WHERE id = $1", identity_id,
        )
        if not identity:
            raise HTTPException(status_code=404, detail="Identity not found")
        embeddings = await conn.fetch(
            "SELECT source_content_id, source_entity_id, confidence, frame_index, created_at "
            "FROM wa_face_embeddings WHERE identity_id = $1 ORDER BY created_at DESC LIMIT 50",
            identity_id,
        )
    return {
        "identity": dict(identity),
        "embeddings": [dict(e) for e in embeddings],
    }


@app.post("/whatsapp/faces/{identity_id}/label")
async def label_face(identity_id: str, req: LabelRequest, _user: dict = Depends(require_role("operator"))):
    from src.core.face_matcher import FaceMatcher
    pool = await get_pool()
    matcher = FaceMatcher()
    await matcher.rename_identity(pool, identity_id, req.label)
    return {"status": "ok"}


@app.post("/whatsapp/faces/merge")
async def merge_faces(req: MergeRequest, _user: dict = Depends(require_role("operator"))):
    from src.core.face_matcher import FaceMatcher
    pool = await get_pool()
    matcher = FaceMatcher()
    await matcher.merge_identities(pool, req.source_id, req.target_id)
    return {"status": "ok"}


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
                "SELECT * FROM wa_user_profiles "
                "WHERE jid ILIKE $1 ESCAPE '\\' OR push_name ILIKE $1 ESCAPE '\\' "
                "OR display_name ILIKE $1 ESCAPE '\\' "
                "ORDER BY last_seen DESC NULLS LAST LIMIT $2",
                f"%{esc}%", limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM wa_user_profiles ORDER BY last_seen DESC NULLS LAST LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


@app.get("/whatsapp/users/{jid}/history")
async def wa_user_history(jid: str, limit: int = 100,
                           _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 1000))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM wa_user_history WHERE user_jid = $1 ORDER BY changed_at DESC LIMIT $2",
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
        rows = await conn.fetch(
            f"SELECT * FROM wa_discovered_links {where} ORDER BY discovered_at DESC LIMIT ${idx}",
            *params, limit,
        )
    return [dict(r) for r in rows]


@app.get("/whatsapp/links/stats")
async def wa_link_stats(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
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


@app.websocket("/ws/health")
async def ws_health(ws):
    await health_ws(ws)


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
