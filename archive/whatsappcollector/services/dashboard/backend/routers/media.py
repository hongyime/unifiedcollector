"""
backend/routers/media.py — Media archival service data endpoints.
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
async def get_media_stats() -> dict[str, Any]:
    try:
        total = await database.fetchval(
            "SELECT COUNT(*) FROM media_archival.media_files"
        )
        downloaded = await database.fetchval(
            "SELECT COUNT(*) FROM media_archival.media_files WHERE download_status = 'downloaded'"
        )
        pending = await database.fetchval(
            "SELECT COUNT(*) FROM media_archival.media_files WHERE download_status = 'pending'"
        )
        failed = await database.fetchval(
            "SELECT COUNT(*) FROM media_archival.media_files WHERE download_status = 'failed'"
        )
        expiring_soon = await database.fetchval(
            "SELECT COUNT(*) FROM media_archival.media_files "
            "WHERE expiry_at IS NOT NULL AND expiry_at < NOW() + INTERVAL '2 hours' "
            "AND expiry_at > NOW()"
        )
        total_size = await database.fetchval(
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM media_archival.media_files "
            "WHERE download_status = 'downloaded'"
        )
        # Pending from cursor perspective
        cursor_id = await database.fetchval(
            "SELECT last_message_id FROM collector.service_cursors "
            "WHERE service_name = 'media_archival'"
        ) or 0
        queue_depth = await database.fetchval(
            "SELECT COUNT(*) FROM collector.raw_messages "
            "WHERE has_media = TRUE AND id > $1",
            cursor_id,
        )
        return {
            "total": total or 0,
            "downloaded": downloaded or 0,
            "pending": pending or 0,
            "failed": failed or 0,
            "expiring_soon": expiring_soon or 0,
            "total_size_bytes": int(total_size or 0),
            "queue_depth": queue_depth or 0,
            "error": None,
        }
    except Exception as exc:
        logger.error("get_media_stats_error: %s", exc)
        return {
            "total": 0, "downloaded": 0, "pending": 0, "failed": 0,
            "expiring_soon": 0, "total_size_bytes": 0, "queue_depth": 0,
            "error": str(exc),
        }


@router.get("/queue")
async def get_media_queue(limit: int = 50) -> dict[str, Any]:
    try:
        limit = min(limit, 200)
        rows = await database.fetchall(
            "SELECT id, raw_message_id, message_id, chat_jid, mime_type, "
            "file_size_bytes, download_status, downloaded_at, expiry_at, collected_at "
            "FROM media_archival.media_files "
            "WHERE download_status = 'pending' "
            "ORDER BY collected_at DESC LIMIT $1",
            limit,
        )
        return {"items": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_media_queue_error: %s", exc)
        return {"items": [], "error": str(exc)}


@router.get("/expiring")
async def get_expiring_media(hours: int = 2) -> dict[str, Any]:
    try:
        hours = min(hours, 72)
        rows = await database.fetchall(
            "SELECT id, message_id, chat_jid, mime_type, file_size_bytes, "
            "download_status, expiry_at, by_id_path "
            "FROM media_archival.media_files "
            "WHERE expiry_at IS NOT NULL "
            "AND expiry_at < NOW() + ($1 || ' hours')::INTERVAL "
            "AND expiry_at > NOW() "
            "ORDER BY expiry_at ASC LIMIT 100",
            str(hours),
        )
        return {"items": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_expiring_media_error: %s", exc)
        return {"items": [], "error": str(exc)}
