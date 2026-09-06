"""X profile target claim/result endpoints for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 6
(``docs/plans/perf-file-splits.md`` §4B).

The X collector claims a next-due profile via
``GET /social/x-profile-target`` and reports the outcome back via
``POST /social/x-profile-target-result``. The queue is
``x_profile_targets`` (writes here happen inline).
"""
import json
import logging

from aiohttp import web

from .constants import (
    X_PROFILE_TARGET_RETRY_SECONDS,
    X_PROFILE_TARGET_REVISIT_SECONDS,
)
from .cors import _cors


logger = logging.getLogger("social_ingest")


async def x_profile_target_next(request):
    owner = (request.query.get("owner") or "").strip().lstrip("@")
    try:
        async with request.app["pool"].acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT username
                    FROM x_profile_targets
                    WHERE status IN ('pending', 'completed', 'failed', 'claimed')
                      AND next_visit_at <= now()
                    ORDER BY
                        CASE status
                          WHEN 'pending' THEN 0
                          WHEN 'failed' THEN 1
                          WHEN 'completed' THEN 2
                          ELSE 3
                        END,
                        priority DESC,
                        last_success_at ASC NULLS FIRST,
                        updated_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE x_profile_targets t
                SET status = 'claimed',
                    attempts = attempts + 1,
                    last_attempt_at = now(),
                    next_visit_at = now() + ($1::int * interval '1 second'),
                    metadata = metadata || $2::jsonb,
                    updated_at = now()
                FROM candidate
                WHERE t.username = candidate.username
                RETURNING t.username, t.source, t.priority, t.status,
                          t.attempts, t.last_success_at, t.next_visit_at
                """,
                X_PROFILE_TARGET_RETRY_SECONDS,
                json.dumps({"claimed_by": owner or None}),
            )
        target = None
        if row:
            target = {
                k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in dict(row).items()
            }
        return _cors(web.json_response({
            "ok": True,
            "target": target,
            "revisit_seconds": X_PROFILE_TARGET_REVISIT_SECONDS,
        }))
    except Exception as e:
        logger.warning("x profile target next failed: %s", e)
        return _cors(web.json_response({"ok": False, "target": None, "error": str(e)}, status=500))


async def x_profile_target_result(request):
    from . import _safe_json, _x_handle

    body = await _safe_json(request)
    username = _x_handle(body.get("username"))
    status = str(body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or body.get("error") or "").strip()[:500] or None
    owner = str(body.get("owner") or "").strip().lstrip("@") or None
    if not username:
        return _cors(web.json_response({"ok": False, "error": "missing_username"}, status=400))
    if status in ("success", "completed", "ok"):
        next_seconds = X_PROFILE_TARGET_REVISIT_SECONDS
        db_status = "completed"
    elif status in ("unavailable", "missing", "not_found", "protected"):
        next_seconds = 7 * 24 * 60 * 60
        db_status = "unavailable"
    else:
        next_seconds = X_PROFILE_TARGET_RETRY_SECONDS
        db_status = "failed"
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                INSERT INTO x_profile_targets
                    (username, source, priority, status, next_visit_at, last_error, metadata)
                VALUES ($1, 'result', 50, $2, now() + ($3::int * interval '1 second'), $4, $5::jsonb)
                ON CONFLICT (username) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_success_at = CASE WHEN EXCLUDED.status = 'completed' THEN now() ELSE x_profile_targets.last_success_at END,
                    next_visit_at = EXCLUDED.next_visit_at,
                    last_error = EXCLUDED.last_error,
                    metadata = x_profile_targets.metadata || EXCLUDED.metadata,
                    updated_at = now()
                """,
                username,
                db_status,
                next_seconds,
                reason,
                json.dumps({"result_owner": owner, "result_status": status or db_status}),
            )
        return _cors(web.json_response({"ok": True, "username": username, "status": db_status}))
    except Exception as e:
        logger.warning("x profile target result failed: %s", e)
        return _cors(web.json_response({"ok": False, "error": str(e)}, status=500))
