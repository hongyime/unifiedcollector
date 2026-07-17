"""Collector-side cache of analyzer account proximity tiers.

Collectors run against the unifiedcollector database, while account_proximity is
owned by unifiedanalyzer. Postgres cannot join across databases, so queue code
uses this small local cache to order T1/T2 work ahead of discovery.
"""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.parse import urlsplit, urlunsplit

import asyncpg

logger = logging.getLogger(__name__)

_LAST_REFRESH = 0.0
_REFRESH_LOCK = None
_DDL_READY = False

DDL = """
CREATE TABLE IF NOT EXISTS account_proximity_cache (
    platform VARCHAR(30) NOT NULL,
    account_id VARCHAR(255) NOT NULL,
    owner_account VARCHAR(255) NOT NULL,
    tier SMALLINT NOT NULL CHECK (tier BETWEEN 1 AND 4),
    reasons JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, account_id, owner_account)
);
CREATE INDEX IF NOT EXISTS idx_account_proximity_cache_lookup
    ON account_proximity_cache(platform, account_id, tier);
CREATE INDEX IF NOT EXISTS idx_account_proximity_cache_tier
    ON account_proximity_cache(tier);
"""


def analyzer_database_url() -> str | None:
    explicit = os.getenv("ANALYZER_DATABASE_URL")
    if explicit:
        return explicit
    collector = os.getenv("DATABASE_URL")
    if not collector:
        return None
    parts = urlsplit(collector)
    return urlunsplit((parts.scheme, parts.netloc, "/unifiedanalyzer", "", ""))


async def _ensure_lock():
    global _REFRESH_LOCK
    if _REFRESH_LOCK is None:
        import asyncio

        _REFRESH_LOCK = asyncio.Lock()
    return _REFRESH_LOCK


async def ensure_account_proximity_cache(pool) -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('public.account_proximity_cache') IS NOT NULL"
        )
        if exists:
            _DDL_READY = True
            return
        await conn.execute(DDL)
    _DDL_READY = True


async def refresh_account_proximity_cache(pool, *, force: bool = False) -> dict:
    """Best-effort sync from analyzer.account_proximity into collector cache."""
    if os.getenv("PROXIMITY_CACHE_ENABLED", "true").lower() != "true":
        return {"skipped": "disabled"}

    interval = int(os.getenv("PROXIMITY_CACHE_REFRESH_SECONDS", "900"))
    now = time.monotonic()
    global _LAST_REFRESH
    if not force and _LAST_REFRESH and now - _LAST_REFRESH < interval:
        return {"skipped": "fresh"}

    lock = await _ensure_lock()
    async with lock:
        now = time.monotonic()
        if not force and _LAST_REFRESH and now - _LAST_REFRESH < interval:
            return {"skipped": "fresh"}

        await ensure_account_proximity_cache(pool)
        dsn = analyzer_database_url()
        if not dsn:
            return {"skipped": "no_analyzer_dsn"}

        try:
            analyzer_conn = await asyncpg.connect(dsn, command_timeout=120)
            try:
                rows = await analyzer_conn.fetch("""
                    SELECT platform, account_id, owner_account, tier, reasons, updated_at
                    FROM account_proximity
                """)
            finally:
                await analyzer_conn.close()
        except Exception as exc:
            logger.debug("proximity cache refresh: analyzer fetch failed: %s", exc, exc_info=True)
            return {"error": str(exc)[:300]}

        records = [
            (
                r["platform"],
                r["account_id"],
                r["owner_account"],
                r["tier"],
                json.dumps(r["reasons"] or [], default=str),
                r["updated_at"],
            )
            for r in rows
        ]
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("""
                        CREATE TEMP TABLE tmp_account_proximity_cache (
                            platform VARCHAR(30) NOT NULL,
                            account_id VARCHAR(255) NOT NULL,
                            owner_account VARCHAR(255) NOT NULL,
                            tier SMALLINT NOT NULL,
                            reasons JSONB NOT NULL,
                            updated_at TIMESTAMPTZ
                        ) ON COMMIT DROP
                    """)
                    if records:
                        await conn.copy_records_to_table(
                            "tmp_account_proximity_cache",
                            records=records,
                            columns=[
                                "platform",
                                "account_id",
                                "owner_account",
                                "tier",
                                "reasons",
                                "updated_at",
                            ],
                        )
                    await conn.execute("DELETE FROM account_proximity_cache")
                    if records:
                        await conn.execute(
                            """
                            INSERT INTO account_proximity_cache
                                (platform, account_id, owner_account, tier, reasons, updated_at, synced_at)
                            SELECT platform, account_id, owner_account, tier, reasons, updated_at, NOW()
                            FROM tmp_account_proximity_cache
                            ON CONFLICT (platform, account_id, owner_account) DO UPDATE SET
                                tier = EXCLUDED.tier,
                                reasons = EXCLUDED.reasons,
                                updated_at = EXCLUDED.updated_at,
                                synced_at = NOW()
                            """
                        )
                    await _enrich_cache_aliases(conn)
        except Exception as exc:
            logger.debug("proximity cache refresh: collector write failed: %s", exc, exc_info=True)
            return {"error": str(exc)[:300]}

        _LAST_REFRESH = time.monotonic()
        seeded = await seed_proximity_backfill_targets(pool)
        logger.info("proximity cache refreshed: %d rows, seeded=%s", len(records), seeded)
        return {"rows": len(records), "seeded": seeded}


async def _exec_count(conn, sql: str) -> int:
    result = await conn.execute(sql)
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0


async def _enrich_cache_aliases(conn) -> None:
    """Add local username aliases for queues that store handles, not numeric ids."""
    alias_sql = [
        """
        INSERT INTO account_proximity_cache (platform, account_id, owner_account, tier, reasons, updated_at, synced_at)
        SELECT ap.platform, lower(gu.login), ap.owner_account, ap.tier, ap.reasons, ap.updated_at, NOW()
        FROM account_proximity_cache ap
        JOIN github_users gu ON gu.platform_user_id::text = ap.account_id
        WHERE ap.platform = 'github' AND NULLIF(gu.login, '') IS NOT NULL
        ON CONFLICT (platform, account_id, owner_account) DO UPDATE SET
            tier = LEAST(account_proximity_cache.tier, EXCLUDED.tier),
            reasons = EXCLUDED.reasons,
            updated_at = EXCLUDED.updated_at,
            synced_at = NOW()
        """,
        """
        INSERT INTO account_proximity_cache (platform, account_id, owner_account, tier, reasons, updated_at, synced_at)
        SELECT ap.platform, lower(p.username), ap.owner_account, ap.tier, ap.reasons, ap.updated_at, NOW()
        FROM account_proximity_cache ap
        JOIN instagram_profiles p ON p.platform_user_id::text = ap.account_id
        WHERE ap.platform = 'instagram' AND NULLIF(p.username, '') IS NOT NULL
        ON CONFLICT (platform, account_id, owner_account) DO UPDATE SET
            tier = LEAST(account_proximity_cache.tier, EXCLUDED.tier),
            reasons = EXCLUDED.reasons,
            updated_at = EXCLUDED.updated_at,
            synced_at = NOW()
        """,
        """
        INSERT INTO account_proximity_cache (platform, account_id, owner_account, tier, reasons, updated_at, synced_at)
        SELECT ap.platform, lower(p.username), ap.owner_account, ap.tier, ap.reasons, ap.updated_at, NOW()
        FROM account_proximity_cache ap
        JOIN tiktok_profiles p ON p.platform_user_id::text = ap.account_id
        WHERE ap.platform = 'tiktok' AND NULLIF(p.username, '') IS NOT NULL
        ON CONFLICT (platform, account_id, owner_account) DO UPDATE SET
            tier = LEAST(account_proximity_cache.tier, EXCLUDED.tier),
            reasons = EXCLUDED.reasons,
            updated_at = EXCLUDED.updated_at,
            synced_at = NOW()
        """,
    ]
    for sql in alias_sql:
        await conn.execute(sql)


async def seed_proximity_backfill_targets(pool) -> dict[str, int]:
    """Promote T1/T2 accounts into concrete platform backfill queues."""
    out: dict[str, int] = {}
    try:
        async with pool.acquire() as conn:
            out["github"] = await _exec_count(conn, """
                WITH prox AS (
                    SELECT account_id AS login, MIN(tier) AS tier
                    FROM account_proximity_cache
                    WHERE platform = 'github'
                      AND tier <= 2
                      AND account_id !~ '^[0-9]+$'
                    GROUP BY account_id
                )
                INSERT INTO github_spider_queue (target_type, target_identifier, source, priority, status)
                SELECT 'user', login, 'proximity_t' || tier::text, tier, 'pending'
                FROM prox
                WHERE login IS NOT NULL AND login <> ''
                ON CONFLICT (target_type, target_identifier) DO UPDATE SET
                    priority = LEAST(github_spider_queue.priority, EXCLUDED.priority),
                    source = EXCLUDED.source,
                    status = CASE
                        WHEN github_spider_queue.status IN ('done', 'processing') THEN github_spider_queue.status
                        ELSE 'pending'
                    END
            """)

            out["strava"] = await _exec_count(conn, """
                WITH prox AS (
                    SELECT account_id, MIN(tier) AS tier
                    FROM account_proximity_cache
                    WHERE platform = 'strava' AND tier <= 2 AND account_id ~ '^[0-9]+$'
                    GROUP BY account_id
                )
                INSERT INTO strava_spider_queue (platform_athlete_id, source, priority, status)
                SELECT account_id::bigint, 'proximity_t' || tier::text, tier, 'pending'
                FROM prox
                ON CONFLICT (platform_athlete_id) DO UPDATE SET
                    priority = LEAST(strava_spider_queue.priority, EXCLUDED.priority),
                    source = EXCLUDED.source,
                    status = CASE
                        WHEN strava_spider_queue.status IN ('completed', 'processing') THEN strava_spider_queue.status
                        ELSE 'pending'
                    END
            """)

            out["instagram_queue"] = await _exec_count(conn, """
                WITH prox AS (
                    SELECT account_id, MIN(tier) AS tier
                    FROM account_proximity_cache
                    WHERE platform = 'instagram' AND tier <= 2
                    GROUP BY account_id
                ), resolved AS (
                    SELECT
                           COALESCE(NULLIF(p.platform_user_id, ''), prox.account_id) AS platform_user_id,
                           COALESCE(NULLIF(p.username, ''),
                                    CASE WHEN prox.account_id !~ '^[0-9]+$' THEN prox.account_id ELSE NULL END) AS username,
                           prox.tier
                    FROM prox
                    LEFT JOIN instagram_profiles p
                      ON p.platform_user_id::text = prox.account_id
                    UNION ALL
                    SELECT p.platform_user_id, p.username, prox.tier
                    FROM prox
                    JOIN instagram_profiles p
                      ON lower(p.username) = lower(prox.account_id)
                    WHERE prox.account_id !~ '^[0-9]+$'
                ), users AS (
                    SELECT DISTINCT
                           platform_user_id, username, MIN(tier) AS tier
                    FROM resolved
                    GROUP BY platform_user_id, username
                )
                INSERT INTO instagram_spider_queue (platform_user_id, username, source, priority, status)
                SELECT platform_user_id, username, 'proximity_t' || tier::text, tier, 'pending'
                FROM users
                WHERE platform_user_id IS NOT NULL AND platform_user_id <> ''
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, instagram_spider_queue.username),
                    priority = LEAST(instagram_spider_queue.priority, EXCLUDED.priority),
                    source = EXCLUDED.source,
                    status = CASE
                        WHEN instagram_spider_queue.status IN ('completed', 'processing') THEN instagram_spider_queue.status
                        ELSE 'pending'
                    END
            """)

            out["instagram_targets"] = await _exec_count(conn, """
                WITH prox AS (
                    SELECT account_id, MIN(tier) AS tier
                    FROM account_proximity_cache
                    WHERE platform = 'instagram' AND tier <= 2
                    GROUP BY account_id
                ), resolved AS (
                    SELECT
                           COALESCE(NULLIF(p.username, ''),
                                    CASE WHEN prox.account_id !~ '^[0-9]+$' THEN prox.account_id ELSE NULL END) AS username,
                           prox.tier
                    FROM prox
                    LEFT JOIN instagram_profiles p
                      ON p.platform_user_id::text = prox.account_id
                    UNION ALL
                    SELECT p.username, prox.tier
                    FROM prox
                    JOIN instagram_profiles p
                      ON lower(p.username) = lower(prox.account_id)
                    WHERE prox.account_id !~ '^[0-9]+$'
                ), users AS (
                    SELECT DISTINCT
                           username, MIN(tier) AS tier
                    FROM resolved
                    GROUP BY username
                )
                INSERT INTO instagram_spider_targets (username, hop, discovered_from, status)
                SELECT username, 0, 'proximity_t' || tier::text, 'active'
                FROM users
                WHERE username IS NOT NULL AND username <> ''
                ON CONFLICT (username) DO UPDATE SET
                    hop = LEAST(instagram_spider_targets.hop, EXCLUDED.hop),
                    discovered_from = EXCLUDED.discovered_from,
                    status = 'active'
            """)

            out["tiktok"] = await _exec_count(conn, """
                WITH prox AS (
                    SELECT account_id, MIN(tier) AS tier
                    FROM account_proximity_cache
                    WHERE platform = 'tiktok' AND tier <= 2
                    GROUP BY account_id
                ), users AS (
                    SELECT DISTINCT
                           COALESCE(NULLIF(p.platform_user_id, ''), NULLIF(su.platform_user_id, ''), prox.account_id) AS platform_user_id,
                           COALESCE(NULLIF(p.username, ''), NULLIF(su.username, ''),
                                    CASE WHEN prox.account_id !~ '^[0-9]+$' THEN prox.account_id ELSE NULL END) AS username,
                           MIN(prox.tier) AS tier
                    FROM prox
                    LEFT JOIN tiktok_profiles p
                      ON p.platform_user_id::text = prox.account_id
                      OR lower(p.username) = lower(prox.account_id)
                    LEFT JOIN social_users su
                      ON su.platform = 'tiktok'
                     AND (
                            su.platform_user_id = prox.account_id
                         OR lower(su.username) = lower(prox.account_id)
                     )
                    GROUP BY COALESCE(NULLIF(p.platform_user_id, ''), NULLIF(su.platform_user_id, ''), prox.account_id),
                             COALESCE(NULLIF(p.username, ''), NULLIF(su.username, ''),
                                      CASE WHEN prox.account_id !~ '^[0-9]+$' THEN prox.account_id ELSE NULL END)
                )
                INSERT INTO tiktok_spider_queue (platform_user_id, username, source, priority, status)
                SELECT platform_user_id, username, 'proximity_t' || tier::text, tier, 'pending'
                FROM users
                WHERE platform_user_id IS NOT NULL AND platform_user_id <> ''
                ON CONFLICT (platform_user_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, tiktok_spider_queue.username),
                    priority = LEAST(tiktok_spider_queue.priority, EXCLUDED.priority),
                    source = EXCLUDED.source,
                    status = CASE
                        WHEN tiktok_spider_queue.status IN ('completed', 'processing') THEN tiktok_spider_queue.status
                        ELSE 'pending'
                    END
            """)

            out["telegram_chats"] = await _exec_count(conn, """
                WITH prox AS (
                    SELECT account_id, MIN(tier) AS tier
                    FROM account_proximity_cache
                    WHERE platform = 'telegram' AND tier <= 2
                    GROUP BY account_id
                ), chats AS (
                    SELECT DISTINCT c.platform_chat_id, c.title, MIN(prox.tier) AS tier
                    FROM prox
                    JOIN telegram_users u ON u.platform_user_id = prox.account_id
                    JOIN telegram_chat_members m ON m.user_id = u.id
                    JOIN telegram_chats c ON c.id = m.chat_id
                    WHERE c.platform_chat_id IS NOT NULL
                    GROUP BY c.platform_chat_id, c.title
                )
                INSERT INTO telegram_spider_queue (platform_chat_id, title, source, priority, status)
                SELECT platform_chat_id, title, 'proximity_group_t' || tier::text, tier, 'pending'
                FROM chats
                ON CONFLICT (platform_chat_id) DO UPDATE SET
                    title = COALESCE(EXCLUDED.title, telegram_spider_queue.title),
                    priority = LEAST(telegram_spider_queue.priority, EXCLUDED.priority),
                    source = EXCLUDED.source,
                    status = CASE
                        WHEN telegram_spider_queue.status IN ('completed', 'processing') THEN telegram_spider_queue.status
                        ELSE 'pending'
                    END
            """)
    except Exception as exc:
        logger.debug("proximity target seed failed: %s", exc, exc_info=True)
        out.setdefault("error", 1)
    return out
