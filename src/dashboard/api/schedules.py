"""Collection schedules and runs routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 17
(cluster 3b). Covers:

* ``GET /schedules``
* ``POST /schedules``
* ``PUT /schedules/{source}``
* ``DELETE /schedules/{source}``
* ``GET /runs``
* ``GET /runs/{run_id}``

The ``_enrich_runs_with_ingestion`` and ``_collection_run_payload`` helpers
still live in ``__init__.py`` and are looked up at call time to preserve
test monkey-patch semantics.
"""
from __future__ import annotations

import logging
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.core.schedule_status import list_collection_schedules, set_collection_schedule

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


class ScheduleRequest(BaseModel):
    source: str
    interval_hours: int = Field(default=24, ge=1, le=720)
    enabled: bool = True


router = APIRouter()


@router.put("/schedules/{source}")
async def update_schedule(source: str, req: ScheduleRequest, _user: dict = Depends(require_role("operator"))):
    pool = await _get_pool()
    return {"status": "ok", **await set_collection_schedule(
        pool, source, interval_hours=req.interval_hours, enabled=req.enabled,
    )}


@router.get("/runs/{run_id}")
async def get_run(run_id: int, _user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM collection_runs WHERE id = $1", run_id,
        )
        _enrich = _lookup("_enrich_runs_with_ingestion")
        _payload = _lookup("_collection_run_payload")
        rows = await _enrich(conn, [_payload(row)] if row else [])
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return rows[0]


@router.get("/schedules")
async def list_schedules(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    return await list_collection_schedules(pool)


@router.post("/schedules")
async def create_schedule(req: ScheduleRequest, _user: dict = Depends(require_role("operator"))):
    pool = await _get_pool()
    return {"status": "ok", **await set_collection_schedule(
        pool, req.source, interval_hours=req.interval_hours, enabled=True,
    )}


@router.delete("/schedules/{source}")
async def delete_schedule(source: str, _user: dict = Depends(require_role("admin"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM collection_schedules WHERE source = $1", source,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"No schedule for {source}")
    return {"status": "deleted", "source": source}


@router.get("/runs")
async def list_runs(source: str | None = None, limit: int = 20,
                     _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT * FROM collection_runs WHERE source = $1 "
                "ORDER BY started_at DESC LIMIT $2",
                source, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT $1",
                limit,
            )
        _enrich = _lookup("_enrich_runs_with_ingestion")
        _payload = _lookup("_collection_run_payload")
        out = await _enrich(conn, [_payload(r) for r in rows])
    return out
