"""Platform content routes (Matrix, Telegram, TikTok, Threads, GitHub, Lemon8, Beeper) plus operator system routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 23
(clusters 11-14: matrix + content + system).

Covers:
* ``/api/matrix/*`` — matrix collector state
* ``/telegram/chats``, ``/telegram/chat/{chat_id}``
* ``/tiktok/profiles``, ``/tiktok/profile/{username}``
* ``/threads/profiles``, ``/threads/profile/{username}``
* ``/github/profiles``, ``/github/edge-stats``, ``/github/profile/{owner}``,
  ``/github/repos``, ``/github/repo/{full_name:path}``
* ``/lemon8/profiles``, ``/lemon8/profile/{username}``
* ``/beeper/chats``, ``/beeper/chat/{chat_id:path}``
* ``/seen/targets``, ``/optional-rollout/status``,
  ``/recon/targets``, ``/recon/observations``
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.core.seen_targets import (
    list_seen_targets,
    refresh_seen_targets_from_sources,
    seen_target_summary_by_source,
)
from src.core.optional_rollout import optional_rollout_report

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


# ---------------------------------------------------------------------------
# Matrix collector (Wave 1 Phase 3) — read-only.
# ---------------------------------------------------------------------------

def _matrix_enabled() -> bool:
    return os.getenv("MATRIX_COLLECTOR_ENABLED", "").lower() in ("1", "true", "yes", "on")


def _matrix_disabled_response():
    raise HTTPException(
        status_code=503,
        detail={"enabled": False, "reason": "matrix collector disabled"},
    )


@router.get("/api/matrix/sync-state")
async def matrix_sync_state(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, next_batch, last_sync_at "
                "FROM matrix_sync_state ORDER BY last_sync_at DESC NULLS LAST LIMIT 1"
            )
    except Exception as e:
        logger.warning("matrix_sync_state query failed: %s", e)
        return {"user_id": None, "next_batch": None, "last_sync_at": None}
    if not row:
        return {"user_id": None, "next_batch": None, "last_sync_at": None}
    return dict(row)


@router.get("/api/matrix/backfill-state")
async def matrix_backfill_state(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            summary = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total_rooms,
                       COUNT(*) FILTER (WHERE done = TRUE) AS done,
                       COUNT(*) FILTER (WHERE done = FALSE) AS pending,
                       COUNT(*) FILTER (WHERE last_error IS NOT NULL AND done = FALSE) AS errored,
                       COALESCE(SUM(events_fetched), 0) AS events_total
                  FROM matrix_backfill_state
                """
            )
    except Exception as e:
        logger.warning("matrix_backfill_state query failed: %s", e)
        return {"total_rooms": 0, "done": 0, "pending": 0, "errored": 0, "events_total": 0}
    return dict(summary) if summary else {
        "total_rooms": 0, "done": 0, "pending": 0, "errored": 0, "events_total": 0,
    }


@router.get("/api/matrix/queue-depths")
async def matrix_queue_depths(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            undecrypted = await conn.fetchval(
                "SELECT COUNT(*) FROM matrix_events "
                "WHERE is_encrypted = TRUE AND is_decrypted = FALSE"
            )
            pending_media = await conn.fetchval(
                "SELECT COUNT(*) FROM matrix_events "
                "WHERE media_mxc IS NOT NULL "
                "AND media_local_path IS NULL "
                "AND (is_encrypted = FALSE OR is_decrypted = TRUE)"
            )
    except Exception as e:
        logger.warning("matrix_queue_depths query failed: %s", e)
        return {"undecrypted": 0, "pending_media": 0}
    return {"undecrypted": int(undecrypted or 0), "pending_media": int(pending_media or 0)}


@router.get("/api/matrix/coverage")
async def matrix_coverage(_user: dict = Depends(require_role("viewer"))):
    if not _matrix_enabled():
        _matrix_disabled_response()
    from src.core.matrix_dedupe_queries import coverage_overlap_summary
    pool = await _get_pool()
    return await coverage_overlap_summary(pool)


# ---------------------------------------------------------------------------
# Telegram: chats + messages
# ---------------------------------------------------------------------------

@router.get("/telegram/chats")
async def list_telegram_chats(owner: str | None = None, limit: int = 100,
                              _user: dict = Depends(require_role("viewer"))):
    """Recent Telegram chats, newest activity first."""
    _ = owner
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('telegram_chats')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT c.platform_chat_id,
                   c.title,
                   c.username,
                   c.type,
                   c.description,
                   c.members_count,
                   c.updated_at,
                   c.collected_at
            FROM telegram_chats c
            ORDER BY c.updated_at DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/telegram/chat/{chat_id}")
async def telegram_chat_detail(chat_id: str, limit: int = 200,
                               _user: dict = Depends(require_role("viewer"))):
    """Chat metadata + newest N messages for one Telegram chat."""
    limit = max(1, min(limit, 1000))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('telegram_chats')") is None:
            return {"chat": None, "messages": []}
        chat_row = await conn.fetchrow(
            """
            SELECT id,
                   platform_chat_id,
                   title,
                   username,
                   type,
                   description,
                   members_count,
                   updated_at,
                   collected_at
            FROM telegram_chats
            WHERE platform_chat_id = $1
            """,
            chat_id,
        )
        if not chat_row:
            return {"chat": None, "messages": []}
        chat = dict(chat_row)
        chat_uuid = chat.pop("id")
        chat["message_count"] = int(
            await conn.fetchval(
                "SELECT COUNT(*) FROM telegram_messages WHERE chat_id = $1",
                chat_uuid,
            )
            or 0
        )
        rows = await conn.fetch(
            """
            WITH picked AS (
                SELECT id
                FROM telegram_messages
                WHERE chat_id = $1
                ORDER BY platform_created_at DESC NULLS LAST, collected_at DESC
                LIMIT $3
            )
            SELECT m.platform_message_id,
                   m.text,
                   m.caption,
                   m.media_type,
                   m.media_file_id,
                   m.is_edited,
                   m.edit_date,
                   m.reply_to_message_id,
                   m.platform_created_at,
                   m.collected_at,
                   (m.metadata->>'deleted' = 'true' IS TRUE)  AS is_deleted,
                   m.metadata->>'deleted_at'                  AS deleted_at,
                   u.platform_user_id                 AS sender_platform_id,
                   u.username                         AS sender_username,
                   u.first_name                       AS sender_first_name,
                   u.last_name                        AS sender_last_name,
                   mi.id                              AS media_item_id
            FROM picked p
            JOIN telegram_messages m ON m.id = p.id
            LEFT JOIN telegram_users u ON u.id = m.sender_id
            LEFT JOIN media_items mi
                   ON mi.source = 'telegram'
                  AND mi.entity_id = $2
                  AND mi.content_id = split_part(m.platform_message_id, ':', 2)
            ORDER BY m.platform_created_at DESC NULLS LAST, m.collected_at DESC
            """,
            chat_uuid, chat_id, limit,
        )
    return {
        "chat": chat,
        "messages": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# TikTok feed (profiles + posts)
# ---------------------------------------------------------------------------

@router.get("/tiktok/profiles")
async def list_tiktok_profiles(limit: int = 100,
                               _user: dict = Depends(require_role("viewer"))):
    """TikTok profiles, biggest audience first."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_profiles')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT p.platform_user_id,
                   p.username,
                   p.nickname,
                   p.avatar_url,
                   p.bio,
                   p.followers_count,
                   p.following_count,
                   p.heart_count,
                   p.video_count,
                   p.is_verified,
                   p.updated_at,
                   p.collected_at,
                   (SELECT MAX(create_time) FROM tiktok_posts WHERE profile_id = p.id) AS last_post_at,
                   (SELECT COUNT(*)         FROM tiktok_posts WHERE profile_id = p.id) AS posts_collected
            FROM tiktok_profiles p
            ORDER BY p.followers_count DESC NULLS LAST,
                     p.updated_at      DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/tiktok/profile/{username}")
async def tiktok_profile_detail(username: str, limit: int = 200,
                                _user: dict = Depends(require_role("viewer"))):
    """Profile metadata + newest N posts for one TikTok account."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_profiles')") is None:
            return {"profile": None, "posts": []}
        profile_row = await conn.fetchrow(
            """
            SELECT id,
                   platform_user_id,
                   username,
                   nickname,
                   avatar_url,
                   bio,
                   followers_count,
                   following_count,
                   heart_count,
                   video_count,
                   digg_count,
                   is_verified,
                   is_private,
                   updated_at,
                   collected_at
            FROM tiktok_profiles
            WHERE username = $1
            """,
            username,
        )
        if not profile_row:
            return {"profile": None, "posts": []}
        profile = dict(profile_row)
        profile_uuid = profile.pop("id")
        rows = await conn.fetch(
            """
            SELECT p.platform_post_id,
                   p.title,
                   p.description,
                   p.video_url,
                   p.cover_image_url,
                   p.hashtags,
                   p.view_count,
                   p.like_count,
                   p.comment_count,
                   p.share_count,
                   p.duration,
                   p.music_title,
                   p.music_author,
                   p.create_time,
                   p.collected_at,
                   mi.id                     AS media_item_id,
                   mi.content_type           AS media_content_type,
                   mi.source_url             AS media_source_url
            FROM tiktok_posts p
            LEFT JOIN media_items mi
                   ON mi.source = 'tiktok'
                  AND mi.content_id = p.platform_post_id
            WHERE p.profile_id = $1
            ORDER BY p.create_time DESC NULLS LAST, p.collected_at DESC
            LIMIT $2
            """,
            profile_uuid, limit,
        )
        posts = []
        for r in rows:
            d = dict(r)
            d["post_url"] = d.pop("media_source_url") or (
                f"https://www.tiktok.com/@{username}/video/{d['platform_post_id']}"
            )
            posts.append(d)
    return {"profile": profile, "posts": posts}


# ---------------------------------------------------------------------------
# Threads feed
# ---------------------------------------------------------------------------

@router.get("/threads/profiles")
async def list_threads_profiles(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """Threads profiles derived from collected posts."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('threads_posts')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT p.author_username AS username,
                   COUNT(p.id) AS posts_collected,
                   MAX(p.platform_created_at) AS last_post_at,
                   (SELECT profile_photo_url FROM social_users WHERE platform='threads' AND username=p.author_username ORDER BY times_seen DESC LIMIT 1) AS avatar_url
            FROM threads_posts p
            GROUP BY p.author_username
            ORDER BY posts_collected DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/threads/profile/{username}")
async def threads_profile_detail(username: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Posts for one Threads account."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('threads_posts')") is None:
            return {"profile": None, "posts": []}

        profile_info = await conn.fetchrow(
            """
            SELECT p.author_username AS username,
                   COUNT(p.id) AS posts_collected,
                   MAX(p.platform_created_at) AS last_post_at,
                   (SELECT profile_photo_url FROM social_users WHERE platform='threads' AND username=p.author_username ORDER BY times_seen DESC LIMIT 1) AS avatar_url
            FROM threads_posts p
            WHERE p.author_username = $1
            GROUP BY p.author_username
            """,
            username
        )
        if not profile_info:
            return {"profile": None, "posts": []}

        posts = await conn.fetch(
            """
            SELECT p.platform_post_id,
                   p.caption,
                   p.hashtags,
                   p.likes_count,
                   p.comments_count,
                   p.reposts_count,
                   p.media_type,
                   p.platform_created_at,
                   p.collected_at,
                   mi.id AS media_item_id,
                   mi.content_type AS media_content_type
            FROM threads_posts p
            LEFT JOIN media_items mi
                   ON mi.source = 'threads'
                  AND mi.entity_id = p.author_username
                  AND mi.content_id = p.platform_post_id
            WHERE p.author_username = $1
            ORDER BY p.platform_created_at DESC NULLS LAST, p.collected_at DESC
            LIMIT $2
            """,
            username, limit,
        )

    out_posts = []
    for r in posts:
        d = dict(r)
        d["post_url"] = f"https://www.threads.net/@{username}/post/{d['platform_post_id']}"
        out_posts.append(d)

    return {"profile": dict(profile_info), "posts": out_posts}


# ---------------------------------------------------------------------------
# GitHub feed
# ---------------------------------------------------------------------------

@router.get("/github/profiles")
async def list_github_profiles(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """GitHub owners first, with repo totals."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return []
        rows = await conn.fetch(
            """
            WITH recent_commits AS MATERIALIZED (
                SELECT repo_id, collected_at
                FROM github_commits
                WHERE collected_at IS NOT NULL
                ORDER BY collected_at DESC
                LIMIT 5000
            )
            SELECT split_part(r.full_name, '/', 1) AS owner,
                   COUNT(DISTINCT r.id) AS repos_collected,
                   COALESCE(MAX(r.stargazers_count), 0)::bigint AS stargazers_count,
                   COALESCE(MAX(r.forks_count), 0)::bigint AS forks_count,
                   MAX(r.platform_updated_at) AS updated_at,
                   MAX(rc.collected_at) AS collected_at,
                   COUNT(*) AS commits_loaded
            FROM recent_commits rc
            JOIN github_repos r ON r.id = rc.repo_id
            WHERE r.full_name IS NOT NULL AND r.full_name <> ''
            GROUP BY split_part(r.full_name, '/', 1)
            ORDER BY MAX(rc.collected_at) DESC NULLS LAST,
                     COUNT(*) DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/github/edge-stats")
async def github_edge_stats(_user: dict = Depends(require_role("viewer"))):
    """Collected GitHub relationship evidence counts."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_edges')") is None:
            return {
                "total_edges": 0,
                "edges_current_hour": 0,
                "distinct_sources": 0,
                "distinct_targets": 0,
                "queued_profiles": 0,
                "by_type": [],
            }
        totals = await conn.fetchrow(
            """
            SELECT COUNT(*)::bigint AS total_edges,
                   COUNT(*) FILTER (
                       WHERE collected_at >= date_trunc('hour', now())
                   )::bigint AS edges_current_hour,
                   COUNT(DISTINCT source_login)::bigint AS distinct_sources,
                   COUNT(DISTINCT target_login)::bigint AS distinct_targets
            FROM github_edges
            """
        )
        by_type = await conn.fetch(
            """
            SELECT edge_type,
                   COUNT(*)::bigint AS count,
                   MAX(last_seen) AS last_seen
            FROM github_edges
            GROUP BY edge_type
            ORDER BY COUNT(*) DESC, edge_type ASC
            LIMIT 20
            """
        )
        queued = 0
        if await conn.fetchval("SELECT to_regclass('github_spider_queue')") is not None:
            queued = int(await conn.fetchval(
                """
                SELECT COUNT(*)::bigint
                FROM github_spider_queue
                WHERE status = 'pending'
                """
            ) or 0)
    return {
        **dict(totals or {}),
        "queued_profiles": queued,
        "by_type": [dict(r) for r in by_type],
    }


@router.get("/github/profile/{owner}")
async def github_profile_detail(owner: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Repos + recent commits for one GitHub owner."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return {"profile": None, "repos": [], "commits": []}

        profile_row = await conn.fetchrow(
            """
            SELECT split_part(full_name, '/', 1) AS owner,
                   COUNT(*) AS repos_collected,
                   COALESCE(SUM(stargazers_count), 0)::bigint AS stargazers_count,
                   COALESCE(SUM(forks_count), 0)::bigint AS forks_count,
                   MAX(platform_updated_at) AS updated_at,
                   MAX(collected_at) AS collected_at
            FROM github_repos
            WHERE split_part(full_name, '/', 1) = $1
            GROUP BY split_part(full_name, '/', 1)
            """,
            owner,
        )
        if not profile_row:
            return {"profile": None, "repos": [], "commits": []}

        repos = await conn.fetch(
            """
            SELECT id,
                   platform_repo_id,
                   name,
                   full_name,
                   description,
                   language,
                   stargazers_count,
                   forks_count,
                   open_issues_count,
                   platform_updated_at
            FROM github_repos
            WHERE split_part(full_name, '/', 1) = $1
            ORDER BY stargazers_count DESC NULLS LAST,
                     platform_updated_at DESC NULLS LAST
            LIMIT 2000
            """,
            owner,
        )
        repo_ids = [r["id"] for r in repos]
        commits = []
        if repo_ids:
            commits = await conn.fetch(
                """
                SELECT c.sha,
                       c.author_name,
                       c.author_login,
                       c.message,
                       c.date,
                       c.files_changed,
                       c.insertions,
                       c.deletions,
                       c.collected_at,
                       r.full_name
                FROM github_commits c
                JOIN github_repos r ON r.id = c.repo_id
                WHERE c.repo_id = ANY($1::uuid[])
                ORDER BY c.date DESC NULLS LAST, c.collected_at DESC
                LIMIT $2
                """,
                repo_ids, limit,
            )

    profile = dict(profile_row)
    out_commits = []
    for r in commits:
        d = dict(r)
        full_name = d.pop("full_name")
        d["repo_full_name"] = full_name
        d["commit_url"] = f"https://github.com/{full_name}/commit/{d['sha']}"
        out_commits.append(d)
    if out_commits:
        profile["last_commit_at"] = out_commits[0].get("date")
        profile["commits_loaded"] = len(out_commits)

    out_repos = []
    for r in repos:
        d = dict(r)
        d.pop("id", None)
        out_repos.append(d)
    return {"profile": profile, "repos": out_repos, "commits": out_commits}


@router.get("/github/repos")
async def list_github_repos(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """GitHub repos and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return []
        rows = await conn.fetch(
            """
            WITH top AS (
                SELECT id, platform_repo_id, name, full_name, description,
                       language, stargazers_count, forks_count,
                       open_issues_count, platform_updated_at
                FROM github_repos
                ORDER BY stargazers_count DESC NULLS LAST, platform_updated_at DESC
                LIMIT $1
            )
            SELECT t.id, t.platform_repo_id, t.name, t.full_name, t.description,
                   t.language, t.stargazers_count, t.forks_count,
                   t.open_issues_count, t.platform_updated_at,
                   cc.commits_collected, cc.last_commit_at
            FROM top t
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS commits_collected, MAX(date) AS last_commit_at
                FROM github_commits gc WHERE gc.repo_id = t.id
            ) cc ON TRUE
            ORDER BY t.stargazers_count DESC NULLS LAST, t.platform_updated_at DESC
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/github/repo/{full_name:path}")
async def github_repo_detail(full_name: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Commits for one GitHub repo."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('github_repos')") is None:
            return {"repo": None, "commits": []}

        repo_row = await conn.fetchrow(
            """
            SELECT r.id,
                   r.platform_repo_id,
                   r.name,
                   r.full_name,
                   r.description,
                   r.language,
                   r.stargazers_count,
                   r.forks_count,
                   r.open_issues_count,
                   r.platform_updated_at
            FROM github_repos r
            WHERE r.full_name = $1
            """,
            full_name
        )
        if not repo_row:
            return {"repo": None, "commits": []}

        repo = dict(repo_row)
        repo_uuid = repo.pop("id")

        commits = await conn.fetch(
            """
            SELECT c.sha,
                   c.author_name,
                   c.author_login,
                   c.message,
                   c.date,
                   c.files_changed,
                   c.insertions,
                   c.deletions,
                   c.collected_at
            FROM github_commits c
            WHERE c.repo_id = $1
            ORDER BY c.date DESC NULLS LAST, c.collected_at DESC
            LIMIT $2
            """,
            repo_uuid, limit,
        )

    out_commits = []
    for r in commits:
        d = dict(r)
        d["commit_url"] = f"https://github.com/{full_name}/commit/{d['sha']}"
        out_commits.append(d)

    return {"repo": repo, "commits": out_commits}


# ---------------------------------------------------------------------------
# Lemon8 feed (profiles + posts)
# ---------------------------------------------------------------------------

@router.get("/lemon8/profiles")
async def list_lemon8_profiles(limit: int = 100, _user: dict = Depends(require_role("viewer"))):
    """Lemon8 profiles and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('lemon8_profiles')") is None:
            return []
        rows = await conn.fetch(
            """
            SELECT p.id,
                   p.platform_user_id,
                   p.username,
                   p.nickname,
                   p.avatar_url,
                   p.bio,
                   p.followers_count,
                   p.following_count,
                   p.like_count,
                   p.updated_at,
                   (SELECT COUNT(*) FROM lemon8_posts WHERE profile_id = p.id) AS posts_collected,
                   (SELECT MAX(platform_created_at) FROM lemon8_posts WHERE profile_id = p.id) AS last_post_at
            FROM lemon8_profiles p
            ORDER BY p.followers_count DESC NULLS LAST, p.updated_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/lemon8/profile/{username}")
async def lemon8_profile_detail(username: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Posts for one Lemon8 account."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('lemon8_profiles')") is None:
            return {"profile": None, "posts": []}

        profile_row = await conn.fetchrow(
            """
            SELECT p.id,
                   p.platform_user_id,
                   p.username,
                   p.nickname,
                   p.avatar_url,
                   p.bio,
                   p.followers_count,
                   p.following_count,
                   p.like_count,
                   p.updated_at
            FROM lemon8_profiles p
            WHERE p.username = $1
            """,
            username
        )
        if not profile_row:
            return {"profile": None, "posts": []}

        profile = dict(profile_row)
        profile_uuid = profile.pop("id")

        posts = await conn.fetch(
            """
            SELECT p.platform_post_id,
                   p.title,
                   p.description,
                   p.music_title,
                   p.like_count,
                   p.comment_count,
                   p.share_count,
                   p.platform_created_at,
                   p.collected_at,
                   mi.id AS media_item_id,
                   mi.content_type AS media_content_type
            FROM lemon8_posts p
            LEFT JOIN media_items mi
                   ON mi.source = 'lemon8'
                  AND mi.content_id = p.platform_post_id
            WHERE p.profile_id = $1
            ORDER BY p.platform_created_at DESC NULLS LAST, p.collected_at DESC
            LIMIT $2
            """,
            profile_uuid, limit,
        )

    out_posts = []
    for r in posts:
        d = dict(r)
        d["post_url"] = f"https://www.lemon8-app.com/{username}/post/{d['platform_post_id']}"
        out_posts.append(d)

    return {"profile": profile, "posts": out_posts}


# ---------------------------------------------------------------------------
# Beeper feed (chats + messages)
# ---------------------------------------------------------------------------

@router.get("/beeper/chats")
async def list_beeper_chats(
    limit: int = 100,
    network: str | None = None,
    _user: dict = Depends(require_role("viewer")),
):
    """Beeper chats and collection stats."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('beeper_shadow_chats')") is None:
            return []
        where = ""
        args: list = [limit]
        if network:
            args.append(network)
            where = "WHERE c.network = $2"
        rows = await conn.fetch(
            f"""
            SELECT c.chat_id,
                   c.local_chat_id,
                   c.network,
                   c.title,
                   c.img_url,
                   c.chat_type,
                   (c.chat_type = 'dm') AS is_direct,
                   c.account_id,
                   c.last_seen_at,
                   (SELECT COUNT(*) FROM beeper_shadow_messages WHERE chat_id = c.chat_id) AS messages_collected,
                   (SELECT MAX(timestamp) FROM beeper_shadow_messages WHERE chat_id = c.chat_id) AS last_message_at
            FROM beeper_shadow_chats c
            {where}
            ORDER BY c.last_seen_at DESC NULLS LAST
            LIMIT $1
            """,
            *args,
        )
    return [dict(r) for r in rows]


@router.get("/beeper/chat/{chat_id:path}")
async def beeper_chat_detail(chat_id: str, limit: int = 200, _user: dict = Depends(require_role("viewer"))):
    """Messages for one Beeper chat."""
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('beeper_shadow_chats')") is None:
            return {"chat": None, "messages": []}

        chat_row = await conn.fetchrow(
            """
            SELECT c.chat_id,
                   c.local_chat_id,
                   c.network,
                   c.title,
                   c.img_url,
                   c.chat_type,
                   (c.chat_type = 'dm') AS is_direct,
                   c.account_id,
                   c.last_seen_at
            FROM beeper_shadow_chats c
            WHERE c.chat_id = $1
            """,
            chat_id
        )
        if not chat_row:
            return {"chat": None, "messages": []}

        chat = dict(chat_row)

        messages = await conn.fetch(
            """
            SELECT m.message_id,
                   m.network,
                   m.sender_id,
                   m.sender_name,
                   m.text,
                   m.timestamp,
                   m.sort_key,
                   (m.msg_type IN ('IMAGE','VIDEO','STICKER','FILE','VOICE','AUDIO')
                    OR (m.attachments IS NOT NULL
                        AND m.attachments::text NOT IN ('null','[]','{}'))) AS is_media,
                   NULL::text AS media_url,
                   m.msg_type AS media_type,
                   m.is_deleted,
                   m.deleted_at,
                   m.ingested_at
            FROM beeper_shadow_messages m
            WHERE m.chat_id = $1
            ORDER BY m.timestamp DESC NULLS LAST
            LIMIT $2
            """,
            chat_id, limit,
        )

        mids = [m["message_id"] for m in messages]
        media_map: dict[str, str] = {}
        if mids:
            media_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (split_part(content_id, '_', 1))
                       split_part(content_id, '_', 1) AS mid,
                       id AS media_item_id
                FROM media_items
                WHERE source = 'beeper'
                  AND split_part(content_id, '_', 1) = ANY($1::text[])
                ORDER BY split_part(content_id, '_', 1), collected_at
                """,
                mids,
            )
            media_map = {r["mid"]: r["media_item_id"] for r in media_rows}

    out_messages = []
    for r in messages:
        d = dict(r)
        d["media_item_id"] = media_map.get(d["message_id"])
        out_messages.append(d)

    out_messages.reverse()

    return {"chat": chat, "messages": out_messages}


# ---------------------------------------------------------------------------
# System / seen-targets / optional rollout / recon
# ---------------------------------------------------------------------------

@router.get("/seen/targets")
async def seen_targets(
    source: str | None = None,
    status: str | None = None,
    target_type: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    refresh: bool = False,
    _user: dict = Depends(require_role("viewer")),
):
    # Look up on parent module first so test monkey-patches on dashboard_api
    # continue to take effect.
    _refresh = _lookup("refresh_seen_targets_from_sources") or refresh_seen_targets_from_sources
    _summary = _lookup("seen_target_summary_by_source") or seen_target_summary_by_source
    _list = _lookup("list_seen_targets") or list_seen_targets
    pool = await _get_pool()
    refresh_report = None
    async with pool.acquire() as conn:
        if refresh:
            refresh_report = await _refresh(conn, source=source)
        summary = await _summary(conn, source=source)
        rows = await _list(
            conn,
            source=source,
            status=status,
            target_type=target_type,
            limit=limit,
        )
    return {
        "targets": rows,
        "total": len(rows),
        "summary": summary,
        "refresh": refresh_report,
    }


@router.get("/optional-rollout/status")
async def optional_rollout_status(
    feature: str = Query("spiderfoot", pattern="^(spiderfoot|recon|lemon8|browser-heavy)$"),
    stage: str = Query("dry-run", pattern="^(dry-run|five|daily25|daily100|daily250|daily500)$"),
    window_hours: int = Query(24, ge=1, le=168),
    limit: int | None = Query(None, ge=0, le=1000),
    _user: dict = Depends(require_role("viewer")),
):
    acquire_cm = None
    try:
        pool = await asyncio.wait_for(_get_pool(), timeout=1.5)
        acquire_cm = pool.acquire()
        conn = await asyncio.wait_for(acquire_cm.__aenter__(), timeout=1.5)
    except TimeoutError:
        return JSONResponse(
            {"ok": False, "error": "db_busy_retry", "detail": "database connection timed out"},
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001 - status endpoint should fail soft
        return JSONResponse(
            {"ok": False, "error": "db_busy_retry", "detail": exc.__class__.__name__},
            status_code=503,
        )
    try:
        _report = _lookup("optional_rollout_report") or optional_rollout_report
        return await asyncio.wait_for(
            _report(
                conn,
                feature=feature,
                stage=stage,
                window_hours=window_hours,
                limit=limit,
            ),
            timeout=8,
        )
    except TimeoutError:
        return JSONResponse(
            {"ok": False, "error": "db_busy_retry", "detail": "rollout status read timed out"},
            status_code=503,
        )
    finally:
        if acquire_cm is not None and "conn" in locals():
            await acquire_cm.__aexit__(None, None, None)


@router.get("/recon/targets")
async def recon_targets(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, target_type, target_value, source, priority, status,
                   scope_json, error, created_at, updated_at
            FROM recon_targets
            ORDER BY priority, created_at DESC
            LIMIT 200
            """
        )
    return {"targets": [dict(row) for row in rows], "total": len(rows)}


@router.get("/recon/observations")
async def recon_observations(
    target_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    _user: dict = Depends(require_role("viewer")),
):
    parsed_target_id = None
    if target_id:
        try:
            parsed_target_id = str(_uuid.UUID(target_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid target_id") from exc
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id::text, o.target_id::text, t.target_type, t.target_value,
                   o.module, o.observation_type, o.value, o.confidence,
                   o.first_seen_at, o.last_seen_at
            FROM recon_observations o
            JOIN recon_targets t ON t.id = o.target_id
            WHERE ($1::uuid IS NULL OR o.target_id = $1::uuid)
            ORDER BY o.last_seen_at DESC
            LIMIT $2
            """,
            parsed_target_id,
            limit,
        )
    return {"observations": [dict(row) for row in rows], "total": len(rows), "limit": limit}
