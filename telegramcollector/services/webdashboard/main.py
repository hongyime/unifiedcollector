"""Unified web dashboard for TelegramCollector."""
from __future__ import annotations

import os
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
import psycopg_pool
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)

DB_DSN = (
    f"host={os.environ.get('DB_HOST','postgres')} "
    f"port={os.environ.get('DB_PORT','5432')} "
    f"dbname={os.environ.get('DB_NAME','telegramcollector')} "
    f"user={os.environ.get('DB_USER','postgres')} "
    f"password={os.environ.get('DB_PASSWORD','')}"
)

pool: psycopg_pool.AsyncConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    try:
        pool = psycopg_pool.AsyncConnectionPool(conninfo=DB_DSN, min_size=1, max_size=5, open=False)
        await pool.open()
        logger.info("DB pool ready")
    except Exception as e:
        logger.error(f"DB pool failed: {e}")
    yield
    if pool:
        await pool.close()


app = FastAPI(lifespan=lifespan, title="TelegramCollector Dashboard")
BASE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


async def q(sql: str, params=(), fetch: str = "all") -> Any:
    if not pool:
        return [] if fetch == "all" else None
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            await cur.execute(sql, params)
            if fetch == "one":
                return await cur.fetchone()
            if fetch == "none":
                return None
            return await cur.fetchall()


async def qw(sql: str, params=()):
    """Write query (INSERT/UPDATE/DELETE)."""
    if not pool:
        return
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)


def _safe_json(obj):
    if isinstance(obj, (bytes,)):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


# ── Nav helper ────────────────────────────────────────────────────────────────

NAV = [
    {"id": "overview",    "label": "Overview",        "icon": "⬡",  "href": "/"},
    {"id": "accounts",    "label": "Accounts",         "icon": "👤", "href": "/accounts"},
    {"id": "collection",  "label": "Collection",       "icon": "📡", "href": "/collection"},
    {"id": "faces",       "label": "Face Recognition", "icon": "🎭", "href": "/faces"},
    {"id": "users",       "label": "User Intel",       "icon": "🕸",  "href": "/users"},
    {"id": "links",       "label": "Link Discovery",   "icon": "🔗", "href": "/links"},
    {"id": "config",      "label": "Config",           "icon": "⚙",  "href": "/config"},
]


def ctx(request: Request, page: str, **kwargs):
    return {"request": request, "nav": NAV, "page": page, **kwargs}


# ── Overview ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    stats = await _build_stats()
    return templates.TemplateResponse("overview.html", ctx(request, "overview", stats=stats))


@app.get("/api/stats", response_class=JSONResponse)
async def api_stats():
    try:
        r = await q("SELECT COUNT(*) AS n FROM collector.raw_messages WHERE collected_at > NOW() - INTERVAL '5 minutes'", fetch="one")
        m5 = r["n"] if r else 0
        r = await q("SELECT COUNT(*) AS n FROM collector.raw_messages", fetch="one")
        total = r["n"] if r else 0
        r = await q("SELECT COUNT(*) AS n FROM face_recognition.face_embeddings", fetch="one")
        emb = r["n"] if r else 0
        return {"messages_5m": m5, "messages_total": total, "embeddings": emb, "ts": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"error": str(e)}


async def _build_stats() -> dict:
    stats: dict = {}
    keys = [
        ("messages",     "SELECT COUNT(*) AS n FROM collector.raw_messages"),
        ("messages_1h",  "SELECT COUNT(*) AS n FROM collector.raw_messages WHERE collected_at > NOW()-INTERVAL '1 hour'"),
        ("messages_5m",  "SELECT COUNT(*) AS n FROM collector.raw_messages WHERE collected_at > NOW()-INTERVAL '5 minutes'"),
        ("accounts",     "SELECT COUNT(*) AS n FROM collector.telegram_accounts WHERE status='active'"),
        ("identities",   "SELECT COUNT(*) AS n FROM face_recognition.telegram_topics"),
        ("embeddings",   "SELECT COUNT(*) AS n FROM face_recognition.face_embeddings"),
        ("users",        "SELECT COUNT(*) AS n FROM collector.users"),
        ("links",        "SELECT COUNT(*) AS n FROM link_discovery.discovered_links"),
        ("media_24h",    "SELECT COUNT(*) AS n FROM collector.raw_messages WHERE has_media=TRUE AND collected_at > NOW()-INTERVAL '24 hours'"),
        ("processed_24h","SELECT COUNT(*) AS n FROM face_recognition.processed_media WHERE processed_at > NOW()-INTERVAL '24 hours'"),
    ]
    for key, sql in keys:
        try:
            r = await q(sql, fetch="one")
            stats[key] = r["n"] if r else 0
        except Exception:
            stats[key] = 0

    try:
        stats["top_chats"] = await q(
            "SELECT chat_id, COUNT(*) AS cnt FROM collector.raw_messages "
            "WHERE collected_at > NOW()-INTERVAL '1 hour' "
            "GROUP BY chat_id ORDER BY cnt DESC LIMIT 8"
        ) or []
    except Exception:
        stats["top_chats"] = []

    try:
        stats["cursors"] = await q(
            "SELECT service_name, last_message_id, updated_at FROM collector.service_cursors ORDER BY service_name"
        ) or []
    except Exception:
        stats["cursors"] = []

    try:
        stats["recent_errors"] = await q(
            "SELECT error_type, COUNT(*) AS n FROM collector.processing_errors "
            "WHERE occurred_at > NOW()-INTERVAL '24 hours' GROUP BY error_type ORDER BY n DESC LIMIT 6"
        ) or []
    except Exception:
        stats["recent_errors"] = []

    try:
        stats["recent_msgs"] = await q(
            "SELECT chat_id, message_type, has_media, collected_at "
            "FROM collector.raw_messages ORDER BY collected_at DESC LIMIT 12"
        ) or []
    except Exception:
        stats["recent_msgs"] = []

    try:
        stats["accounts_list"] = await q(
            "SELECT phone_number, display_name, status FROM collector.telegram_accounts ORDER BY created_at"
        ) or []
    except Exception:
        stats["accounts_list"] = []

    return stats


@app.get("/api/live", response_class=HTMLResponse)
async def api_live(request: Request):
    """HTMX partial — replaces just the bento grid content."""
    stats = await _build_stats()
    return templates.TemplateResponse("partials/bento.html", ctx(request, "overview", stats=stats))


# ── Accounts ──────────────────────────────────────────────────────────────────

@app.get("/accounts", response_class=HTMLResponse)
async def accounts(request: Request):
    rows = await q(
        "SELECT id, phone_number, display_name, status, session_file_path, last_error, created_at, last_active "
        "FROM collector.telegram_accounts ORDER BY created_at DESC"
    )
    bot_tokens_raw = os.environ.get("BOT_TOKENS", "")
    bot_names = []
    for entry in bot_tokens_raw.split(";"):
        parts = entry.strip().split(":")
        if len(parts) >= 2:
            bot_names.append(parts[0].strip())

    return templates.TemplateResponse("accounts.html", ctx(
        request, "accounts", accounts=rows or [], bot_names=bot_names
    ))


@app.post("/accounts/{account_id}/pause")
async def pause_account(account_id: int):
    await qw("UPDATE collector.telegram_accounts SET status='paused' WHERE id=%s", (account_id,))
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/activate")
async def activate_account(account_id: int):
    await qw("UPDATE collector.telegram_accounts SET status='active' WHERE id=%s", (account_id,))
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int):
    await qw("DELETE FROM collector.telegram_accounts WHERE id=%s", (account_id,))
    return RedirectResponse("/accounts", status_code=303)


# ── Collection ────────────────────────────────────────────────────────────────

@app.get("/collection", response_class=HTMLResponse)
async def collection(request: Request, page: int = 1, chat_id: str = "", media_only: str = ""):
    per_page = 50
    offset = (page - 1) * per_page
    filters = []
    params: list = []
    if chat_id:
        filters.append("chat_id = %s")
        params.append(int(chat_id))
    if media_only == "1":
        filters.append("has_media = TRUE")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    total_row = await q(f"SELECT COUNT(*) AS n FROM collector.raw_messages {where}", params, fetch="one")
    total = total_row["n"] if total_row else 0

    rows = await q(
        f"SELECT id, chat_id, message_id, sender_id, message_type, has_media, file_unique_id, collected_at "
        f"FROM collector.raw_messages {where} ORDER BY collected_at DESC LIMIT %s OFFSET %s",
        params + [per_page, offset]
    )

    chats = await q("SELECT DISTINCT chat_id FROM collector.raw_messages ORDER BY chat_id LIMIT 100")

    backfill = await q(
        "SELECT bj.id, bj.account_id, bj.chat_id, bj.status, bj.messages_done, bj.created_at, "
        "ta.phone_number FROM collector.backfill_jobs bj "
        "LEFT JOIN collector.telegram_accounts ta ON ta.id=bj.account_id "
        "ORDER BY bj.created_at DESC LIMIT 20"
    )

    return templates.TemplateResponse("collection.html", ctx(
        request, "collection",
        messages=rows or [], total=total, page=page, per_page=per_page,
        chat_id=chat_id, media_only=media_only,
        chats=[r["chat_id"] for r in (chats or [])],
        backfill_jobs=backfill or [],
        pages=max(1, (total + per_page - 1) // per_page),
    ))


# ── Faces ─────────────────────────────────────────────────────────────────────

@app.get("/faces", response_class=HTMLResponse)
async def faces(request: Request, page: int = 1):
    per_page = 24
    offset = (page - 1) * per_page

    total_row = await q("SELECT COUNT(*) AS n FROM face_recognition.telegram_topics", fetch="one")
    total = total_row["n"] if total_row else 0

    identities = await q(
        "SELECT id, topic_id, label, face_count, message_count, created_at, updated_at "
        "FROM face_recognition.telegram_topics ORDER BY face_count DESC LIMIT %s OFFSET %s",
        (per_page, offset)
    )

    stats_row = await q(
        "SELECT COUNT(*) AS embeddings, "
        "SUM(CASE WHEN processed_at > NOW()-INTERVAL '24 hours' THEN 1 ELSE 0 END) AS processed_24h "
        "FROM face_recognition.processed_media", fetch="one"
    )

    corrections = await q(
        "SELECT fc.id, fc.from_topic_id, fc.to_topic_id, fc.corrected_at, "
        "t1.label AS from_label, t2.label AS to_label "
        "FROM face_recognition.identity_corrections fc "
        "LEFT JOIN face_recognition.telegram_topics t1 ON t1.id=fc.from_topic_id "
        "LEFT JOIN face_recognition.telegram_topics t2 ON t2.id=fc.to_topic_id "
        "ORDER BY fc.corrected_at DESC LIMIT 10"
    )

    return templates.TemplateResponse("faces.html", ctx(
        request, "faces",
        identities=identities or [],
        total=total, page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
        stats=stats_row or {},
        corrections=corrections or [],
    ))


@app.post("/faces/{identity_id}/label")
async def update_label(identity_id: int, label: str = Form(...)):
    await qw("UPDATE face_recognition.telegram_topics SET label=%s, updated_at=NOW() WHERE id=%s", (label, identity_id))
    return RedirectResponse("/faces", status_code=303)


# ── Users ─────────────────────────────────────────────────────────────────────

@app.get("/users", response_class=HTMLResponse)
async def users(request: Request, search: str = "", page: int = 1):
    per_page = 50
    offset = (page - 1) * per_page
    where = ""
    params: list = []
    if search:
        where = "WHERE username ILIKE %s OR first_name ILIKE %s OR phone ILIKE %s"
        like = f"%{search}%"
        params = [like, like, like]

    total_row = await q(f"SELECT COUNT(*) AS n FROM collector.users {where}", params, fetch="one")
    total = total_row["n"] if total_row else 0

    rows = await q(
        f"SELECT id, username, first_name, last_name, phone, is_bot, is_verified, is_premium, "
        f"first_seen, last_seen FROM collector.users {where} ORDER BY last_seen DESC LIMIT %s OFFSET %s",
        params + [per_page, offset]
    )

    sightings_count = await q(
        "SELECT COUNT(*) AS n FROM collector.user_sightings WHERE seen_at > NOW()-INTERVAL '24 hours'", fetch="one"
    )

    return templates.TemplateResponse("users.html", ctx(
        request, "users",
        users=rows or [], total=total, page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
        search=search,
        sightings_24h=sightings_count["n"] if sightings_count else 0,
    ))


# ── Links ─────────────────────────────────────────────────────────────────────

@app.get("/links", response_class=HTMLResponse)
async def links(request: Request, page: int = 1):
    per_page = 50
    offset = (page - 1) * per_page

    total_row = await q("SELECT COUNT(*) AS n FROM link_discovery.discovered_links", fetch="one")
    total = total_row["n"] if total_row else 0

    rows = await q(
        "SELECT id, link_type, link_value, source_chat_id, source_message_id, "
        "resolved, discovered_at FROM link_discovery.discovered_links "
        "ORDER BY discovered_at DESC LIMIT %s OFFSET %s",
        (per_page, offset)
    )

    join_queue = await q(
        "SELECT id, peer_identifier, status, queued_at FROM collector.group_join_queue "
        "ORDER BY queued_at DESC LIMIT 20"
    )

    return templates.TemplateResponse("links.html", ctx(
        request, "links",
        links=rows or [], total=total, page=page, per_page=per_page,
        pages=max(1, (total + per_page - 1) // per_page),
        join_queue=join_queue or [],
    ))


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    settings_rows = await q(
        "SELECT config_key, group_name, value_plain, is_sensitive, updated_at "
        "FROM collector.config_settings ORDER BY group_name, config_key"
    )

    env_display = {
        k: ("***" if any(s in k for s in ["PASSWORD", "TOKEN", "HASH", "SECRET"]) else v)
        for k, v in os.environ.items()
        if k.startswith(("TG_", "BOT_", "DB_", "REDIS_", "FACE_", "COLLECTOR_", "HUB_", "USER_INTEL", "LINK_"))
    }

    return templates.TemplateResponse("config.html", ctx(
        request, "config",
        settings=settings_rows or [],
        env=sorted(env_display.items()),
    ))


@app.post("/config/update")
async def update_config(config_key: str = Form(...), value: str = Form(...)):
    await qw(
        "UPDATE collector.config_settings SET value_plain=%s, updated_at=NOW() WHERE config_key=%s",
        (value, config_key)
    )
    return RedirectResponse("/config", status_code=303)
