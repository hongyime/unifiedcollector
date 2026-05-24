from __future__ import annotations
import logging
import os
from typing import Any
from fastapi import APIRouter
import database

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    try:
        messages_total = await database.fetchval("SELECT COUNT(*) FROM collector.raw_messages") or 0
        messages_5m    = await database.fetchval("SELECT COUNT(*) FROM collector.raw_messages WHERE collected_at > NOW() - INTERVAL '5 minutes'") or 0
        messages_1h    = await database.fetchval("SELECT COUNT(*) FROM collector.raw_messages WHERE collected_at > NOW() - INTERVAL '1 hour'") or 0
        accounts       = await database.fetchval("SELECT COUNT(*) FROM collector.telegram_accounts WHERE status='active'") or 0
        media_24h      = await database.fetchval("SELECT COUNT(*) FROM collector.raw_messages WHERE has_media=TRUE AND collected_at > NOW() - INTERVAL '24 hours'") or 0
        chats          = await database.fetchval("SELECT COUNT(*) FROM collector.chats") or 0
        return {"messages_total": messages_total, "messages_5m": messages_5m, "messages_1h": messages_1h, "accounts": accounts, "media_24h": media_24h, "chats": chats, "error": None}
    except Exception as e:
        logger.error("collector_stats_error: %s", e)
        return {"messages_total": 0, "messages_5m": 0, "messages_1h": 0, "accounts": 0, "media_24h": 0, "chats": 0, "error": str(e)}


@router.get("/accounts")
async def get_accounts() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT id, phone_number, display_name, status, session_file_path, last_active, created_at, last_error FROM collector.telegram_accounts ORDER BY created_at DESC")
        return {"accounts": rows, "error": None}
    except Exception as e:
        logger.error("collector_accounts_error: %s", e)
        return {"accounts": [], "error": str(e)}


@router.get("/backfill")
async def get_backfill() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT bj.id, bj.account_id, bj.chat_id, bj.status, bj.messages_done, bj.error, bj.created_at, bj.updated_at, ta.phone_number FROM collector.backfill_jobs bj LEFT JOIN collector.telegram_accounts ta ON ta.id=bj.account_id ORDER BY bj.created_at DESC LIMIT 50")
        return {"jobs": rows, "error": None}
    except Exception as e:
        logger.error("collector_backfill_error: %s", e)
        return {"jobs": [], "error": str(e)}


@router.get("/cursors")
async def get_cursors() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT service_name, last_message_id, updated_at FROM collector.service_cursors ORDER BY service_name")
        return {"cursors": rows, "error": None}
    except Exception as e:
        logger.error("collector_cursors_error: %s", e)
        return {"cursors": [], "error": str(e)}


@router.get("/recent-messages")
async def get_recent_messages() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT id, chat_id, message_id, sender_id, message_type, has_media, collected_at FROM collector.raw_messages ORDER BY id DESC LIMIT 30")
        return {"messages": rows, "error": None}
    except Exception as e:
        logger.error("collector_messages_error: %s", e)
        return {"messages": [], "error": str(e)}
