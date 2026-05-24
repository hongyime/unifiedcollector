"""
backend/routers/bulk.py — Bulk sender service data endpoints.
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


class CreateJobBody(BaseModel):
    session_name: str
    mode: str  # "broadcast" | "sequential"
    source_type: str  # "path" | "query"
    source_path: str | None = None
    collector_query: dict | None = None
    targets: list[str] = []  # list of chat JIDs


@router.get("/stats")
async def get_bulk_stats() -> dict[str, Any]:
    try:
        pending = await database.fetchval(
            "SELECT COUNT(*) FROM bulk_sender.send_jobs WHERE status = 'pending'"
        )
        running = await database.fetchval(
            "SELECT COUNT(*) FROM bulk_sender.send_jobs WHERE status = 'running'"
        )
        completed = await database.fetchval(
            "SELECT COUNT(*) FROM bulk_sender.send_jobs WHERE status = 'completed'"
        )
        failed = await database.fetchval(
            "SELECT COUNT(*) FROM bulk_sender.send_jobs WHERE status = 'failed'"
        )
        total_sent = await database.fetchval(
            "SELECT COALESCE(SUM(sent_count), 0) FROM bulk_sender.send_jobs"
        )
        return {
            "pending": pending or 0,
            "running": running or 0,
            "completed": completed or 0,
            "failed": failed or 0,
            "total_sent": int(total_sent or 0),
            "error": None,
        }
    except Exception as exc:
        logger.error("get_bulk_stats_error: %s", exc)
        return {
            "pending": 0, "running": 0, "completed": 0, "failed": 0,
            "total_sent": 0, "error": str(exc),
        }


@router.get("/jobs")
async def get_bulk_jobs(limit: int = 25) -> dict[str, Any]:
    try:
        limit = min(limit, 100)
        rows = await database.fetchall(
            "SELECT id, session_name, mode, source_type, source_path, status, "
            "operator_confirmed, total_files, sent_count, created_at, updated_at "
            "FROM bulk_sender.send_jobs ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return {"jobs": [_record_to_dict(r) for r in rows], "error": None}
    except Exception as exc:
        logger.error("get_bulk_jobs_error: %s", exc)
        return {"jobs": [], "error": str(exc)}


@router.post("/jobs")
async def create_bulk_job(body: CreateJobBody) -> dict[str, Any]:
    try:
        import json
        collector_query_json = json.dumps(body.collector_query) if body.collector_query else None

        # Single transaction: crash between job INSERT and targets INSERT would
        # leave a job the sender picks up with no targets → silent no-op job.
        async with database.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO bulk_sender.send_jobs "
                    "(session_name, mode, source_type, source_path, collector_query, status) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb, 'pending') "
                    "RETURNING id, created_at",
                    body.session_name,
                    body.mode,
                    body.source_type,
                    body.source_path,
                    collector_query_json,
                )
                job_id = row["id"]
                if body.targets:
                    await conn.execute(
                        "INSERT INTO bulk_sender.send_targets (job_id, chat_jid) "
                        "SELECT $1, unnest($2::text[])",
                        job_id,
                        body.targets,
                    )

        return {"job_id": job_id, "created_at": row["created_at"].isoformat(), "error": None}
    except Exception as exc:
        logger.error("create_bulk_job_error: %s", exc)
        return {"job_id": None, "error": str(exc)}
