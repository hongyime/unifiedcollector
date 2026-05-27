"""
backend/routers/users.py — User intelligence service data endpoints.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

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


@router.get("/stats")
async def get_user_stats() -> dict[str, Any]:
    try:
        total_users = await database.fetchval("SELECT COUNT(*) FROM collector.users")
        changes_today = await database.fetchval(
            "SELECT COUNT(*) FROM user_intelligence.user_history "
            "WHERE changed_at >= NOW() - INTERVAL '24 hours'"
        )
        connections = await database.fetchval(
            "SELECT COUNT(*) FROM user_intelligence.user_connections"
        )
        memberships = await database.fetchval(
            "SELECT COUNT(DISTINCT user_jid) FROM user_intelligence.user_chat_memberships"
        )
        top_chat = await database.fetchone(
            "SELECT chat_jid, COUNT(*) as member_count "
            "FROM user_intelligence.user_chat_memberships "
            "GROUP BY chat_jid ORDER BY member_count DESC LIMIT 1"
        )
        return {
            "total_users": total_users or 0,
            "changes_today": changes_today or 0,
            "connections": connections or 0,
            "tracked_memberships": memberships or 0,
            "top_chat": _record_to_dict(top_chat) if top_chat else None,
            "error": None,
        }
    except Exception as exc:
        logger.error("get_user_stats_error: %s", exc)
        return {
            "total_users": 0, "changes_today": 0, "connections": 0,
            "tracked_memberships": 0, "top_chat": None,
            "error": str(exc),
        }


@router.get("/search")
async def search_users(q: str = "") -> dict[str, Any]:
    try:
        q = q[:200]  # cap length — long ILIKE patterns cause CPU spikes
        if not q or len(q) < 2:
            rows = await database.fetchall(
                "SELECT jid, phone_number, display_name, push_name, business_name, "
                "is_business, is_verified, first_seen, last_seen "
                "FROM collector.users ORDER BY last_seen DESC LIMIT 50"
            )
        else:
            pattern = f"%{q}%"
            rows = await database.fetchall(
                "SELECT jid, phone_number, display_name, push_name, business_name, "
                "is_business, is_verified, first_seen, last_seen "
                "FROM collector.users "
                "WHERE jid ILIKE $1 OR display_name ILIKE $1 "
                "OR push_name ILIKE $1 OR phone_number ILIKE $1 "
                "ORDER BY last_seen DESC LIMIT 50",
                pattern,
            )
        return {"users": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("search_users_error: %s", exc)
        return {"users": [], "error": str(exc)}


@router.get("/{jid}/history")
async def get_user_history(jid: str) -> dict[str, Any]:
    try:
        # Decode any URL encoding in jid
        from urllib.parse import unquote
        jid = unquote(jid)

        profile_history = await database.fetchall(
            "SELECT id, user_jid, field_name, old_value, new_value, changed_at "
            "FROM user_intelligence.user_history "
            "WHERE user_jid = $1 ORDER BY changed_at DESC LIMIT 100",
            jid,
        )
        memberships = await database.fetchall(
            "SELECT user_jid, chat_jid, first_seen, last_seen, message_count "
            "FROM user_intelligence.user_chat_memberships "
            "WHERE user_jid = $1 ORDER BY last_seen DESC",
            jid,
        )
        user = await database.fetchone(
            "SELECT jid, phone_number, display_name, push_name, business_name, "
            "is_business, is_verified, first_seen, last_seen "
            "FROM collector.users WHERE jid = $1",
            jid,
        )
        return {
            "user": _record_to_dict(user),
            "profile_history": [_record_to_dict(r) for r in profile_history],
            "memberships": [_record_to_dict(r) for r in memberships],
            "error": None,
        }
    except Exception as exc:
        logger.error("get_user_history_error: %s", exc)
        return {"user": {}, "profile_history": [], "memberships": [], "error": str(exc)}
