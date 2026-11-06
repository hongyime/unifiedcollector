"""Shared, bounded social-graph reader for the dashboard and control room."""
from __future__ import annotations


async def get_social_graph(pool, *, source: str = "instagram", limit: int = 5000) -> dict:
    source = (source or "instagram").strip().lower()
    limit = max(1, min(limit, 50_000))
    async with pool.acquire() as conn:
        if source == "github" and await conn.fetchval("SELECT to_regclass('github_edges') IS NOT NULL"):
            edges = await conn.fetch(
                """
                SELECT source_login AS source_user, target_login AS target_user, edge_type
                FROM github_edges ORDER BY last_seen DESC NULLS LAST LIMIT $1
                """, limit,
            )
        else:
            edges = await conn.fetch(
                "SELECT source_user, target_user, edge_type FROM graph_edges WHERE source=$1 LIMIT $2",
                source, limit,
            )
            if not edges:
                edges = await conn.fetch(
                    """
                    SELECT owner_account AS source_user,
                           COALESCE(target_username, target_uid) AS target_user,
                           direction AS edge_type
                    FROM follow_edges WHERE platform=$1 LIMIT $2
                    """, source, limit,
                )
    edge_list = [dict(edge) for edge in edges]
    nodes = sorted({node for edge in edge_list for node in (edge.get("source_user"), edge.get("target_user")) if node})
    return {"nodes": [{"id": node} for node in nodes], "edges": edge_list}
