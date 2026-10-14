"""Bounded feeder for the five queue-driven profile-only probes.

Candidates come from two existing sources:

* same-handle candidates derived from ``social_users`` (the collected
  profile/post identity index), fanned out to the five profile-only sites;
* direct profile URLs captured by the spider in ``discovered_links``, routed
  only to the matching site's probe.

The queue's unique ``(source, username)`` constraint makes repeated passes
idempotent. Each pass has a per-source cap and a long cadence; the probe
workers apply their own per-request QPS limits while consuming the queue.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)

_SOURCES = ("snapchat", "paypal", "airbnb", "bluesky", "pinterest")

_FEED_SQL = r"""
WITH link_rules(source, domain_pattern, path_pattern) AS (
    VALUES
        ('snapchat',  '(^|\.)snapchat\.com$', 'snapchat\.com/add/([a-zA-Z0-9._-]{2,80})'),
        ('paypal',    '(^|\.)paypal\.me$',    'paypal\.me/([a-zA-Z0-9._-]{2,80})'),
        ('airbnb',    '(^|\.)airbnb\.[a-z.]+$', 'airbnb\.[a-z.]+/users/show/([0-9]{2,20})'),
        ('bluesky',   '(^|\.)bsky\.app$',     'bsky\.app/profile/((did:plc:[a-zA-Z0-9]{10,}|[a-zA-Z0-9][a-zA-Z0-9.-]{0,251}[a-zA-Z0-9]))'),
        ('pinterest', '(^|\.)pinterest\.[a-z.]+$', 'pinterest\.[a-z.]+/([a-zA-Z0-9._-]{2,80})')
),
direct_candidates AS (
    SELECT
        rules.source,
        lower((regexp_match(dl.url, rules.path_pattern, 'i'))[1]) AS username,
        'spider_discovered_links'::text AS enqueued_by,
        dl.discovered_at AS seen_at,
        0 AS evidence_rank
    FROM discovered_links AS dl
    JOIN link_rules AS rules ON lower(dl.domain) ~ rules.domain_pattern
    WHERE dl.url ~* rules.path_pattern
),
social_handles AS (
    SELECT
        lower(btrim(su.username, '@')) AS username,
        (array_agg(
            su.platform
            ORDER BY CASE su.platform
                WHEN 'instagram' THEN 0
                WHEN 'tiktok' THEN 1
                WHEN 'threads' THEN 2
                WHEN 'x' THEN 3
                WHEN 'facebook' THEN 4
                WHEN 'lemon8' THEN 5
                WHEN 'youtube' THEN 6
                WHEN 'github' THEN 7
                WHEN 'strava' THEN 8
                WHEN 'telegram' THEN 9
                ELSE 10
            END,
            COALESCE(su.last_seen, su.first_seen) DESC NULLS LAST,
            su.platform
        ))[1] AS origin_platform,
        min(CASE su.platform
            WHEN 'instagram' THEN 0
            WHEN 'tiktok' THEN 1
            WHEN 'threads' THEN 2
            WHEN 'x' THEN 3
            WHEN 'facebook' THEN 4
            WHEN 'lemon8' THEN 5
            WHEN 'youtube' THEN 6
            WHEN 'github' THEN 7
            WHEN 'strava' THEN 8
            WHEN 'telegram' THEN 9
            ELSE 10
        END) AS platform_priority,
        max(COALESCE(su.last_seen, su.first_seen)) AS seen_at
    FROM social_users AS su
    LEFT JOIN profile_probe_queue AS existing
      ON existing.username = lower(btrim(su.username, '@'))
     AND existing.source IN ('snapchat', 'paypal', 'pinterest')
    WHERE su.username IS NOT NULL
      AND su.platform NOT IN ('snapchat', 'paypal', 'airbnb', 'bluesky', 'pinterest')
      AND lower(btrim(su.username, '@')) ~ '^[a-z0-9][a-z0-9._-]{1,79}$'
    GROUP BY lower(btrim(su.username, '@'))
    HAVING count(DISTINCT existing.source) < 3
    ORDER BY platform_priority,
             max(COALESCE(su.last_seen, su.first_seen)) DESC NULLS LAST,
             lower(btrim(su.username, '@'))
    LIMIT $1
),
social_candidates AS (
    SELECT
        target.source,
        handles.username,
        ('cross_platform:' || handles.origin_platform)::text AS enqueued_by,
        handles.seen_at,
        1 AS evidence_rank
    FROM social_handles AS handles
    CROSS JOIN (VALUES ('snapchat'), ('paypal'), ('pinterest'))
        AS target(source)
),
raw_candidates AS (
    SELECT source, username, enqueued_by, seen_at, evidence_rank
    FROM direct_candidates
    UNION ALL
    SELECT source, username, enqueued_by, seen_at, evidence_rank
    FROM social_candidates
),
eligible AS (
    SELECT source, username, enqueued_by, seen_at, evidence_rank
    FROM raw_candidates
    WHERE (
          (source = 'bluesky' AND username ~ '^(did:plc:[a-z0-9]{10,}|[a-z0-9][a-z0-9.-]{0,251}[a-z0-9])$')
          OR (source <> 'bluesky' AND username ~ '^[a-z0-9][a-z0-9._-]{1,79}$')
      )
      AND NOT (source = 'pinterest' AND username IN
          ('pin', 'search', 'explore', 'ideas', 'business', 'today', 'login', 'settings', 'help'))
      AND NOT EXISTS (
          SELECT 1
          FROM profile_probe_queue AS existing
          WHERE existing.source = raw_candidates.source
            AND lower(btrim(existing.username, '@')) = raw_candidates.username
      )
),
deduplicated AS (
    SELECT DISTINCT ON (source, username)
        source, username, enqueued_by, seen_at, evidence_rank
    FROM eligible
    ORDER BY source, username, evidence_rank, seen_at DESC NULLS LAST, enqueued_by
),
ranked AS (
    SELECT
        source, username, enqueued_by,
        row_number() OVER (
            PARTITION BY source
            ORDER BY evidence_rank, seen_at DESC NULLS LAST, username
        ) AS source_rank
    FROM deduplicated
),
inserted AS (
    INSERT INTO profile_probe_queue (source, username, enqueued_by)
    SELECT source, username, enqueued_by
    FROM ranked
    WHERE source_rank <= $1
    ON CONFLICT (source, username) DO NOTHING
    RETURNING source
)
SELECT source, count(*)::int AS queued
FROM inserted
GROUP BY source
"""


class ProfileProbeFeederHandler:
    """Periodically seed the profile-only probe queue at a bounded rate."""

    name = "profile_probe_feeder"

    def __init__(self) -> None:
        self._last_run = 0.0
        self._interval_seconds: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if ctx.get_env_int("PROFILE_PROBE_FEEDER_ENABLED", 1, min_value=0) <= 0:
            return False
        if self._interval_seconds is None:
            self._interval_seconds = ctx.get_env_int(
                "PROFILE_PROBE_FEEDER_INTERVAL_SECONDS", 900, min_value=60,
            )
        return _time.monotonic() - self._last_run >= self._interval_seconds

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        batch_per_source = min(
            500,
            ctx.get_env_int("PROFILE_PROBE_FEEDER_BATCH_PER_SOURCE", 100, min_value=1),
        )
        try:
            async with ctx.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '60s'")
                    rows = await conn.fetch(_FEED_SQL, batch_per_source)
            queued = {row["source"]: row["queued"] for row in rows}
            logger.info(
                "profile_probe_feeder: queued=%s batch_per_source=%d",
                {source: queued.get(source, 0) for source in _SOURCES},
                batch_per_source,
            )
        except Exception:
            logger.warning("profile_probe_feeder failed", exc_info=True)


__all__ = ["ProfileProbeFeederHandler"]
