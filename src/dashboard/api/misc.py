"""Miscellaneous cross-cutting routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 20
(cluster 9). Covers:

* ``/graph`` — social relationship graph
* ``/messaging/coverage`` — native vs Beeper messaging coverage
* ``/stories/overview`` — stories/highlights aggregate
* ``/worker/health`` — worker liveness probe
"""
from __future__ import annotations

import logging
import sys
import time

from fastapi import APIRouter, Depends, Query

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import _MESSAGING_COVERAGE_CACHE

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


@router.get("/graph")
async def social_graph(
    source: str = Query("instagram", description="Platform source"),
    limit: int = Query(5000, description="Max edges to return", ge=1, le=50000),
    _user: dict = Depends(require_role("viewer"))
):
    # Relationship edges live in TWO tables depending on the collector:
    #   * graph_edges  (source, source_user, target_user, edge_type) — whatsapp
    #   * follow_edges (platform, owner_account, target_uid/username, direction)
    #     — instagram (and any future follow-graph collector)
    # Query the right one for the requested platform and normalise follow_edges
    # into the (source_user, target_user, edge_type) shape the frontend expects.
    pool = await _get_pool()
    async with pool.acquire() as conn:
        source_norm = (source or "").strip().lower()
        if source_norm == "github" and await conn.fetchval("SELECT to_regclass('github_edges') IS NOT NULL"):
            edges = await conn.fetch(
                """
                SELECT source_login AS source_user,
                       target_login AS target_user,
                       edge_type
                FROM github_edges
                ORDER BY last_seen DESC NULLS LAST
                LIMIT $1
                """,
                limit,
            )
        else:
            edges = await conn.fetch(
                "SELECT source_user, target_user, edge_type FROM graph_edges WHERE source = $1 LIMIT $2",
                source_norm, limit,
            )
            if not edges:
                edges = await conn.fetch(
                    """
                    SELECT owner_account AS source_user,
                           COALESCE(target_username, target_uid) AS target_user,
                           direction AS edge_type
                    FROM follow_edges
                    WHERE platform = $1
                    LIMIT $2
                    """,
                    source_norm, limit,
                )
    nodes = set()
    edge_list = []
    for e in edges:
        nodes.add(e["source_user"])
        nodes.add(e["target_user"])
        edge_list.append(dict(e))
    return {
        "nodes": [{"id": n} for n in nodes],
        "edges": edge_list,
    }


@router.get("/messaging/coverage")
async def messaging_coverage(_user: dict = Depends(require_role("viewer"))):
    """Native-vs-Beeper messaging coverage.

    Native Telegram/WhatsApp tables are canonical. Beeper is a mirror for those
    networks and the canonical source for networks with no native collector.
    """
    _estimated_table_rows = _lookup("_estimated_table_rows")
    _messaging_policy = _lookup("_messaging_policy")
    _normalize_beeper_network = _lookup("_normalize_beeper_network")
    cache_ttl = 300
    now = time.time()
    cached = _MESSAGING_COVERAGE_CACHE.get("rows")
    if cached is not None and now - float(_MESSAGING_COVERAGE_CACHE.get("ts") or 0) < cache_ttl:
        return cached

    pool = await _get_pool()
    native_networks = {
        "Telegram": {
            "native_source": "telegram",
            "canonical_source": "native",
            "policy": _messaging_policy("telegram"),
        },
        "WhatsApp": {
            "native_source": "whatsapp",
            "canonical_source": "native",
            "policy": _messaging_policy("whatsapp"),
        },
    }
    async with pool.acquire() as conn:
        telegram_people_row = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE COALESCE(is_bot, false) = false) AS people,
                   count(*) FILTER (WHERE is_bot = true) AS bots
            FROM telegram_users
            """,
            timeout=8,
        )
        whatsapp_people = await conn.fetchval("SELECT COUNT(*) FROM whatsapp_users", timeout=8)
        whatsapp_people_basis = "whatsapp_users"
        if not whatsapp_people:
            whatsapp_people = await conn.fetchval(
                """
                SELECT count(DISTINCT sender_id)
                FROM whatsapp_messages
                WHERE sender_id IS NOT NULL
                """,
                timeout=10,
            )
            whatsapp_people_basis = "distinct whatsapp message senders"
        native = {
            "telegram": {
                "messages": await _estimated_table_rows(conn, "telegram_messages"),
                "chats": await conn.fetchval("SELECT COUNT(*) FROM telegram_chats"),
                "people": int((telegram_people_row and telegram_people_row["people"]) or 0),
                "people_basis": "telegram_users excluding bots",
                "bots": int((telegram_people_row and telegram_people_row["bots"]) or 0),
                "last_message": await conn.fetchval(
                    "SELECT platform_created_at FROM telegram_messages "
                    "ORDER BY platform_created_at DESC NULLS LAST LIMIT 1"
                ),
            },
            "whatsapp": {
                "messages": await conn.fetchval("SELECT COUNT(*) FROM whatsapp_messages"),
                "chats": await conn.fetchval("SELECT COUNT(*) FROM whatsapp_chats"),
                "people": int(whatsapp_people or 0),
                "people_basis": whatsapp_people_basis,
                "bots": 0,
                "last_message": await conn.fetchval(
                    'SELECT "timestamp" FROM whatsapp_messages ORDER BY "timestamp" DESC NULLS LAST LIMIT 1'
                ),
            },
        }
        beeper_message_rows = await conn.fetch(
            """
            SELECT network,
                   COUNT(*)::bigint AS messages,
                   COUNT(DISTINCT chat_id)::int AS message_chats,
                   MAX(timestamp) AS last_message
            FROM beeper_shadow_messages
            WHERE network IS NOT NULL
              AND network <> ''
              AND network <> 'unknown'
            GROUP BY network
            ORDER BY network
            """,
            timeout=45,
        )
        beeper_unknown_message_rows = await conn.fetch(
            """
            SELECT COALESCE(
                       NULLIF(
                           CASE
                               WHEN lower(trim(COALESCE(c.network, ''))) = 'unknown' THEN ''
                               ELSE trim(COALESCE(c.network, ''))
                           END,
                           ''
                       ),
                       'Unmapped Beeper'
                   ) AS network,
                   COUNT(*)::bigint AS messages,
                   COUNT(DISTINCT m.chat_id)::int AS message_chats,
                   MAX(m.timestamp) AS last_message
            FROM beeper_shadow_messages m
            LEFT JOIN beeper_shadow_chats c ON c.chat_id = m.chat_id
            WHERE m.network = 'unknown'
               OR m.network IS NULL
               OR m.network = ''
            GROUP BY 1
            ORDER BY 1
            """,
            timeout=20,
        )
        beeper_chat_rows = await conn.fetch(
            """
            SELECT COALESCE(
                       NULLIF(
                           CASE
                               WHEN lower(trim(COALESCE(network, ''))) = 'unknown' THEN ''
                               ELSE trim(COALESCE(network, ''))
                           END,
                           ''
                       ),
                       'Unmapped Beeper'
                   ) AS network,
                   COUNT(*) AS chats,
                   MAX(last_seen_at) AS last_seen
            FROM beeper_shadow_chats
            GROUP BY 1
            """,
            timeout=30,
        )
        beeper_people_rows = await conn.fetch(
            """
            SELECT COALESCE(
                       NULLIF(
                           CASE
                               WHEN lower(trim(COALESCE(network, ''))) = 'unknown' THEN ''
                               ELSE trim(COALESCE(network, ''))
                           END,
                           ''
                       ),
                       'Unmapped Beeper'
                   ) AS network,
                   COUNT(DISTINCT participant_id)::int AS people
            FROM beeper_shadow_participants
            GROUP BY 1
            """,
            timeout=30,
        )
    beeper_messages = {}
    for r in list(beeper_message_rows) + list(beeper_unknown_message_rows):
        net = _normalize_beeper_network(r["network"])
        row = beeper_messages.setdefault(
            net,
            {"network": net, "messages": 0, "message_chats": 0, "last_message": None},
        )
        row["messages"] += int(r["messages"] or 0)
        row["message_chats"] += int(r["message_chats"] or 0)
        last_message = r["last_message"]
        if last_message and (not row["last_message"] or last_message > row["last_message"]):
            row["last_message"] = last_message
    beeper_chats = {r["network"]: dict(r) for r in beeper_chat_rows}
    beeper_people = {r["network"]: dict(r) for r in beeper_people_rows}
    networks = sorted(
        set(native_networks) | set(beeper_messages) | set(beeper_chats),
        key=lambda n: (0 if n in native_networks else 1, n.lower()),
    )
    out = []
    for net in networks:
        row = native_networks.get(net, {
            "native_source": None,
            "canonical_source": "beeper",
            "policy": _messaging_policy(None),
        })
        src = row["native_source"]
        native_stats = native.get(
            src or "",
            {"messages": 0, "chats": 0, "people": 0, "people_basis": None, "bots": 0, "last_message": None},
        )
        mirror_messages = beeper_messages.get(net, {})
        mirror_chats = beeper_chats.get(net, {})
        mirror_people = beeper_people.get(net, {})
        beeper_people_count = int(mirror_people.get("people") or 0)
        coverage_note = None
        if src:
            coverage_note = "Native rows are the canonical count; Beeper rows are a mirror/backstop and should not be added on top."
        elif net == "Unmapped Beeper":
            coverage_note = "Messages could not be mapped to a Beeper network; chat metadata needs repair before analyzer should infer relationships."
        d = {
            **row,
            "network": net,
            "beeper_network": net,
            "native_messages": native_stats.get("messages") or 0,
            "native_chats": native_stats.get("chats") or 0,
            "native_people": native_stats.get("people") or 0,
            "native_people_basis": native_stats.get("people_basis"),
            "native_bots": native_stats.get("bots") or 0,
            "native_last_message": native_stats.get("last_message"),
            "beeper_messages": mirror_messages.get("messages") or 0,
            "beeper_chats": mirror_chats.get("chats") or mirror_messages.get("message_chats") or 0,
            "beeper_people": beeper_people_count,
            "beeper_people_basis": "beeper_shadow_participants" if beeper_people_count else None,
            "beeper_message_senders": None,
            "beeper_last_message": mirror_messages.get("last_message") or mirror_chats.get("last_seen"),
            "coverage_note": coverage_note,
        }
        for key in ("native_last_message", "beeper_last_message"):
            if d[key]:
                d[key] = d[key].isoformat()
        out.append(d)
    _MESSAGING_COVERAGE_CACHE["ts"] = now
    _MESSAGING_COVERAGE_CACHE["rows"] = out
    return out


@router.get("/stories/overview")
async def stories_overview(
    limit: int = Query(300, ge=1, le=2000),
    _user: dict = Depends(require_role("viewer")),
):
    """Ephemeral media (media_items.kind story/highlight) grouped per account,
    with overall stats — powers the Stories dashboard page. Stories live under
    the `kind` column (not content_type), across any source that captures them
    (instagram today; whatsapp status / telegram stories / tiktok as they land).
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE kind='story')      AS stories,
                   count(*) FILTER (WHERE kind='highlight')  AS highlights,
                   count(DISTINCT entity_name)               AS accounts,
                   count(DISTINCT source)                     AS sources,
                   max(collected_at)                          AS newest
            FROM media_items
            WHERE kind IN ('story','highlight')
            """
        )
        rows = await conn.fetch(
            """
            SELECT source, entity_name,
                   count(*) FILTER (WHERE kind='story')     AS story_count,
                   count(*) FILTER (WHERE kind='highlight') AS highlight_count,
                   count(*)                                  AS total,
                   max(collected_at)                         AS newest
            FROM media_items
            WHERE kind IN ('story','highlight')
            GROUP BY source, entity_name
            ORDER BY max(collected_at) DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return {"stats": dict(stats) if stats else {}, "accounts": [dict(r) for r in rows]}


@router.get("/worker/health")
async def worker_health(_user: dict = Depends(require_role("viewer"))):
    from src.worker import get_worker_health
    return get_worker_health()
