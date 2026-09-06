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
