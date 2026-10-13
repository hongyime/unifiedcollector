"""Persistence helpers for typed, observed platform relationships."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from src.core.spider_discover import Edge, EdgeType

logger = logging.getLogger(__name__)


def shared_spider_enabled(platform: str) -> bool:
    """Return the explicit shared-claim rollout gate for one platform.

    Missing flags stay off so deploying adapters cannot silently move queue
    claims away from the platform's established worker.
    """
    key = f"{platform.strip().upper()}_SHARED_SPIDER_ENABLED"
    return os.getenv(key, "0").strip().lower() in {"1", "true", "yes", "on"}


def bounded_limit(name: str, default: int, *, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(1, value))


async def shared_spider_schema_ready(pool) -> bool:
    """Check the additive lifecycle and graph tables without mutating schema."""
    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                """
                SELECT to_regclass('spider_queue') IS NOT NULL
                   AND to_regclass('spider_graph_edges') IS NOT NULL
                   AND to_regclass('spider_fetch_observations') IS NOT NULL
                   AND (
                        SELECT COUNT(*) = 4
                          FROM information_schema.columns
                         WHERE table_schema = current_schema()
                           AND table_name = 'spider_queue'
                           AND column_name IN (
                               'next_attempt_at', 'claim_token',
                               'lease_expires_at', 'idempotency_key'
                           )
                   )
                """
            ))
    except Exception:
        logger.debug("shared spider schema readiness check failed", exc_info=True)
        return False


async def persist_spider_edge(
    pool,
    platform: str,
    edge: Edge,
    hop_distance: int,
    *,
    discovery_type: str = "graph_edge",
    source_metadata: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> None:
    """Idempotently store a typed relationship observed by a platform adapter."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO spider_graph_edges (
                platform, source_node_id, target_node_id, edge_type,
                discovery_type, hop_distance, source_metadata,
                first_seen_at, last_seen_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(),
                      COALESCE($8::TIMESTAMPTZ, NOW()))
            ON CONFLICT (platform, source_node_id, target_node_id, edge_type)
            DO UPDATE SET
                discovery_type = EXCLUDED.discovery_type,
                hop_distance = LEAST(spider_graph_edges.hop_distance, EXCLUDED.hop_distance),
                source_metadata = spider_graph_edges.source_metadata || EXCLUDED.source_metadata,
                last_seen_at = GREATEST(
                    spider_graph_edges.last_seen_at, EXCLUDED.last_seen_at
                )
            """,
            platform,
            edge.source,
            edge.target,
            edge.edge_type.value,
            discovery_type[:64],
            max(0, int(hop_distance)),
            json.dumps(source_metadata or edge.metadata or {}, default=str),
            observed_at,
        )


async def record_spider_fetch_observation(
    pool,
    platform: str,
    node_id: str,
    edge_type: EdgeType,
    outcome: str,
    edge_count: int,
    error_class: str | None,
    *,
    source_observed_at: datetime | None = None,
) -> None:
    if outcome not in {"success", "empty", "unavailable", "error"}:
        raise ValueError(f"unsupported spider fetch outcome: {outcome}")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO spider_fetch_observations (
                platform, node_id, edge_type, outcome, edge_count,
                error_class, attempts, last_attempt_at, last_success_at
            ) VALUES ($1, $2, $3, $4::VARCHAR(16), $5, $6, 1, NOW(),
                      CASE WHEN $4::VARCHAR(16) IN ('success', 'empty')
                           THEN COALESCE($7::TIMESTAMPTZ, NOW()) ELSE NULL END)
            ON CONFLICT (platform, node_id, edge_type) DO UPDATE SET
                outcome = EXCLUDED.outcome,
                edge_count = EXCLUDED.edge_count,
                error_class = EXCLUDED.error_class,
                attempts = spider_fetch_observations.attempts + 1,
                last_attempt_at = NOW(),
                last_success_at = CASE
                    WHEN EXCLUDED.outcome IN ('success', 'empty')
                    THEN COALESCE($7::TIMESTAMPTZ, NOW())
                    ELSE spider_fetch_observations.last_success_at
                END
            """,
            platform,
            node_id,
            edge_type.value,
            outcome,
            max(0, int(edge_count)),
            error_class[:64] if error_class else None,
            source_observed_at,
        )


async def persist_visible_follow_edges(
    pool,
    platform: str,
    owner: str,
    *,
    max_edges: int = 250,
) -> dict[str, Any]:
    """Copy a bounded browser-observed owner graph into normalized edge rows.

    ``follow_edges`` is evidence from a visible authenticated roster, not a
    source for recursively expanding other users' graphs. Empty snapshots are
    left unclassified because the source table does not record completed-empty
    browser sweeps.
    """
    counts = {"following": 0, "follower": 0}
    latest_seen_at: dict[str, datetime | None] = {
        "following": None,
        "follower": None,
    }
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT target_uid, target_username, direction, last_seen
              FROM follow_edges
             WHERE platform = $1 AND owner_account = $2
             ORDER BY last_seen DESC, target_uid
             LIMIT $3
            """,
            platform,
            owner,
            max(1, min(1000, int(max_edges))),
        )
        edge_rows = []
        for row in rows:
            direction = str(row["direction"] or "").lower()
            edge_type = {
                "following": EdgeType.FOLLOWING,
                "follower": EdgeType.FOLLOWER,
            }.get(direction)
            target = str(row["target_uid"] or "").strip()
            if edge_type is None or not target:
                continue
            metadata = {"target_username": row["target_username"]}
            observed_at = row["last_seen"]
            edge_rows.append((
                platform, owner, target, edge_type.value, "visible_follow_list",
                0, json.dumps(metadata, default=str), observed_at,
            ))
            counts[direction] += 1
            if observed_at is not None and (
                latest_seen_at[direction] is None
                or observed_at > latest_seen_at[direction]
            ):
                latest_seen_at[direction] = observed_at
        if edge_rows:
            await conn.executemany(
                """
                INSERT INTO spider_graph_edges (
                    platform, source_node_id, target_node_id, edge_type,
                    discovery_type, hop_distance, source_metadata,
                    first_seen_at, last_seen_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, NOW(),
                          COALESCE($8::TIMESTAMPTZ, NOW()))
                ON CONFLICT (platform, source_node_id, target_node_id, edge_type)
                DO UPDATE SET
                    discovery_type = EXCLUDED.discovery_type,
                    hop_distance = LEAST(spider_graph_edges.hop_distance, EXCLUDED.hop_distance),
                    source_metadata = spider_graph_edges.source_metadata || EXCLUDED.source_metadata,
                    last_seen_at = GREATEST(
                        spider_graph_edges.last_seen_at, EXCLUDED.last_seen_at
                    )
                """,
                edge_rows,
            )
    counts["following_last_seen_at"] = latest_seen_at["following"]
    counts["follower_last_seen_at"] = latest_seen_at["follower"]
    return counts
