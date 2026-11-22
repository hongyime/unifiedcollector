"""Strava route-capture endpoints for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 8
(``docs/plans/perf-file-splits.md`` §4B).

The extension observes real strava.com traffic in the operator's browser and
POSTs the intermediate JSON here. Two flows:

- ``strava_route_queue_handler`` — the extension polls this for a list of
  activity_ids we still need to fetch route-stream data for. Response cache
  and cool-down live in ``.constants``.
- ``strava_route_visit_handler`` — the extension confirms it visited an
  activity page (may or may not have captured streams).
- ``strava_streams_handler`` — the actual GPS polyline data. This is the
  hot write path — persists into ``strava_gps_streams``.

Heavy DB helpers (``_upsert_strava_browser_stream``,
``_record_strava_stream_http_event``, ``_record_browser_ingest_event``,
``_archive_browser_capture``, ``_strava_stream_response_status``,
``fetch_strava_route_capture_queue``) still live in ``__init__.py``.
"""
import asyncio
import logging
import time

from aiohttp import web

from .cors import _cors

from ._util import _json_array_or_none
from ._util import _stream_values
from src.collectors.strava import _derive_gps_route_fields
from src.core.rate_limit_events import record_rate_limit_event
import json


logger = logging.getLogger("social_ingest")


async def strava_streams_handler(request):
    from . import (
        _archive_browser_capture,
        _record_browser_ingest_event,
        _record_strava_stream_http_event,
        _safe_json,
        _strava_stream_response_status,
        _upsert_strava_browser_stream,
    )

    body = await _safe_json(request)
    body["platform"] = "strava"
    pool = request.app["pool"]
    await _archive_browser_capture(pool, "strava", "strava_streams", body)
    http_event_recorded = await _record_strava_stream_http_event(pool, body)
    result = await _upsert_strava_browser_stream(pool, body)
    if http_event_recorded:
        result["rate_limit_recorded"] = True
    await _record_browser_ingest_event(
        pool,
        "strava",
        "strava_streams",
        str(body.get("activity_id") or body.get("platform_activity_id") or "unknown"),
        observed_count=1 if body.get("activity_id") or body.get("platform_activity_id") else 0,
        stored_count=int(result.get("stored") or 0),
        metadata={
            "point_count": int(result.get("point_count") or 0),
            "reason": result.get("reason"),
            "request_url": body.get("request_url") or body.get("url"),
            "extension_version": body.get("extension_version"),
        },
    )
    status = _strava_stream_response_status(result, http_event_recorded=http_event_recorded)
    return _cors(web.json_response(result, status=status))


async def strava_route_queue_handler(request):
    from . import (
        STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS,
        STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS,
        STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS,
        _STRAVA_ROUTE_QUEUE_RESPONSE_CACHE,
        _STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST,
        fetch_strava_route_capture_queue,
    )

    raw_limit = request.query.get("limit", "5")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 5
    account = str(
        request.query.get("account")
        or request.query.get("owner")
        or ""
    ).strip() or None
    cache_key = f"{account or ''}:{limit}"
    now = time.time()
    cached = _STRAVA_ROUTE_QUEUE_RESPONSE_CACHE.get(cache_key)
    try:
        queue = await asyncio.wait_for(
            fetch_strava_route_capture_queue(
                request.app["pool"],
                limit=limit,
                account=account,
                respect_cooldown=True,
            ),
            timeout=max(0.1, STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS),
        )
        _STRAVA_ROUTE_QUEUE_RESPONSE_CACHE[cache_key] = (time.time(), queue)
    except asyncio.TimeoutError:
        if cached and now - cached[0] <= max(STRAVA_ROUTE_QUEUE_RESPONSE_CACHE_SECONDS, 1.0):
            queue = dict(cached[1])
            queue["stale"] = True
            queue["timeout"] = True
            queue["cache_age_seconds"] = int(now - cached[0])
        else:
            queue = {
                "items": [],
                "timeout": True,
                "reason": "route_queue_timeout",
                "account": account,
            }
        last_warn = _STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST.get(cache_key, 0.0)
        should_warn = now - last_warn >= max(1.0, STRAVA_ROUTE_QUEUE_TIMEOUT_WARN_SECONDS)
        if should_warn:
            _STRAVA_ROUTE_QUEUE_TIMEOUT_LOG_LAST[cache_key] = now
        log = logger.warning if should_warn else logger.info
        log(
            "strava route queue timed out after %.1fs account=%s limit=%s; returned %d cached/live items",
            max(0.1, STRAVA_ROUTE_QUEUE_RESPONSE_TIMEOUT_SECONDS),
            account or "",
            limit,
            len(queue.get("items") or []),
        )
    return _cors(web.json_response(queue))


async def strava_route_visit_handler(request):
    from . import _record_browser_ingest_event, _safe_json

    body = await _safe_json(request)
    raw_activity_id = body.get("activity_id") or body.get("platform_activity_id")
    activity_id = str(raw_activity_id or "").strip()
    if not activity_id:
        return _cors(web.json_response({"ok": False, "reason": "bad_activity_id"}, status=400))
    await _record_browser_ingest_event(
        request.app["pool"],
        "strava",
        "strava_route_visit",
        activity_id,
        observed_count=1,
        stored_count=0,
        metadata={
            "status": body.get("status") or "observed",
            "url": body.get("url"),
            "activity_url": body.get("activity_url"),
            "owner": body.get("owner") or body.get("owner_account") or body.get("account"),
            "extension_version": body.get("extension_version"),
        },
    )
    return _cors(web.json_response({"ok": True, "activity_id": activity_id}))


async def _upsert_strava_browser_stream(pool, body: dict) -> dict:
    """Persist a Strava route stream observed by the browser extension."""
    if not pool:
        return {"stored": 0, "point_count": 0, "reason": "no_pool"}
    raw_activity_id = body.get("activity_id") or body.get("platform_activity_id")
    try:
        activity_id = int(raw_activity_id)
    except (TypeError, ValueError):
        return {"stored": 0, "point_count": 0, "reason": "bad_activity_id"}

    streams = body.get("streams") if isinstance(body.get("streams"), (dict, list)) else body
    latlng = _stream_values(streams, "latlng")
    point_count = len(latlng) if isinstance(latlng, list) else 0
    if point_count < 2:
        if _browser_http_status(body) == 200:
            return await _record_empty_strava_browser_stream(pool, activity_id, body, point_count)
        return {"stored": 0, "point_count": point_count, "reason": "no_route_points"}

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO strava_activities (platform_activity_id, metadata, collected_at)
            VALUES (
                $1,
                jsonb_build_object(
                    'browser_stream_stub', TRUE,
                    'browser_stream_first_seen_at', now(),
                    'browser_stream_request_url', $2::text
                ),
                now()
            )
            ON CONFLICT (platform_activity_id) DO NOTHING
            """,
            activity_id,
            body.get("request_url") or body.get("url"),
        )
        act_row = await conn.fetchrow(
            """
            SELECT id, start_latlng, end_latlng
            FROM strava_activities
            WHERE platform_activity_id = $1
            """,
            activity_id,
        )
        if not act_row:
            return {"stored": 0, "point_count": point_count, "reason": "activity_missing"}

        await conn.execute(
            """
            INSERT INTO strava_gps_streams (
                activity_id, latlng, altitude, distance, time, heartrate,
                cadence, watts, speed, grade_smooth, collected_at
            )
            VALUES (
                $1, $2::jsonb, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb,
                $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb, now()
            )
            ON CONFLICT (activity_id) DO UPDATE SET
                latlng = EXCLUDED.latlng,
                altitude = COALESCE(EXCLUDED.altitude, strava_gps_streams.altitude),
                distance = COALESCE(EXCLUDED.distance, strava_gps_streams.distance),
                time = COALESCE(EXCLUDED.time, strava_gps_streams.time),
                heartrate = COALESCE(EXCLUDED.heartrate, strava_gps_streams.heartrate),
                cadence = COALESCE(EXCLUDED.cadence, strava_gps_streams.cadence),
                watts = COALESCE(EXCLUDED.watts, strava_gps_streams.watts),
                speed = COALESCE(EXCLUDED.speed, strava_gps_streams.speed),
                grade_smooth = COALESCE(EXCLUDED.grade_smooth, strava_gps_streams.grade_smooth),
                collected_at = now()
            """,
            act_row["id"],
            json.dumps(latlng),
            _json_array_or_none(_stream_values(streams, "altitude")),
            _json_array_or_none(_stream_values(streams, "distance")),
            _json_array_or_none(_stream_values(streams, "time")),
            _json_array_or_none(_stream_values(streams, "heartrate")),
            _json_array_or_none(_stream_values(streams, "cadence")),
            _json_array_or_none(_stream_values(streams, "watts")),
            _json_array_or_none(_stream_values(streams, "speed")),
            _json_array_or_none(_stream_values(streams, "grade_smooth")),
        )

        fields = _derive_gps_route_fields(
            act_row["start_latlng"],
            act_row["end_latlng"],
            latlng,
        )
        await conn.execute(
            """
            UPDATE strava_activities
            SET start_latlng = COALESCE(start_latlng, $1),
                end_latlng = COALESCE(end_latlng, $2),
                stream_status = 'ok',
                privacy_zone_start = $3,
                privacy_zone_end = $4,
                truncation_point_start = COALESCE(truncation_point_start, $5),
                truncation_point_end = COALESCE(truncation_point_end, $6),
                summary_polyline = COALESCE(NULLIF(summary_polyline, ''), $7),
                metadata = (COALESCE(metadata, '{}'::jsonb)
                    - 'browser_stream_empty_count'
                    - 'browser_stream_empty_at'
                    - 'browser_stream_empty_defer_until') || jsonb_build_object(
                    'browser_stream_last_seen_at', now(),
                    'browser_stream_extension_version', $8::text,
                    'browser_stream_request_url', $9::text,
                    'browser_stream_point_count', $10::int
                ),
                collected_at = COALESCE(collected_at, now())
            WHERE id = $11
            """,
            fields["start_latlng"],
            fields["end_latlng"],
            fields["privacy_zone_start"],
            fields["privacy_zone_end"],
            fields["truncation_point_start"],
            fields["truncation_point_end"],
            fields["summary_polyline"] or None,
            body.get("extension_version"),
            body.get("request_url") or body.get("url"),
            point_count,
            act_row["id"],
        )
    return {"stored": 1, "point_count": point_count, "activity_id": activity_id}


async def _record_empty_strava_browser_stream(pool, activity_id: int, body: dict, point_count: int) -> dict:
    """Defer one empty browser stream, then stop retrying documented empties."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO strava_activities (platform_activity_id, metadata, collected_at)
            VALUES ($1, jsonb_build_object('browser_stream_stub', TRUE), now())
            ON CONFLICT (platform_activity_id) DO NOTHING
            """,
            activity_id,
        )
        row = await conn.fetchrow(
            """
            UPDATE strava_activities
            SET stream_status = CASE
                    WHEN COALESCE(NULLIF(metadata->>'browser_stream_empty_count', '')::int, 0) + 1 >= 2
                    THEN 'truncated_empty'
                    ELSE stream_status
                END,
                metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                    'browser_stream_empty_count', COALESCE(NULLIF(metadata->>'browser_stream_empty_count', '')::int, 0) + 1,
                    'browser_stream_empty_at', now(),
                    'browser_stream_empty_defer_until', CASE
                        WHEN COALESCE(NULLIF(metadata->>'browser_stream_empty_count', '')::int, 0) + 1 >= 2 THEN NULL
                        ELSE now() + interval '24 hours'
                    END,
                    'browser_stream_request_url', $2::text,
                    'browser_stream_http_status', 200
                ),
                collected_at = COALESCE(collected_at, now())
            WHERE platform_activity_id = $1
            RETURNING COALESCE(NULLIF(metadata->>'browser_stream_empty_count', '')::int, 0) AS empty_count,
                      stream_status
            """,
            activity_id,
            body.get("request_url") or body.get("url"),
        )
    empty_count = int(row["empty_count"] or 0) if row else 0
    terminal = bool(row and row["stream_status"] == "truncated_empty")
    return {
        "stored": 0,
        "point_count": point_count,
        "activity_id": activity_id,
        "reason": "truncated_empty" if terminal else "empty_stream_deferred",
        "empty_count": empty_count,
    }


def _browser_http_status(body: dict) -> int | None:
    for key in ("http_status", "status_code"):
        try:
            value = body.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _browser_account_label(body: dict, *, fallback: str = "extension") -> str:
    owner = body.get("owner") or body.get("owner_account") or body.get("account")
    if isinstance(owner, dict):
        owner = owner.get("username") or owner.get("id")
    text = str(owner or fallback).strip() or fallback
    return text[:128]


async def _record_strava_stream_http_event(pool, body: dict) -> bool:
    from . import (
        STRAVA_BROWSER_429_COOLDOWN_SECONDS,
        STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS,
        STRAVA_BROWSER_429_MEMORY_SECONDS,
        record_dynamic_cooldown,
    )
    status = _browser_http_status(body)
    if status not in {401, 403, 429}:
        return False
    activity_id = str(body.get("activity_id") or body.get("platform_activity_id") or "unknown")
    account = _browser_account_label(body)
    if await _touch_recent_strava_stream_http_event(pool, account, activity_id, status, body):
        return False
    cooldown = None
    dynamic_metadata = {}
    if status == 429:
        state = await record_dynamic_cooldown(
            pool,
            source="strava",
            scope="gps_streams",
            account=account,
            base_seconds=STRAVA_BROWSER_429_COOLDOWN_SECONDS,
            max_seconds=STRAVA_BROWSER_429_MAX_COOLDOWN_SECONDS,
            memory_seconds=STRAVA_BROWSER_429_MEMORY_SECONDS,
            write_source_cursor=True,
        )
        cooldown = state.seconds_remaining
        dynamic_metadata = {
            "dynamic_cooldown_service": state.service,
            "dynamic_cooldown_streak": state.streak,
        }
    await record_rate_limit_event(
        pool,
        source="strava",
        account=account,
        scope="browser_strava_streams",
        status_code=status,
        cooldown_seconds=cooldown,
        reason=f"browser Strava stream HTTP {status} for {activity_id}",
        metadata={
            "activity_id": activity_id,
            "request_url": body.get("request_url") or body.get("url"),
            "extension_version": body.get("extension_version"),
            "point_count": body.get("point_count"),
            "browser_capture": True,
            **dynamic_metadata,
        },
    )
    return True


async def _touch_recent_strava_stream_http_event(
    pool,
    account: str,
    activity_id: str,
    status: int,
    body: dict,
) -> bool:
    """Fold duplicate browser stream HTTP failures into the newest recent row."""
    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                """
                WITH latest AS (
                    SELECT id
                    FROM rate_limit_events
                    WHERE source = 'strava'
                      AND account IS NOT DISTINCT FROM $1
                      AND scope = 'browser_strava_streams'
                      AND status_code = $3
                      AND metadata->>'activity_id' = $2
                      AND created_at >= now() - interval '6 hours'
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                UPDATE rate_limit_events r
                SET metadata = r.metadata || jsonb_build_object(
                    'duplicate_suppressed_count',
                    COALESCE((r.metadata->>'duplicate_suppressed_count')::int, 0) + 1,
                    'duplicate_last_seen_at',
                    now(),
                    'latest_request_url',
                    $4::text,
                    'latest_extension_version',
                    $5::text,
                    'latest_point_count',
                    $6::int
                )
                FROM latest
                WHERE r.id = latest.id
                RETURNING 1
                """,
                account,
                activity_id,
                status,
                body.get("request_url") or body.get("url"),
                body.get("extension_version"),
                body.get("point_count"),
            ))
    except Exception:
        logger.debug(
            "recent Strava stream HTTP event touch failed account=%s activity=%s status=%s",
            account,
            activity_id,
            status,
            exc_info=True,
        )
        return False


def _strava_stream_response_status(result: dict, *, http_event_recorded: bool = False) -> int:
    """Return HTTP status for an accepted browser route-stream observation."""
    if result.get("stored") or http_event_recorded:
        return 200
    reason = result.get("reason")
    if reason == "no_route_points":
        return 200
    if reason == "bad_activity_id":
        return 400
    if reason == "no_pool":
        return 503
    return 422
