"""Target-selection endpoints for the ig_ingest bridge.

Extracted from ``__init__.py`` per PERF-003 step 4
(``docs/plans/perf-file-splits.md`` §4B).

Owns:

- ``/social/targets`` (``get_targets``) and its ``/ig/targets`` alias.
- ``_targets_for(pool, platform)`` — the DB query that unions
  ``collection_targets``, ``instagram_spider_targets``, and
  ``x_profile_targets`` into a single list.
- ``_cached_targets_for`` — TTL/stale-cache wrapper with a per-platform
  lock so slow queries don't starve the extension.
- ``_refresh_target_side_caches`` — inline-budgeted refresh of the
  proximity + priority side caches.

Mutable module state travels with these helpers (dicts and locks). Tests
reach for them via ``ig_ingest._SOCIAL_TARGET_RESPONSE_CACHE`` etc.; the
``__init__.py`` re-exports keep those paths working.
"""
import asyncio
import logging
import time

from aiohttp import web

from src.core.priority_hints import refresh_collector_priority_hints
from src.core.proximity import refresh_account_proximity_cache

from .constants import (
    IG_SPIDER_MAX_HOP,
    IG_SPIDER_TARGETS_LIMIT,
    SOCIAL_TARGET_CACHE_REFRESH_INLINE_BUDGET_SECONDS,
    SOCIAL_TARGET_CACHE_REFRESH_ON_REQUEST,
    SOCIAL_TARGET_CACHE_REFRESH_SECONDS,
    SOCIAL_TARGET_QUERY_TIMEOUT_SECONDS,
    SOCIAL_TARGET_RESPONSE_CACHE_SECONDS,
    SOCIAL_TARGET_STALE_RESPONSE_SECONDS,
)
from .cors import _cors


logger = logging.getLogger("social_ingest")


# Mutable module state. Dicts/sets/locks are mutated in-place; the float
# ``_SOCIAL_TARGET_CACHE_REFRESH_LAST`` is rebound via ``global``.
_SOCIAL_TARGET_CACHE_REFRESH_LAST = 0.0
_SOCIAL_TARGET_CACHE_REFRESH_LOCK: asyncio.Lock | None = None
_SOCIAL_TARGET_CACHE_REFRESH_TASKS: set[asyncio.Task] = set()
_SOCIAL_TARGET_RESPONSE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SOCIAL_TARGET_RESPONSE_LOCKS: dict[str, asyncio.Lock] = {}


async def _refresh_target_side_caches(pool) -> None:
    """Refresh target ranking side caches at most once per TTL.

    These cache builders can be expensive on the live DB. Running them for every
    browser /social/targets poll made target selection miss its response budget
    during active extension bursts.
    """
    # Read config and side-cache builders dynamically from the ``ig_ingest``
    # package namespace so tests can monkey-patch them via
    # ``ig_ingest.SOCIAL_TARGET_CACHE_REFRESH_SECONDS``, etc.
    from . import (  # noqa
        SOCIAL_TARGET_CACHE_REFRESH_SECONDS as _refresh_ttl,
        SOCIAL_TARGET_CACHE_REFRESH_INLINE_BUDGET_SECONDS as _budget,
        refresh_account_proximity_cache as _refresh_proximity,
        refresh_collector_priority_hints as _refresh_priority,
    )
    global _SOCIAL_TARGET_CACHE_REFRESH_LAST, _SOCIAL_TARGET_CACHE_REFRESH_LOCK
    ttl = max(0, _refresh_ttl)
    now = time.time()
    if ttl and now - _SOCIAL_TARGET_CACHE_REFRESH_LAST < ttl:
        return
    lock = _SOCIAL_TARGET_CACHE_REFRESH_LOCK
    if lock is None:
        lock = asyncio.Lock()
        _SOCIAL_TARGET_CACHE_REFRESH_LOCK = lock
    if lock.locked():
        return

    async def _runner() -> None:
        global _SOCIAL_TARGET_CACHE_REFRESH_LAST
        async with lock:
            now = time.time()
            if ttl and now - _SOCIAL_TARGET_CACHE_REFRESH_LAST < ttl:
                return
            _SOCIAL_TARGET_CACHE_REFRESH_LAST = now
            try:
                await _refresh_proximity(pool)
                await _refresh_priority(pool)
                _SOCIAL_TARGET_CACHE_REFRESH_LAST = time.time()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("target side-cache refresh failed", exc_info=True)

    task = asyncio.create_task(_runner())
    _SOCIAL_TARGET_CACHE_REFRESH_TASKS.add(task)
    task.add_done_callback(_SOCIAL_TARGET_CACHE_REFRESH_TASKS.discard)
    budget = max(0.0, _budget)
    if budget <= 0:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except asyncio.TimeoutError:
        logger.info(
            "target side-cache refresh continuing in background after %.2fs",
            budget,
        )


async def _targets_for(pool, platform):
    """seed targets (collection_targets) UNION instagram spider targets (IG only)."""
    seen = set()
    out = []
    if platform == "x":
        try:
            async with pool.acquire() as conn:
                seeds = await conn.fetch(
                    """
                    SELECT target_id AS username, priority
                    FROM collection_targets
                    WHERE source = 'x'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                queued = await conn.fetch(
                    """
                    SELECT username, source, priority, status, next_visit_at
                    FROM x_profile_targets
                    WHERE status IN ('pending', 'completed', 'failed')
                    ORDER BY
                        CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                        priority DESC,
                        next_visit_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                for row in list(seeds) + list(queued):
                    r = dict(row)
                    u = (r.get("username") or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({
                            "username": u,
                            "hop": 0,
                            "source": r.get("source") or "collection_targets",
                            "priority": int(r.get("priority") or 0),
                            "status": r.get("status") or "pending",
                            "next_visit_at": r["next_visit_at"].isoformat() if r.get("next_visit_at") else None,
                        })
        except Exception:
            logger.exception("targets query failed (%s)", platform)
        return out
    try:
        # Read the refresh-on-request flag dynamically for test monkeypatching.
        from . import SOCIAL_TARGET_CACHE_REFRESH_ON_REQUEST as _refresh_on_request
        if _refresh_on_request:
            await _refresh_target_side_caches(pool)
        async with pool.acquire() as conn:
            seeds = await conn.fetch(
                """
                SELECT ct.target_id, ct.priority
                FROM collection_targets ct
                WHERE ct.source = $1
                ORDER BY
                    ct.priority DESC,
                    ct.created_at ASC
                """,
                platform,
            )
            for r in seeds:
                u = r["target_id"]
                if u and u not in seen:
                    seen.add(u)
                    out.append({"username": u, "hop": 0})
            if platform == "instagram":
                spider = await conn.fetch(
                    """
                    SELECT s.username, s.hop
                    FROM instagram_spider_targets s
                    WHERE s.status='active' AND s.hop <= $1
                    ORDER BY
                        s.hop ASC,
                        s.last_scraped_at ASC NULLS FIRST,
                        s.discovered_at ASC
                    LIMIT $2
                    """,
                    IG_SPIDER_MAX_HOP,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                for r in spider:
                    u = r["username"]
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": int(r["hop"])})
            elif platform == "threads":
                # REVERSE cross-pollination: a Threads handle IS an Instagram handle,
                # so the real people we know on Instagram (your follow graph + spider)
                # are scrapeable Threads profiles. Hand them to the Threads tab to visit.
                ig = await conn.fetch(
                    """
                    SELECT s.username
                    FROM instagram_spider_targets s
                    WHERE s.status='active'
                    ORDER BY
                        s.last_scraped_at ASC NULLS FIRST,
                        s.discovered_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                ig2 = await conn.fetch(
                    """
                    SELECT ct.target_id AS username
                    FROM collection_targets ct
                    WHERE ct.source='instagram'
                    ORDER BY
                        ct.priority DESC,
                        ct.created_at ASC
                    """
                )
                for r in list(ig) + list(ig2):
                    u = (r["username"] or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({"username": u, "hop": 1})
            elif platform == "x":
                queued = await conn.fetch(
                    """
                    SELECT username, source, priority, status, next_visit_at
                    FROM x_profile_targets
                    WHERE status IN ('pending', 'completed', 'failed')
                    ORDER BY
                        CASE status WHEN 'pending' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                        priority DESC,
                        next_visit_at ASC
                    LIMIT $1
                    """,
                    IG_SPIDER_TARGETS_LIMIT,
                )
                for r in queued:
                    u = (r["username"] or "").strip().lstrip("@")
                    if u and u not in seen:
                        seen.add(u)
                        out.append({
                            "username": u,
                            "hop": 0,
                            "source": r["source"],
                            "priority": int(r["priority"] or 0),
                            "status": r["status"],
                            "next_visit_at": r["next_visit_at"].isoformat() if r["next_visit_at"] else None,
                        })
    except Exception:
        logger.exception("targets query failed (%s)", platform)
    return out


async def _cached_targets_for(pool, platform):
    # Read TTL / query-timeout / stale-window and the ``_targets_for`` symbol
    # dynamically from the ig_ingest package namespace so tests can
    # monkey-patch them via ``ig_ingest.<name>``.
    from . import (  # noqa
        SOCIAL_TARGET_RESPONSE_CACHE_SECONDS as _ttl_cfg,
        SOCIAL_TARGET_STALE_RESPONSE_SECONDS as _stale_cfg,
        SOCIAL_TARGET_QUERY_TIMEOUT_SECONDS as _qtimeout_cfg,
        _targets_for as _targets_for_impl,
    )
    ttl = max(0.0, _ttl_cfg)
    stale_ttl = max(ttl, _stale_cfg)
    query_timeout = max(0.1, _qtimeout_cfg)
    now = time.time()
    cached = _SOCIAL_TARGET_RESPONSE_CACHE.get(platform)
    if ttl and cached and now - cached[0] <= ttl:
        return cached[1]
    lock = _SOCIAL_TARGET_RESPONSE_LOCKS.get(platform)
    if lock is None:
        lock = asyncio.Lock()
        _SOCIAL_TARGET_RESPONSE_LOCKS[platform] = lock
    if lock.locked() and cached:
        return cached[1]
    async with lock:
        now = time.time()
        cached = _SOCIAL_TARGET_RESPONSE_CACHE.get(platform)
        if ttl and cached and now - cached[0] <= ttl:
            return cached[1]
        try:
            out = await asyncio.wait_for(_targets_for_impl(pool, platform), timeout=query_timeout)
        except asyncio.TimeoutError:
            if cached and now - cached[0] <= stale_ttl:
                logger.warning(
                    "targets query timed out for %s after %.1fs; serving stale cache age=%.0fs",
                    platform,
                    query_timeout,
                    now - cached[0],
                )
                return cached[1]
            logger.warning(
                "targets query timed out for %s after %.1fs; serving empty list",
                platform,
                query_timeout,
            )
            return []
        _SOCIAL_TARGET_RESPONSE_CACHE[platform] = (time.time(), out)
        return out


async def get_targets(request):
    # Lazy import to avoid circular: _norm_platform still lives in __init__.py.
    from . import _norm_platform

    platform = _norm_platform(request.query.get("platform"))
    out = await _cached_targets_for(request.app["pool"], platform)
    return _cors(web.json_response({
        "platform": platform,
        "targets": out,
        "usernames": [t["username"] for t in out],  # back-compat
        "max_hop": IG_SPIDER_MAX_HOP if platform == "instagram" else 0,
    }))


async def get_targets_ig(request):  # /ig/targets alias
    request.query  # noqa
    out = await _cached_targets_for(request.app["pool"], "instagram")
    return _cors(web.json_response({
        "targets": out,
        "usernames": [t["username"] for t in out],
        "max_hop": IG_SPIDER_MAX_HOP,
    }))
