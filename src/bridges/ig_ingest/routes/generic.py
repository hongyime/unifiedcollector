"""Generic target, user, seed, profile, post, and comment HTTP routes."""
from __future__ import annotations

import asyncio

from aiohttp import web

from ..constants import SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY
from ..cors import _cors
from ..storage.generic import record_users, save_comments, save_posts, save_profile


async def target_status_handler(request):
    from .. import _norm_platform, _safe_json, logger

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
            SET status = $2, last_scraped_at = now()
            WHERE username = $1 AND status = 'active'
            """,
            username, status,
        )
        if result.endswith("1"):
            updated = 1
    if updated:
        logger.info("target-status[%s] %s -> %s (%s)", platform, username, status, reason)
    return _cors(web.json_response({"updated": updated, "status": status}))


async def users_handler(request):
    from .. import _norm_platform, _safe_json

    body = await _safe_json(request)
    recorded = await record_users(
        request.app["pool"], _norm_platform(body.get("platform")),
        body.get("users") or [], body.get("context") or "seen", body.get("owner"),
    )
    return _cors(web.json_response({"recorded": recorded}))


async def seed_handler(request):
    from .. import _explicitly_verified_capture, _norm_platform, _safe_json

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    raw_users = body.get("users") or []
    users = [user for user in raw_users if not _explicitly_verified_capture(user)] if isinstance(raw_users, list) else []
    added = 0
    if platform == "instagram":
        async with request.app["pool"].acquire() as conn:
            for user in users:
                username = (user.get("username") if isinstance(user, dict) else str(user) or "").strip().lstrip("@")
                if not username:
                    continue
                result = await conn.execute(
                    """INSERT INTO instagram_spider_targets (username, hop, discovered_from)
                       VALUES ($1, 0, 'self') ON CONFLICT (username) DO NOTHING""",
                    username,
                )
                if result.endswith("1"):
                    added += 1
        await record_users(request.app["pool"], platform, users, "follow")
    return _cors(web.json_response({"added": added}))


async def profile_handler(request):
    from .. import (
        _VERIFIED_FILTER_PLATFORMS, _archive_browser_capture, _explicitly_verified_capture,
        _ingest, _norm_platform, _record_browser_ingest_event, _record_ig_access, _safe_json,
    )

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    profile = body.get("profile") or {}
    if platform in _VERIFIED_FILTER_PLATFORMS and (
        _explicitly_verified_capture(body) or _explicitly_verified_capture(profile)
    ):
        return _cors(web.json_response({"ok": True, "excluded_verified": True}))
    await _archive_browser_capture(request.app["pool"], platform, "profile", body)
    saved = await save_profile(request.app["pool"], platform, profile)
    await _record_browser_ingest_event(
        request.app["pool"], platform, "profile",
        (profile.get("username") or body.get("username") or "").strip().lstrip("@") or None,
        observed_count=1 if profile else 0, stored_count=1 if saved else 0,
    )
    if platform == "instagram":
        await _record_ig_access(request.app["pool"], (profile.get("username") or "").strip().lstrip("@"), body.get("owner"), profile)
    picture = profile.get("profile_pic_url")
    username = (profile.get("username") or "").strip().lstrip("@")
    if platform == "instagram" and saved and picture and username:
        await _ingest(request.app, platform, {"username": username, "items": [{
            "url": picture, "content_id": str(profile.get("user_id") or username), "content_type": "photo",
            "kind": "profile", "entity_name": username,
        }]})
    return _cors(web.json_response({"ok": True}))


async def posts_handler(request):
    from .. import _VERIFIED_FILTER_PLATFORMS, _explicitly_verified_capture, _norm_platform, _safe_json, _schedule_app_task

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    raw_posts = body.get("posts") or []
    raw_posts = raw_posts if isinstance(raw_posts, list) else []
    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return _cors(web.json_response({"ok": True, "queued": False, "observed": len(raw_posts), "excluded_verified": len(raw_posts), "excluded_verified_target": True, "saved": 0}))
    posts = [post for post in raw_posts if not _explicitly_verified_capture(post)] if platform in _VERIFIED_FILTER_PLATFORMS else raw_posts
    _schedule_app_task(request.app, _posts_ingest_background(request.app, platform, {**body, "posts": posts}), "browser_posts_ingest")
    return _cors(web.json_response({"ok": True, "queued": True, "observed": len(raw_posts), "excluded_verified": len(raw_posts) - len(posts), "saved": 0}))


async def _posts_ingest_background(app, platform: str, body: dict) -> None:
    from .. import (
        _VERIFIED_FILTER_PLATFORMS, _archive_browser_capture, _explicitly_verified_capture,
        _record_browser_ingest_event, normalize_collection_surface,
    )

    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return
    posts = body.get("posts") or []
    if not isinstance(posts, list):
        posts = []
    elif platform in _VERIFIED_FILTER_PLATFORMS:
        posts = [post for post in posts if not _explicitly_verified_capture(post)]
        body = {**body, "posts": posts}
    semaphore = app.get("structured_sem")
    if semaphore is None:
        semaphore = asyncio.Semaphore(SOCIAL_INGEST_STRUCTURED_BACKGROUND_CONCURRENCY)
        app["structured_sem"] = semaphore
    surface = normalize_collection_surface(body.get("collection_surface") or body.get("surface"))
    async with semaphore:
        await _archive_browser_capture(app["pool"], platform, "posts", body)
        saved = await save_posts(app["pool"], platform, posts, surface)
        await _record_browser_ingest_event(
            app["pool"], platform, "posts", body.get("username") or body.get("owner"),
            observed_count=len(posts), stored_count=saved, collection_surface=surface,
            failed_count=max(0, len(posts) - saved),
            outcome="partial" if saved and saved < len(posts) else "failed" if posts and not saved else "success" if saved else "empty",
            error_class="post_row_not_saved" if saved < len(posts) else None,
        )
        await record_users(app["pool"], platform, [
            {"username": post.get("author_username"), "source_record_id": post.get("platform_post_id") or post.get("post_id") or post.get("id")}
            for post in posts if isinstance(post, dict) and post.get("author_username")
        ], "author")


async def comments_handler(request):
    from .. import (
        _VERIFIED_FILTER_PLATFORMS, _archive_browser_capture, _explicitly_verified_capture,
        _norm_platform, _record_browser_ingest_event, _safe_json, normalize_collection_surface,
    )

    body = await _safe_json(request)
    platform = _norm_platform(body.get("platform"))
    raw_comments = body.get("comments") or []
    comments = raw_comments if isinstance(raw_comments, list) else []
    if platform in _VERIFIED_FILTER_PLATFORMS and _explicitly_verified_capture(body):
        return _cors(web.json_response({"ok": True, "observed": len(comments), "saved": 0, "excluded_verified": len(comments), "excluded_verified_target": True}))
    if platform in _VERIFIED_FILTER_PLATFORMS:
        comments = [comment for comment in comments if not _explicitly_verified_capture(comment)]
    body = {**body, "comments": comments}
    await _archive_browser_capture(request.app["pool"], platform, "comments", body)
    saved = await save_comments(request.app["pool"], platform, body.get("post_id"), comments)
    await _record_browser_ingest_event(
        request.app["pool"], platform, "comments", body.get("post_id"),
        observed_count=len(comments), stored_count=saved,
        collection_surface=normalize_collection_surface(body.get("collection_surface")),
        failed_count=max(0, len(comments) - saved),
        outcome="partial" if saved and saved < len(comments) else "failed" if comments and not saved else "success" if saved else "empty",
        error_class="comment_row_not_saved" if saved < len(comments) else None,
    )
    await record_users(request.app["pool"], platform, [
        {"username": comment.get("author_username"), "user_id": comment.get("author_platform_id"), "source_record_id": comment.get("comment_id") or comment.get("id")}
        for comment in comments if comment.get("author_username") or comment.get("author_platform_id")
    ], "comment")
    return _cors(web.json_response({"saved": saved}))
