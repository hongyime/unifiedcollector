"""``coverage`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.core.collection_coverage import build_collection_coverage_snapshot, get_collection_coverage


def _lookup(name: str):
    """Read a name from the parent api module at call time (for test patches)."""
    import sys as _sys
    root = _sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


def _get_helper(name: str, fallback=None):
    """Return the parent-api helper or a caller-supplied fallback."""
    val = _lookup(name)
    return val if val is not None else fallback


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


@router.get("/coverage/collectors")
async def collectors_coverage(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    return await get_collection_coverage(
        pool,
        stale_seconds=_lookup("_COVERAGE_SNAPSHOT_STALE_SECONDS") or 3600,
        refresh=_lookup("build_collection_coverage_snapshot") or build_collection_coverage_snapshot,
    )


