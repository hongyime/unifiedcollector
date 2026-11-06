"""Collection targets CRUD routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 17
(cluster 3a). Covers:

* ``POST /targets``
* ``DELETE /targets/{target_id}``
* ``GET /targets``
"""
from __future__ import annotations

import logging
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.core.target_status import (
    TargetAlreadyKnown,
    add_collection_target,
    list_collection_targets,
    target_already_known as _target_already_known,
)

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


class TargetRequest(BaseModel):
    source: str
    target: str
    priority: int = 0


router = APIRouter()


@router.post("/targets")
async def create_target(
    req: TargetRequest,
    force: bool = False,
    _user: dict = Depends(require_role("operator")),
):
    pool = await _get_pool()
    try:
        result = await add_collection_target(pool, req.source, req.target, priority=req.priority, force=force)
    except TargetAlreadyKnown as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "already_discovered", "source": exc.source, "target_id": exc.target_id, **exc.detail},
        ) from exc
    return {"status": "ok", **result}


@router.delete("/targets/{target_id}")
async def delete_target(target_id: int, _user: dict = Depends(require_role("admin"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM collection_targets WHERE id = $1", target_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Target not found")
    return {"status": "deleted"}


@router.get("/targets")
async def list_targets(source: str | None = None,
                        _user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    return await list_collection_targets(pool, source=source)
