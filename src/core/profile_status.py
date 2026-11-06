"""Bounded source-native profile listings shared by dashboard and control room."""
from __future__ import annotations


async def list_profiles(pool, *, source: str, limit: int = 100) -> list[dict]:
    source = (source or "").strip().lower()
    limit = max(1, min(limit, 500))
    if source not in {"tiktok", "lemon8", "threads"}:
        raise ValueError("unsupported profile source")
    async with pool.acquire() as conn:
        if source == "tiktok":
            if await conn.fetchval("SELECT to_regclass('tiktok_profiles')") is None:
                return []
            rows = await conn.fetch(
                """
                SELECT p.platform_user_id, p.username, p.nickname, p.avatar_url, p.bio,
                       p.followers_count, p.following_count, p.heart_count, p.video_count,
                       p.is_verified, p.updated_at, p.collected_at,
                       (SELECT MAX(create_time) FROM tiktok_posts WHERE profile_id=p.id) AS last_post_at,
                       (SELECT COUNT(*) FROM tiktok_posts WHERE profile_id=p.id) AS posts_collected
                FROM tiktok_profiles p
                ORDER BY p.followers_count DESC NULLS LAST, p.updated_at DESC NULLS LAST LIMIT $1
                """, limit,
            )
        elif source == "lemon8":
            if await conn.fetchval("SELECT to_regclass('lemon8_profiles')") is None:
                return []
            rows = await conn.fetch(
                """
                SELECT p.id, p.platform_user_id, p.username, p.nickname, p.avatar_url, p.bio,
                       p.followers_count, p.following_count, p.like_count, p.updated_at,
                       (SELECT COUNT(*) FROM lemon8_posts WHERE profile_id=p.id) AS posts_collected,
                       (SELECT MAX(platform_created_at) FROM lemon8_posts WHERE profile_id=p.id) AS last_post_at
                FROM lemon8_profiles p
                ORDER BY p.followers_count DESC NULLS LAST, p.updated_at DESC LIMIT $1
                """, limit,
            )
        else:
            if await conn.fetchval("SELECT to_regclass('threads_posts')") is None:
                return []
            rows = await conn.fetch(
                """
                SELECT p.author_username AS username, COUNT(p.id) AS posts_collected,
                       MAX(p.platform_created_at) AS last_post_at,
                       (SELECT profile_photo_url FROM social_users WHERE platform='threads'
                        AND username=p.author_username ORDER BY times_seen DESC LIMIT 1) AS avatar_url
                FROM threads_posts p GROUP BY p.author_username
                ORDER BY posts_collected DESC LIMIT $1
                """, limit,
            )
    return [dict(row) for row in rows]
