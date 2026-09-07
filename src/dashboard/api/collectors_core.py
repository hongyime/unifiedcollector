"""Core collector-status routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 21
(cluster 2). Covers:

* ``/collectors`` — service_cursors listing
* ``/collectors/live`` — real per-source liveness
* ``/collectors/action-queue/sync`` — derive open collector actions
* ``/collectors/action-queue`` — list open/resolved actions
* ``/collectors/{source}`` — per-collector deep view

Cross-module helpers (``_collectors_live_fallback_payload``,
``_COLLECTORS_LIVE_CACHE``, ``_with_bridge_overrides``, and the source-matrix
route which is being extracted separately) are looked up at call time.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _COLLECTORS_LIVE_CACHE,
    _COLLECTORS_LIVE_CACHE_TTL_SECONDS,
    _acquire_dashboard_conn as _default_acquire,
    _release_dashboard_conn as _default_release,
    _copy_cache_value,
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


async def _acquire_dashboard_conn(pool):
    fn = _lookup("_acquire_dashboard_conn") or _default_acquire
    return await fn(pool)


async def _release_dashboard_conn(pool, conn, label: str = "dashboard"):
    fn = _lookup("_release_dashboard_conn") or _default_release
    return await fn(pool, conn, label)


router = APIRouter()


@router.get("/collectors")
async def list_collectors(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service, last_processed_id, last_processed_at, status "
            "FROM service_cursors ORDER BY service"
        )
    return [dict(r) for r in rows]


@router.get("/collectors/live")
async def collectors_live(_user: dict = Depends(require_role("viewer"))):
    """REAL per-source liveness from data freshness + source_health.

    service_cursors.status (used by /collectors) flips to 'idle' between cycles for
    healthy long-sleep collectors and is 'never' for realtime feeds, so counting
    status=='running' made healthy collectors look down (the "9/12" confusion). This
    reports true live/stale/degraded/dead per source from the actual data tables.
    """
    from src.core.source_freshness import compute_liveness
    _collectors_live_fallback_payload = _lookup("_collectors_live_fallback_payload")
    _with_bridge_overrides = _lookup("_with_bridge_overrides")
    pool = await _get_pool()
    now_ts = time.time()
    cached = _COLLECTORS_LIVE_CACHE.get("payload")
    cache_age = now_ts - float(_COLLECTORS_LIVE_CACHE.get("ts") or 0.0)
    if cached is not None and cache_age < _COLLECTORS_LIVE_CACHE_TTL_SECONDS:
        payload = _copy_cache_value(cached)
        payload["cache_age_seconds"] = int(cache_age)
        return payload
    conn = None
    try:
        conn = await _acquire_dashboard_conn(pool)
        sources = await asyncio.wait_for(compute_liveness(conn), timeout=18)
    except Exception as exc:  # noqa: BLE001 - status page must fail soft under DB load
        logger.warning("collectors live failed: %s", exc.__class__.__name__)
        return _collectors_live_fallback_payload(exc)
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "collectors live")
    try:
        sources, whatsapp_bridge_health = await asyncio.wait_for(
            _with_bridge_overrides(sources),
            timeout=3,
        )
    except Exception as exc:  # noqa: BLE001 - bridge override should not hang source liveness
        logger.warning("collectors live bridge override failed: %s", exc.__class__.__name__)
        whatsapp_bridge_health = {
            "summary": {
                "status": "unreachable",
                "detail": f"WhatsApp bridge health check failed: {exc.__class__.__name__}",
                "ready_count": 0,
                "reachable_count": 0,
                "total": 2,
            },
            "bridges": [],
        }
    live = sum(1 for s in sources if s["status"] == "live")
    degraded = sum(1 for s in sources if s["status"] in {"degraded", "stale", "unpaired", "unreachable"})
    payload = {
        "total": len(sources),
        "live": live,
        "degraded": degraded,
        "sources": sources,
        "whatsapp_bridge_health": whatsapp_bridge_health,
    }
    _COLLECTORS_LIVE_CACHE.update({"ts": time.time(), "payload": _copy_cache_value(payload)})
    return payload


@router.post("/collectors/action-queue/sync")
async def collectors_action_queue_sync(_user: dict = Depends(require_role("operator"))):
    from src.core.collection_action_queue import (
        resolve_stale_actions_from_direct_health,
        sync_collection_action_queue,
    )

    # Look up the source-matrix route handler on the parent module so tests
    # that monkey-patch it (or the sibling ``collectors_source_matrix`` used
    # via the source_matrix module) still route through the override.
    collectors_source_matrix = _lookup("collectors_source_matrix")
    source_matrix = await collectors_source_matrix(_user={}, force_refresh=True)
    cache = source_matrix.get("cache") if isinstance(source_matrix, dict) else None
    errors = source_matrix.get("errors") if isinstance(source_matrix, dict) else None
    non_evidentiary_sections = {
        "source_matrix",
        "current_content",
        "current_rate",
        "rolling_content",
        "previous_hour_content",
        "previous_hour_rate",
        "day_content",
        "day_rate",
        "media_totals",
        "active_cursors",
        "browser_extension",
        "db_acquire",
    }
    non_evidentiary_errors = any(
        isinstance(item, dict)
        and item.get("section") in non_evidentiary_sections
        and item.get("error") in {"TimeoutError", "CancelledError", "BuildInProgress", "Timeout", "PoolTimeout"}
        for item in (errors or [])
    )
    non_evidentiary_fallback = (
        isinstance(cache, dict)
        and cache.get("status") in {"unavailable", "refreshing"}
        and non_evidentiary_errors
    )
    source_matrix_db_acquire_failed = any(
        isinstance(item, dict)
        and item.get("section") == "db_acquire"
        for item in (errors or [])
    )
    skeleton_detail = "source matrix could not acquire a db connection quickly"
    skeleton_sources = [
        row for row in (source_matrix.get("sources") if isinstance(source_matrix, dict) else []) or []
        if skeleton_detail in str(row.get("detail") or row.get("source_health_error") or "").lower()
    ]
    skeleton_only_payload = bool(skeleton_sources) and len(skeleton_sources) == len(
        (source_matrix.get("sources") if isinstance(source_matrix, dict) else []) or []
    )
    if source_matrix_db_acquire_failed or skeleton_only_payload:
        non_evidentiary_fallback = True
    if non_evidentiary_fallback:
        return {
            "status": "skipped",
            "generated_at": source_matrix.get("generated_at"),
            "derived": 0,
            "open": None,
            "resolved": 0,
            "actions": [],
            "reason": "source_matrix_unavailable",
            "detail": "Skipped action-queue derivation because source-matrix returned a non-evidentiary fallback during DB/source-matrix pressure.",
        }
    pool = await _get_pool()
    conn = await _acquire_dashboard_conn(pool)
    try:
        result = await sync_collection_action_queue(conn, source_matrix)
        direct_resolved = await resolve_stale_actions_from_direct_health(
            conn,
            browser_extension=source_matrix.get("browser_extension") if isinstance(source_matrix, dict) else None,
        )
        if direct_resolved:
            result["resolved"] = int(result.get("resolved") or 0) + int(direct_resolved)
            result["open"] = max(0, int(result.get("open") or 0) - int(direct_resolved))
    finally:
        await _release_dashboard_conn(pool, conn, "collection action queue sync")
    return {
        "status": "ok",
        "generated_at": source_matrix.get("generated_at"),
        **result,
    }


@router.get("/collectors/action-queue")
async def collectors_action_queue(
    status: str = "open",
    limit: int = 50,
    _user: dict = Depends(require_role("viewer")),
):
    from src.core.collection_action_queue import ensure_collection_action_queue

    normalized_status = str(status or "open").strip().lower()
    if normalized_status not in {"open", "resolved", "all"}:
        normalized_status = "open"
    bounded_limit = min(max(int(limit or 50), 1), 200)
    pool = await _get_pool()
    conn = await _acquire_dashboard_conn(pool)
    try:
        await ensure_collection_action_queue(conn)
        if normalized_status == "all":
            rows = await conn.fetch(
                """
                SELECT source, action_type, scope_key, status, priority, reason,
                       evidence, first_seen_at, last_seen_at, resolved_at
                FROM collection_action_queue
                ORDER BY status ASC, priority ASC, last_seen_at DESC
                LIMIT $1
                """,
                bounded_limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT source, action_type, scope_key, status, priority, reason,
                       evidence, first_seen_at, last_seen_at, resolved_at
                FROM collection_action_queue
                WHERE status = $1
                ORDER BY priority ASC, last_seen_at DESC
                LIMIT $2
                """,
                normalized_status,
                bounded_limit,
            )
    finally:
        await _release_dashboard_conn(pool, conn, "collection action queue list")
    return {
        "status": "ok",
        "filter": normalized_status,
        "limit": bounded_limit,
        "count": len(rows),
        "actions": [dict(row) for row in rows],
    }


@router.get("/collectors/{source}")
async def collector_detail(source: str, _user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        cursor = await conn.fetchrow(
            "SELECT * FROM service_cursors WHERE service = $1", source,
        )
        media_count = await conn.fetchval(
            "SELECT COUNT(*) FROM media_items WHERE source = $1", source,
        )
        error_count = await conn.fetchval(
            "SELECT COUNT(*) FROM dead_letter_queue WHERE source = $1", source,
        )
        recent = await conn.fetch(
            "SELECT id, entity_name, content_type, filename, file_size, collected_at "
            "FROM media_items WHERE source = $1 ORDER BY collected_at DESC LIMIT 10",
            source,
        )
    return {
        "source": source,
        "cursor": dict(cursor) if cursor else None,
        "media_count": media_count,
        "error_count": error_count,
        "recent_items": [dict(r) for r in recent],
    }
