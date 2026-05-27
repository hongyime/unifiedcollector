"""
backend/routers/collector.py — Collector service data endpoints.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter

import database
from config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _record_to_dict(record) -> dict:
    """Convert asyncpg Record to a plain dict, stringifying non-serializable types."""
    if record is None:
        return {}
    d = dict(record)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, (bytes, memoryview)):
            d[k] = None
    return d


@router.get("/sessions")
async def get_sessions() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT id, session_name, phone_jid, display_name, status, "
            "last_connected, cooldown_until, created_at "
            "FROM collector.wa_sessions ORDER BY id"
        )
        return {"sessions": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_sessions_error: %s", exc)
        return {"sessions": [], "error": str(exc)}


@router.post("/sessions/{name}/request-backfill")
async def request_backfill(name: str) -> dict[str, Any]:
    """Forward a backfill request to the relevant wa-client bridge."""
    settings = get_settings()
    url_map = settings.wa_client_url_map

    # Find the URL for this session
    base_url = None
    for session_name, url in url_map.items():
        if session_name == name or session_name.endswith(name):
            base_url = url
            break

    if not base_url:
        # Default to first available
        if url_map:
            base_url = next(iter(url_map.values()))
        else:
            return {"ok": False, "error": "No wa-client URL configured"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/backfill/request",
                json={"session_name": name},
            )
            return {"ok": resp.status_code < 400, "status_code": resp.status_code, "error": None}
    except Exception as exc:
        logger.error("request_backfill_error: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.get("/backfill")
async def get_backfill_jobs() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT id, session_name, chat_jid, status, oldest_msg_ts, "
            "messages_done, cutoff_date, created_at, updated_at "
            "FROM collector.backfill_jobs "
            "ORDER BY created_at DESC LIMIT 50"
        )
        return {"jobs": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_backfill_error: %s", exc)
        return {"jobs": [], "error": str(exc)}


@router.get("/dlq-depth")
async def get_dlq_depth() -> dict[str, Any]:
    """Query RabbitMQ management API for DLQ depth."""
    settings = get_settings()
    mgmt_url = settings.rabbitmq_management_url
    try:
        auth = (settings.rabbitmq_user, settings.rabbitmq_password)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{mgmt_url}/api/queues/%2F/dlq.failed",
                auth=auth,
            )
            if resp.status_code == 200:
                data = resp.json()
                depth = data.get("messages", 0)
                return {"depth": depth, "error": None}
            # Try listing all queues and summing DLQ-looking ones
            resp2 = await client.get(f"{mgmt_url}/api/queues", auth=auth)
            if resp2.status_code == 200:
                queues = resp2.json()
                dlq_depth = sum(
                    q.get("messages", 0)
                    for q in queues
                    if "dead" in q.get("name", "").lower() or "dlq" in q.get("name", "").lower()
                )
                return {"depth": dlq_depth, "queues": len(queues), "error": None}
            return {"depth": 0, "error": f"HTTP {resp2.status_code}"}
    except Exception as exc:
        logger.debug("get_dlq_depth_error: %s", exc)
        return {"depth": 0, "error": str(exc)}


@router.get("/cursors")
async def get_cursors() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT service_name, last_message_id, updated_at "
            "FROM collector.service_cursors ORDER BY service_name"
        )
        return {"cursors": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_cursors_error: %s", exc)
        return {"cursors": [], "error": str(exc)}


@router.get("/stats")
async def get_collector_stats() -> dict[str, Any]:
    try:
        msg_count = await database.fetchval("SELECT COUNT(*) FROM collector.raw_messages")
        user_count = await database.fetchval("SELECT COUNT(*) FROM collector.users")
        chat_count = await database.fetchval("SELECT COUNT(*) FROM collector.chats")
        media_count = await database.fetchval(
            "SELECT COUNT(*) FROM collector.raw_messages WHERE has_media = TRUE"
        )
        today_count = await database.fetchval(
            "SELECT COUNT(*) FROM collector.raw_messages "
            "WHERE collected_at >= NOW() - INTERVAL '24 hours'"
        )
        return {
            "raw_messages": msg_count or 0,
            "users": user_count or 0,
            "chats": chat_count or 0,
            "media_messages": media_count or 0,
            "messages_last_24h": today_count or 0,
            "error": None,
        }
    except Exception as exc:
        logger.error("get_collector_stats_error: %s", exc)
        return {
            "raw_messages": 0,
            "users": 0,
            "chats": 0,
            "media_messages": 0,
            "messages_last_24h": 0,
            "error": str(exc),
        }


@router.get("/bootstrap")
async def get_bootstrap_state() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT key, value, updated_at FROM collector.system_config ORDER BY key"
        )
        return {"config": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_bootstrap_error: %s", exc)
        return {"config": [], "error": str(exc)}


@router.get("/sessions/{name}/qr")
async def get_session_qr(name: str) -> dict[str, Any]:
    """Proxy the QR code PNG from the wa-client bridge for a session."""
    settings = get_settings()
    url_map = settings.wa_client_url_map
    base_url = url_map.get(name) or (next(iter(url_map.values()), None) if url_map else None)
    if not base_url:
        return {"status": "unknown", "qr": None, "error": "No wa-client URL configured"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url}/qr")
            if resp.status_code == 200:
                data = resp.json()
                return {"status": data.get("status"), "qr": data.get("qr"), "error": None}
            return {"status": "unknown", "qr": None, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        logger.debug("get_session_qr_error session=%s err=%s", name, exc)
        return {"status": "unknown", "qr": None, "error": str(exc)}


@router.get("/recent-messages")
async def get_recent_messages() -> dict[str, Any]:
    try:
        rows = await database.fetchall(
            "SELECT id, message_id, chat_jid, chat_type, sender_jid, session_name, "
            "message_type, body, has_media, is_forwarded, is_deleted, collected_at "
            "FROM collector.raw_messages ORDER BY id DESC LIMIT 20"
        )
        return {"messages": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_recent_messages_error: %s", exc)
        return {"messages": [], "error": str(exc)}
