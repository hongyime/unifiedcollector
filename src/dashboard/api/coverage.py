"""``coverage`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.core.collection_coverage import build_collection_coverage_snapshot
from src.dashboard.api.helpers import _row_get

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


@router.get("/coverage/collectors")
async def collectors_coverage(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async def fetch_latest():
            return await conn.fetch(
                """
                SELECT DISTINCT ON (source)
                       *, expected_cadence::text AS expected_cadence_text
                FROM collection_coverage_snapshots
                ORDER BY source, created_at DESC
                """
            )

        rows = await fetch_latest()
        latest_created_at = max((row["created_at"] for row in rows if row["created_at"]), default=None)
        snapshot_age_seconds = None
        if latest_created_at:
            snapshot_age_seconds = int((datetime.now(timezone.utc) - latest_created_at.astimezone(timezone.utc)).total_seconds())
        refresh_attempted = False
        refresh_error = None
        if not rows or snapshot_age_seconds is None or snapshot_age_seconds > (_lookup("_COVERAGE_SNAPSHOT_STALE_SECONDS") or 3600):
            refresh_attempted = True
            try:
                await (_lookup("build_collection_coverage_snapshot") or build_collection_coverage_snapshot)(conn)
                rows = await fetch_latest()
                latest_created_at = max((row["created_at"] for row in rows if row["created_at"]), default=None)
                if latest_created_at:
                    snapshot_age_seconds = int((datetime.now(timezone.utc) - latest_created_at.astimezone(timezone.utc)).total_seconds())
            except Exception as exc:  # noqa: BLE001 - dashboard should still serve cached rows.
                refresh_error = str(exc)[:300]
                logger.warning("coverage snapshot refresh failed: %s", refresh_error)
    payload = []
    for row in rows:
        payload.append({
            "source": row["source"],
            "expected_cadence": _row_get(row, "expected_cadence_text", row["expected_cadence"]),
            "latest_data_at": row["latest_data_at"].isoformat() if row["latest_data_at"] else None,
            "latest_run_at": row["latest_run_at"].isoformat() if row["latest_run_at"] else None,
            "status": row["status"],
            "rows_24h": row["rows_24h"],
            "media_24h": row["media_24h"],
            "errors_24h": row["errors_24h"],
            "rate_limits_24h": row["rate_limits_24h"],
            "private_access_failures": row["private_access_failures"],
            "stale_targets": row["stale_targets"],
            "seen_targets_total": int(_row_get(row, "seen_targets_total", 0) or 0),
            "seen_targets_backfilled": int(_row_get(row, "seen_targets_backfilled", 0) or 0),
            "seen_targets_pending": int(_row_get(row, "seen_targets_pending", 0) or 0),
            "seen_targets_fresh": int(_row_get(row, "seen_targets_fresh", 0) or 0),
            "seen_targets_stale": int(_row_get(row, "seen_targets_stale", 0) or 0),
            "seen_targets_newly_discovered": int(_row_get(row, "seen_targets_newly_discovered", 0) or 0),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })
    fresh = sum(1 for row in payload if row["status"] == "fresh")
    degraded = sum(1 for row in payload if row["status"] == "degraded")
    stale = sum(1 for row in payload if row["status"] == "stale")
    unknown = sum(1 for row in payload if row["status"] not in {"fresh", "degraded", "stale"})
    return {
        "sources": payload,
        "total": len(payload),
        "summary": {"total": len(payload), "fresh": fresh, "degraded": degraded, "stale": stale, "unknown": unknown},
        "snapshot_created_at": latest_created_at.isoformat() if latest_created_at else None,
        "snapshot_age_seconds": snapshot_age_seconds,
        "snapshot_stale": bool(snapshot_age_seconds is not None and snapshot_age_seconds > (_lookup("_COVERAGE_SNAPSHOT_STALE_SECONDS") or 3600)),
        "refresh_attempted": refresh_attempted,
        "refresh_error": refresh_error,
    }


