"""Priority queue for browser-assisted Strava route capture."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


async def fetch_strava_route_capture_queue(
    pool,
    *,
    limit: int = 5,
    account: str | None = None,
    respect_cooldown: bool | None = None,
    recent_visit_hours: int | None = None,
) -> dict[str, Any]:
    """Return route-missing Strava activities ordered for browser capture.

    This feeds the Chrome extension and dashboard. It deliberately excludes
    activities recently handed to the browser, so a failed capture does not spin
    on the same activity every loop.
    """
    if respect_cooldown is None:
        respect_cooldown = (
            os.getenv("STRAVA_BROWSER_ROUTE_RESPECT_GPS_COOLDOWN", "true").lower()
            == "true"
        )
    if recent_visit_hours is None:
        recent_visit_hours = int(os.getenv("STRAVA_BROWSER_ROUTE_VISIT_TTL_HOURS", "6"))
    limit = max(1, min(int(limit or 5), 25))
    recent_visit_hours = max(1, min(int(recent_visit_hours or 6), 72))
    account = str(account or "").strip() or None

    async with pool.acquire() as conn:
        cooldown = await conn.fetchrow(
            """
            SELECT created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS cooldown_until,
                   reason,
                   account,
                   scope
            FROM rate_limit_events
            WHERE source = 'strava'
              AND scope IN ('gps_streams', 'browser_strava_streams')
              AND status_code = 429
              AND cooldown_seconds IS NOT NULL
              AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > now()
              AND (
                    NULLIF(account, '') IS NULL
                 OR ($1::text IS NOT NULL AND account = $1::text)
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            account,
        )
        cooldown_until = cooldown["cooldown_until"] if cooldown else None
        cooldown_active = bool(cooldown_until)
        if cooldown_active and respect_cooldown:
            return {
                "items": [],
                "cooldown": {
                    "active": True,
                    "until": _iso(cooldown_until),
                    "reason": cooldown["reason"],
                    "account": cooldown["account"],
                    "scope": cooldown["scope"],
                },
                "account": account,
                "recent_visit_ttl_hours": recent_visit_hours,
            }

        rows = await conn.fetch(
            """
            SELECT a.platform_activity_id,
                   a.name,
                   a.type,
                   a.sport_type,
                   a.start_date,
                   a.start_latlng,
                   a.stream_status,
                   ath.platform_athlete_id,
                   COALESCE(NULLIF(ath.firstname, ''), NULLIF(ath.username, ''), ath.platform_athlete_id::text) AS athlete_name,
                   COALESCE(prox.tier, 9)::int AS proximity_tier,
                   COALESCE(target.priority, 0)::int AS target_priority,
                   recent_visit.created_at AS last_browser_visit_at
            FROM strava_activities a
            LEFT JOIN strava_athletes ath ON ath.id = a.athlete_id
            LEFT JOIN strava_gps_streams s ON s.activity_id = a.id
            LEFT JOIN LATERAL (
                SELECT MIN(ap.tier) AS tier
                FROM account_proximity_cache ap
                WHERE ap.platform = 'strava'
                  AND ath.platform_athlete_id IS NOT NULL
                  AND ap.account_id = ath.platform_athlete_id::text
            ) prox ON TRUE
            LEFT JOIN LATERAL (
                SELECT MAX(ct.priority) AS priority
                FROM collection_targets ct
                WHERE ct.source = 'strava'
                  AND ath.platform_athlete_id IS NOT NULL
                  AND ct.target_id = ath.platform_athlete_id::text
            ) target ON TRUE
            LEFT JOIN LATERAL (
                SELECT max(created_at) AS created_at
                FROM browser_ingest_events bie
                WHERE bie.platform = 'strava'
                  AND bie.endpoint = 'strava_route_visit'
                  AND bie.subject = a.platform_activity_id::text
            ) recent_visit ON TRUE
            WHERE (a.summary_polyline IS NULL OR a.summary_polyline = '')
              AND COALESCE(a.stream_status, '') NOT IN ('ok', 'incomplete', 'truncated_empty', 'ok_unverifiable')
              AND (
                    s.latlng IS NULL
                 OR s.latlng = '[]'::jsonb
                 OR s.latlng = 'null'::jsonb
                 OR CASE
                      WHEN jsonb_typeof(s.latlng) = 'array' THEN jsonb_array_length(s.latlng) <= 1
                      ELSE TRUE
                    END
              )
              AND (
                    recent_visit.created_at IS NULL
                 OR recent_visit.created_at < now() - ($2::int * INTERVAL '1 hour')
              )
              AND lower(COALESCE(a.sport_type, a.type, '')) NOT IN (
                    'crossfit', 'elliptical', 'hiit', 'pilates',
                    'stairstepper', 'weighttraining', 'workout', 'yoga'
              )
              AND lower(COALESCE(a.sport_type, a.type, '')) NOT LIKE 'virtual%'
              AND lower(COALESCE(a.sport_type, a.type, '')) NOT LIKE 'indoor%'
              AND (
                    a.start_latlng IS NOT NULL
                 OR lower(COALESCE(a.sport_type, a.type, '')) ~
                    '(run|ride|walk|hike|trail|bike|cycle|ski|snowboard|kayak|canoe|row|paddle|surf|sail|skate|wheelchair|velomobile)'
              )
            ORDER BY
                CASE WHEN prox.tier IN (1, 2) THEN 0 ELSE 1 END,
                prox.tier ASC NULLS LAST,
                target.priority DESC,
                CASE WHEN a.start_latlng IS NOT NULL THEN 0 ELSE 1 END,
                a.start_date DESC NULLS LAST,
                a.platform_activity_id DESC
            LIMIT $1
            """,
            limit,
            recent_visit_hours,
        )

    return {
        "items": [_route_row(row) for row in rows],
        "cooldown": {
            "active": cooldown_active,
            "until": _iso(cooldown_until),
            "reason": cooldown["reason"] if cooldown else None,
            "account": cooldown["account"] if cooldown else None,
            "scope": cooldown["scope"] if cooldown else None,
        },
        "account": account,
        "recent_visit_ttl_hours": recent_visit_hours,
    }


def _route_row(row) -> dict[str, Any]:
    activity_id = row["platform_activity_id"]
    return {
        "platform_activity_id": int(activity_id),
        "activity_url": f"https://www.strava.com/activities/{activity_id}",
        "name": row["name"],
        "type": row["type"],
        "sport_type": row["sport_type"],
        "start_date": _iso(row["start_date"]),
        "start_latlng": row["start_latlng"],
        "stream_status": row["stream_status"],
        "platform_athlete_id": (
            int(row["platform_athlete_id"]) if row["platform_athlete_id"] is not None else None
        ),
        "athlete_name": row["athlete_name"],
        "proximity_tier": int(row["proximity_tier"] or 9),
        "target_priority": int(row["target_priority"] or 0),
        "last_browser_visit_at": _iso(row["last_browser_visit_at"]),
    }
