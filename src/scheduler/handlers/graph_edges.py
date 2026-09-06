"""BuildGraphEdgesHandler — periodic social-graph edge synthesis.

Extracted from ``Scheduler._build_graph_edges`` in the LOGIC-005 refactor
(``docs/plans/scheduler-refactor.md`` step 11). Owns its own last-fire
timestamp; env-var reads happen inside ``should_run`` / ``run``.

Computes three edge types from messaging membership into ``graph_edges``:

  * WhatsApp **co-group**: pairs of users who both sent messages in the
    same WhatsApp group (weight = number of shared groups).
  * WhatsApp **DM**: users who sent direct messages to another user.
  * Telegram **co-group**: pairs of users observed in the same bounded
    Telegram group.

Very large groups are intentionally excluded via
``GRAPH_EDGES_MAX_GROUP_SENDERS`` / ``GRAPH_EDGES_MAX_TELEGRAM_GROUP_MEMBERS``
because they are weak OSINT evidence and create O(n²) edge explosions.

Cadence: ``GRAPH_EDGES_BUILD_INTERVAL_SECONDS`` (default 21600 = 6h, min
1800). The co-group upsert touches a large derived pair set and is not
needed minute-by-minute.

The three SQL statements are executed with a 180-second server-side
statement timeout so a long-running upsert cannot wedge the scheduler.

Fail-soft: any exception is logged and swallowed so a bad graph build never
disturbs the scheduling loop.
"""
from __future__ import annotations

import logging
import time as _time

from src.scheduler.handlers.base import SchedulerContext

logger = logging.getLogger(__name__)


class BuildGraphEdgesHandler:
    """Compute social-graph edges from messaging membership."""

    name = "build_graph_edges"

    def __init__(self) -> None:
        # 0.0 forces a build on the first should_run.
        self._last_run: float = 0.0
        self._interval_seconds: int | None = None

    async def should_run(self, ctx: SchedulerContext) -> bool:
        if self._interval_seconds is None:
            self._interval_seconds = ctx.get_env_int(
                "GRAPH_EDGES_BUILD_INTERVAL_SECONDS", 21600, min_value=1800,
            )
        return _time.monotonic() - self._last_run >= self._interval_seconds

    async def run(self, ctx: SchedulerContext) -> None:
        self._last_run = _time.monotonic()
        max_group_senders = ctx.get_env_int(
            "GRAPH_EDGES_MAX_GROUP_SENDERS", 80, min_value=2,
        )
        max_telegram_group_members = ctx.get_env_int(
            "GRAPH_EDGES_MAX_TELEGRAM_GROUP_MEMBERS", 40, min_value=2,
        )
        try:
            async with ctx.pool.acquire() as conn:
                # Co-group edges: distinct (chat, sender) pairs joined against
                # themselves. Very large groups are intentionally excluded: they
                # are weak OSINT evidence and create O(n²) edge explosions.
                inserted_cg = await conn.fetchval("""
                    WITH group_members AS (
                        SELECT
                            wm.chat_id,
                            wm.sender_id,
                            MIN(wm.timestamp) AS first_seen_at,
                            MAX(wm.timestamp) AS last_seen_at
                        FROM whatsapp_messages wm
                        JOIN whatsapp_chats wc ON wm.chat_id = wc.id
                        WHERE wm.sender_id IS NOT NULL AND wc.is_group = true
                        GROUP BY wm.chat_id, wm.sender_id
                    ),
                    eligible_groups AS (
                        SELECT chat_id
                        FROM group_members
                        GROUP BY chat_id
                        HAVING COUNT(*) BETWEEN 2 AND $1
                    ),
                    co_group AS (
                        SELECT
                            gm1.sender_id AS sender1,
                            gm2.sender_id AS sender2,
                            COUNT(DISTINCT gm1.chat_id) AS shared_groups,
                            MIN(LEAST(gm1.first_seen_at, gm2.first_seen_at)) AS first_seen_at,
                            MAX(GREATEST(gm1.last_seen_at, gm2.last_seen_at)) AS last_seen_at
                        FROM group_members gm1
                        JOIN group_members gm2
                            ON gm1.chat_id = gm2.chat_id
                            AND gm1.sender_id < gm2.sender_id
                        JOIN eligible_groups eg ON eg.chat_id = gm1.chat_id
                        GROUP BY gm1.sender_id, gm2.sender_id
                    ),
                    upserted AS (
                        INSERT INTO graph_edges
                            (source, source_user, target_user, edge_type, weight,
                             first_seen_at, last_seen_at)
                        SELECT
                            'whatsapp',
                            u1.platform_user_id,
                            u2.platform_user_id,
                            'co_group',
                            cg.shared_groups::integer,
                            COALESCE(cg.first_seen_at, NOW()),
                            COALESCE(cg.last_seen_at, NOW())
                        FROM co_group cg
                        JOIN whatsapp_users u1 ON cg.sender1 = u1.id
                        JOIN whatsapp_users u2 ON cg.sender2 = u2.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET
                            weight = EXCLUDED.weight,
                            last_seen_at = GREATEST(graph_edges.last_seen_at, EXCLUDED.last_seen_at)
                        WHERE graph_edges.weight IS DISTINCT FROM EXCLUDED.weight
                           OR graph_edges.last_seen_at < EXCLUDED.last_seen_at
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, max_group_senders, timeout=180)

                # DM edges: who sent messages in which 1:1 chat.
                inserted_dm = await conn.fetchval("""
                    WITH dm_senders AS (
                        SELECT DISTINCT
                            wm.sender_id,
                            wc.platform_chat_id AS target_jid
                        FROM whatsapp_messages wm
                        JOIN whatsapp_chats wc ON wm.chat_id = wc.id
                        WHERE wc.is_group = false
                          AND wm.sender_id IS NOT NULL
                          AND wc.platform_chat_id LIKE '%@s.whatsapp.net'
                    ),
                    upserted AS (
                        INSERT INTO graph_edges
                            (source, source_user, target_user, edge_type, weight,
                             first_seen_at, last_seen_at)
                        SELECT
                            'whatsapp',
                            u.platform_user_id,
                            ds.target_jid,
                            'dm',
                            1,
                            NOW(),
                            NOW()
                        FROM dm_senders ds
                        JOIN whatsapp_users u ON ds.sender_id = u.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET last_seen_at = NOW()
                        WHERE graph_edges.last_seen_at < NOW() - interval '1 hour'
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, timeout=180)

                inserted_tg_cg = await conn.fetchval("""
                    WITH group_members AS (
                        SELECT
                            tm.chat_id,
                            tm.user_id,
                            MIN(COALESCE(tm.joined_at, tm.last_seen_at, tm.refreshed_at, NOW())) AS first_seen_at,
                            MAX(COALESCE(tm.last_seen_at, tm.refreshed_at, tm.joined_at, NOW())) AS last_seen_at
                        FROM telegram_chat_members tm
                        JOIN telegram_chats tc ON tm.chat_id = tc.id
                        WHERE tc.type = 'group'
                          AND (tc.members_count IS NULL
                               OR tc.members_count = 0
                               OR tc.members_count <= $1)
                        GROUP BY tm.chat_id, tm.user_id
                    ),
                    eligible_groups AS (
                        SELECT chat_id
                        FROM group_members
                        GROUP BY chat_id
                        HAVING COUNT(*) BETWEEN 2 AND $1
                    ),
                    co_group AS (
                        SELECT
                            gm1.user_id AS sender1,
                            gm2.user_id AS sender2,
                            COUNT(DISTINCT gm1.chat_id) AS shared_groups,
                            MIN(LEAST(gm1.first_seen_at, gm2.first_seen_at)) AS first_seen_at,
                            MAX(GREATEST(gm1.last_seen_at, gm2.last_seen_at)) AS last_seen_at
                        FROM group_members gm1
                        JOIN group_members gm2
                            ON gm1.chat_id = gm2.chat_id
                            AND gm1.user_id < gm2.user_id
                        JOIN eligible_groups eg ON eg.chat_id = gm1.chat_id
                        GROUP BY gm1.user_id, gm2.user_id
                    ),
                    upserted AS (
                        INSERT INTO graph_edges
                            (source, source_user, target_user, edge_type, weight,
                             first_seen_at, last_seen_at)
                        SELECT
                            'telegram',
                            u1.platform_user_id,
                            u2.platform_user_id,
                            'co_group',
                            cg.shared_groups::integer,
                            COALESCE(cg.first_seen_at, NOW()),
                            COALESCE(cg.last_seen_at, NOW())
                        FROM co_group cg
                        JOIN telegram_users u1 ON cg.sender1 = u1.id
                        JOIN telegram_users u2 ON cg.sender2 = u2.id
                        ON CONFLICT (source, source_user, target_user, edge_type)
                        DO UPDATE SET
                            weight = EXCLUDED.weight,
                            last_seen_at = GREATEST(graph_edges.last_seen_at, EXCLUDED.last_seen_at)
                        WHERE graph_edges.weight IS DISTINCT FROM EXCLUDED.weight
                           OR graph_edges.last_seen_at < EXCLUDED.last_seen_at
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM upserted
                """, max_telegram_group_members, timeout=180)

            logger.info(
                "graph_edges build: whatsapp_co_group=%d whatsapp_dm=%d telegram_co_group=%d max_group_senders=%d max_telegram_group_members=%d",
                inserted_cg or 0,
                inserted_dm or 0,
                inserted_tg_cg or 0,
                max_group_senders,
                max_telegram_group_members,
            )
        except Exception:
            logger.warning("graph_edges build failed", exc_info=True)


__all__ = ["BuildGraphEdgesHandler"]
