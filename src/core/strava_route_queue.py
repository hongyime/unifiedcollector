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
    candidate_limit = max(
        limit,
        min(int(os.getenv("STRAVA_BROWSER_ROUTE_QUEUE_CANDIDATE_LIMIT", "300")), 10000),
    )
    important_candidate_limit = max(
        limit,
        min(
            int(os.getenv("STRAVA_BROWSER_ROUTE_QUEUE_IMPORTANT_CANDIDATE_LIMIT", "10000")),
            25000,
        ),
    )
    account = str(account or "").strip() or None

    async with pool.acquire() as conn:
        cooldown = await conn.fetchrow(
            """
            SELECT rl.created_at + (COALESCE(rl.cooldown_seconds, 0) * INTERVAL '1 second') AS cooldown_until,
                   rl.reason,
                   rl.account,
                   rl.scope
            FROM rate_limit_events rl
            WHERE rl.source = 'strava'
              AND rl.scope IN ('gps_streams', 'browser_strava_streams')
              AND rl.status_code = 429
              AND rl.cooldown_seconds IS NOT NULL
              AND rl.created_at + (COALESCE(rl.cooldown_seconds, 0) * INTERVAL '1 second') > now()
              AND (
                    NULLIF(rl.account, '') IS NULL
                 OR ($1::text IS NOT NULL AND rl.account = $1::text)
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM strava_activities a
                    JOIN strava_gps_streams s ON s.activity_id = a.id
                    WHERE rl.metadata->>'activity_id' ~ '^[0-9]+$'
                      AND a.platform_activity_id = (rl.metadata->>'activity_id')::bigint
                      AND s.collected_at > rl.created_at
                      AND jsonb_typeof(s.latlng) = 'array'
                      AND jsonb_array_length(s.latlng) > 1
              )
            ORDER BY rl.created_at DESC
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
            WITH important_accounts AS MATERIALIZED (
                SELECT account_id
                FROM (
                    SELECT ap.account_id
                    FROM account_proximity_cache ap
                    WHERE ap.platform = 'strava'
                      AND ap.tier <= 2
                      AND ($4::text IS NULL OR ap.owner_account = $4::text)
                    UNION
                    SELECT ct.target_id AS account_id
                    FROM collection_targets ct
                    WHERE ct.source = 'strava'
                      AND COALESCE(ct.priority, 0) > 0
                      AND COALESCE(ct.status, 'active') NOT IN ('disabled', 'paused')
                ) ids
                WHERE NULLIF(account_id, '') IS NOT NULL
            ),
            important_candidates AS MATERIALIZED (
                SELECT a.id,
                       a.platform_activity_id,
                       a.name,
                       a.type,
                       a.sport_type,
                       a.start_date,
                       a.start_latlng,
                       a.stream_status,
                       a.athlete_id,
                       ath.platform_athlete_id
                FROM important_accounts ia
                JOIN strava_athletes ath ON ath.platform_athlete_id::text = ia.account_id
                JOIN strava_activities a ON a.athlete_id = ath.id
                LEFT JOIN strava_gps_streams s ON s.activity_id = a.id
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
                ORDER BY a.start_date DESC NULLS LAST, a.platform_activity_id DESC
                LIMIT $5
            ),
            recent_candidates AS MATERIALIZED (
                SELECT a.id,
                       a.platform_activity_id,
                       a.name,
                       a.type,
                       a.sport_type,
                       a.start_date,
                       a.start_latlng,
                       a.stream_status,
                       a.athlete_id,
                       ath.platform_athlete_id
                FROM strava_activities a
                LEFT JOIN strava_gps_streams s ON s.activity_id = a.id
                LEFT JOIN strava_athletes ath ON ath.id = a.athlete_id
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
                ORDER BY a.start_date DESC NULLS LAST, a.platform_activity_id DESC
                LIMIT $3
            ),
            candidates AS MATERIALIZED (
                SELECT DISTINCT ON (id) id,
                       platform_activity_id,
                       name,
                       type,
                       sport_type,
                       start_date,
                       start_latlng,
                       stream_status,
                       athlete_id,
                       platform_athlete_id
                FROM (
                    SELECT 0 AS candidate_rank, ic.*
                    FROM important_candidates ic
                    UNION ALL
                    SELECT 1 AS candidate_rank, rc.*
                    FROM recent_candidates rc
                ) merged
                ORDER BY id, candidate_rank
            )
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
            FROM candidates a
            LEFT JOIN strava_athletes ath ON ath.id = a.athlete_id
            LEFT JOIN LATERAL (
                SELECT MIN(ap.tier) AS tier
                FROM account_proximity_cache ap
                WHERE ap.platform = 'strava'
                  AND ath.platform_athlete_id IS NOT NULL
                  AND ap.account_id = ath.platform_athlete_id::text
                  AND ($4::text IS NULL OR ap.owner_account = $4::text)
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
            WHERE (
                    recent_visit.created_at IS NULL
                 OR recent_visit.created_at < now() - ($2::int * INTERVAL '1 hour')
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
            candidate_limit,
            account,
            important_candidate_limit,
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
        "recent_candidate_limit": candidate_limit,
        "important_candidate_limit": important_candidate_limit,
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
