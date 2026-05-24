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
        total_users  = await database.fetchval("SELECT COUNT(*) FROM collector.users") or 0
        bots         = await database.fetchval("SELECT COUNT(*) FROM collector.users WHERE is_bot=TRUE") or 0
        premium      = await database.fetchval("SELECT COUNT(*) FROM collector.users WHERE is_premium=TRUE") or 0
        sightings_24h = await database.fetchval("SELECT COUNT(*) FROM collector.user_sightings WHERE seen_at > NOW() - INTERVAL '24 hours'") or 0
        return {"total_users": total_users, "bots": bots, "premium": premium, "sightings_24h": sightings_24h, "error": None}
    except Exception as e:
        logger.error("users_stats_error: %s", e)
        return {"total_users": 0, "bots": 0, "premium": 0, "sightings_24h": 0, "error": str(e)}


@router.get("/list")
async def get_users() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT id, username, first_name, last_name, phone, is_bot, is_verified, is_premium, first_seen, last_seen FROM collector.users ORDER BY last_seen DESC LIMIT 100")
        return {"users": rows, "error": None}
    except Exception as e:
        logger.error("users_list_error: %s", e)
        return {"users": [], "error": str(e)}
