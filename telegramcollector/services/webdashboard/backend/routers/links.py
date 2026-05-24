from __future__ import annotations
import logging
from typing import Any
from fastapi import APIRouter
import database

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    try:
        total_links = await database.fetchval("SELECT COUNT(*) FROM link_discovery.discovered_links") or 0
        resolved    = await database.fetchval("SELECT COUNT(*) FROM link_discovery.discovered_links WHERE resolved=TRUE") or 0
        join_queue  = await database.fetchval("SELECT COUNT(*) FROM collector.group_join_queue WHERE status='pending'") or 0
        return {"total_links": total_links, "resolved": resolved, "join_queue": join_queue, "error": None}
    except Exception as e:
        logger.error("links_stats_error: %s", e)
        return {"total_links": 0, "resolved": 0, "join_queue": 0, "error": str(e)}


@router.get("/list")
async def get_links() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT id, link_type, link_value, source_chat_id, resolved, discovered_at FROM link_discovery.discovered_links ORDER BY discovered_at DESC LIMIT 100")
        return {"links": rows, "error": None}
    except Exception as e:
        logger.error("links_list_error: %s", e)
        return {"links": [], "error": str(e)}


@router.get("/queue")
async def get_queue() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT id, peer_identifier, status, queued_at FROM collector.group_join_queue ORDER BY queued_at DESC LIMIT 30")
        return {"queue": rows, "error": None}
    except Exception as e:
        logger.error("links_queue_error: %s", e)
        return {"queue": [], "error": str(e)}
