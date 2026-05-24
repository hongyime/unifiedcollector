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
        identities    = await database.fetchval("SELECT COUNT(*) FROM face_recognition.telegram_topics") or 0
        embeddings    = await database.fetchval("SELECT COUNT(*) FROM face_recognition.face_embeddings") or 0
        processed_24h = await database.fetchval("SELECT COUNT(*) FROM face_recognition.processed_media WHERE processed_at > NOW() - INTERVAL '24 hours'") or 0
        processed_total = await database.fetchval("SELECT COUNT(*) FROM face_recognition.processed_media") or 0
        return {"identities": identities, "embeddings": embeddings, "processed_24h": processed_24h, "processed_total": processed_total, "error": None}
    except Exception as e:
        logger.error("faces_stats_error: %s", e)
        return {"identities": 0, "embeddings": 0, "processed_24h": 0, "processed_total": 0, "error": str(e)}


@router.get("/identities")
async def get_identities() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT id, topic_id, label, face_count, message_count, created_at, updated_at FROM face_recognition.telegram_topics ORDER BY face_count DESC LIMIT 100")
        return {"identities": rows, "error": None}
    except Exception as e:
        logger.error("faces_identities_error: %s", e)
        return {"identities": [], "error": str(e)}


@router.get("/corrections")
async def get_corrections() -> dict[str, Any]:
    try:
        rows = await database.fetchall("SELECT fc.id, fc.from_topic_id, fc.to_topic_id, fc.corrected_at, t1.label AS from_label, t2.label AS to_label FROM face_recognition.identity_corrections fc LEFT JOIN face_recognition.telegram_topics t1 ON t1.id=fc.from_topic_id LEFT JOIN face_recognition.telegram_topics t2 ON t2.id=fc.to_topic_id ORDER BY fc.corrected_at DESC LIMIT 20")
        return {"corrections": rows, "error": None}
    except Exception as e:
        logger.error("faces_corrections_error: %s", e)
        return {"corrections": [], "error": str(e)}
