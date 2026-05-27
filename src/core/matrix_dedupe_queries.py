"""Cross-source dedupe / coverage queries for the Matrix collector.

Wave 1 Phase 3: pure read-only async helpers used by the dashboard (and
ad-hoc analyses) to ask "which native messages also have a Matrix twin?"
and "which Matrix events were never picked up by a native collector?".

DESIGN NOTES
------------
* No writes. Ever. These queries can be wired into any read-only viewer
  without auth concerns beyond the table grants.
* Robust to schema drift: the unified collector is in flux and the
  telegram_messages / whatsapp_messages tables may not exist on every
  deployment. Each helper wraps its SQL in try/except, logs a warning,
  and returns an empty result on failure so a missing native table can
  never take the dashboard down.
* Always parameterized ($1/$2 asyncpg style).
* Always LIMIT-bounded — no unbounded scans from a UI path.
* Heuristic twin matching: same sender + same body text + server_ts
  within ±window seconds of the native message timestamp. The Beeper
  bridge rewrites the sender MXID to include the platform user id, but
  body+timestamp alone is usually enough for a probabilistic match in a
  dashboard widget. Callers needing strict identity should write their
  own join.

Indexes used (already created in add_matrix_events_table.sql):
  idx_matrix_events_room_ts   (room_id, server_ts DESC)
  idx_matrix_events_sender    (sender)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Hard upper bound on rows any helper will return. Dashboards should pass
# their own smaller limit; this is the safety net.
_MAX_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    if limit is None or limit <= 0:
        return 1
    return min(int(limit), _MAX_LIMIT)


async def find_matrix_twin_telegram(
    pool: Any,
    telegram_chat_id: str,
    telegram_msg_id: str,
    time_window_seconds: int = 60,
    limit: int = 25,
) -> list[dict]:
    """Return matrix_events that look like the same message as the given
    telegram message.

    Args:
      telegram_chat_id: platform_chat_id (string) of the Telegram chat
      telegram_msg_id:  platform_message_id (string) of the message
      time_window_seconds: ± window around the telegram message timestamp

    Returns empty list if telegram_messages doesn't exist or the row is
    not found.
    """
    limit = _clamp_limit(limit)
    window = max(1, int(time_window_seconds))
    try:
        async with pool.acquire() as conn:
            tg_row = await conn.fetchrow(
                """
                SELECT m.text, m.platform_created_at
                  FROM telegram_messages m
                  JOIN telegram_chats c ON c.id = m.chat_id
                 WHERE c.platform_chat_id = $1
                   AND m.platform_message_id = $2
                 LIMIT 1
                """,
                telegram_chat_id,
                telegram_msg_id,
            )
            if not tg_row or not tg_row["platform_created_at"]:
                return []
            body = tg_row["text"] or ""
            ts = tg_row["platform_created_at"]
            # Match on same body + server_ts within window. Body may be
            # NULL on either side (media-only); we only return matches
            # where both have non-empty body to keep the heuristic tight.
            if not body:
                return []
            records = await conn.fetch(
                """
                SELECT event_id, room_id, sender, body, server_ts, msgtype
                  FROM matrix_events
                 WHERE body = $1
                   AND server_ts BETWEEN $2::timestamptz - ($3 || ' seconds')::interval
                                     AND $2::timestamptz + ($3 || ' seconds')::interval
                 ORDER BY server_ts ASC
                 LIMIT $4
                """,
                body,
                ts,
                str(window),
                limit,
            )
            return [dict(r) for r in records]
    except Exception as e:
        logger.warning("find_matrix_twin_telegram failed: %s", e)
        return []


async def find_matrix_twin_whatsapp(
    pool: Any,
    whatsapp_jid: str,
    whatsapp_msg_id: str,
    time_window_seconds: int = 60,
    limit: int = 25,
) -> list[dict]:
    """Return matrix_events that look like the same message as the given
    WhatsApp message.

    Args:
      whatsapp_jid: platform_chat_id (string) of the WhatsApp chat (jid)
      whatsapp_msg_id: platform_message_id (string)
      time_window_seconds: ± window around the WhatsApp timestamp

    Returns empty list if whatsapp_messages doesn't exist or row missing.
    """
    limit = _clamp_limit(limit)
    window = max(1, int(time_window_seconds))
    try:
        async with pool.acquire() as conn:
            wa_row = await conn.fetchrow(
                """
                SELECT m.text, m.timestamp
                  FROM whatsapp_messages m
                  JOIN whatsapp_chats c ON c.id = m.chat_id
                 WHERE c.platform_chat_id = $1
                   AND m.platform_message_id = $2
                 LIMIT 1
                """,
                whatsapp_jid,
                whatsapp_msg_id,
            )
            if not wa_row or not wa_row["timestamp"]:
                return []
            body = wa_row["text"] or ""
            ts = wa_row["timestamp"]
            if not body:
                return []
            records = await conn.fetch(
                """
                SELECT event_id, room_id, sender, body, server_ts, msgtype
                  FROM matrix_events
                 WHERE body = $1
                   AND server_ts BETWEEN $2::timestamptz - ($3 || ' seconds')::interval
                                     AND $2::timestamptz + ($3 || ' seconds')::interval
                 ORDER BY server_ts ASC
                 LIMIT $4
                """,
                body,
                ts,
                str(window),
                limit,
            )
            return [dict(r) for r in records]
    except Exception as e:
        logger.warning("find_matrix_twin_whatsapp failed: %s", e)
        return []


async def matrix_only_events(
    pool: Any,
    since_ts: Optional[Any] = None,
    limit: int = 100,
) -> list[dict]:
    """Return matrix_events that have NO twin in telegram_messages or
    whatsapp_messages (matched by identical body + ±60s window).

    Useful for surfacing rooms that are bridged via Beeper but where the
    native collector hasn't onboarded the chat yet.

    Robust: if either native table is missing the helper degrades to
    "compared only against the table that exists". If both are missing
    every matrix event is "matrix only" by definition.
    """
    limit = _clamp_limit(limit)
    try:
        async with pool.acquire() as conn:
            # Detect which native tables exist so we can adapt the NOT EXISTS
            # subqueries rather than blow up on a missing relation.
            has_tg = await _table_exists(conn, "telegram_messages")
            has_wa = await _table_exists(conn, "whatsapp_messages")

            where = ["e.body IS NOT NULL", "e.body <> ''"]
            params: list[Any] = []
            idx = 1
            if since_ts is not None:
                where.append(f"e.server_ts >= ${idx}")
                params.append(since_ts)
                idx += 1
            if has_tg:
                where.append(
                    "NOT EXISTS (SELECT 1 FROM telegram_messages tm "
                    "WHERE tm.text = e.body "
                    "AND tm.platform_created_at "
                    "BETWEEN e.server_ts - interval '60 seconds' "
                    "AND e.server_ts + interval '60 seconds')"
                )
            if has_wa:
                where.append(
                    "NOT EXISTS (SELECT 1 FROM whatsapp_messages wm "
                    "WHERE wm.text = e.body "
                    "AND wm.timestamp "
                    "BETWEEN e.server_ts - interval '60 seconds' "
                    "AND e.server_ts + interval '60 seconds')"
                )
            where_sql = " AND ".join(where)
            sql = (
                "SELECT e.event_id, e.room_id, e.sender, e.body, "
                "       e.server_ts, e.msgtype "
                "  FROM matrix_events e "
                f" WHERE {where_sql} "
                " ORDER BY e.server_ts DESC "
                f" LIMIT ${idx}"
            )
            params.append(limit)
            records = await conn.fetch(sql, *params)
            return [dict(r) for r in records]
    except Exception as e:
        logger.warning("matrix_only_events failed: %s", e)
        return []


async def coverage_overlap_summary(pool: Any) -> dict:
    """Return a summary of native vs Matrix coverage.

    Shape:
      {
        "telegram": {"total": n, "with_matrix_twin": m, "matrix_only": k},
        "whatsapp": {"total": n, "with_matrix_twin": m, "matrix_only": k},
      }

    Each side is independently robust: if one native table is missing
    its slot is `{"total": 0, "with_matrix_twin": 0, "matrix_only": 0,
    "available": False}`.
    """
    out: dict = {
        "telegram": {"total": 0, "with_matrix_twin": 0, "matrix_only": 0, "available": False},
        "whatsapp": {"total": 0, "with_matrix_twin": 0, "matrix_only": 0, "available": False},
    }
    try:
        async with pool.acquire() as conn:
            if await _table_exists(conn, "telegram_messages"):
                out["telegram"]["available"] = True
                try:
                    out["telegram"]["total"] = await conn.fetchval(
                        "SELECT COUNT(*) FROM telegram_messages"
                    ) or 0
                    out["telegram"]["with_matrix_twin"] = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM telegram_messages tm
                         WHERE tm.text IS NOT NULL AND tm.text <> ''
                           AND EXISTS (
                             SELECT 1 FROM matrix_events e
                              WHERE e.body = tm.text
                                AND e.server_ts BETWEEN tm.platform_created_at - interval '60 seconds'
                                                    AND tm.platform_created_at + interval '60 seconds'
                           )
                        """
                    ) or 0
                except Exception as inner:
                    logger.warning("telegram coverage query failed: %s", inner)

            if await _table_exists(conn, "whatsapp_messages"):
                out["whatsapp"]["available"] = True
                try:
                    out["whatsapp"]["total"] = await conn.fetchval(
                        "SELECT COUNT(*) FROM whatsapp_messages"
                    ) or 0
                    out["whatsapp"]["with_matrix_twin"] = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM whatsapp_messages wm
                         WHERE wm.text IS NOT NULL AND wm.text <> ''
                           AND EXISTS (
                             SELECT 1 FROM matrix_events e
                              WHERE e.body = wm.text
                                AND e.server_ts BETWEEN wm.timestamp - interval '60 seconds'
                                                    AND wm.timestamp + interval '60 seconds'
                           )
                        """
                    ) or 0
                except Exception as inner:
                    logger.warning("whatsapp coverage query failed: %s", inner)

            # matrix_only count = events with body that have no twin in
            # either native table that exists. Cheap because matrix_events
            # is bounded; bound the COUNT(*) implicitly via a subquery
            # LIMIT to keep dashboards snappy on huge deployments.
            try:
                conds = ["e.body IS NOT NULL", "e.body <> ''"]
                if out["telegram"]["available"]:
                    conds.append(
                        "NOT EXISTS (SELECT 1 FROM telegram_messages tm "
                        "WHERE tm.text = e.body "
                        "AND tm.platform_created_at BETWEEN e.server_ts - interval '60 seconds' "
                        "AND e.server_ts + interval '60 seconds')"
                    )
                if out["whatsapp"]["available"]:
                    conds.append(
                        "NOT EXISTS (SELECT 1 FROM whatsapp_messages wm "
                        "WHERE wm.text = e.body "
                        "AND wm.timestamp BETWEEN e.server_ts - interval '60 seconds' "
                        "AND e.server_ts + interval '60 seconds')"
                    )
                sql = (
                    "SELECT COUNT(*) FROM ("
                    "SELECT 1 FROM matrix_events e WHERE "
                    + " AND ".join(conds)
                    + " LIMIT 10000) sub"
                )
                matrix_only_count = await conn.fetchval(sql) or 0
                # Distribute matrix_only_count to the side that's
                # available — UI shows it once per platform; if both
                # native tables exist we show the same total under each
                # since we can't cheaply attribute orphaned matrix events
                # to a specific bridge.
                if out["telegram"]["available"]:
                    out["telegram"]["matrix_only"] = matrix_only_count
                if out["whatsapp"]["available"]:
                    out["whatsapp"]["matrix_only"] = matrix_only_count
            except Exception as inner:
                logger.warning("matrix_only count failed: %s", inner)

    except Exception as e:
        logger.warning("coverage_overlap_summary failed: %s", e)
    return out


async def _table_exists(conn: Any, table_name: str) -> bool:
    """Return True if `table_name` exists in the public schema."""
    try:
        return bool(
            await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = $1)",
                table_name,
            )
        )
    except Exception as e:
        logger.warning("_table_exists(%s) failed: %s", table_name, e)
        return False
