"""aiohttp middlewares for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 (see
``docs/plans/perf-file-splits.md`` §4B). Three middlewares run in order
around every request:

1. ``request_timeout_middleware`` — path-tuned ``asyncio.timeout`` wrapper.
2. ``lane_isolation_middleware`` — per-lane semaphores so heartbeat/write/
   revisit/dm_sample traffic can't starve each other.
3. ``db_pool_middleware`` — lazy DB-pool bootstrap (delegates to the
   startup-state helpers still defined in ``__init__.py`` — those move to
   ``pool.py`` in step 3 of the split plan).

The path-classifier helpers (``_social_ingest_lane_for_path``,
``_request_timeout_seconds``) and the lane-path sets travel with the
middlewares — they're only meaningful together.

``__init__.py`` re-exports all these names for test back-compat
(``ig_ingest.lane_isolation_middleware``, ``ig_ingest._social_ingest_lane_for_path``,
etc.).
"""
import asyncio
import logging

from aiohttp import web

from .constants import (
    SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS,
    SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS,
    SOCIAL_INGEST_LANE_WAIT_SECONDS,
    SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS,
    SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS,
    SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS,
)
from .cors import _cors


logger = logging.getLogger("social_ingest")


_STRUCTURED_CAPTURE_PATHS = {
    "/social/browser-media-candidates",
    "/social/comments",
    "/social/discover",
    "/social/dm-decoded",
    "/social/dm-frame",
    "/social/dm-probe",
    "/social/dm-sample",
    "/social/dms",
    "/social/posts",
    "/social/profile",
    "/social/seed",
    "/social/strava-route-visit",
    "/social/strava-streams",
    "/social/target-status",
    "/social/users",
    "/social/x-profile-target-result",
}


_HEARTBEAT_LANE_PATHS = {
    "/social/browser-heartbeat",
    "/social/dm-heartbeat",
}
_REVISIT_LANE_PATHS = {
    "/social/browser-revisit-target",
    "/social/browser-revisit-result",
    "/social/tiktok-revisit-target",
    "/social/tiktok-revisit-result",
    "/social/x-profile-target",
    "/social/x-profile-target-result",
}
_DM_SAMPLE_LANE_PATHS = {
    "/social/dm-sample",
}
_WRITE_LANE_PATHS = {
    "/ig/discover",
    "/ig/ingest",
    "/social/browser-media-candidates",
    "/social/comments",
    "/social/cookies",
    "/social/discover",
    "/social/dm-decoded",
    "/social/dm-frame",
    "/social/dm-probe",
    "/social/dms",
    "/social/ingest",
    "/social/ingest-upload",
    "/social/ingest-upload-binary",
    "/social/posts",
    "/social/profile",
    "/social/seed",
    "/social/strava-route-visit",
    "/social/strava-streams",
    "/social/target-status",
    "/social/users",
}
_FAST_LANE_PATHS = {
    "/health",
    "/metrics",
    "/ready",
    "/social/ig_cooldown",
    "/social/strava-route-queue",
    "/social/targets",
    "/ig/targets",
}


def _social_ingest_lane_for_path(path: str) -> str | None:
    if path in _HEARTBEAT_LANE_PATHS:
        return "heartbeat"
    if path in _DM_SAMPLE_LANE_PATHS:
        return "dm_sample"
    if path in _REVISIT_LANE_PATHS:
        return "revisit"
    if path in _WRITE_LANE_PATHS or path in _STRUCTURED_CAPTURE_PATHS:
        return "write"
    if path in _FAST_LANE_PATHS:
        return None
    return None


def _request_timeout_seconds(path: str) -> float:
    # Read timeouts dynamically from the ig_ingest package namespace so tests
    # that monkey-patch ``ig_ingest.SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS`` still
    # take effect after this move. The values are also re-exported from
    # ``__init__.py`` via ``from .constants import *``.
    from . import (
        SOCIAL_INGEST_HEARTBEAT_REQUEST_TIMEOUT_SECONDS as _heartbeat,
        SOCIAL_INGEST_REQUEST_TIMEOUT_SECONDS as _default,
        SOCIAL_INGEST_STRUCTURED_REQUEST_TIMEOUT_SECONDS as _structured,
        SOCIAL_INGEST_UPLOAD_REQUEST_TIMEOUT_SECONDS as _upload,
    )
    if path in {"/social/ingest-upload", "/social/ingest-upload-binary"}:
        return _upload
    if path == "/social/browser-heartbeat":
        return _heartbeat
    if path in _STRUCTURED_CAPTURE_PATHS:
        return _structured
    return _default


@web.middleware
async def request_timeout_middleware(request, handler):
    timeout_seconds = _request_timeout_seconds(request.path)
    try:
        async with asyncio.timeout(timeout_seconds):
            return await handler(request)
    except TimeoutError:
        logger.warning(
            "social ingest request timed out after %.2fs method=%s path=%s",
            timeout_seconds,
            request.method,
            request.path,
        )
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "handler_timeout",
                "path": request.path,
            },
            status=503,
        ))


@web.middleware
async def lane_isolation_middleware(request, handler):
    if request.method == "OPTIONS":
        return await handler(request)
    lane = _social_ingest_lane_for_path(request.path)
    if not lane:
        return await handler(request)
    sem = request.app.get(f"{lane}_lane_sem")
    if sem is None:
        return await handler(request)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=SOCIAL_INGEST_LANE_WAIT_SECONDS)
    except TimeoutError:
        logger.warning("social ingest lane busy lane=%s path=%s", lane, request.path)
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "busy_retry",
                "lane": lane,
                "path": request.path,
                "retry_after": 1,
            },
            status=503,
        ))
    try:
        return await handler(request)
    finally:
        sem.release()


@web.middleware
async def db_pool_middleware(request, handler):
    # ``_ensure_app_pool`` / ``_set_startup_error`` live in ``.pool`` (extracted
    # in split step 3). Direct top-level import is safe since ``.pool`` only
    # depends on ``.constants`` — no cycle.
    from .pool import _ensure_app_pool, _set_startup_error

    if request.method == "OPTIONS" or request.path in {
        "/health",
        "/social/browser-heartbeat",
        "/social/dm-heartbeat",
    }:
        return await handler(request)
    try:
        await _ensure_app_pool(request.app)
    except TimeoutError:
        _set_startup_error(request.app, "db_pool_lazy_timeout")
        logger.warning(
            "social ingest lazy DB pool init timed out after %.2fs path=%s",
            SOCIAL_INGEST_DB_INIT_TIMEOUT_SECONDS,
            request.path,
        )
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "db_pool_timeout",
                "path": request.path,
            },
            status=503,
        ))
    except Exception as exc:
        _set_startup_error(request.app, f"db_pool_lazy_error:{exc.__class__.__name__}")
        logger.exception("social ingest lazy DB pool init failed path=%s", request.path)
        return _cors(web.json_response(
            {
                "ok": False,
                "error": "db_pool_error",
                "path": request.path,
            },
            status=503,
        ))
    return await handler(request)
