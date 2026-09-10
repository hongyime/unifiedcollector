"""``strava`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.

Cross-module helpers that still live in ``__init__.py`` are late-imported
inside thin wrappers to preserve test monkey-patch semantics.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from src.db.connection import get_pool
from src.core.strava_route_queue import fetch_strava_route_capture_queue
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _acquire_dashboard_conn,
    _release_dashboard_conn,
)

logger = logging.getLogger(__name__)


def _lookup(name: str):
    """Look up a name on the parent dashboard_api module at call time."""
    import sys as _sys
    root = _sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


router = APIRouter()


@router.get("/strava/athletes")
async def strava_list_athletes(
    limit: int = 200,
    _user: dict = Depends(require_role("viewer")),
):
    """List athletes with at least one activity, ordered by activity count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.platform_athlete_id, a.username, a.firstname, a.lastname,
                   a.profile, COUNT(act.id) AS activity_count
            FROM strava_athletes a
            LEFT JOIN strava_activities act ON act.athlete_id = a.id
            GROUP BY a.id
            ORDER BY activity_count DESC, a.platform_athlete_id ASC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/strava/feed/dates")
async def strava_feed_dates(
    athlete_id: int | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    _user: dict = Depends(require_role("viewer")),
):
    """List dates with activity counts for a given athlete (or all)."""
    pool = await get_pool()
    where = ["start_date IS NOT NULL"]
    args: list = []
    if athlete_id is not None:
        args.append(int(athlete_id))
        where.append(
            f"athlete_id = (SELECT id FROM strava_athletes WHERE platform_athlete_id = ${len(args)})"
        )
    if from_:
        try:
            args.append(datetime.fromisoformat(from_))
            where.append(f"start_date >= ${len(args)}")
        except Exception:
            raise HTTPException(status_code=400, detail="bad 'from' date")
    if to:
        try:
            args.append(datetime.fromisoformat(to))
            where.append(f"start_date <= ${len(args)}")
        except Exception:
            raise HTTPException(status_code=400, detail="bad 'to' date")
    sql = (
        "SELECT DATE(start_date) AS date, COUNT(*) AS count "
        "FROM strava_activities "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY DATE(start_date) ORDER BY DATE(start_date) DESC LIMIT 365"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [{"date": r["date"].isoformat(), "count": r["count"]} for r in rows]


@router.get("/strava/feed/activities")
async def strava_feed_activities(
    date: str,
    athlete_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    _user: dict = Depends(require_role("viewer")),
):
    """Activities on a given UTC date for an athlete (or all)."""
    try:
        day = datetime.fromisoformat(date).date()
    except Exception:
        raise HTTPException(status_code=400, detail="bad 'date'")
    pool = await get_pool()
    where = ["DATE(act.start_date) = $1"]
    args: list = [day]
    if athlete_id is not None:
        args.append(int(athlete_id))
        where.append(
            f"act.athlete_id = (SELECT id FROM strava_athletes WHERE platform_athlete_id = ${len(args)})"
        )
    args.append(int(limit))
    args.append(int(offset))
    # NB: summary_polyline / start_latlng / distance_unit / stream_status feed the
    # dashboard map thumbnail. Older scrapes sometimes populated the full GPS
    # stream without backfilling summary_polyline, so we also fetch latlng and
    # derive the compact thumbnail line below when needed.
    sql = (
        "SELECT act.platform_activity_id, act.name, act.type, act.sport_type, "
        "       act.distance, act.distance_unit, act.moving_time, act.elapsed_time, "
        "       act.total_elevation_gain, act.average_speed, act.start_date, "
        "       act.summary_polyline, act.start_latlng, act.stream_status, s.latlng AS gps_latlng, "
        "       rl.created_at AS gps_rate_limit_at, rl.cooldown_until AS gps_rate_limit_until, "
        "       rl.reason AS gps_rate_limit_reason, rl.context AS gps_rate_limit_context, "
        "       a.platform_athlete_id, a.username, a.firstname, a.lastname, a.profile "
        "FROM strava_activities act "
        "LEFT JOIN strava_athletes a ON a.id = act.athlete_id "
        "LEFT JOIN strava_gps_streams s ON s.activity_id = act.id "
        "LEFT JOIN LATERAL ( "
        "    SELECT created_at, "
        "           created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS cooldown_until, "
        "           reason, metadata->>'context' AS context "
        "    FROM rate_limit_events "
        "    WHERE source = 'strava' "
        "      AND scope = 'gps_streams' "
        "      AND metadata->>'activity_id' = act.platform_activity_id::text "
        "    ORDER BY created_at DESC "
        "    LIMIT 1 "
        ") rl ON TRUE "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY act.start_date DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    out = []
    for r in rows:
        d = dict(r)
        if d.get("start_date"):
            d["start_date"] = d["start_date"].isoformat()
        if not d.get("summary_polyline"):
            points = _jsonb_points(d.pop("gps_latlng", None))
            if len(points) > 1:
                d["summary_polyline"] = _encode_polyline(points)
                d["stream_status"] = d.get("stream_status") or "ok"
        else:
            d.pop("gps_latlng", None)
        d.update(_strava_route_status(d))
        for key in ("gps_rate_limit_at", "gps_rate_limit_until"):
            if d.get(key):
                d[key] = d[key].isoformat()
        out.append(d)
    return out


@router.get("/strava/feed/stats")
async def strava_feed_stats(
    athlete_id: int | None = None,
    _user: dict = Depends(require_role("viewer")),
):
    """Summary stats for an athlete's activities (or all)."""
    pool = await get_pool()
    where = ["1 = 1"]
    args: list = []
    if athlete_id is not None:
        args.append(int(athlete_id))
        where.append(
            f"athlete_id = (SELECT id FROM strava_athletes WHERE platform_athlete_id = ${len(args)})"
        )
    sql = (
        "SELECT COUNT(*) AS total_activities, "
        "       COALESCE(SUM(distance), 0) AS total_distance, "
        "       COALESCE(SUM(moving_time), 0) AS total_moving_time, "
        "       COALESCE(SUM(total_elevation_gain), 0) AS total_elevation_gain, "
        "       MIN(start_date) AS earliest, "
        "       MAX(start_date) AS latest "
        f"FROM strava_activities WHERE {' AND '.join(where)}"
    )
    coverage_sql = (
        "WITH base AS ( "
        "  SELECT *, "
        "         COALESCE((summary_polyline IS NOT NULL AND summary_polyline <> '') OR stream_status = 'ok', FALSE) AS is_mapped, "
        "         (COALESCE(metadata, '{}'::jsonb) ? 'browser_stream_last_seen_at') AS is_browser_captured "
        "  FROM strava_activities "
        f"  WHERE {' AND '.join(where)} "
        ") "
        "SELECT COUNT(*)::int AS total, "
        "       COUNT(*) FILTER (WHERE is_mapped)::int AS mapped, "
        "       COUNT(*) FILTER (WHERE is_browser_captured)::int AS browser_captured, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status = 'truncated_empty')::int AS privacy_zone, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status = 'incomplete')::int AS no_gps, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status = 'ok_unverifiable')::int AS unverifiable, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status IS NULL AND start_latlng IS NOT NULL)::int AS start_only, "
        "       COUNT(*) FILTER (WHERE NOT is_mapped AND stream_status IS NULL AND start_latlng IS NULL)::int AS queued "
        "FROM base"
    )
    profile_sql = """
        WITH activity_by_athlete AS (
            SELECT athlete_id,
                   COUNT(*)::int AS activity_count,
                   BOOL_OR(
                       COALESCE(
                           (summary_polyline IS NOT NULL AND summary_polyline <> '')
                           OR stream_status = 'ok',
                           FALSE
                       )
                   ) AS has_route
            FROM strava_activities
            GROUP BY athlete_id
        )
        SELECT COUNT(*)::int AS total_athletes,
               COUNT(*) FILTER (
                   WHERE profile IS NOT NULL
                      OR follower_count IS NOT NULL
                      OR following_count IS NOT NULL
                      OR city IS NOT NULL
                      OR state IS NOT NULL
                      OR country IS NOT NULL
                      OR sex IS NOT NULL
                      OR weight IS NOT NULL
                      OR height IS NOT NULL
               )::int AS enriched_profiles,
               COUNT(*) FILTER (
                   WHERE username IS NOT NULL OR firstname IS NOT NULL OR lastname IS NOT NULL
               )::int AS with_name,
               COUNT(*) FILTER (WHERE profile IS NOT NULL)::int AS with_profile_photo,
               COUNT(*) FILTER (
                   WHERE follower_count IS NOT NULL OR following_count IS NOT NULL
               )::int AS with_social_counts,
               COUNT(*) FILTER (
                   WHERE city IS NOT NULL OR state IS NOT NULL OR country IS NOT NULL
               )::int AS with_location,
               COUNT(ab.athlete_id)::int AS athletes_with_activity,
               COUNT(*) FILTER (WHERE COALESCE(ab.has_route, FALSE))::int AS athletes_with_route,
               COALESCE(SUM(ab.activity_count), 0)::int AS activity_rows,
               MAX(a.updated_at) FILTER (
                   WHERE profile IS NOT NULL
                      OR follower_count IS NOT NULL
                      OR following_count IS NOT NULL
                      OR city IS NOT NULL
                      OR state IS NOT NULL
                      OR country IS NOT NULL
                      OR sex IS NOT NULL
                      OR weight IS NOT NULL
                      OR height IS NOT NULL
               ) AS latest_profile_update_at
        FROM strava_athletes a
        LEFT JOIN activity_by_athlete ab ON ab.athlete_id = a.id
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        coverage = await conn.fetchrow(coverage_sql, *args)
        profile_completeness = await conn.fetchrow(profile_sql)
        recent_429_events = int(await conn.fetchval(
            """
            SELECT COUNT(*)::int
            FROM rate_limit_events
            WHERE source = 'strava'
              AND scope = 'gps_streams'
              AND status_code = 429
              AND created_at >= date_trunc('hour', now())
            """
        ) or 0)
        active_cooldown = await conn.fetchrow(
            """
            SELECT created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS cooldown_until,
                   reason
            FROM rate_limit_events
            WHERE source = 'strava'
              AND scope = 'gps_streams'
              AND status_code = 429
              AND cooldown_seconds IS NOT NULL
              AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > now()
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
        latest_browser_capture_at = await conn.fetchval(
            """
            SELECT max(created_at)
            FROM browser_ingest_events
            WHERE platform = 'strava'
              AND endpoint = 'strava_streams'
            """
        )
    d = dict(row) if row else {}
    for k in ("earliest", "latest"):
        if d.get(k):
            d[k] = d[k].isoformat()
    c = dict(coverage) if coverage else {}
    total = int(c.get("total") or 0)
    mapped = int(c.get("mapped") or 0)
    d["route_coverage"] = {
        "total": total,
        "mapped": mapped,
        "queued": int(c.get("queued") or 0),
        "start_only": int(c.get("start_only") or 0),
        "privacy_zone": int(c.get("privacy_zone") or 0),
        "no_gps": int(c.get("no_gps") or 0),
        "unverifiable": int(c.get("unverifiable") or 0),
        "browser_captured": int(c.get("browser_captured") or 0),
        "completion_pct": round((mapped / total) * 100, 1) if total else 0.0,
        "recent_gps_429_events": recent_429_events,
        "active_gps_cooldown_until": (
            active_cooldown["cooldown_until"].isoformat()
            if active_cooldown and active_cooldown["cooldown_until"]
            else None
        ),
        "active_gps_cooldown_reason": active_cooldown["reason"] if active_cooldown else None,
        "latest_browser_capture_at": latest_browser_capture_at.isoformat() if latest_browser_capture_at else None,
    }
    pc = dict(profile_completeness) if profile_completeness else {}
    total_athletes = int(pc.get("total_athletes") or 0)
    enriched_profiles = int(pc.get("enriched_profiles") or 0)
    with_name = int(pc.get("with_name") or 0)
    athletes_with_activity = int(pc.get("athletes_with_activity") or 0)
    athletes_with_route = int(pc.get("athletes_with_route") or 0)
    latest_profile_update_at = pc.get("latest_profile_update_at")
    d["profile_completeness"] = {
        "total_athletes": total_athletes,
        "enriched_profiles": enriched_profiles,
        "profile_backfill_remaining": max(total_athletes - enriched_profiles, 0),
        "id_only_athletes": max(total_athletes - with_name, 0),
        "with_name": with_name,
        "with_profile_photo": int(pc.get("with_profile_photo") or 0),
        "with_social_counts": int(pc.get("with_social_counts") or 0),
        "with_location": int(pc.get("with_location") or 0),
        "athletes_with_activity": athletes_with_activity,
        "athletes_with_route": athletes_with_route,
        "activity_rows": int(pc.get("activity_rows") or 0),
        "profile_completion_pct": round((enriched_profiles / total_athletes) * 100, 1) if total_athletes else 0.0,
        "activity_coverage_pct": round((athletes_with_activity / total_athletes) * 100, 1) if total_athletes else 0.0,
        "route_athlete_pct": round((athletes_with_route / total_athletes) * 100, 1) if total_athletes else 0.0,
        "latest_profile_update_at": latest_profile_update_at.isoformat() if latest_profile_update_at else None,
    }
    return d


@router.get("/strava/route-capture-queue")
async def strava_route_capture_queue(
    limit: int = 8,
    account: str | None = None,
    respect_cooldown: bool = True,
    _user: dict = Depends(require_role("viewer")),
):
    pool = await get_pool()
    return await fetch_strava_route_capture_queue(
        pool,
        limit=limit,
        account=account,
        respect_cooldown=respect_cooldown,
    )


