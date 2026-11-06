"""Shared, bounded read model for recent collector rate limits."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _cursor_payload(row, now_utc: datetime) -> dict:
    payload = dict(row)
    expiry = None
    streak = None
    raw = str(payload.get("last_processed_id") or "")
    if ":" in raw:
        raw_expiry, raw_streak = raw.split(":", 1)
        try:
            expiry = datetime.fromtimestamp(float(raw_expiry), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
        try:
            streak = int(raw_streak)
        except (TypeError, ValueError):
            pass
    payload.update(active_until=expiry, streak=streak, active_now=bool(expiry and expiry > now_utc))
    return payload


async def get_recent_rate_limits(conn, *, hours: int = 24, limit: int = 100) -> dict:
    """Return dashboard/control-room rate-limit status without web auth."""
    hours = max(1, min(hours, 168))
    limit = max(1, min(limit, 500))
    if not await conn.fetchval("SELECT to_regclass('public.rate_limit_events') IS NOT NULL", timeout=8):
        return {"events": [], "active": [], "active_event_summary": [], "cursor_history": [], "recent_summary": []}

    events = [dict(row) for row in await conn.fetch(
        """
        SELECT id, source, account, scope, status_code, cooldown_seconds, reason, metadata, created_at
        FROM rate_limit_events
        WHERE created_at >= now() - ($1 || ' hours')::interval
        ORDER BY created_at DESC LIMIT $2
        """, str(hours), limit, timeout=15,
    )]
    for event in events:
        if isinstance(event.get("metadata"), str):
            try:
                event["metadata"] = json.loads(event["metadata"])
            except json.JSONDecodeError:
                event["metadata"] = {}
    recent_summary = [dict(row) for row in await conn.fetch(
        """
        SELECT source, account, scope,
               (array_agg(status_code ORDER BY created_at DESC))[1]::int AS status_code,
               count(*)::int AS count,
               (array_agg(cooldown_seconds ORDER BY created_at DESC))[1]::int AS cooldown_seconds,
               (array_agg(reason ORDER BY created_at DESC))[1] AS reason,
               min(created_at) AS first_seen_at, max(created_at) AS last_seen_at,
               max(created_at + COALESCE(cooldown_seconds, 0) * interval '1 second') AS active_until
        FROM rate_limit_events
        WHERE created_at >= now() - ($1 || ' hours')::interval
        GROUP BY source, account, scope ORDER BY last_seen_at DESC LIMIT 24
        """, str(hours), timeout=15,
    )]
    now_utc = datetime.now(timezone.utc)
    for row in recent_summary:
        row["active_now"] = bool(row.get("active_until") and row["active_until"] > now_utc)

    active = []
    cursor_history = []
    try:
        rows = await conn.fetch(
            """
            SELECT service, last_processed_id, last_processed_at, status
            FROM service_cursors
            WHERE service ILIKE '%rate_limit' OR service ILIKE '%ratelimit'
            ORDER BY last_processed_at DESC NULLS LAST
            """, timeout=8,
        )
        cursor_history = [_cursor_payload(row, now_utc) for row in rows]
        active = [row for row in cursor_history if row["active_now"] or (row.get("status") == "blocked" and not row.get("active_until"))]
    except Exception:
        # The event ledger remains authoritative when the optional cursor table is unavailable.
        pass
    return {
        "events": events,
        "active": active,
        "active_event_summary": [row for row in recent_summary if row["active_now"]],
        "cursor_history": cursor_history,
        "recent_summary": recent_summary,
    }
