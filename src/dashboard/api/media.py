"""``media`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role


def _realtime_feed_status_from_redis(*args, **kwargs):
    """Late-import proxy — resolved on parent api module at call time."""
    fn = _lookup("_realtime_feed_status_from_redis")
    if fn is None:
        from src.dashboard.api import _realtime_feed_status_from_redis as fn
    return fn(*args, **kwargs)


def _realtime_delivery_ledger_status(*args, **kwargs):
    fn = _lookup("_realtime_delivery_ledger_status")
    if fn is None:
        from src.dashboard.api import _realtime_delivery_ledger_status as fn
    return fn(*args, **kwargs)

logger = logging.getLogger(__name__)


def _lookup(name: str):
    """Read a name from the parent api module at call time (for test patches)."""
    import sys as _sys
    root = _sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


def _get_helper(name: str, fallback=None):
    """Return the parent-api helper or a caller-supplied fallback."""
    val = _lookup(name)
    return val if val is not None else fallback


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


@router.get("/media")
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


@router.get("/media/stats")
async def media_stats(_user: dict = Depends(require_role("viewer"))):
    pool = await get_pool()
    async with pool.acquire() as conn:
        media_totals_error = None
        try:
            media_totals = await _source_media_totals(conn)
        except Exception as exc:  # noqa: BLE001 - dashboard must degrade, not 500
            logger.warning("media stats totals failed: %s", exc)
            media_totals_error = exc.__class__.__name__
            media_totals = {}
        try:
            from src.core.source_freshness import compute_liveness
            live_sources = await compute_liveness(conn)
            live_sources, _whatsapp_bridge_health = await _with_bridge_overrides(live_sources)
            try:
                live_sources = [*live_sources, *await _beeper_subsource_liveness(conn)]
            except Exception as exc:  # noqa: BLE001
                logger.warning("media stats beeper sub-source liveness failed: %s", exc)
            live = {s["source"]: s for s in live_sources}
        except Exception:
            live = {}
        out = []
        now = datetime.now(timezone.utc)
        for source in sorted(set(media_totals) | set(live)):
            stats = media_totals.get(source, {})
            if not isinstance(stats, dict):
                # _source_media_totals can yield a non-dict (e.g. bool) for a
                # source; degrade to empty rather than 500 the whole dashboard.
                stats = {}
            d = {
                "source": source,
                "display_name": stats.get("display_name"),
                "parent_source": stats.get("parent_source"),
                "rollup_exclude": bool(stats.get("rollup_exclude")),
                "total_items": int(stats.get("total_media_items") or 0),
                "total_bytes": int(stats.get("total_media_bytes") or 0),
                "last_collected": stats.get("latest_media_at"),
                "stats_stale": bool(stats.get("stats_stale")),
                "stats_error": media_totals_error,
            }
            query_spec = _LATEST_ACTIVITY_QUERIES.get(source)
            cur = live.get(source)
            if cur:
                d["display_name"] = d.get("display_name") or cur.get("display_name")
                d["parent_source"] = d.get("parent_source") or cur.get("parent_source")
                d["rollup_exclude"] = bool(d.get("rollup_exclude") or cur.get("rollup_exclude"))
                d["live"] = cur["status"]
                d["age_seconds"] = cur["age_seconds"]
                d["stale_after_seconds"] = cur.get("stale_after_seconds")
                d["collection_mode"] = cur.get("collection_mode")
                d["freshness_basis"] = cur.get("freshness_basis")
                d["health_detail"] = cur.get("detail")
                d["source_health_status"] = cur.get("source_health_status")
                d["source_health_error"] = cur.get("source_health_error")
                if cur["age_seconds"] is not None:
                    d["last_activity"] = now - timedelta(seconds=cur["age_seconds"])
                else:
                    d["last_activity"] = d.get("last_collected")
            else:
                d["last_activity"] = d.get("last_collected")
            if source.startswith(_BEEPER_SUBSOURCE_PREFIX):
                d["activity_basis"] = "beeper shadow network"
            else:
                d["activity_basis"] = query_spec[1] if query_spec else "media"
            out.append(d)
    return out


@router.get("/media/realtime-feed/status")
async def media_realtime_feed_status(_user: dict = Depends(require_role("viewer"))):
    payload = await _realtime_feed_status_from_redis()
    try:
        payload["delivery_ledger"] = await _realtime_delivery_ledger_status()
    except Exception as exc:  # noqa: BLE001 - dashboard visibility must degrade.
        payload["delivery_ledger"] = {"available": False, "error": exc.__class__.__name__}
    return payload


@router.get("/media/realtime-feed/deliveries")
async def media_realtime_feed_deliveries(
    source: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    _user: dict = Depends(require_role("viewer")),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT to_regclass('public.realtime_media_deliveries')")
        if not exists:
            return {"available": False, "items": []}
        clauses = []
        args = []
        if source:
            args.append(source.strip().lower())
            clauses.append(f"source = ${len(args)}")
        if status:
            args.append(status.strip().lower())
            clauses.append(f"status = ${len(args)}")
        args.append(limit)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await conn.fetch(
            f"""
            SELECT id::text, media_item_id::text, source, content_id, status,
                   reason, file_size, content_type, target_name, queued_at,
                   sent_at, created_at, updated_at
            FROM realtime_media_deliveries
            {where}
            ORDER BY updated_at DESC
            LIMIT ${len(args)}
            """,
            *args,
            timeout=5,
        )
    return {"available": True, "items": [dict(row) for row in rows]}


@router.get("/media/artifact-audit")
async def media_artifact_audit(
    source: str | None = None,
    sample_per_source: int = Query(100, ge=1, le=500),
    cursor_after: str = "",
    timeout_seconds: float = Query(5.0, alias="timeout", ge=0.5, le=20.0),
    _user: dict = Depends(require_role("viewer")),
):
    from src.core.media_artifact_audit import audit_media_artifacts

    pool = await get_pool()
    async with pool.acquire() as conn:
        report = await audit_media_artifacts(
            conn,
            source=source,
            sample_per_source=sample_per_source,
            cursor_after=cursor_after,
            timeout=timeout_seconds,
        )
    return report.to_dict()


@router.get("/media/browse")
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


@router.get("/media/{media_id}/thumbnail")
async def media_thumbnail(media_id: str, _user: dict = Depends(require_role("viewer"))):
    mid = _parse_media_uuid(media_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_path, content_type FROM media_items WHERE id = $1", mid,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Media not found")

    try:
        file_path = _resolve_media_path(row["file_path"])
    except HTTPException as exc:
        detail = "path blocked" if exc.status_code == 403 else "file not on disk"
        return _thumbnail_placeholder("missing", detail)

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


@router.get("/media/{media_id}/file")
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


