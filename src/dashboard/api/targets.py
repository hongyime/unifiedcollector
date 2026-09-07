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


async def _target_already_known(conn, source: str, target_id: str) -> dict | None:
    """Return {discovered_via, last_seen} if target already in spider data, else None."""
    queries = {
        'github': "SELECT login AS hit FROM github_users WHERE login=$1 OR platform_user_id::text=$1 LIMIT 1",
        'instagram': "SELECT username AS hit FROM instagram_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'telegram': "SELECT username AS hit FROM telegram_users WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'lemon8': "SELECT username AS hit FROM lemon8_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'strava': "SELECT platform_athlete_id::text AS hit FROM strava_athletes WHERE platform_athlete_id::text=$1 LIMIT 1",
        'tiktok': "SELECT username AS hit FROM tiktok_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        'whatsapp': "SELECT platform_user_id AS hit FROM whatsapp_users WHERE platform_user_id=$1 LIMIT 1",
        'website': "SELECT domain AS hit FROM website_targets WHERE domain=$1 LIMIT 1",
    }
    q = queries.get(source)
    if not q:
        return None  # youtube, search - no profile table or no dedupe applicable
    try:
        row = await conn.fetchrow(q, target_id)
    except Exception:
        return None  # table missing - don't block
    if not row:
        return None
    try:
        parent = await conn.fetchval(
            "SELECT parent_node_id FROM spider_queue WHERE platform=$1 AND node_id=$2 AND parent_node_id IS NOT NULL LIMIT 1",
            source, target_id,
        )
    except Exception:
        parent = None
    try:
        last_seen = await conn.fetchval(
            "SELECT last_attempted_at FROM spider_queue WHERE platform=$1 AND node_id=$2 LIMIT 1",
            source, target_id,
        )
    except Exception:
        last_seen = None
    return {'discovered_via': parent, 'last_seen': last_seen.isoformat() if last_seen else None}


@router.post("/targets")
async def create_target(
    req: TargetRequest,
    force: bool = False,
    _user: dict = Depends(require_role("operator")),
):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if not force:
            # Prefer the parent-module version so test monkey-patches on
            # ``dashboard_api._target_already_known`` continue to apply.
            fn = _lookup("_target_already_known") or _target_already_known
            already = await fn(conn, req.source, req.target)
            if already is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        'code': 'already_discovered',
                        'source': req.source,
                        'target_id': req.target,
                        **already,
                    },
                )
        priority = req.priority + 5 if force else req.priority
        await conn.execute(
            "INSERT INTO collection_targets (source, target_id, priority) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            req.source, req.target, priority,
        )
    return {"status": "ok", "source": req.source, "target": req.target, "forced": force}


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
    async with pool.acquire() as conn:
        if source:
            rows = await conn.fetch(
                "SELECT * FROM collection_targets WHERE source = $1 ORDER BY priority DESC",
                source,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM collection_targets ORDER BY source, priority DESC"
            )
    return [dict(r) for r in rows]
