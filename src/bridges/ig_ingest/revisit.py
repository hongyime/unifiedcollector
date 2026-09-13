"""Browser and TikTok revisit endpoints for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 6
(``docs/plans/perf-file-splits.md`` §4B).

The extension polls these to fetch the next queued item that needs a
re-scrape (e.g. the initial pass got only a thumbnail; a follow-up visit
should grab the full media). Two parallel queues:

- ``tiktok_browser_revisit_queue`` (single-platform queue, historical).
- ``browser_media_revisit_queue`` (multi-platform queue, current).

Endpoints:

- GET  /social/tiktok-revisit-target  → ``tiktok_revisit_target``
- POST /social/tiktok-revisit-result  → ``tiktok_revisit_result``
- GET  /social/browser-revisit-target → ``browser_revisit_target``
- POST /social/browser-revisit-result → ``browser_revisit_result``
"""
import json
import logging
import os
from datetime import datetime, timezone

from aiohttp import web

from .cors import _cors


logger = logging.getLogger("social_ingest")


async def tiktok_revisit_target(request):
    from . import (
        TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS as _hold,
        TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS as _timeout,
    )
    try:
        max_attempts = max(1, int(os.getenv("TIKTOK_BROWSER_REVISIT_MAX_ATTEMPTS", "5")))
    except (TypeError, ValueError):
        max_attempts = 5
    claim_timeout = _timeout
    claim_hold = _hold
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE tiktok_browser_revisit_queue
                SET status = 'failed',
                    reason = COALESCE(reason, 'exhausted_browser_revisit_attempts'),
                    next_visit_at = now() + interval '1 day',
                    metadata = metadata || jsonb_build_object(
                      'exhausted_browser_revisit_attempts', true,
                      'exhausted_at', now(),
                      'max_attempts', $1::int,
                      'previous_status', status
                    ),
                    updated_at = now()
                WHERE status IN ('claimed', 'pending')
                  AND attempts >= $1
                  AND COALESCE(last_attempt_at, updated_at, created_at)
                      <= now() - ($2::int * interval '1 second')
                """,
                max_attempts,
                claim_timeout,
            )
            row = await conn.fetchrow(
                """
                WITH picked AS (
                  SELECT id, status AS previous_status
                  FROM tiktok_browser_revisit_queue
                  WHERE (
                      (status IN ('pending', 'failed') AND next_visit_at <= now())
                      OR (
                        status = 'claimed'
                        AND COALESCE(last_attempt_at, updated_at, created_at)
                            <= now() - ($2::int * interval '1 second')
                      )
                    )
                    AND attempts < $1
                  ORDER BY
                    CASE WHEN status = 'claimed' THEN 0 ELSE 1 END,
                    priority DESC,
                    next_visit_at ASC,
                    created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE tiktok_browser_revisit_queue q
                SET status = 'claimed',
                    attempts = q.attempts + 1,
                    last_attempt_at = now(),
                    next_visit_at = now() + ($3::int * interval '1 second'),
                    metadata = q.metadata || jsonb_build_object(
                      'last_claim_previous_status', picked.previous_status,
                      'last_claimed_at', now()
                    ),
                    updated_at = now()
                FROM picked
                WHERE q.id = picked.id
                RETURNING q.content_id, q.username, q.post_url, q.source_url,
                          q.reason, q.priority, q.attempts, picked.previous_status,
                          q.metadata
                """,
                max_attempts,
                claim_timeout,
                claim_hold,
            )
        if not row:
            return _cors(web.json_response({"ok": True, "target": None}))
        target = dict(row)
        if isinstance(target.get("metadata"), str):
            try:
                target["metadata"] = json.loads(target["metadata"])
            except Exception:
                target["metadata"] = {"raw": target["metadata"]}
        if target.get("metadata") is None:
            target["metadata"] = {}
        return _cors(web.json_response({"ok": True, "target": target}, dumps=lambda v: json.dumps(v, default=str)))
    except Exception as exc:
        logger.debug("tiktok revisit target claim failed", exc_info=True)
        return _cors(web.json_response({"ok": False, "target": None, "error": str(exc)[:300]}, status=500))


async def tiktok_revisit_result(request):
    from . import _safe_json

    body = await _safe_json(request)
    content_id = str(body.get("content_id") or "").strip()
    if not content_id:
        return _cors(web.json_response({"ok": False, "error": "missing content_id"}, status=400))
    raw_status = str(body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or raw_status or "unknown")[:300]
    success = raw_status in {"success", "ok", "stored", "completed"}
    unavailable = raw_status in {"unavailable", "private", "deleted", "no_media"}
    status = "completed" if success else ("unavailable" if unavailable else "failed")
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE tiktok_browser_revisit_queue
                SET status = $2,
                    reason = COALESCE($3, reason),
                    last_success_at = CASE WHEN $2 = 'completed' THEN now() ELSE last_success_at END,
                    next_visit_at = CASE
                      WHEN $2 = 'completed' THEN next_visit_at
                      WHEN $2 = 'unavailable' THEN now() + interval '7 days'
                      ELSE now() + (LEAST(3600, GREATEST(120, attempts * 300)) * interval '1 second')
                    END,
                    metadata = metadata || $4::jsonb,
                    updated_at = now()
                WHERE content_id = $1
                """,
                content_id,
                status,
                reason,
                json.dumps(
                    {
                        "last_result": {
                            "status": raw_status or status,
                            "reason": body.get("reason"),
                            "stored": body.get("stored"),
                            "observed": body.get("observed"),
                            "extension_version": body.get("extension_version"),
                            "reported_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    default=str,
                ),
            )
        return _cors(web.json_response({"ok": True, "status": status}))
    except Exception as exc:
        logger.debug("tiktok revisit result update failed content_id=%s", content_id, exc_info=True)
        return _cors(web.json_response({"ok": False, "error": str(exc)[:300]}, status=500))


def _browser_revisit_platform(value: str | None) -> str | None:
    from . import _norm_platform

    platform = _norm_platform(value)
    if platform in {"instagram", "x", "facebook", "threads", "lemon8"}:
        return platform
    return None


async def browser_revisit_target(request):
    from . import (
        TIKTOK_BROWSER_REVISIT_CLAIM_HOLD_SECONDS as _hold,
        TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS as _timeout,
        _verify_url,
    )

    platform = _browser_revisit_platform(request.query.get("platform"))
    if not platform:
        return _cors(web.json_response({"ok": False, "target": None, "error": "unsupported platform"}, status=400))
    try:
        max_attempts = max(1, int(os.getenv("BROWSER_MEDIA_REVISIT_MAX_ATTEMPTS", "5")))
    except (TypeError, ValueError):
        max_attempts = 5
    claim_timeout = _timeout
    claim_hold = _hold
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE browser_media_revisit_queue
                SET status = 'failed',
                    reason = COALESCE(reason, 'exhausted_browser_revisit_attempts'),
                    next_visit_at = now() + interval '1 day',
                    metadata = metadata || jsonb_build_object(
                      'exhausted_browser_revisit_attempts', true,
                      'exhausted_at', now(),
                      'max_attempts', $2::int,
                      'previous_status', status
                    ),
                    updated_at = now()
                WHERE platform = $1
                  AND status IN ('claimed', 'pending')
                  AND attempts >= $2
                  AND COALESCE(last_attempt_at, updated_at, created_at)
                      <= now() - ($3::int * interval '1 second')
                """,
                platform,
                max_attempts,
                claim_timeout,
            )
            row = await conn.fetchrow(
                """
                WITH picked AS (
                  SELECT id, status AS previous_status
                  FROM browser_media_revisit_queue
                  WHERE platform = $1
                    AND (
                      (status IN ('pending', 'failed') AND next_visit_at <= now())
                      OR (
                        status = 'claimed'
                        AND COALESCE(last_attempt_at, updated_at, created_at)
                            <= now() - ($3::int * interval '1 second')
                      )
                    )
                    AND attempts < $2
                  ORDER BY
                    CASE WHEN status = 'claimed' THEN 0 ELSE 1 END,
                    priority DESC,
                    next_visit_at ASC,
                    created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE browser_media_revisit_queue q
                SET status = 'claimed',
                    attempts = q.attempts + 1,
                    last_attempt_at = now(),
                    next_visit_at = now() + ($4::int * interval '1 second'),
                    metadata = q.metadata || jsonb_build_object(
                      'last_claim_previous_status', picked.previous_status,
                      'last_claimed_at', now()
                    ),
                    updated_at = now()
                FROM picked
                WHERE q.id = picked.id
                RETURNING q.platform, q.content_id, q.username, q.post_url, q.source_url,
                          q.reason, q.priority, q.attempts, picked.previous_status,
                          q.metadata
                """,
                platform,
                max_attempts,
                claim_timeout,
                claim_hold,
            )
        if not row:
            return _cors(web.json_response({"ok": True, "target": None}))
        target = dict(row)
        if isinstance(target.get("metadata"), str):
            try:
                target["metadata"] = json.loads(target["metadata"])
            except Exception:
                target["metadata"] = {"raw": target["metadata"]}
        if target.get("metadata") is None:
            target["metadata"] = {}
        if platform == "instagram" and not target.get("post_url"):
            fallback_post_url = _verify_url("instagram", target.get("content_id"))
            if fallback_post_url:
                target["post_url"] = fallback_post_url
                try:
                    async with request.app["pool"].acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE browser_media_revisit_queue
                            SET post_url = COALESCE(post_url, $3),
                                metadata = metadata || jsonb_build_object(
                                  'post_url_synthesized_from_content_id', true
                                ),
                                updated_at = now()
                            WHERE platform = $1
                              AND content_id = $2
                            """,
                            platform,
                            target.get("content_id"),
                            fallback_post_url,
                        )
                except Exception:
                    logger.debug(
                        "browser media revisit post_url fallback persist failed platform=%s content_id=%s",
                        platform,
                        target.get("content_id"),
                        exc_info=True,
                    )
        return _cors(web.json_response({"ok": True, "target": target}, dumps=lambda v: json.dumps(v, default=str)))
    except Exception as exc:
        logger.debug("browser media revisit target claim failed platform=%s", platform, exc_info=True)
        return _cors(web.json_response({"ok": False, "target": None, "error": str(exc)[:300]}, status=500))


async def browser_revisit_result(request):
    from . import _safe_json

    body = await _safe_json(request)
    platform = _browser_revisit_platform(body.get("platform"))
    content_id = str(body.get("content_id") or "").strip()
    if not platform:
        return _cors(web.json_response({"ok": False, "error": "unsupported platform"}, status=400))
    if not content_id:
        return _cors(web.json_response({"ok": False, "error": "missing content_id"}, status=400))
    raw_status = str(body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or raw_status or "unknown")[:300]
    success = raw_status in {"success", "ok", "stored", "completed"}
    unavailable = raw_status in {"unavailable", "private", "deleted", "no_media", "missing_revisit_url"}
    status = "completed" if success else ("unavailable" if unavailable else "failed")
    try:
        async with request.app["pool"].acquire() as conn:
            await conn.execute(
                """
                UPDATE browser_media_revisit_queue
                SET status = $3,
                    reason = COALESCE($4, reason),
                    last_success_at = CASE WHEN $3 = 'completed' THEN now() ELSE last_success_at END,
                    next_visit_at = CASE
                      WHEN $3 = 'completed' THEN next_visit_at
                      WHEN $3 = 'unavailable' THEN now() + interval '7 days'
                      ELSE now() + (LEAST(3600, GREATEST(120, attempts * 300)) * interval '1 second')
                    END,
                    metadata = metadata || $5::jsonb,
                    updated_at = now()
                WHERE platform = $1 AND content_id = $2
                """,
                platform,
                content_id,
                status,
                reason,
                json.dumps(
                    {
                        "last_result": {
                            "status": raw_status or status,
                            "reason": body.get("reason"),
                            "stored": body.get("stored"),
                            "observed": body.get("observed"),
                            "extension_version": body.get("extension_version"),
                            "reported_at": datetime.now(timezone.utc).isoformat(),
                        }
                    },
                    default=str,
                ),
            )
        return _cors(web.json_response({"ok": True, "status": status}))
    except Exception as exc:
        logger.debug(
            "browser media revisit result update failed platform=%s content_id=%s",
            platform,
            content_id,
            exc_info=True,
        )
        return _cors(web.json_response({"ok": False, "error": str(exc)[:300]}, status=500))
