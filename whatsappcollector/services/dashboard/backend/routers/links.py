"""
backend/routers/links.py — Link discovery service data endpoints.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

import database

logger = logging.getLogger(__name__)
router = APIRouter()


def _record_to_dict(record) -> dict:
    if record is None:
        return {}
    d = dict(record)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, (bytes, memoryview)):
            d[k] = None
    return d


class ApproveBody(BaseModel):
    session_name: str


class BulkAssignBody(BaseModel):
    ids: list[int]
    session_name: str


@router.get("/stats")
async def get_links_stats() -> dict[str, Any]:
    try:
        total_discovered = await database.fetchval(
            "SELECT COUNT(*) FROM link_discovery.discovered_links"
        )
        queued_joins = await database.fetchval(
            "SELECT COUNT(*) FROM link_discovery.join_queue WHERE status = 'pending'"
        )
        unassigned = await database.fetchval(
            "SELECT COUNT(*) FROM link_discovery.join_queue "
            "WHERE status = 'pending' AND session_name IS NULL"
        )
        processed = await database.fetchval(
            "SELECT COUNT(*) FROM link_discovery.join_queue WHERE status = 'processed'"
        )
        failed = await database.fetchval(
            "SELECT COUNT(*) FROM link_discovery.join_queue WHERE status = 'failed'"
        )
        return {
            "total_discovered": total_discovered or 0,
            "queued_joins": queued_joins or 0,
            "unassigned": unassigned or 0,
            "processed": processed or 0,
            "failed": failed or 0,
            "error": None,
        }
    except Exception as exc:
        logger.error("get_links_stats_error: %s", exc)
        return {
            "total_discovered": 0, "queued_joins": 0, "unassigned": 0,
            "processed": 0, "failed": 0,
            "error": str(exc),
        }


@router.get("/queue")
async def get_join_queue() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT id, link, session_name, status, source, added_at, processed_at, error "
            "FROM link_discovery.join_queue "
            "WHERE status = 'pending' ORDER BY added_at DESC LIMIT 100"
        )
        return {"items": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_join_queue_error: %s", exc)
        return {"items": [], "error": str(exc)}


@router.post("/queue/{item_id}/approve")
async def approve_queue_item(item_id: int, body: ApproveBody) -> dict[str, Any]:
    try:
        await database.execute(
            "UPDATE link_discovery.join_queue "
            "SET status = 'queued', session_name = $1 "
            "WHERE id = $2",
            body.session_name,
            item_id,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        logger.error("approve_queue_item_error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.get("/sessions")
async def get_active_sessions() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT id, session_name, phone_jid, display_name, status, last_connected "
            "FROM collector.wa_sessions WHERE status = 'active' ORDER BY session_name"
        )
        return {"sessions": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_active_sessions_error: %s", exc)
        return {"sessions": [], "error": str(exc)}


@router.post("/bulk-assign")
async def bulk_assign(body: BulkAssignBody) -> dict[str, Any]:
    try:
        if not body.ids:
            return {"updated": 0, "error": None}
        # Use ANY for bulk update
        count = await database.fetchval(
            "WITH updated AS ("
            "  UPDATE link_discovery.join_queue "
            "  SET status = 'queued', session_name = $1 "
            "  WHERE id = ANY($2::int[]) AND status = 'pending' "
            "  RETURNING id"
            ") SELECT COUNT(*) FROM updated",
            body.session_name,
            body.ids,
        )
        return {"updated": int(count or 0), "error": None}
    except Exception as exc:
        logger.error("bulk_assign_error: %s", exc)
        return {"updated": 0, "error": str(exc)}
