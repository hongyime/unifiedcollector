"""Back-compat legacy HTTP handlers.

Split out of ``__init__.py`` (split step 11); re-exported there.
"""
import logging
from ._ids import _VERIFIED_FILTER_PLATFORMS
from ._ids import _explicitly_verified_capture
from ._ids import _norm_platform
from ._posts import _save_comments
from ._profiles import _record_ig_access
from ._util import _safe_json
from .constants import SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY
from .cors import _cors
from aiohttp import web
from src.core.collection_surface import normalize_collection_surface
import asyncio

logger = logging.getLogger("social_ingest")


async def _legacy_target_status_handler(request):
    """Let the extension retire bad spider targets.

    Instagram returns HTTP 400/404 for some unavailable/private/deleted profile
    lookups. Those are not throttles and should not keep rotating through the
    active spider queue forever.
    """
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    username = (body.get("username") or "").strip().lstrip("@")
    status = (body.get("status") or "").strip().lower()
    reason = str(body.get("reason") or "")[:120]
    if platform != "instagram" or not username:
        return _cors(web.json_response({"updated": 0}))
    if status not in {"unavailable", "skipped"}:
        status = "unavailable"
    updated = 0
    async with request.app["pool"].acquire() as conn:
        result = await conn.execute(
            """
            UPDATE instagram_spider_targets
            SET status = $2,
                last_scraped_at = now()
            WHERE username = $1
              AND status = 'active'
            """,
            username, status,
        )
        if result.endswith("1"):
            updated = 1
    if updated:
        logger.info("target-status[%s] %s -> %s (%s)", platform, username, status, reason)
    return _cors(web.json_response({"updated": updated, "status": status}))


async def _legacy_users_handler(request):
    from . import _record_users
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    n = await _record_users(request.app["pool"], platform, body.get("users") or [],
                            body.get("context") or "seen", owner=body.get("owner"))
    return _cors(web.json_response({"recorded": n}))


async def _legacy_seed_handler(request):
    """Seed the spider from the user's own followers/following as hop-0 targets."""
    from . import _record_users
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    raw_users = body.get("users") or []
    users = [u for u in raw_users if not _explicitly_verified_capture(u)] if isinstance(raw_users, list) else []
    added = 0
    if platform == "instagram":
        pool = request.app["pool"]
        async with pool.acquire() as conn:
            for u in users:
                uname = (u.get("username") if isinstance(u, dict) else str(u) or "").strip().lstrip("@")
                if not uname:
                    continue
                res = await conn.execute(
                    """
                    INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                    VALUES ($1, 0, 'self') ON CONFLICT (username) DO NOTHING
                    """,
                    uname,
                )
                if res.endswith("1"):
                    added += 1
        await _record_users(request.app["pool"], platform, users, "follow")
    return _cors(web.json_response({"added": added}))


async def _legacy_profile_handler(request):
    from . import _archive_browser_capture, _ingest, _record_browser_ingest_event, _save_profile
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    p = body.get("profile") or {}
    if platform in _VERIFIED_FILTER_PLATFORMS and (
        _explicitly_verified_capture(body) or _explicitly_verified_capture(p)
    ):
        return _cors(web.json_response({"ok": True, "excluded_verified": True}))
    await _archive_browser_capture(request.app["pool"], platform, "profile", body)
    saved = await _save_profile(request.app["pool"], platform, p)
    await _record_browser_ingest_event(
        request.app["pool"],
        platform,
        "profile",
        (p.get("username") or body.get("username") or "").strip().lstrip("@") or None,
        observed_count=1 if p else 0,
        stored_count=1 if saved else 0,
    )
    # Follow-aware access recording (Phase 0): the extension just successfully
    # fetched this profile with its logged-in account (body["owner"]) — so that
    # account CAN see the target. Populates profile_access for the selector.
    if platform == "instagram":
        await _record_ig_access(
            request.app["pool"], (p.get("username") or "").strip().lstrip("@"),
            body.get("owner"), p,
        )
    # download the profile photo as a kind=profile media item
    pic = p.get("profile_pic_url")
    uname = (p.get("username") or "").strip().lstrip("@")
    # Browser profile canaries only persist fields and receipts. Their images
    # stay disabled until a provider-authorized qualified-media path exists.
    if platform == "instagram" and saved and pic and uname:
        await _ingest(request.app, platform, {"username": uname, "items": [
            {"url": pic, "content_id": str(p.get("user_id") or uname), "content_type": "photo",
             "kind": "profile", "entity_name": uname}]})
    return _cors(web.json_response({"ok": True}))


async def _legacy_posts_handler(request):
    from . import _schedule_app_task
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    raw_posts = body.get("posts") or []
    if not isinstance(raw_posts, list):
        raw_posts = []
    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return _cors(web.json_response({
            "ok": True,
            "queued": False,
            "observed": len(raw_posts),
            "excluded_verified": len(raw_posts),
            "excluded_verified_target": True,
            "saved": 0,
        }))
    posts = (
        [post for post in raw_posts if not _explicitly_verified_capture(post)]
        if platform in _VERIFIED_FILTER_PLATFORMS else raw_posts
    )
    body = {**body, "posts": posts}
    _schedule_app_task(
        request.app,
        _posts_ingest_background(request.app, platform, body),
        "browser_posts_ingest",
    )
    return _cors(web.json_response({
        "ok": True,
        "queued": True,
        "observed": len(raw_posts),
        "excluded_verified": len(raw_posts) - len(posts),
        "saved": 0,
    }))


async def _posts_ingest_background(app, platform: str, body: dict) -> None:
    from . import _archive_browser_capture, _record_browser_ingest_event, _record_users, _save_posts
    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return
    posts = body.get("posts") or []
    if not isinstance(posts, list):
        posts = []
    else:
        if platform in _VERIFIED_FILTER_PLATFORMS:
            posts = [post for post in posts if not _explicitly_verified_capture(post)]
            body = {**body, "posts": posts}
    sem = app.get("structured_sem")
    if sem is None:
        sem = asyncio.Semaphore(SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY)
        app["structured_sem"] = sem
    surface = normalize_collection_surface(body.get("collection_surface") or body.get("surface"))
    async with sem:
        await _archive_browser_capture(app["pool"], platform, "posts", body)
        n = await _save_posts(app["pool"], platform, posts, surface)
        await _record_browser_ingest_event(
            app["pool"],
            platform,
            "posts",
            body.get("username") or body.get("owner"),
            observed_count=len(posts),
            stored_count=n,
            collection_surface=surface,
            failed_count=max(0, len(posts) - n),
            outcome="partial" if n and n < len(posts) else "failed" if posts and not n else "success" if n else "empty",
            error_class="post_row_not_saved" if n < len(posts) else None,
        )
        # post authors (threads/facebook carry author_username) count as seen users
        authors = [
            {
                "username": p.get("author_username"),
                "source_record_id": p.get("platform_post_id") or p.get("post_id") or p.get("id"),
            }
            for p in posts
            if isinstance(p, dict) and p.get("author_username")
        ]
        await _record_users(app["pool"], platform, authors, "author")


async def _legacy_comments_handler(request):
    from . import _archive_browser_capture, _record_browser_ingest_event, _record_users
    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    raw_comments = body.get("comments") or []
    comments = raw_comments if isinstance(raw_comments, list) else []
    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return _cors(web.json_response({
            "ok": True,
            "observed": len(comments),
            "saved": 0,
            "excluded_verified": len(comments),
            "excluded_verified_target": True,
        }))
    if platform in _VERIFIED_FILTER_PLATFORMS:
        comments = [comment for comment in comments if not _explicitly_verified_capture(comment)]
    body = {**body, "comments": comments}
    await _archive_browser_capture(request.app["pool"], platform, "comments", body)
    n = await _save_comments(request.app["pool"], platform, body.get("post_id"), comments)
    await _record_browser_ingest_event(
        request.app["pool"],
        platform,
        "comments",
        body.get("post_id"),
        observed_count=len(comments),
        stored_count=n,
        collection_surface=normalize_collection_surface(body.get("collection_surface")),
        failed_count=max(0, len(comments) - n),
        outcome="partial" if n and n < len(comments) else "failed" if comments and not n else "success" if n else "empty",
        error_class="comment_row_not_saved" if n < len(comments) else None,
    )
    # every commenter is a user we've seen
    authors = [
        {
            "username": c.get("author_username"),
            "user_id": c.get("author_platform_id"),
            "source_record_id": c.get("comment_id") or c.get("id"),
        }
        for c in comments
        if c.get("author_username") or c.get("author_platform_id")
    ]
    await _record_users(request.app["pool"], platform, authors, "comment")
    return _cors(web.json_response({"saved": n}))
