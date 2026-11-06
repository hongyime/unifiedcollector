"""Bounded source-native profile post detail shared by dashboard and control room."""
from __future__ import annotations


async def get_profile_posts(pool, *, source: str, username: str, limit: int = 200) -> dict:
    source = (source or "").strip().lower()
    if source not in {"lemon8", "tiktok", "threads"}:
        raise ValueError("unsupported profile-post source")
    limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if source == "tiktok":
            if await conn.fetchval("SELECT to_regclass('tiktok_profiles')") is None:
                return {"profile": None, "posts": []}
            profile_row = await conn.fetchrow(
                """
                SELECT id, platform_user_id, username, nickname, avatar_url, bio,
                       followers_count, following_count, heart_count, video_count,
                       digg_count, is_verified, is_private, updated_at, collected_at
                FROM tiktok_profiles WHERE username=$1
                """,
                username,
            )
            if not profile_row:
                return {"profile": None, "posts": []}
            profile = dict(profile_row)
            profile_id = profile.pop("id")
            rows = await conn.fetch(
                """
                SELECT p.platform_post_id, p.title, p.description, p.video_url, p.cover_image_url,
                       p.hashtags, p.view_count, p.like_count, p.comment_count, p.share_count,
                       p.duration, p.music_title, p.music_author, p.create_time, p.collected_at,
                       mi.id AS media_item_id, mi.content_type AS media_content_type,
                       mi.source_url AS media_source_url
                FROM tiktok_posts p LEFT JOIN media_items mi
                  ON mi.source='tiktok' AND mi.content_id=p.platform_post_id
                WHERE p.profile_id=$1
                ORDER BY p.create_time DESC NULLS LAST, p.collected_at DESC LIMIT $2
                """,
                profile_id, limit,
            )
            posts = []
            for row in rows:
                post = dict(row)
                post["post_url"] = post.pop("media_source_url") or (
                    f"https://www.tiktok.com/@{username}/video/{post['platform_post_id']}"
                )
                posts.append(post)
            return {"profile": profile, "posts": posts}
        if source == "threads":
            if await conn.fetchval("SELECT to_regclass('threads_posts')") is None:
                return {"profile": None, "posts": []}
            profile = await conn.fetchrow(
                """
                SELECT p.author_username AS username, COUNT(p.id) AS posts_collected,
                       MAX(p.platform_created_at) AS last_post_at,
                       (SELECT profile_photo_url FROM social_users WHERE platform='threads'
                        AND username=p.author_username ORDER BY times_seen DESC LIMIT 1) AS avatar_url
                FROM threads_posts p WHERE p.author_username=$1 GROUP BY p.author_username
                """,
                username,
            )
            if not profile:
                return {"profile": None, "posts": []}
            rows = await conn.fetch(
                """
                SELECT p.platform_post_id, p.caption, p.hashtags, p.likes_count,
                       p.comments_count, p.reposts_count, p.media_type, p.platform_created_at,
                       p.collected_at, mi.id AS media_item_id, mi.content_type AS media_content_type
                FROM threads_posts p LEFT JOIN media_items mi
                  ON mi.source='threads' AND mi.entity_id=p.author_username
                  AND mi.content_id=p.platform_post_id
                WHERE p.author_username=$1
                ORDER BY p.platform_created_at DESC NULLS LAST, p.collected_at DESC LIMIT $2
                """,
                username, limit,
            )
            posts = []
            for row in rows:
                post = dict(row)
                post["post_url"] = f"https://www.threads.net/@{username}/post/{post['platform_post_id']}"
                posts.append(post)
            return {"profile": dict(profile), "posts": posts}
        if await conn.fetchval("SELECT to_regclass('lemon8_profiles')") is None:
            return {"profile": None, "posts": []}
        profile_row = await conn.fetchrow(
            """
            SELECT p.id, p.platform_user_id, p.username, p.nickname, p.avatar_url, p.bio,
                   p.followers_count, p.following_count, p.like_count, p.updated_at
            FROM lemon8_profiles p WHERE p.username=$1
            """, username,
        )
        if not profile_row:
            return {"profile": None, "posts": []}
        profile = dict(profile_row)
        profile_id = profile.pop("id")
        rows = await conn.fetch(
            """
            SELECT p.platform_post_id, p.title, p.description, p.music_title, p.like_count,
                   p.comment_count, p.share_count, p.platform_created_at, p.collected_at,
                   mi.id AS media_item_id, mi.content_type AS media_content_type
            FROM lemon8_posts p
            LEFT JOIN media_items mi ON mi.source='lemon8' AND mi.content_id=p.platform_post_id
            WHERE p.profile_id=$1
            ORDER BY p.platform_created_at DESC NULLS LAST, p.collected_at DESC LIMIT $2
            """, profile_id, limit,
        )
    posts = []
    for row in rows:
        post = dict(row)
        post["post_url"] = f"https://www.lemon8-app.com/{username}/post/{post['platform_post_id']}"
        posts.append(post)
    return {"profile": profile, "posts": posts}
