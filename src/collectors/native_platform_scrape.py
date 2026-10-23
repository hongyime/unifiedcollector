"""Shared leases and target seeding for read-only native platform scrapers."""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

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
_FOLLOW_ONLY_SEED_QUERY_INDEXES = {
    # These are user-selected targets or enrichment for already selected
    # identities/listings. Other seed queries expand public discovery.
    "pinterest": (2,),
    "snapchat": (),
    # Seed explicit Wishlist and homepage selections before the general
    # enrichment pass for listings already in the database.
    "airbnb": (4, 5, 0, 1, 2),
}
_FOLLOW_ONLY_QUEUE_SOURCES = {
    "pinterest": ("followed_handle_candidate", "pinterest_pin_details"),
    "snapchat": ("followed_handle_candidate",),
    "airbnb": (
        "followed_account_link",
        "airbnb_listing_hosts",
        "discovered_links",
        "airbnb_listings",
        "airbnb_wishlist",
        "airbnb_homepage",
    ),
}


def _public_discovery_enabled(platform: str | None = None) -> bool:
    """Same precedence as the profile-only collector: an explicit
    source-scoped ``<PLATFORM>_PROBE_NATIVE_PUBLIC_DISCOVERY_ENABLED``
    wins, then the global ``NATIVE_PUBLIC_DISCOVERY_ENABLED``, else disabled.
    """
    if platform:
        scoped = os.getenv(
            f"{platform.upper()}_PROBE_NATIVE_PUBLIC_DISCOVERY_ENABLED"
        )
        if scoped is not None:
            return scoped.strip().lower() in {"1", "true", "yes", "on"}
    return os.getenv("NATIVE_PUBLIC_DISCOVERY_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }

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
               AND f.platform=$1
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
                       AND (COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') IN ('true', '1', 'yes'))
               )
               AND NOT EXISTS (
                    SELECT 1 FROM pinterest_profiles p
                     WHERE (f.platform='pinterest' OR $1='pinterest')
                       AND LOWER(p.username)=LOWER(BTRIM(f.target_username))
                       AND (COALESCE(LOWER(BTRIM(p.raw_payload->>'profile_is_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'isVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'isBlueVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'blueVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'verified_badge')), '') IN ('true', '1', 'yes'))
               )
               AND NOT EXISTS (
                    SELECT 1 FROM snapchat_profiles p
                     WHERE (f.platform='snapchat' OR $1='snapchat')
                       AND LOWER(p.username)=LOWER(BTRIM(f.target_username))
                       AND (COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'isVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'isBlueVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'blueVerified')), '') IN ('true', '1', 'yes')
                            OR COALESCE(LOWER(BTRIM(p.raw_payload->>'verified_badge')), '') IN ('true', '1', 'yes'))
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
                                   AND (COALESCE(LOWER(BTRIM(video.metadata->>'channel_is_verified')), '') IN ('true', '1', 'yes')
                                        OR COALESCE(LOWER(BTRIM(video.metadata->>'is_verified')), '') IN ('true', '1', 'yes')
                                        OR COALESCE(LOWER(BTRIM(video.metadata->>'verified')), '') IN ('true', '1', 'yes'))
                            )
                            OR EXISTS (
                                SELECT 1 FROM youtube_community_posts post
                                 WHERE post.channel_id=channel.platform_channel_id
                                   AND (COALESCE(LOWER(BTRIM(post.metadata->>'channel_is_verified')), '') IN ('true', '1', 'yes')
                                        OR COALESCE(LOWER(BTRIM(post.metadata->>'is_verified')), '') IN ('true', '1', 'yes')
                                        OR COALESCE(LOWER(BTRIM(post.metadata->>'verified')), '') IN ('true', '1', 'yes'))
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
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                )
                OR EXISTS (
                    SELECT 1 FROM x_posts p
                    JOIN follow_edges f ON f.platform='x' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.author_username)
                    LEFT JOIN x_profiles profile ON LOWER(profile.username)=LOWER(p.author_username)
                    WHERE l.source_table='x_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
                      AND profile.is_verified IS NOT TRUE
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
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
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                )
                OR EXISTS (
                    SELECT 1 FROM threads_posts p
                    JOIN follow_edges f ON f.platform='threads' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.author_username)
                    WHERE l.source_table='threads_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                )
                OR EXISTS (
                    SELECT 1 FROM pinterest_pins p
                    JOIN follow_edges f ON f.platform='pinterest' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.username)
                    LEFT JOIN pinterest_profiles profile ON LOWER(profile.username)=LOWER(p.username)
                    WHERE l.source_table='pinterest_pins'
                      AND l.source_record_id=p.pin_id
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'profile_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                )
                OR EXISTS (
                    SELECT 1 FROM snapchat_stories p
                    JOIN follow_edges f ON f.platform='snapchat' AND f.direction='following'
                        AND LOWER(f.target_username)=LOWER(p.username)
                    LEFT JOIN snapchat_profiles profile ON LOWER(profile.username)=LOWER(p.username)
                    WHERE l.source_table='snapchat_stories'
                      AND l.source_record_id=p.story_id
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'isVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(profile.raw_payload->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
                )
                OR EXISTS (
                    SELECT 1 FROM facebook_posts p
                    JOIN facebook_profiles profile
                      ON LOWER(profile.username)=LOWER(p.author_username)
                    JOIN follow_edges f ON f.platform='facebook' AND f.direction='following'
                        AND (f.target_uid=profile.platform_user_id
                             OR LOWER(f.target_username)=LOWER(profile.username))
                    WHERE l.source_table='facebook_posts'
                      AND (l.source_record_id=p.id::text OR l.source_record_id=p.platform_post_id)
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
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'channel_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
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
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'channel_is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
                      AND COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
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

_FOLLOWED_AIRBNB_PROFILE_URLS_SQL = r"""
    WITH source_links AS (
        SELECT p.external_url AS raw_url, p.updated_at AS seen_at
          FROM instagram_profiles p
          JOIN follow_edges f ON f.platform='instagram' AND f.direction='following'
            AND (f.target_uid::text=p.platform_user_id::text
                 OR LOWER(f.target_username)=LOWER(p.username))
         WHERE p.is_verified IS NOT TRUE AND p.external_url IS NOT NULL
        UNION ALL
        SELECT p.external_url, p.updated_at
          FROM x_profiles p
          JOIN follow_edges f ON f.platform='x' AND f.direction='following'
            AND (f.target_uid::text=p.platform_user_id::text
                 OR LOWER(f.target_username)=LOWER(p.username))
         WHERE p.is_verified IS NOT TRUE AND p.external_url IS NOT NULL
        UNION ALL
        SELECT p.bio, p.updated_at
          FROM tiktok_profiles p
          JOIN follow_edges f ON f.platform='tiktok' AND f.direction='following'
            AND (f.target_uid::text=p.platform_user_id::text
                 OR LOWER(f.target_username)=LOWER(p.username))
         WHERE p.is_verified IS NOT TRUE AND p.bio IS NOT NULL
        UNION ALL
        SELECT p.external_url, p.updated_at
          FROM facebook_profiles p
          JOIN follow_edges f ON f.platform='facebook' AND f.direction='following'
            AND (f.target_uid=p.platform_user_id
                 OR LOWER(f.target_username)=LOWER(p.username))
         WHERE p.external_url IS NOT NULL
        UNION ALL
        SELECT p.bio, p.updated_at
          FROM facebook_profiles p
          JOIN follow_edges f ON f.platform='facebook' AND f.direction='following'
            AND (f.target_uid=p.platform_user_id
                 OR LOWER(f.target_username)=LOWER(p.username))
         WHERE p.bio IS NOT NULL
        UNION ALL
        SELECT p.bio, p.probed_at
          FROM bluesky_profiles p
          JOIN follow_edges f ON f.platform='bluesky' AND f.direction='following'
            AND (f.target_uid=p.did
                 OR LOWER(f.target_username)=LOWER(p.username))
         WHERE p.bio IS NOT NULL
        UNION ALL
        SELECT p.bio, p.probed_at
          FROM pinterest_profiles p
          JOIN follow_edges f ON f.platform='pinterest' AND f.direction='following'
            AND LOWER(f.target_username)=LOWER(p.username)
         WHERE p.bio IS NOT NULL
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'profile_is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
        UNION ALL
        SELECT p.raw_payload::text, p.probed_at
          FROM snapchat_profiles p
          JOIN follow_edges f ON f.platform='snapchat' AND f.direction='following'
            AND LOWER(f.target_username)=LOWER(p.username)
         WHERE p.raw_payload IS NOT NULL
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'isVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
        UNION ALL
        SELECT s.metadata::text, s.last_seen
          FROM social_users s
          JOIN follow_edges f ON f.platform='threads' AND f.direction='following'
            AND (f.target_uid=s.uid OR LOWER(f.target_username)=LOWER(s.username))
         WHERE s.metadata IS NOT NULL
           AND COALESCE(LOWER(BTRIM(s.metadata->>'author_is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'isVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(s.metadata->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
        UNION ALL
        SELECT p.blog, p.collected_at
          FROM github_users p
          JOIN follow_edges f ON f.platform='github' AND f.direction='following'
            AND (f.target_uid::text=p.platform_user_id::text
                 OR LOWER(f.target_username)=LOWER(p.login))
         WHERE p.blog IS NOT NULL
        UNION ALL
        SELECT link.value->>'url', c.updated_at
          FROM youtube_channels c
          JOIN follow_edges f ON f.platform='youtube' AND f.direction='following'
            AND (f.target_uid::text=c.platform_channel_id::text
                 OR LOWER(f.target_username)=LOWER(c.custom_url))
          CROSS JOIN LATERAL jsonb_array_elements(COALESCE(c.external_links, '[]'::jsonb)) AS link(value)
         WHERE c.last_skip_reason IS DISTINCT FROM 'verified_account'
           AND link.value->>'url' IS NOT NULL
        UNION ALL
        SELECT c.description, c.updated_at
          FROM youtube_channels c
          JOIN follow_edges f ON f.platform='youtube' AND f.direction='following'
            AND (f.target_uid::text=c.platform_channel_id::text
                 OR LOWER(f.target_username)=LOWER(c.custom_url))
         WHERE c.last_skip_reason IS DISTINCT FROM 'verified_account'
           AND c.description IS NOT NULL
    ), normalized AS (
        SELECT DISTINCT ON (extracted.parts[1])
               'https://www.airbnb.com/rooms/' || extracted.parts[1] AS target_url,
               source_links.seen_at
          FROM source_links
          CROSS JOIN LATERAL (
              SELECT regexp_match(source_links.raw_url, $3, 'i') AS parts
          ) AS extracted
         WHERE extracted.parts IS NOT NULL
         ORDER BY extracted.parts[1], source_links.seen_at DESC NULLS LAST
    )
    INSERT INTO browser_native_scrape_queue
        (platform, target_url, username, source, priority)
    SELECT 'airbnb', normalized.target_url, NULL, 'followed_account_link', $1
      FROM normalized
     WHERE NOT EXISTS (
            SELECT 1 FROM browser_native_scrape_queue q
             WHERE q.platform='airbnb' AND q.target_url=normalized.target_url
               AND (
                   q.status='completed'
                   OR q.source='followed_account_link'
                   OR (q.status='claimed' AND COALESCE(q.claim_expires_at, 'infinity'::timestamptz) > now())
               )
     )
     ORDER BY normalized.seen_at DESC NULLS LAST, normalized.target_url
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


_PINTEREST_PIN_DETAIL_SEED_SQL = r"""
INSERT INTO browser_native_scrape_queue
    (platform, target_url, username, source, priority)
SELECT 'pinterest', p.pin_url, p.username, 'pinterest_pin_details', 20
  FROM pinterest_pins p
  JOIN follow_edges f
    ON f.platform='pinterest' AND f.direction='following'
   AND LOWER(BTRIM(f.target_username))=LOWER(BTRIM(p.username))
 WHERE p.pin_id ~ '^[0-9]+$'
   AND p.pin_url ~* '^https://(www\.)?pinterest\.com/pin/[0-9]+/?$'
   AND p.metadata->>'owner_verified'='true'
   AND COALESCE(LOWER(p.metadata->>'author_is_verified'),'') NOT IN ('true','1','yes')
   AND (p.description IS NULL OR p.destination_url IS NULL
        OR p.repin_count IS NULL OR p.comment_count IS NULL)
   AND NOT EXISTS (
        SELECT 1 FROM pinterest_profiles profile
         WHERE LOWER(profile.username)=LOWER(p.username)
           AND (
                COALESCE(LOWER(profile.raw_payload->>'profile_is_verified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'is_verified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'verified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'isVerified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'is_blue_verified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'isBlueVerified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'blue_verified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'blueVerified'),'') IN ('true','1','yes')
                OR COALESCE(LOWER(profile.raw_payload->>'verified_badge'),'') IN ('true','1','yes')
           )
   )
   AND NOT EXISTS (
        SELECT 1 FROM browser_native_scrape_queue q
         WHERE q.platform='pinterest' AND q.target_url=p.pin_url
   )
 ORDER BY p.collected_at DESC NULLS LAST, p.pin_id
 LIMIT GREATEST(0, 10 - (
     SELECT count(*) FROM browser_native_scrape_queue
      WHERE platform='pinterest' AND source='pinterest_pin_details'
        AND status IN ('pending','claimed')
 ))
ON CONFLICT (platform, target_url) DO NOTHING
"""


_SEED_QUERIES: dict[str, tuple[str, ...]] = {
    "pinterest": (
        """
        INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
        SELECT 'pinterest', 'https://www.pinterest.com/' || btrim(p.username) || '/',
               btrim(p.username), 'pinterest_profiles'
          FROM pinterest_profiles p
         WHERE btrim(p.username) ~ '^[A-Za-z0-9._-]{1,100}$'
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'profile_is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'isVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
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
        _PINTEREST_PIN_DETAIL_SEED_SQL,
    ),
    "snapchat": (
        """
        INSERT INTO browser_native_scrape_queue (platform, target_url, username, source)
        SELECT 'snapchat', 'https://story.snapchat.com/@' || btrim(p.username),
               btrim(p.username), 'snapchat_profiles'
          FROM snapchat_profiles p
         WHERE btrim(p.username) ~ '^[A-Za-z0-9._-]{1,100}$'
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'isVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'isBlueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'blueVerified')), '') NOT IN ('true', '1', 'yes')
           AND COALESCE(LOWER(BTRIM(p.raw_payload->>'verified_badge')), '') NOT IN ('true', '1', 'yes')
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
        WITH candidates AS (
            SELECT DISTINCT ON (l.host_id)
                   l.host_id,
                   'https://www.airbnb.com/users/show/' || l.host_id AS target_url,
                   l.collected_at
              FROM airbnb_listings l
              LEFT JOIN airbnb_hosts h ON h.host_id=l.host_id
             WHERE l.host_id ~ '^[0-9]{1,20}$'
               AND (
                    h.host_id IS NULL
                    OR NULLIF(BTRIM(h.display_name), '') IS NULL
                    OR NULLIF(BTRIM(h.avatar_url), '') IS NULL
                    OR NULLIF(BTRIM(h.bio), '') IS NULL
                    OR h.rating IS NULL
                    OR h.reviews_count IS NULL
                    OR h.host_since IS NULL
                    OR h.response_rate IS NULL
               )
               AND NOT EXISTS (
                    SELECT 1 FROM browser_native_scrape_queue q
                     WHERE q.platform='airbnb'
                       AND q.target_url='https://www.airbnb.com/users/show/' || l.host_id
               )
             ORDER BY l.host_id, l.collected_at DESC NULLS LAST
        )
        INSERT INTO browser_native_scrape_queue
            (platform, target_url, username, source, priority)
        SELECT 'airbnb', candidates.target_url, candidates.host_id,
               'airbnb_listing_hosts', 20
          FROM candidates
         ORDER BY candidates.collected_at DESC NULLS LAST, candidates.host_id
         LIMIT GREATEST(0, 10 - (
                SELECT count(*) FROM browser_native_scrape_queue
                 WHERE platform='airbnb' AND status IN ('pending','claimed')
         ))
        ON CONFLICT (platform, target_url) DO NOTHING
        """,
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
                   AND (COALESCE(LOWER(BTRIM(p.raw_payload->>'profile_is_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'is_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'isVerified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'is_blue_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'isBlueVerified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'blue_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'blueVerified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.raw_payload->>'verified_badge')), '') IN ('true', '1', 'yes'))
           )
           AND NOT EXISTS (
                SELECT 1 FROM youtube_videos p
                LEFT JOIN youtube_channels channel ON channel.id=p.channel_id
                 WHERE l.source_table='youtube_videos'
                   AND (l.source_record_id=p.id::text
                        OR l.source_record_id=p.platform_video_id)
                   AND (channel.last_skip_reason='verified_account'
                        OR COALESCE(LOWER(BTRIM(p.metadata->>'channel_is_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') IN ('true', '1', 'yes'))
           )
           AND NOT EXISTS (
                SELECT 1 FROM youtube_community_posts p
                LEFT JOIN youtube_channels channel
                  ON channel.platform_channel_id=p.channel_id
                 WHERE l.source_table='youtube_community_posts'
                   AND (l.source_record_id=p.id::text
                        OR l.source_record_id=p.platform_post_id)
                   AND (channel.last_skip_reason='verified_account'
                        OR COALESCE(LOWER(BTRIM(p.metadata->>'channel_is_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.metadata->>'is_verified')), '') IN ('true', '1', 'yes')
                        OR COALESCE(LOWER(BTRIM(p.metadata->>'verified')), '') IN ('true', '1', 'yes'))
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
        """
        INSERT INTO browser_native_scrape_queue
            (platform, target_url, username, source, priority)
        SELECT 'airbnb', 'https://www.airbnb.com/rooms/' || l.listing_id,
               NULL, 'airbnb_wishlist', 45
          FROM airbnb_listings l
         WHERE l.listing_id ~ '^[0-9]{1,24}$'
           AND l.metadata->>'collection_source'='airbnb_wishlist'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='airbnb'
                   AND q.target_url='https://www.airbnb.com/rooms/' || l.listing_id
           )
         ORDER BY l.collected_at DESC NULLS LAST, l.listing_id
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
               NULL, 'airbnb_homepage', 30
          FROM airbnb_listings l
         WHERE l.listing_id ~ '^[0-9]{1,24}$'
           AND l.metadata->>'collection_source'='airbnb_homepage'
           AND NOT EXISTS (
                SELECT 1 FROM browser_native_scrape_queue q
                 WHERE q.platform='airbnb'
                   AND q.target_url='https://www.airbnb.com/rooms/' || l.listing_id
           )
         ORDER BY l.collected_at DESC NULLS LAST, l.listing_id
         LIMIT GREATEST(0, 10 - (
             SELECT count(*) FROM browser_native_scrape_queue
              WHERE platform='airbnb' AND status IN ('pending','claimed')
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
            if platform in {"pinterest", "snapchat"}:
                await quarantine_unfollowed_handle_candidates(conn, platform)
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
                await conn.execute(
                    _FOLLOWED_AIRBNB_PROFILE_URLS_SQL,
                    _FOLLOWED_TARGET_PRIORITY,
                    _FOLLOWED_TARGET_CAP,
                    r"(?:^|[^A-Za-z0-9.-])(?:https?://)?(?:www\.)?airbnb\.(?:com(?:\.[a-z]{2})?|co\.[a-z]{2}|[a-z]{2,3})/rooms/([0-9]{1,24})",
                )
                await conn.execute(
                    """
                    UPDATE browser_native_scrape_queue q
                       SET source=CASE
                               WHEN l.metadata->>'collection_source'='airbnb_wishlist'
                                   THEN 'airbnb_wishlist'
                               ELSE 'airbnb_homepage'
                           END,
                           priority=GREATEST(q.priority, CASE
                               WHEN l.metadata->>'collection_source'='airbnb_wishlist' THEN 45
                               ELSE 30
                           END),
                           updated_at=now()
                      FROM airbnb_listings l
                     WHERE q.platform='airbnb'
                       AND q.target_url='https://www.airbnb.com/rooms/' || l.listing_id
                       AND q.source IN ('airbnb_listings','airbnb_homepage')
                       AND l.metadata->>'collection_source' IN ('airbnb_homepage','airbnb_wishlist')
                    """
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

            queries = _SEED_QUERIES[platform]
            if not _public_discovery_enabled(platform):
                queries = tuple(
                    queries[index]
                    for index in _FOLLOW_ONLY_SEED_QUERY_INDEXES[platform]
                )
            elif platform == "airbnb":
                # Even with broad discovery enabled, user-selected Wishlist
                # and homepage seeds get first use of the bounded queue.
                queries = tuple(queries[index] for index in (4, 5, 0, 1, 2, 3))
            for query in queries:
                if platform == "airbnb" and "'airbnb_listings'" in query:
                    active_listings = await conn.fetchval(
                        """
                        SELECT count(*) FROM browser_native_scrape_queue
                         WHERE platform='airbnb'
                           AND status IN ('pending','claimed')
                        """
                    )
                    remaining = _remaining_queue_capacity(active_listings or 0)
                    await conn.execute(query, remaining)
                else:
                    await conn.execute(query)


async def quarantine_unfollowed_handle_candidates(conn: Any, platform: str) -> None:
    """Exhaust stale guessed handles that are absent from the platform's own follow graph."""
    if platform not in {"pinterest", "snapchat"}:
        return
    await conn.execute(
        """
        UPDATE browser_native_scrape_queue q
           SET status='failed', attempts=GREATEST(q.attempts, $2),
               last_error='follow_edge_missing', updated_at=now()
         WHERE q.platform=$1 AND q.source='followed_handle_candidate'
           AND (
                q.status IN ('pending','failed')
                OR (q.status='claimed' AND q.claim_expires_at <= now())
           )
           AND (
                q.status <> 'failed'
                OR q.attempts < $2
                OR q.last_error IS DISTINCT FROM 'follow_edge_missing'
           )
           AND NOT EXISTS (
                SELECT 1 FROM follow_edges f
                 WHERE f.platform=$1 AND f.direction='following'
                   AND LOWER(BTRIM(f.target_username))=LOWER(BTRIM(q.username))
           )
        """,
        platform,
        _MAX_ATTEMPTS,
    )


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
                       claim_token=$6,
                       claim_expires_at=now()+interval '10 minutes',
                       updated_at=now(), last_error=NULL
                 WHERE q.id=(
                    SELECT due.id
                      FROM browser_native_scrape_queue due
                     WHERE due.platform=$1
                       AND due.attempts < $2
                       AND due.next_visit_at <= now()
                       AND ($4::boolean OR due.source = ANY($5::text[]))
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
                     -- Give never-tried pending identities a first pass before
                     -- due retries, so repeated failures cannot starve a
                     -- followed-identity candidate.
                     ORDER BY due.priority DESC,
                              CASE WHEN due.status='pending' THEN 0 ELSE 1 END,
                              due.attempts,
                              due.next_visit_at,
                              due.id
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                 )
                RETURNING q.id, q.target_url, q.username, q.source, q.attempts, q.claim_token
                """,
                platform,
                _MAX_ATTEMPTS,
                _FOLLOWED_TARGET_SOURCES[platform],
                _public_discovery_enabled(platform),
                list(_FOLLOW_ONLY_QUEUE_SOURCES[platform]),
                uuid.uuid4(),
            )
    return dict(row) if row else None


async def finish_native_target(
    pool: Any,
    platform: str,
    target_id: int,
    *,
    claim_token: str | uuid.UUID,
    success: bool,
    error: str | None = None,
    retry_after_hours: int = 6,
    retry_after_seconds: int | None = None,
) -> bool:
    """Release a claim and either schedule a refresh or a bounded retry.

    Completion is compare-and-swap on the claim token issued by
    :func:`claim_native_target`: a worker whose lease expired and whose row was
    re-claimed by another worker gets a no-op rejection (False) instead of
    completing someone else's claim.
    """
    if platform not in _PLATFORMS:
        return False
    if isinstance(claim_token, str):
        try:
            token = uuid.UUID(claim_token)
        except ValueError:
            logger.warning(
                "native claim finish rejected: malformed claim token platform=%s target_id=%s",
                platform,
                target_id,
            )
            return False
    else:
        token = claim_token
    async with pool.acquire() as conn:
        if success:
            finished = await conn.fetchval(
                """
                UPDATE browser_native_scrape_queue
                   SET status='completed', attempts=0, last_error=NULL,
                       next_visit_at=now()+interval '12 hours',
                       claim_expires_at=NULL, last_result_at=now(), updated_at=now()
                 WHERE id=$1 AND platform=$2 AND status='claimed' AND claim_token=$3
                RETURNING 1
                """,
                target_id,
                platform,
                token,
            )
        else:
            delay_seconds = (
                max(1, min(int(retry_after_seconds), 168 * 60 * 60))
                if retry_after_seconds is not None
                else max(1, min(int(retry_after_hours), 168)) * 60 * 60
            )
            finished = await conn.fetchval(
                """
                UPDATE browser_native_scrape_queue
                   SET status='failed', last_error=$4,
                       next_visit_at=now()+($5 * interval '1 second'),
                       claim_expires_at=NULL, last_result_at=now(), updated_at=now()
                 WHERE id=$1 AND platform=$2 AND status='claimed' AND claim_token=$3
                RETURNING 1
                """,
                target_id,
                platform,
                token,
                (error or "native_page_unavailable")[:120],
                delay_seconds,
            )
    if finished is None:
        logger.warning(
            "native claim finish was a no-op: lease no longer owned platform=%s target_id=%s success=%s",
            platform,
            target_id,
            success,
        )
    return finished is not None
