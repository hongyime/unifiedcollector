"""
backend/routers/faces.py — Face recognition service data endpoints.
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
        elif hasattr(v, "__class__") and v.__class__.__name__ == "UUID":
            d[k] = str(v)
    return d


@router.get("/stats")
async def get_faces_stats() -> dict[str, Any]:
    try:
        identities = await database.fetchval(
            "SELECT COUNT(*) FROM face_recognition.identity_entities"
        )
        embeddings = await database.fetchval(
            "SELECT COUNT(*) FROM face_recognition.face_embeddings WHERE is_valid = TRUE"
        )
        processed = await database.fetchval(
            "SELECT COUNT(*) FROM face_recognition.processed_media"
        )
        findings = await database.fetchval(
            "SELECT COUNT(*) FROM face_recognition.published_findings"
        )
        unassigned = await database.fetchval(
            "SELECT COUNT(*) FROM face_recognition.face_embeddings WHERE identity_id IS NULL"
        )
        return {
            "identities": identities or 0,
            "embeddings": embeddings or 0,
            "processed_media": processed or 0,
            "published_findings": findings or 0,
            "unassigned_embeddings": unassigned or 0,
            "error": None,
        }
    except Exception as exc:
        logger.error("get_faces_stats_error: %s", exc)
        return {
            "identities": 0, "embeddings": 0, "processed_media": 0,
            "published_findings": 0, "unassigned_embeddings": 0,
            "error": str(exc),
        }


@router.get("/identities")
async def get_identities(limit: int = 50, sort: str = "last_seen") -> dict[str, Any]:
    try:
        limit = min(limit, 200)
        # CASE-based ORDER BY: fully parameterized, no f-string interpolation.
        # NULL arms in non-matching branches collapse to NULLS LAST and are ignored.
        rows = await database.fetchall(
            """
            SELECT id, label, occurrence_count, first_seen, last_seen
            FROM face_recognition.identity_entities
            ORDER BY
              CASE WHEN $2 = 'occurrence_count' THEN occurrence_count  END DESC NULLS LAST,
              CASE WHEN $2 = 'first_seen'       THEN EXTRACT(EPOCH FROM first_seen) END DESC NULLS LAST,
              CASE WHEN $2 NOT IN ('occurrence_count','first_seen')
                   THEN EXTRACT(EPOCH FROM last_seen) END DESC NULLS LAST
            LIMIT $1
            """,
            limit,
            sort,
        )
        return {"identities": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_identities_error: %s", exc)
        return {"identities": [], "error": str(exc)}


@router.get("/embeddings")
async def get_embeddings(identity_id: str | None = None) -> dict[str, Any]:
    try:
        if identity_id:
            rows = await database.fetchall(
                "SELECT id, identity_id, source_message_id, source_chat_jid, "
                "frame_index, is_valid, created_at "
                "FROM face_recognition.face_embeddings "
                "WHERE identity_id = $1::uuid "
                "ORDER BY created_at DESC LIMIT 100",
                identity_id,
            )
        else:
            rows = await database.fetchall(
                "SELECT id, identity_id, source_message_id, source_chat_jid, "
                "frame_index, is_valid, created_at "
                "FROM face_recognition.face_embeddings "
                "ORDER BY created_at DESC LIMIT 50"
            )
        return {"embeddings": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_embeddings_error: %s", exc)
        return {"embeddings": [], "error": str(exc)}
