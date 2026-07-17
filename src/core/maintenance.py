"""Small collector-owned data repairs that must not run from the analyzer."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


async def _exec_count(conn, sql: str) -> int:
    result = await conn.execute(sql)
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0


async def repair_instagram_post_profile_ids(conn) -> dict[str, int]:
    """Attach recoverable instagram_posts rows to their profile rows.

    platform_post_id stores `<media_id>_<author_uid>`. Creating a minimal
    profile stub is collector-owned because instagram_posts.profile_id is a
    collector FK and analyzer consumers must treat this database as read-only.
    """
    inserted = await _exec_count(
        conn,
        """
        WITH missing_uids AS (
            SELECT DISTINCT split_part(p.platform_post_id, '_'::text, 2) AS uid
            FROM instagram_posts p
            LEFT JOIN instagram_profiles ip
              ON ip.platform_user_id = split_part(p.platform_post_id, '_'::text, 2)
            WHERE p.profile_id IS NULL
              AND split_part(p.platform_post_id, '_'::text, 2) ~ '^[0-9]+$'
              AND ip.id IS NULL
        )
        INSERT INTO instagram_profiles (platform_user_id)
        SELECT uid FROM missing_uids
        ON CONFLICT (platform_user_id) DO NOTHING
        """,
    )
    updated = await _exec_count(
        conn,
        """
        WITH candidates AS (
            SELECT p.id AS post_id, ip.id AS profile_id
            FROM instagram_posts p
            JOIN instagram_profiles ip
              ON ip.platform_user_id = split_part(p.platform_post_id, '_'::text, 2)
            WHERE p.profile_id IS NULL
              AND split_part(p.platform_post_id, '_'::text, 2) ~ '^[0-9]+$'
        )
        UPDATE instagram_posts p
        SET profile_id = candidates.profile_id
        FROM candidates
        WHERE p.id = candidates.post_id
          AND p.profile_id IS NULL
        """,
    )
    return {"instagram_profile_stubs": inserted, "instagram_post_profile_ids": updated}


async def repair_strava_activity_athlete_ids(conn) -> dict[str, int]:
    """Attach Strava activities whose athlete id is recoverable from metadata."""
    inserted = await _exec_count(
        conn,
        """
        WITH raw AS (
            SELECT DISTINCT COALESCE(
                NULLIF(metadata #>> '{athlete,id}', ''),
                NULLIF(metadata->>'athlete_id', ''),
                NULLIF(metadata->>'owner_id', '')
            ) AS athlete_platform_id
            FROM strava_activities
            WHERE athlete_id IS NULL
        ), candidates AS (
            SELECT athlete_platform_id
            FROM raw
            WHERE athlete_platform_id ~ '^[0-9]+$'
        )
        INSERT INTO strava_athletes (platform_athlete_id, updated_at)
        SELECT athlete_platform_id::bigint, NOW()
        FROM candidates
        ON CONFLICT (platform_athlete_id) DO NOTHING
        """,
    )
    updated = await _exec_count(
        conn,
        """
        WITH raw AS (
            SELECT id AS activity_id,
                   COALESCE(
                       NULLIF(metadata #>> '{athlete,id}', ''),
                       NULLIF(metadata->>'athlete_id', ''),
                       NULLIF(metadata->>'owner_id', '')
                   ) AS athlete_platform_id
            FROM strava_activities
            WHERE athlete_id IS NULL
        ), candidates AS (
            SELECT activity_id, athlete_platform_id
            FROM raw
            WHERE athlete_platform_id ~ '^[0-9]+$'
        )
        UPDATE strava_activities act
        SET athlete_id = ath.id
        FROM candidates c
        JOIN strava_athletes ath
          ON ath.platform_athlete_id = c.athlete_platform_id::bigint
        WHERE act.id = c.activity_id
          AND act.athlete_id IS NULL
        """,
    )
    return {"strava_athlete_stubs": inserted, "strava_activity_athlete_ids": updated}


async def backfill_telegram_bot_flags(conn) -> dict[str, int]:
    updated = await _exec_count(
        conn,
        """
        UPDATE telegram_users
        SET is_bot = TRUE,
            updated_at = NOW()
        WHERE COALESCE(is_bot, FALSE) IS FALSE
          AND lower(COALESCE(username, '')) LIKE '%bot'
        """,
    )
    return {"telegram_bot_usernames": updated}


async def run_collector_maintenance(pool) -> dict[str, int]:
    """Run cheap idempotent repairs. Fail-soft so startup never wedges."""
    if os.getenv("COLLECTOR_MAINTENANCE_ON_STARTUP", "true").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return {"skipped": 1}

    stats: dict[str, int] = {}
    async with pool.acquire() as conn:
        try:
            await conn.execute("SET lock_timeout = '2s'")
        except Exception:
            logger.debug("collector maintenance could not set lock_timeout", exc_info=True)
        for name, fn in (
            ("instagram_post_profile_ids", repair_instagram_post_profile_ids),
            ("strava_activity_athlete_ids", repair_strava_activity_athlete_ids),
            ("telegram_bot_flags", backfill_telegram_bot_flags),
        ):
            try:
                stats.update(await fn(conn))
            except Exception as exc:  # noqa: BLE001 - maintenance is best effort
                logger.warning("collector maintenance %s failed: %s", name, exc)
                stats[f"{name}_failed"] = 1
        try:
            await conn.execute("RESET lock_timeout")
        except Exception:
            pass

    changed = {k: v for k, v in stats.items() if v}
    if changed:
        logger.info("collector maintenance complete: %s", changed)
    return stats
