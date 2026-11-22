"""Browser content recovery / timeout hints.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from .constants import BROWSER_CONTENT_HINT_TTL_SECONDS
from .constants import BROWSER_HEARTBEAT_ONLY_PLATFORMS
from .constants import X_ZERO_PROGRESS_PROBES
from .constants import _BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS
import asyncio
import time

logger = logging.getLogger("social_ingest")


async def _browser_content_recovery_hint(pool, platform: str) -> dict:
    from . import BROWSER_CONTENT_STALE_SECONDS
    if (
        not pool
        or not platform
        or platform == "bridge"
        or platform in BROWSER_HEARTBEAT_ONLY_PLATFORMS
    ):
        return {}
    now = time.monotonic()
    cached = _BROWSER_CONTENT_HINT_CACHE.get(platform)
    if cached and now - cached[0] < BROWSER_CONTENT_HINT_TTL_SECONDS:
        return dict(cached[1])
    if platform in _BROWSER_CONTENT_HINT_INFLIGHT:
        return {"force_cycle": False, "force_reason": "content_age_check_pending"}
    _BROWSER_CONTENT_HINT_INFLIGHT.add(platform)
    try:
        async with asyncio.timeout(1.0):
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT EXTRACT(EPOCH FROM (NOW() - created_at))::int AS age_seconds
                    FROM browser_ingest_events
                    WHERE platform = $1
                      AND endpoint <> 'browser_heartbeat'
                      AND (
                        observed_count > 0
                        OR stored_count > 0
                        OR (
                          metadata ? 'probe_reason'
                          AND COALESCE(metadata->>'probe_reason', '')
                              NOT IN ('manual_backend_probe', 'forced_recovery_started', 'recoverable_error_shell')
                          AND NOT (
                            $1 = 'x'
                            AND observed_count <= 0
                            AND stored_count <= 0
                            AND COALESCE(metadata->>'probe_reason', '') = ANY($2::text[])
                          )
                        )
                      )
                      AND (
                        stored_count > 0
                        OR endpoint IN ('posts', 'profile', 'strava_route_visit', 'strava_streams')
                        OR (
                          metadata ? 'probe_reason'
                          AND COALESCE(metadata->>'probe_reason', '')
                              NOT IN ('manual_backend_probe', 'forced_recovery_started', 'recoverable_error_shell')
                          AND NOT (
                            $1 = 'x'
                            AND observed_count <= 0
                            AND stored_count <= 0
                            AND COALESCE(metadata->>'probe_reason', '') = ANY($2::text[])
                          )
                        )
                      )
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    platform,
                    sorted(X_ZERO_PROGRESS_PROBES),
                )
        age = row["age_seconds"] if row else None
        if age is None or int(age) > BROWSER_CONTENT_STALE_SECONDS:
            hint = {
                "force_cycle": True,
                "force_reason": "browser_content_stale",
                "content_age_seconds": int(age) if age is not None else None,
                "stale_after_seconds": BROWSER_CONTENT_STALE_SECONDS,
            }
            _BROWSER_CONTENT_HINT_CACHE[platform] = (now, hint)
            return dict(hint)
        _BROWSER_CONTENT_HINT_CACHE[platform] = (now, {})
    except TimeoutError:
        return _browser_content_timeout_hint(platform, "content_age_check_timeout")
    except Exception:
        logger.debug("browser content recovery hint failed platform=%s", platform, exc_info=True)
    finally:
        _BROWSER_CONTENT_HINT_INFLIGHT.discard(platform)
    return {}


def _browser_content_timeout_hint(platform: str, reason: str) -> dict:
    from . import BROWSER_CONTENT_STALE_SECONDS
    if platform in _BROWSER_CONTENT_HINT_FAIL_ACTIVE_PLATFORMS:
        return {
            "force_cycle": True,
            "force_reason": reason,
            "content_age_seconds": None,
            "stale_after_seconds": BROWSER_CONTENT_STALE_SECONDS,
        }
    return {"force_cycle": False, "force_reason": reason}


# Mutable module state — must remain in __init__.py so that ``global``
# reassignments in helpers still find these names. Tests reach for these
# via ``ig_ingest._BROWSER_CONTENT_HINT_CACHE``, etc.
_BROWSER_CONTENT_HINT_CACHE: dict[str, tuple[float, dict]] = {}


_BROWSER_CONTENT_HINT_INFLIGHT: set[str] = set()
