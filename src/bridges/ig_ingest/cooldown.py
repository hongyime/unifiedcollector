"""Instagram cooldown status endpoint.

Extracted from ``__init__.py`` per PERF-003 step 10
(``docs/plans/perf-file-splits.md`` §4B).

The extension polls this to cooperate with the headless collector's 429
cooldown state — both paths share the same account, so a headless throttle
must also pause the extension. Cooldown is derived from both
``rate_limit_events`` (per-account, current) and legacy ``service_cursors``
(``instagram_rate_limit:<account>``).
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from aiohttp import web

from .cors import _cors


logger = logging.getLogger("social_ingest")


async def ig_cooldown(request):
    from . import IG_COOLDOWN_READ_TIMEOUT_SECONDS

    pool = request.app["pool"]
    account = str(request.query.get("account") or request.query.get("owner") or "").strip() or None
    cooling, secs_left, streak = False, 0, 0
    degraded = False
    try:
        async with asyncio.timeout(IG_COOLDOWN_READ_TIMEOUT_SECONDS):
            async with pool.acquire() as conn:
                if account:
                    event = await conn.fetchrow(
                        """
                        SELECT created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') AS active_until,
                               CASE
                                 WHEN metadata->>'streak' ~ '^[0-9]+$' THEN (metadata->>'streak')::int
                                 ELSE NULL
                               END AS streak,
                               account
                        FROM rate_limit_events
                        WHERE source = 'instagram'
                          AND status_code = 429
                          AND account = $1
                          AND COALESCE(cooldown_seconds, 0) > 0
                          AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > NOW()
                        ORDER BY active_until DESC
                        LIMIT 1
                        """,
                        account,
                    )
                    if event and event["active_until"]:
                        active_until = event["active_until"]
                        if active_until.tzinfo is None:
                            active_until = active_until.replace(tzinfo=timezone.utc)
                        left = (active_until - datetime.now(timezone.utc)).total_seconds()
                        if left > 0:
                            cooling, secs_left = True, int(left)
                            streak = int(event["streak"] or 0)
                cursor_service = (
                    f"instagram_rate_limit:{account}"
                    if account
                    else "instagram_rate_limit"
                )
                if not cooling:
                    row = await conn.fetchval(
                        "SELECT last_processed_id FROM service_cursors WHERE service=$1",
                        cursor_service,
                    )
                    if row and ":" in str(row):
                        exp_s, streak_s = str(row).split(":", 1)
                        expiry = float(exp_s)
                        streak = int(float(streak_s))
                        left = expiry - time.time()
                        if left > 0:
                            cooling, secs_left = True, int(left)
    except TimeoutError:
        degraded = True
        logger.info(
            "ig_cooldown read timed out after %.2fs account=%s",
            IG_COOLDOWN_READ_TIMEOUT_SECONDS,
            account or "",
        )
    except Exception:
        degraded = True
        logger.debug("ig_cooldown read failed", exc_info=True)
    return _cors(web.json_response({
        "cooling": cooling,
        "secs_left": secs_left,
        "streak": streak,
        "account": account,
        "scope": "account" if account else "legacy_global",
        "cooldown_degraded": degraded,
    }))
