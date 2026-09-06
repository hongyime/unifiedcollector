"""``rate_limits`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _acquire_dashboard_conn,
    _release_dashboard_conn,
    _copy_cache_value,
)


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
        table_exists = bool(await conn.fetchval("SELECT to_regclass('public.rate_limit_events') IS NOT NULL", timeout=8))
        if table_exists:
            events = [dict(r) for r in await conn.fetch(
                """
                SELECT id, source, account, scope, status_code, cooldown_seconds,
                       reason, metadata, created_at
                FROM rate_limit_events
                WHERE created_at >= now() - ($1 || ' hours')::interval
                ORDER BY created_at DESC
                LIMIT $2
                """,
                str(hours), limit,
                timeout=15,
            )]
            for event in events:
                if isinstance(event.get("metadata"), str):
                    try:
                        event["metadata"] = json.loads(event["metadata"])
                    except Exception:
                        event["metadata"] = {}
            recent_summary = [dict(r) for r in await conn.fetch(
                """
                SELECT source, account, scope,
                       (array_agg(status_code ORDER BY created_at DESC))[1]::int AS status_code,
                       count(*)::int AS count,
                       (array_agg(cooldown_seconds ORDER BY created_at DESC))[1]::int AS cooldown_seconds,
                       (array_agg(reason ORDER BY created_at DESC))[1] AS reason,
                       min(created_at) AS first_seen_at,
                       max(created_at) AS last_seen_at,
                       max(created_at + COALESCE(cooldown_seconds, 0) * interval '1 second') AS active_until
                FROM rate_limit_events
                WHERE created_at >= now() - ($1 || ' hours')::interval
                GROUP BY source, account, scope
                ORDER BY last_seen_at DESC
                LIMIT 24
                """,
                str(hours),
                timeout=15,
            )]
            now_utc = datetime.now(timezone.utc)
            for row in recent_summary:
                active_until = row.get("active_until")
                row["active_now"] = bool(active_until and active_until > now_utc)
        else:
            events = []
            recent_summary = []
        active_event_summary = [row for row in recent_summary if row.get("active_now")]
        active = []
        cursor_history = []
        try:
            now_utc = datetime.now(timezone.utc)
            for r in await conn.fetch(
                """
                SELECT service, last_processed_id, last_processed_at, status
                FROM service_cursors
                WHERE service ILIKE '%rate_limit'
                   OR service ILIKE '%ratelimit'
                ORDER BY last_processed_at DESC NULLS LAST
                """,
                timeout=8,
            ):
                d = _rate_limit_cursor_payload(r, now_utc)
                cursor_history.append(d)
                if d["active_now"] or (d.get("status") == "blocked" and not d.get("active_until")):
                    active.append(d)
        except Exception:
            active = []
            cursor_history = []
    except Exception as exc:  # noqa: BLE001 - dashboard status panels must fail soft
        logger.warning("recent rate limits failed: %s", exc.__class__.__name__)
        return _rate_limits_recent_fallback_payload(hours, limit, exc)
    finally:
        if conn is not None:
            await _release_dashboard_conn(pool, conn, "operational events")
    payload = {
        "events": events,
        "active": active,
        "active_event_summary": active_event_summary,
        "cursor_history": cursor_history,
        "recent_summary": recent_summary,
    }
    (_lookup("_RATE_LIMITS_RECENT_CACHE") or {})[(hours, limit)] = {
        "ts": time.time(),
        "payload": _copy_cache_value(payload),
    }
    return payload


