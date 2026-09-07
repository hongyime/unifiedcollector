"""DM (direct messages) routes for Instagram and TikTok, plus DM probe telemetry.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 16
(cluster 5). Covers:

* ``/instagram/dms/threads``
* ``/instagram/dms/thread/{thread_id}``
* ``/tiktok/dms/threads``
* ``/tiktok/dms/thread/{thread_id}``
* ``/dm/telemetry``
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


@router.get("/instagram/dms/threads")
async def list_ig_dm_threads(owner: str | None = None, limit: int = 100,
                             _user: dict = Depends(require_role("viewer"))):
    """DM threads with a message count and last-activity, newest first.

    instagram_dm_thread may be empty until the extension observes IG DMs in a
    logged-in tab; return [] cleanly if the table doesn't exist yet.
    """
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('instagram_dm_thread')") is None:
            return []
        params: list = []
        where = ""
        if owner:
            where = "WHERE t.owner_account = $1"
            params.append(owner)
        rows = await conn.fetch(
            f"""
            SELECT t.thread_id, t.title, t.participants, t.owner_account,
                   t.last_activity,
                   COALESCE(m.cnt, 0)   AS message_count,
                   m.last_ts            AS last_message_ts
            FROM instagram_dm_thread t
            LEFT JOIN (
                SELECT thread_id, count(*) AS cnt, max("timestamp") AS last_ts
                FROM instagram_dm GROUP BY thread_id
            ) m ON m.thread_id = t.thread_id
            {where}
            ORDER BY COALESCE(t.last_activity, m.last_ts) DESC NULLS LAST
            LIMIT ${len(params) + 1}
            """,
            *params, limit,
        )
    return [dict(r) for r in rows]


@router.get("/instagram/dms/thread/{thread_id}")
async def ig_dm_thread_messages(thread_id: str, limit: int = 200,
                                _user: dict = Depends(require_role("viewer"))):
    """Messages for one IG DM thread, chronological (oldest first) for display."""
    limit = max(1, min(limit, 2000))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('instagram_dm')") is None:
            return {"thread": None, "messages": []}
        thread = await conn.fetchrow(
            "SELECT * FROM instagram_dm_thread WHERE thread_id = $1", thread_id,
        )
        rows = await conn.fetch(
            'SELECT message_id, sender_id, sender_username, text, item_type, '
            '"timestamp", is_from_me, owner_account '
            'FROM instagram_dm WHERE thread_id = $1 '
            'ORDER BY "timestamp" ASC NULLS LAST LIMIT $2',
            thread_id, limit,
        )
    return {
        "thread": dict(thread) if thread else None,
        "messages": [dict(r) for r in rows],
    }


@router.get("/tiktok/dms/threads")
async def list_tt_dm_threads(owner: str | None = None, limit: int = 100,
                             _user: dict = Depends(require_role("viewer"))):
    """TikTok DM threads with per-thread message count + last-activity, newest
    first. Mirrors /instagram/dms/threads. tiktok_dm{,_thread} are populated
    by the extension's client-side decoder POSTing to /social/dm-decoded (see
    src/db/migrations/add_tiktok_dm.sql for the field-number derivation).

    Returns [] cleanly if the table doesn't exist yet — the migration ships
    together with the code that writes to it, but a partial-boot / lock-
    deferred migration is a real state the dashboard shouldn't 500 on.
    """
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_dm_thread')") is None:
            return []
        params: list = []
        where = ""
        if owner:
            where = "WHERE t.owner_account = $1"
            params.append(owner)
        rows = await conn.fetch(
            f"""
            SELECT t.conversation_id      AS thread_id,
                   t.conversation_type,
                   t.participants,
                   t.owner_account,
                   t.last_activity,
                   COALESCE(m.cnt, 0)     AS message_count,
                   m.last_ts              AS last_message_ts
            FROM tiktok_dm_thread t
            LEFT JOIN (
                SELECT conversation_id, count(*) AS cnt, max("timestamp") AS last_ts
                FROM tiktok_dm GROUP BY conversation_id
            ) m ON m.conversation_id = t.conversation_id
            {where}
            ORDER BY COALESCE(t.last_activity, m.last_ts) DESC NULLS LAST
            LIMIT ${len(params) + 1}
            """,
            *params, limit,
        )
    return [dict(r) for r in rows]


@router.get("/tiktok/dms/thread/{thread_id}")
async def tt_dm_thread_messages(thread_id: str, limit: int = 200,
                                _user: dict = Depends(require_role("viewer"))):
    """Messages for one TikTok DM thread, chronological (oldest first) for
    display. Returns awe_type / message_type in the JSON so a caller can
    tell text-message rows apart from other content kinds without querying
    raw_content."""
    limit = max(1, min(limit, 2000))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('tiktok_dm')") is None:
            return {"thread": None, "messages": []}
        thread = await conn.fetchrow(
            "SELECT conversation_id AS thread_id, conversation_type, participants, "
            "       owner_account, last_activity "
            "FROM tiktok_dm_thread WHERE conversation_id = $1",
            thread_id,
        )
        rows = await conn.fetch(
            'SELECT message_id, sender_uid AS sender_id, sender_secuid, '
            '       text, awe_type, message_type, "timestamp", is_from_me, '
            '       owner_account, client_message_id, is_stranger, media_url '
            'FROM tiktok_dm WHERE conversation_id = $1 '
            'ORDER BY "timestamp" ASC NULLS LAST LIMIT $2',
            thread_id, limit,
        )
    return {
        "thread": dict(thread) if thread else None,
        "messages": [dict(r) for r in rows],
    }


@router.get("/dm/telemetry")
async def dm_telemetry(_user: dict = Depends(require_role("viewer"))):
    """Passive DM probe/sample telemetry for the dashboard panel (P1.2 + P1.3).

    Returns per-platform counts of probes and samples the extension's
    observe-only WS hook has emitted, so we can tell at a glance whether real
    DM frames have arrived (particularly Instagram, which has been stuck at
    keepalive-class 1–4 byte frames while TikTok has been streaming 1KB
    protobuf samples on every DM). P1.3 also folds in dm_hook_heartbeat so
    the panel surfaces "last time the extension WS hook checked in" —
    critical for knowing when an IG/TikTok bundle change has silently broken
    the wrapper.

    Empty result if the tables don't exist yet (boot before migration).
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('dm_probe_log')") is None:
            return {"platforms": [], "generated_at": datetime.now(timezone.utc).isoformat()}
        rows = await conn.fetch(
            """
            SELECT
                platform,
                event_type,
                COUNT(*)                                                            AS all_time,
                COUNT(*) FILTER (WHERE seen_at > now() - interval '24 hours')       AS last_24h,
                COUNT(*) FILTER (WHERE seen_at > now() - interval '1 hour')         AS last_1h,
                MAX(seen_at)                                                        AS last_seen,
                MAX(frame_size)                                                     AS max_frame_size,
                MIN(frame_size) FILTER (WHERE frame_size > 0)                       AS min_frame_size
            FROM dm_probe_log
            GROUP BY platform, event_type
            ORDER BY platform, event_type
            """
        )
        heartbeat_rows = []
        if await conn.fetchval("SELECT to_regclass('dm_hook_heartbeat')") is not None:
            heartbeat_rows = await conn.fetch(
                """
                SELECT platform,
                       MAX(last_seen)                                                AS last_seen,
                       SUM(probes_sent)                                              AS probes_sent,
                       SUM(samples_shipped)                                          AS samples_shipped,
                       (ARRAY_AGG(extension_version ORDER BY last_seen DESC))[1]     AS extension_version,
                       COUNT(*) FILTER (WHERE owner_account <> '')                   AS owner_count
                FROM dm_hook_heartbeat
                GROUP BY platform
                """
            )
    # Pivot to per-platform record for easy frontend rendering.
    per_platform: dict[str, dict] = {}
    def _empty_bucket():
        return {"all_time": 0, "last_24h": 0, "last_1h": 0, "last_seen": None,
                "max_frame_size": None, "min_frame_size": None}
    for r in rows:
        p = per_platform.setdefault(r["platform"], {
            "platform": r["platform"],
            "probe":  _empty_bucket(),
            "sample": _empty_bucket(),
            "hook":   None,
        })
        bucket = "sample" if r["event_type"] == "sample" else "probe"
        p[bucket] = {
            "all_time":       int(r["all_time"] or 0),
            "last_24h":       int(r["last_24h"] or 0),
            "last_1h":        int(r["last_1h"] or 0),
            "last_seen":      r["last_seen"].isoformat() if r["last_seen"] else None,
            "max_frame_size": int(r["max_frame_size"]) if r["max_frame_size"] is not None else None,
            "min_frame_size": int(r["min_frame_size"]) if r["min_frame_size"] is not None else None,
        }
    for h in heartbeat_rows:
        p = per_platform.setdefault(h["platform"], {
            "platform": h["platform"],
            "probe":  _empty_bucket(),
            "sample": _empty_bucket(),
            "hook":   None,
        })
        p["hook"] = {
            "last_seen":         h["last_seen"].isoformat() if h["last_seen"] else None,
            "probes_sent":       int(h["probes_sent"] or 0),
            "samples_shipped":   int(h["samples_shipped"] or 0),
            "extension_version": h["extension_version"],
            "owner_count":       int(h["owner_count"] or 0),
        }
    return {
        "platforms": list(per_platform.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
