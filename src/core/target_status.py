"""Shared, bounded collection-target status query for dashboard and control room."""
from __future__ import annotations


class TargetAlreadyKnown(ValueError):
    """A target already has durable discovery evidence and should not be re-added."""

    def __init__(self, source: str, target_id: str, detail: dict):
        super().__init__("target already discovered")
        self.source = source
        self.target_id = target_id
        self.detail = detail


async def list_collection_targets(pool, *, source: str | None = None, limit: int | None = None) -> list[dict]:
    async with pool.acquire() as conn:
        query = "SELECT * FROM collection_targets"
        args: list[object] = []
        if source:
            args.append(source)
            query += " WHERE source=$1"
        query += " ORDER BY source, priority DESC, id"
        if limit is not None:
            args.append(max(1, limit))
            query += f" LIMIT ${len(args)}"
        rows = await conn.fetch(query, *args)
    return [dict(row) for row in rows]


async def target_already_known(conn, source: str, target_id: str) -> dict | None:
    """Return durable discovery evidence that makes a new target redundant."""
    queries = {
        "github": "SELECT login AS hit FROM github_users WHERE login=$1 OR platform_user_id::text=$1 LIMIT 1",
        "instagram": "SELECT username AS hit FROM instagram_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        "telegram": "SELECT username AS hit FROM telegram_users WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        "lemon8": "SELECT username AS hit FROM lemon8_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        "strava": "SELECT platform_athlete_id::text AS hit FROM strava_athletes WHERE platform_athlete_id::text=$1 LIMIT 1",
        "tiktok": "SELECT username AS hit FROM tiktok_profiles WHERE username=$1 OR platform_user_id=$1 LIMIT 1",
        "whatsapp": "SELECT platform_user_id AS hit FROM whatsapp_users WHERE platform_user_id=$1 LIMIT 1",
        "website": "SELECT domain AS hit FROM website_targets WHERE domain=$1 LIMIT 1",
    }
    query = queries.get(source)
    if not query:
        return None
    try:
        row = await conn.fetchrow(query, target_id)
    except Exception:
        return None
    if not row:
        return None
    try:
        parent = await conn.fetchval(
            "SELECT parent_node_id FROM spider_queue WHERE platform=$1 AND node_id=$2 AND parent_node_id IS NOT NULL LIMIT 1",
            source, target_id,
        )
    except Exception:
        parent = None
    try:
        last_seen = await conn.fetchval(
            "SELECT last_attempted_at FROM spider_queue WHERE platform=$1 AND node_id=$2 LIMIT 1",
            source, target_id,
        )
    except Exception:
        last_seen = None
    return {"discovered_via": parent, "last_seen": last_seen.isoformat() if last_seen else None}


async def add_collection_target(pool, source: str, target: str, *, priority: int = 0, force: bool = False) -> dict:
    """Add one target while retaining dashboard discovery de-duplication."""
    async with pool.acquire() as conn:
        if not force:
            already = await target_already_known(conn, source, target)
            if already is not None:
                raise TargetAlreadyKnown(source, target, already)
        await conn.execute(
            "INSERT INTO collection_targets (source, target_id, priority) "
            "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            source, target, int(priority) + (5 if force else 0),
        )
    return {"source": source, "target": target, "forced": force}
