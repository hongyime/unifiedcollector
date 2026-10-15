"""Shared leases and target seeding for read-only native platform scrapers."""
from __future__ import annotations

from typing import Any

_PLATFORMS = frozenset({"pinterest", "snapchat", "airbnb"})
_MAX_ATTEMPTS = 8
_AIRBNB_LISTING_QUEUE_CAP = 10
_SNAPCHAT_CONTENT_TABLES = {
    "public_story": ("snapchat_stories", "story_id"),
    "spotlight": ("snapchat_spotlights", "spotlight_id"),
    "highlight": ("snapchat_highlights", "highlight_id"),
}
_FOLLOWED_TARGET_SOURCES = {
    "pinterest": "followed_handle_candidate",
    "snapchat": "followed_handle_candidate",
    "airbnb": "followed_account_link",
}
_FOLLOWED_TARGET_PRIORITY = 50
_FOLLOWED_TARGET_CAP = 10

_FOLLOWED_HANDLE_SEED_SQL = """
    INSERT INTO browser_native_scrape_queue
        (platform, target_url, username, source, priority)
    SELECT $1, $2 || followed.username || $3, followed.username,
           'followed_handle_candidate', $4
      FROM (
            SELECT DISTINCT ON (LOWER(BTRIM(f.target_username)))
                   BTRIM(f.target_username) AS username,
                   f.last_seen
              FROM follow_edges f
             WHERE f.direction='following'
               AND f.platform IN ('instagram','x','tiktok','threads','pinterest','snapchat','youtube')
               AND BTRIM(COALESCE(f.target_username,'')) ~ '^[A-Za-z0-9._-]{1,100}$'
               AND NOT EXISTS (
                    SELECT 1 FROM instagram_profiles p
                     WHERE f.platform='instagram' AND p.is_verified IS TRUE
                       AND (p.platform_user_id::text=f.target_uid
                            OR LOWER(p.username)=LOWER(BTRIM(f.target_username)))
               )
               AND NOT EXISTS (
                    SELECT 1 FROM x_profiles p
                     WHERE f.platform='x' AND p.is_verified IS TRUE
                       AND (p.platform_user_id::text=f.target_uid
                            OR LOWER(p.username)=LOWER(BTRIM(f.target_username)))
               )
               AND NOT EXISTS (
                    SELECT 1 FROM tiktok_profiles p
                     WHERE f.platform='tiktok' AND p.is_verified IS TRUE
                       AND (p.platform_user_id::text=f.target_uid
                            OR LOWER(p.username)=LOWER(BTRIM(f.target_username)))
               )
               AND NOT EXISTS (
                    SELECT 1 FROM threads_posts p
                     WHERE f.platform='threads'
                       AND LOWER(p.author_username)=LOWER(BTRIM(f.target_username))
                       AND (p.metadata->>'author_is_verified'='true'
                            OR p.metadata->>'is_verified'='true'
                            OR p.metadata->>'verified'='true'
                            OR p.metadata->>'isVerified'='true'
                            OR p.metadata->>'is_blue_verified'='true'
                            OR p.metadata->>'isBlueVerified'='true'
                            OR p.metadata->>'blue_verified'='true'
                            OR p.metadata->>'blueVerified'='true'
                            OR p.metadata->>'verified_badge'='true')
               )
               AND NOT EXISTS (
                    SELECT 1 FROM pinterest_profiles p
                     WHERE (f.platform='pinterest' OR $1='pinterest')
                       AND LOWER(p.username)=LOWER(BTRIM(f.target_username))
                       AND (p.raw_payload->>'profile_is_verified'='true'
                            OR p.raw_payload->>'is_verified'='true'
                            OR p.raw_payload->>'verified'='true'
                            OR p.raw_payload->>'isVerified'='true'
                            OR p.raw_payload->>'is_blue_verified'='true'
                            OR p.raw_payload->>'isBlueVerified'='true'
                            OR p.raw_payload->>'blue_verified'='true'
                            OR p.raw_payload->>'blueVerified'='true'
                            OR p.raw_payload->>'verified_badge'='true')
               )
               AND NOT EXISTS (
                    SELECT 1 FROM snapchat_profiles p
                     WHERE (f.platform='snapchat' OR $1='snapchat')
                       AND LOWER(p.username)=LOWER(BTRIM(f.target_username))
                       AND (p.raw_payload->>'verified'='true'
                            OR p.raw_payload->>'isVerified'='true'
                            OR p.raw_payload->>'is_verified'='true'
                            OR p.raw_payload->>'is_blue_verified'='true'
                            OR p.raw_payload->>'isBlueVerified'='true'
                            OR p.raw_payload->>'blue_verified'='true'
                            OR p.raw_payload->>'blueVerified'='true'
                            OR p.raw_payload->>'verified_badge'='true')
               )
               AND NOT EXISTS (
                    SELECT 1 FROM youtube_channels channel
                     WHERE f.platform='youtube'
                       AND (channel.platform_channel_id=f.target_uid
                            OR LOWER(channel.custom_url)=LOWER(BTRIM(f.target_username)))
                       AND (
                            channel.last_skip_reason='verified_account'
                            OR EXISTS (
                                SELECT 1 FROM youtube_videos video
                                 WHERE video.channel_id=channel.id
                                   AND (video.metadata->>'channel_is_verified'='true'
                                        OR video.metadata->>'is_verified'='true'
                                        OR video.metadata->>'verified'='true')
                            )
                            OR EXISTS (
                                SELECT 1 FROM youtube_community_posts post
                                 WHERE post.channel_id=channel.platform_channel_id
                                   AND (post.metadata->>'channel_is_verified'='true'
                                        OR post.metadata->>'is_verified'='true'
                                        OR post.metadata->>'verified'='true')
                            )
                       )
               )
             ORDER BY LOWER(BTRIM(f.target_username)),
                      f.last_seen DESC NULLS LAST
      ) followed
     WHERE NOT EXISTS (
            SELECT 1 FROM browser_native_scrape_queue q
             WHERE q.platform=$1 AND q.target_url=$2 || followed.username || $3
     )
     ORDER BY followed.last_seen DESC NULLS LAST,
              LOWER(followed.username)
     LIMIT GREATEST(0, $5 - (
            SELECT count(*) FROM browser_native_scrape_queue
             WHERE platform=$1 AND source='followed_handle_candidate'
               AND status IN ('pending','claimed')
     ))
    ON CONFLICT (platform, target_url) DO UPDATE SET
        username=COALESCE(EXCLUDED.username, browser_native_scrape_queue.username),
        source='followed_handle_candidate',
        priority=GREATEST(browser_native_scrape_queue.priority, EXCLUDED.priority),
        updated_at=now()
"""

_FOLLOWED_AIRBNB_LINKS_SQL = r"""
    WITH candidates AS (
        SELECT DISTINCT ON (l.url) l.url, l.discovered_at, l.id
          FROM discovered_links l
         WHERE l.domain ~* '(^|\.)airbnb\.(com|com\.sg|co\.uk|co\.kr)$'
           AND l.url ~* '^https://([^/]+\.)?airbnb\.(com|com\.sg|co\.uk|co\.kr)/rooms/[0-9]+'
           AND (
                EXISTS (
                    SELECT 1 FROM instagram_posts p
                    JOIN instagram_profiles profile ON profile.id=p.profile_id
                    JOIN follow_edges f ON f.platform='instagram'
                        AND f.direction='following'
                        AND (f.target_uid=profile.platform_user_id
                             OR LOWER(f.target_username)=LOWER(profile.username))
                    WHERE l.source_table='instagram_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
                      AND profile.is_verified IS NOT TRUE
                      AND p.metadata->>'author_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified_badge' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM x_posts p
                    JOIN follow_edges f ON f.platform='x' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.author_username)
                    LEFT JOIN x_profiles profile ON LOWER(profile.username)=LOWER(p.author_username)
                    WHERE l.source_table='x_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
                      AND profile.is_verified IS NOT TRUE
                      AND p.metadata->>'author_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified_badge' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM tiktok_posts p
                    JOIN tiktok_profiles profile ON profile.id=p.profile_id
                    JOIN follow_edges f ON f.platform='tiktok' AND f.direction='following'
                        AND (f.target_uid=profile.platform_user_id
                             OR LOWER(f.target_username)=LOWER(profile.username))
                    WHERE l.source_table='tiktok_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
                      AND profile.is_verified IS NOT TRUE
                      AND p.metadata->>'author_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified_badge' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM threads_posts p
                    JOIN follow_edges f ON f.platform='threads' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.author_username)
                    WHERE l.source_table='threads_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
                      AND p.metadata->>'author_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified_badge' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM pinterest_pins p
                    JOIN follow_edges f ON f.platform='pinterest' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.username)
                    LEFT JOIN pinterest_profiles profile ON LOWER(profile.username)=LOWER(p.username)
                    WHERE l.source_table='pinterest_pins'
                      AND l.source_record_id=p.pin_id
                      AND p.metadata->>'author_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified_badge' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'profile_is_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'is_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'isVerified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'blue_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'blueVerified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'verified_badge' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM snapchat_stories p
                    JOIN follow_edges f ON f.platform='snapchat' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.username)
                    LEFT JOIN snapchat_profiles profile ON LOWER(profile.username)=LOWER(p.username)
                    WHERE l.source_table='snapchat_stories'
                      AND l.source_record_id=p.story_id
                      AND p.metadata->>'author_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blue_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'blueVerified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified_badge' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'is_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'isVerified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'is_blue_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'isBlueVerified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'blue_verified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'blueVerified' IS DISTINCT FROM 'true'
                      AND profile.raw_payload->>'verified_badge' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM youtube_videos p
                    JOIN youtube_channels channel ON channel.id=p.channel_id
                    JOIN follow_edges f ON f.platform='youtube' AND f.direction='following'
                        AND (f.target_uid=channel.platform_channel_id
                             OR LOWER(f.target_username)=LOWER(channel.custom_url))
                    WHERE l.source_table='youtube_videos'
                      AND (l.source_record_id=p.id::text
                           OR l.source_record_id=p.platform_video_id)
                      AND channel.last_skip_reason IS DISTINCT FROM 'verified_account'
                      AND p.metadata->>'channel_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM youtube_community_posts p
                    JOIN youtube_channels channel
                      ON channel.platform_channel_id=p.channel_id
                    JOIN follow_edges f ON f.platform='youtube' AND f.direction='following'
                        AND (f.target_uid=channel.platform_channel_id
                             OR LOWER(f.target_username)=LOWER(channel.custom_url))
                    WHERE l.source_table='youtube_community_posts'
                      AND (l.source_record_id=p.id::text
                           OR l.source_record_id=p.platform_post_id)
                      AND channel.last_skip_reason IS DISTINCT FROM 'verified_account'
                      AND p.metadata->>'channel_is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'is_verified' IS DISTINCT FROM 'true'
                      AND p.metadata->>'verified' IS DISTINCT FROM 'true'
                )
                OR EXISTS (
                    SELECT 1 FROM youtube_channels channel
                    JOIN follow_edges f ON f.platform='youtube' AND f.direction='following'
                        AND (f.target_uid=channel.platform_channel_id
                             OR LOWER(f.target_username)=LOWER(channel.custom_url))
                    WHERE l.source_table='youtube_channels'
                      AND (l.source_record_id=channel.id::text
                           OR l.source_record_id=channel.platform_channel_id)
                      AND channel.last_skip_reason IS DISTINCT FROM 'verified_account'
                )
           )
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='airbnb' AND q.target_url=l.url
                   AND (
                       q.status='completed'
                       OR q.source='followed_account_link'
                       OR (q.status='claimed' AND COALESCE(q.claim_expires_at, 'infinity'::timestamptz) > now())
                   )
           )
         ORDER BY l.url, l.discovered_at DESC NULLS LAST, l.id
    )
    INSERT INTO browser_native_scrape_queue
        (platform, target_url, username, source, priority)
    SELECT 'airbnb', candidates.url, NULL, 'followed_account_link', $1
      FROM candidates
     ORDER BY candidates.discovered_at DESC NULLS LAST, candidates.id
     LIMIT GREATEST(0, $2 - (
            SELECT count(*) FROM browser_native_scrape_queue
             WHERE platform='airbnb' AND source='followed_account_link'
               AND status IN ('pending','claimed')
     ))
    ON CONFLICT (platform, target_url) DO UPDATE SET
        source='followed_account_link',
        priority=GREATEST(browser_native_scrape_queue.priority, EXCLUDED.priority),
        updated_at=now()
"""


def snapchat_storage_target(content_type: str) -> tuple[str, str]:
    """Return the fixed table/key pair for a public Snapchat content type."""
    try:
        return _SNAPCHAT_CONTENT_TABLES[content_type.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError("unsupported_snapchat_content_type") from exc


def _remaining_queue_capacity(active_count: int, cap: int = _AIRBNB_LISTING_QUEUE_CAP) -> int:
    return max(0, cap - max(0, int(active_count)))


async def retarget_snapchat_profile_targets(conn: Any) -> None:
    """Move queued public-profile fetches to Snapchat's story-page endpoint.

    Keep each row's existing schedule so this endpoint correction does not
    create a burst of requests. Reset attempts only for failed rows because
    the old URL and the new public content endpoint are different targets.
    """
    await conn.execute(
        """
        UPDATE browser_native_scrape_queue q
           SET target_url='https://story.snapchat.com/@' || btrim(q.username),
               attempts=CASE
                   WHEN q.status='failed'
                     OR (q.status='claimed' AND q.claim_expires_at <= now()) THEN 0
                   ELSE q.attempts END,
               updated_at=now()
         WHERE q.platform='snapchat'
           AND q.source='snapchat_profiles'
           AND (q.status <> 'claimed' OR q.claim_expires_at <= now())
           AND btrim(q.username) ~ '^[A-Za-z0-9._-]{1,100}$'
           AND q.target_url NOT LIKE 'https://story.snapchat.com/@%'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue existing
                 WHERE existing.platform='snapchat'
                   AND existing.target_url='https://story.snapchat.com/@' || btrim(q.username)
                   AND existing.id <> q.id
           )
        """
    )


_SEED_QUERIES: dict[str, tuple[str, ...]] = {
    "pinterest": (
        """
        INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
        SELECT 'pinterest', 'https://www.pinterest.com/' || btrim(p.username) || '/',
               btrim(p.username), 'pinterest_profiles'
          FROM pinterest_profiles p
         WHERE btrim(p.username) ~ '^[A-Za-z0-9._-]{1,100}$'
           AND p.raw_payload->>'profile_is_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'is_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'isVerified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'is_blue_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'isBlueVerified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'blue_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'blueVerified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'verified_badge' IS DISTINCT FROM 'true'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='pinterest' AND q.username=btrim(p.username)
           )
         ORDER BY p.probed_at DESC NULLS LAST
         LIMIT GREATEST(0, 10 - (
             SELECT count(*) FROM browser_native_scrape_queue
              WHERE platform='pinterest' AND source='pinterest_profiles'
                AND status IN ('pending','claimed')
         ))
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
        """
        INSERT INTO browser_native_scrape_queue
            (platform, target_url, username, source, priority)
        SELECT 'pinterest', b.board_url, b.username, 'pinterest_boards', 15
          FROM pinterest_boards b
         WHERE b.board_url ~* '^https://([^/]+\\.)?pinterest\\.com/'
           AND b.board_url !~* '/_(created|saved)/?$'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='pinterest' AND q.target_url=b.board_url
           )
         ORDER BY b.collected_at DESC NULLS LAST, b.board_id
         LIMIT GREATEST(0, 10 - (
             SELECT count(*) FROM browser_native_scrape_queue
              WHERE platform='pinterest' AND source='pinterest_boards'
                AND status IN ('pending','claimed')
         ))
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
    ),
    "snapchat": (
        """
        INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
        SELECT 'snapchat', 'https://story.snapchat.com/@' || btrim(p.username),
               btrim(p.username), 'snapchat_profiles'
          FROM snapchat_profiles p
         WHERE btrim(p.username) ~ '^[A-Za-z0-9._-]{1,100}$'
           AND p.raw_payload->>'is_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'isVerified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'is_blue_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'isBlueVerified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'blue_verified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'blueVerified' IS DISTINCT FROM 'true'
           AND p.raw_payload->>'verified_badge' IS DISTINCT FROM 'true'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='snapchat' AND q.username=btrim(p.username)
           )
         ORDER BY p.probed_at DESC NULLS LAST
         LIMIT GREATEST(0, 10 - (
             SELECT count(*) FROM browser_native_scrape_queue
              WHERE platform='snapchat' AND source='snapchat_profiles'
                AND status IN ('pending','claimed')
         ))
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
        """
        INSERT INTO browser_native_scrape_queue
            (platform, target_url, username, source, priority)
        VALUES ('snapchat', 'https://www.snapchat.com/discover', NULL, 'snapchat_discover', 15),
               ('snapchat', 'https://www.snapchat.com/spotlight', NULL, 'snapchat_spotlight', 15)
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
    ),
    "airbnb": (
        """
        INSERT INTO browser_native_scrape_queue (platform, target_url, username, source, priority)
        SELECT 'airbnb', l.url, NULL, 'discovered_links', 10
          FROM discovered_links l
         WHERE l.domain ~* '(^|\\.)airbnb\\.(com|com\\.sg|co\\.uk|co\\.kr)$'
           AND l.url ~* '^https://([^/]+\\.)?airbnb\\.(com|com\\.sg|co\\.uk|co\\.kr)/rooms/[0-9]+'
           AND COALESCE(l.status, '') NOT IN ('404', 'error', 'ignored')
           AND NOT EXISTS (
                SELECT 1 FROM pinterest_profiles p
                 WHERE l.source_table='pinterest_profiles'
                   AND l.source_record_id=p.username
                   AND (p.raw_payload->>'profile_is_verified'='true'
                        OR p.raw_payload->>'is_verified'='true'
                        OR p.raw_payload->>'verified'='true'
                        OR p.raw_payload->>'isVerified'='true'
                        OR p.raw_payload->>'is_blue_verified'='true'
                        OR p.raw_payload->>'isBlueVerified'='true'
                        OR p.raw_payload->>'blue_verified'='true'
                        OR p.raw_payload->>'blueVerified'='true'
                        OR p.raw_payload->>'verified_badge'='true')
           )
           AND NOT EXISTS (
                SELECT 1 FROM youtube_videos p
                LEFT JOIN youtube_channels channel ON channel.id=p.channel_id
                 WHERE l.source_table='youtube_videos'
                   AND (l.source_record_id=p.id::text
                        OR l.source_record_id=p.platform_video_id)
                   AND (channel.last_skip_reason='verified_account'
                        OR p.metadata->>'channel_is_verified'='true'
                        OR p.metadata->>'is_verified'='true'
                        OR p.metadata->>'verified'='true')
           )
           AND NOT EXISTS (
                SELECT 1 FROM youtube_community_posts p
                LEFT JOIN youtube_channels channel
                  ON channel.platform_channel_id=p.channel_id
                 WHERE l.source_table='youtube_community_posts'
                   AND (l.source_record_id=p.id::text
                        OR l.source_record_id=p.platform_post_id)
                   AND (channel.last_skip_reason='verified_account'
                        OR p.metadata->>'channel_is_verified'='true'
                        OR p.metadata->>'is_verified'='true'
                        OR p.metadata->>'verified'='true')
           )
           AND NOT EXISTS (
                SELECT 1 FROM youtube_channels channel
                 WHERE l.source_table='youtube_channels'
                   AND (l.source_record_id=channel.id::text
                        OR l.source_record_id=channel.platform_channel_id)
                   AND channel.last_skip_reason='verified_account'
           )
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='airbnb' AND q.target_url=l.url
           )
         ORDER BY l.discovered_at DESC NULLS LAST
         LIMIT GREATEST(0, 10 - (
             SELECT count(*) FROM browser_native_scrape_queue
              WHERE platform='airbnb' AND status IN ('pending','claimed')
         ))
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
        """
        INSERT INTO browser_native_scrape_queue
            (platform, target_url, username, source, priority)
        SELECT 'airbnb', 'https://www.airbnb.com/rooms/' || l.listing_id,
               NULL, 'airbnb_listings', 15
          FROM airbnb_listings l
         WHERE l.listing_id ~ '^[0-9]{1,24}$'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='airbnb'
                   AND q.target_url='https://www.airbnb.com/rooms/' || l.listing_id
           )
         ORDER BY l.collected_at DESC NULLS LAST, l.listing_id
         LIMIT $1
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
        """
        INSERT INTO browser_native_scrape_queue
            (platform, target_url, username, source, priority)
        SELECT 'airbnb', 'https://www.airbnb.com/users/show/' || btrim(p.username),
               btrim(p.username), 'airbnb_profiles', 5
          FROM airbnb_profiles p
         WHERE btrim(p.username) ~ '^[0-9]{1,20}$'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='airbnb' AND q.username=btrim(p.username)
           )
         ORDER BY p.probed_at DESC NULLS LAST
         LIMIT GREATEST(0, 10 - (
             SELECT count(*) FROM browser_native_scrape_queue
              WHERE platform='airbnb' AND source='airbnb_profiles'
                AND status IN ('pending','claimed')
         ))
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
    ),
}


async def seed_native_targets(pool: Any, platform: str) -> None:
    """Seed followed-identity targets before low-priority public discovery."""
    if platform not in _PLATFORMS:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"browser-native-scrape:{platform}",
            )
            if platform == "snapchat":
                await retarget_snapchat_profile_targets(conn)

            if platform in {"pinterest", "snapchat"}:
                url_prefix, url_suffix = (
                    ("https://www.pinterest.com/", "/")
                    if platform == "pinterest"
                    else ("https://story.snapchat.com/@", "")
                )
                await conn.execute(
                    _FOLLOWED_HANDLE_SEED_SQL,
                    platform,
                    url_prefix,
                    url_suffix,
                    _FOLLOWED_TARGET_PRIORITY,
                    _FOLLOWED_TARGET_CAP,
                )
            elif platform == "airbnb":
                await conn.execute(
                    _FOLLOWED_AIRBNB_LINKS_SQL,
                    _FOLLOWED_TARGET_PRIORITY,
                    _FOLLOWED_TARGET_CAP,
                )

            preferred_count = await conn.fetchval(
                """
                SELECT count(*) FROM browser_native_scrape_queue
                 WHERE platform=$1 AND source=$2
                   AND status IN ('pending','claimed')
                """,
                platform,
                _FOLLOWED_TARGET_SOURCES[platform],
            )
            # Public discovery is lower priority. Keep its backlog from growing
            # while followed-identity candidates are still waiting for a pass.
            if preferred_count:
                return

            for query in _SEED_QUERIES[platform]:
                if platform == "airbnb" and "'airbnb_listings'" in query:
                    active_listings = await conn.fetchval(
                        """
                        SELECT count(*) FROM browser_native_scrape_queue
                         WHERE platform='airbnb' AND source='airbnb_listings'
                           AND status IN ('pending','claimed')
                        """
                    )
                    remaining = _remaining_queue_capacity(active_listings or 0)
                    await conn.execute(query, remaining)
                else:
                    await conn.execute(query)


async def claim_native_target(pool: Any, platform: str) -> dict[str, Any] | None:
    """Claim one due target; the browser bridge and native client share leases."""
    if platform not in _PLATFORMS:
        return None
    async with pool.acquire() as conn:
        async with conn.transaction():
            # The browser bridge takes this same transaction lock while
            # claiming, so native workers cannot claim a second target for
            # the same platform during the bridge's claim window.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"browser-native-scrape:{platform}",
            )
            row = await conn.fetchrow(
                """
                UPDATE browser_native_scrape_queue q
                   SET status='claimed', attempts=q.attempts+1,
                       claim_expires_at=now()+interval '10 minutes',
                       updated_at=now(), last_error=NULL
                 WHERE q.id=(
                    SELECT due.id
                      FROM browser_native_scrape_queue due
                     WHERE due.platform=$1
                       AND due.attempts < $2
                       AND due.next_visit_at <= now()
                       AND (due.status IN ('pending','failed','completed')
                            OR (due.status='claimed' AND due.claim_expires_at <= now()))
                       AND (
                            due.source=$3
                            OR NOT EXISTS (
                                SELECT 1 FROM browser_native_scrape_queue preferred
                                 WHERE preferred.platform=$1
                                   AND preferred.source=$3
                                   AND preferred.status IN ('pending','claimed')
                            )
                       )
                       AND NOT EXISTS (
                            SELECT 1 FROM browser_native_scrape_queue active
                             WHERE active.platform=$1
                               AND active.status='claimed'
                               AND active.claim_expires_at > now()
                       )
                     ORDER BY due.priority DESC, due.next_visit_at, due.id
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                 )
                RETURNING q.id, q.target_url, q.username, q.source, q.attempts
                """,
                platform,
                _MAX_ATTEMPTS,
                _FOLLOWED_TARGET_SOURCES[platform],
            )
    return dict(row) if row else None


async def finish_native_target(
    pool: Any,
    platform: str,
    target_id: int,
    *,
    success: bool,
    error: str | None = None,
    retry_after_hours: int = 6,
    retry_after_seconds: int | None = None,
) -> None:
    """Release a claim and either schedule a refresh or a bounded retry."""
    if platform not in _PLATFORMS:
        return
    async with pool.acquire() as conn:
        if success:
            await conn.execute(
                """
                UPDATE browser_native_scrape_queue
                   SET status='completed', attempts=0, last_error=NULL,
                       next_visit_at=now()+interval '12 hours',
                       claim_expires_at=NULL, last_result_at=now(), updated_at=now()
                 WHERE id=$1 AND platform=$2 AND status='claimed'
                """,
                target_id,
                platform,
            )
            return
        delay_seconds = (
            max(1, min(int(retry_after_seconds), 168 * 60 * 60))
            if retry_after_seconds is not None
            else max(1, min(int(retry_after_hours), 168)) * 60 * 60
        )
        await conn.execute(
            """
            UPDATE browser_native_scrape_queue
               SET status='failed', last_error=$3,
                   next_visit_at=now()+($4 * interval '1 second'),
                   claim_expires_at=NULL, last_result_at=now(), updated_at=now()
             WHERE id=$1 AND platform=$2 AND status='claimed'
            """,
            target_id,
            platform,
            (error or "native_page_unavailable")[:120],
            delay_seconds,
        )
