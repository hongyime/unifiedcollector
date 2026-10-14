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
    """Seed bounded targets using the source's existing public input tables."""
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
