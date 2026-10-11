"""source_controls route handlers for dashboard.

Exposes Fudan-pattern source health states for the dashboard.
Shows "blocked until X" for RATE_LIMITED instead of silent "stale".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/source-controls", dependencies=[Depends(require_role("viewer"))])
async def list_source_controls():
    """List all source controls with their states.

    Returns source health including blocked_until for RATE_LIMITED sources,
    so the dashboard can show "blocked until X" instead of silent "stale".
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source_name, state, enabled, last_success, last_attempt,
                   blocked_until, stop_reason, consecutive_failures, updated_at
            FROM source_controls
            ORDER BY source_name
            """
        )
        return [
            {
                "source_name": r["source_name"],
                "state": r["state"],
                "enabled": r["enabled"],
                "last_success": r["last_success"].isoformat() if r["last_success"] else None,
                "last_attempt": r["last_attempt"].isoformat() if r["last_attempt"] else None,
                "blocked_until": r["blocked_until"].isoformat() if r["blocked_until"] else None,
                "stop_reason": r["stop_reason"],
                "consecutive_failures": r["consecutive_failures"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
