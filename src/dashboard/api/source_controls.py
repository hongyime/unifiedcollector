"""source_controls route handlers for dashboard.

Exposes Fudan-pattern source health states for the dashboard.
Shows "blocked until X" for RATE_LIMITED instead of silent "stale".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.core.source_controls import list_source_controls as _list_source_controls
from src.core.recovery_profiles import list_recovery_profiles
from src.dashboard.api.auth import require_role

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/source-controls", dependencies=[Depends(require_role("viewer"))])
async def list_source_controls():
    """List all source controls with their states.

    Returns source health including blocked_until for RATE_LIMITED sources,
    so the dashboard can show "blocked until X" instead of silent "stale".
    """
    return await _list_source_controls(await get_pool())


@router.get("/api/source-recovery", dependencies=[Depends(require_role("viewer"))])
async def source_recovery():
    """Show durable restriction profiles separately from manual source pauses."""
    return await list_recovery_profiles(await get_pool())
