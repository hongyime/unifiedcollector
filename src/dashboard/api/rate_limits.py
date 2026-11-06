"""``rate_limits`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _acquire_dashboard_conn,
    _release_dashboard_conn,
    _copy_cache_value,
)
from src.core.rate_limit_status import get_recent_rate_limits


def _rate_limits_recent_fallback_payload(*args, **kwargs):
    """Late proxy to __init__.py helper for tests + caller compatibility."""
    from src.dashboard.api import _rate_limits_recent_fallback_payload as _impl
    return _impl(*args, **kwargs)


# _RATE_LIMITS_RECENT_CACHE lives in __init__.py; access via _lookup at call time.

logger = logging.getLogger(__name__)


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


@router.get("/rate-limits/recent")
async def recent_rate_limits(hours: int = 24, limit: int = 100,
                             _user: dict = Depends(require_role("viewer"))):
    hours = max(1, min(hours, 168))
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
        payload = await get_recent_rate_limits(conn, hours=hours, limit=limit)
    except Exception as exc:  # noqa: BLE001 - dashboard status panels must fail soft
        logger.warning("recent rate limits failed: %s", exc.__class__.__name__)
        return _rate_limits_recent_fallback_payload(hours, limit, exc)
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "operational events")
    (_lookup("_RATE_LIMITS_RECENT_CACHE") or {})[(hours, limit)] = {
        "ts": time.time(),
        "payload": _copy_cache_value(payload),
    }
    return payload


