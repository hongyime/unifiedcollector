"""Follower/following discovery endpoints for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 4
(``docs/plans/perf-file-splits.md`` §4B).

The extension POSTs follower/following results here (POST /social/discover).
Currently only Instagram maintains a spider table (`instagram_spider_targets`);
other platforms flow through ``_record_users`` for author/edge tracking but
do not populate a spider queue.
"""
import logging

from aiohttp import web

from .constants import IG_SPIDER_FAMOUS_CAP, IG_SPIDER_MAX_HOP
from .cors import _cors


logger = logging.getLogger("social_ingest")


async def _discover(pool, platform, body):
    # Lazy import: _record_users still lives in __init__.py until later steps.
    from . import _record_users

    if platform != "instagram":
        return {"added": 0, "reason": "no spider for " + platform}
    try:
        src_hop = int(body.get("hop", 0))
    except (TypeError, ValueError):
        src_hop = 0
    source = body.get("source")
    discovered = body.get("discovered") or []
    target_hop = src_hop + 1
    if target_hop > IG_SPIDER_MAX_HOP:
        return {"added": 0, "reason": "max_hop"}
    added = 0
    async with pool.acquire() as conn:
        for d in discovered:
            uname = (d.get("username") or "").strip().lstrip("@") if isinstance(d, dict) else str(d).strip()
            if not uname:
                continue
            fc = d.get("follower_count") if isinstance(d, dict) else None
            if isinstance(fc, int) and fc > IG_SPIDER_FAMOUS_CAP:
                continue
            res = await conn.execute(
                """
                INSERT INTO instagram_spider_targets (username, hop, discovered_from, follower_count)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (username) DO NOTHING
                """,
                uname, target_hop, source, fc if isinstance(fc, int) else None,
            )
            if res.endswith("1"):
                added += 1
    # every discovered follower/following is a user we've now seen
    await _record_users(pool, platform, discovered, "follow")
    logger.info("discover[%s] from %s (hop %d): +%d new", platform, source, src_hop, added)
    return {"added": added}


async def discover(request):
    from . import _norm_platform, _safe_json

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    try:
        return _cors(web.json_response(await _discover(request.app["pool"], platform, body)))
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": 0, "error": "db"}, status=500))


async def discover_ig(request):  # /ig/discover alias
    from . import _safe_json

    body = await _safe_json(request)
    try:
        return _cors(web.json_response(await _discover(request.app["pool"], "instagram", body)))
    except Exception:
        logger.exception("discover failed")
        return _cors(web.json_response({"added": 0, "error": "db"}, status=500))
