"""Seed collection_targets and github_spider_queue from old sqlite DB + live API for bryanseah234.

Strategy: 1-hop edges only. Live API (followers + following) merged in.
"""
import asyncio
import os
import sqlite3
import sys
import httpx
import asyncpg

SQLITE = r"C:\unifiedcollector\githubtoolkit\data\github_toolkit.db"
SEED = "bryanseah234"
PG_DSN = os.getenv("PG_DSN", "postgresql://collector:collector@localhost:5432/unifiedcollector")
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")


def from_sqlite() -> set[str]:
    con = sqlite3.connect(SQLITE)
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT DISTINCT target_username FROM graph_edges WHERE source_username=?
        UNION
        SELECT DISTINCT source_username FROM graph_edges WHERE target_username=?
        """,
        (SEED, SEED),
    ).fetchall()
    con.close()
    return {r[0] for r in rows if r[0]}


async def from_live_api() -> set[str]:
    if not GH_TOKEN:
        print("[live] no GITHUB_TOKEN; skipping")
        return set()
    # take first PAT if comma-separated
    tok = GH_TOKEN.split(",")[0].strip()
    headers = {"Authorization": f"token {tok}", "Accept": "application/vnd.github+json"}
    out: set[str] = set()
    async with httpx.AsyncClient(timeout=30, headers=headers) as cli:
        for kind in ("followers", "following"):
            page = 1
            while True:
                r = await cli.get(
                    f"https://api.github.com/users/{SEED}/{kind}",
                    params={"per_page": 100, "page": page},
                )
                if r.status_code != 200:
                    print(f"[live] {kind} page {page}: HTTP {r.status_code} {r.text[:200]}")
                    break
                data = r.json()
                if not data:
                    break
                for u in data:
                    out.add(u["login"])
                if len(data) < 100:
                    break
                page += 1
    return out


async def seed(usernames: set[str]) -> tuple[int, int]:
    pool = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=4)
    inserted_targets = 0
    inserted_queue = 0
    async with pool.acquire() as conn:
        # bryanseah234 itself first, priority 10
        r = await conn.execute(
            """INSERT INTO collection_targets (source, target_id, target_name, target_type, status, priority)
               VALUES ('github', $1, $1, 'user', 'pending', 10)
               ON CONFLICT (source, target_id) DO UPDATE SET priority=GREATEST(collection_targets.priority, 10)""",
            SEED,
        )
        inserted_targets += 1
        await conn.execute(
            """INSERT INTO github_spider_queue (target_type, target_identifier, source, priority, status)
               VALUES ('user', $1, 'manual', 10, 'pending')
               ON CONFLICT (target_type, target_identifier) DO NOTHING""",
            SEED,
        )
        inserted_queue += 1
        for u in usernames:
            if u == SEED:
                continue
            await conn.execute(
                """INSERT INTO collection_targets (source, target_id, target_name, target_type, status, priority)
                   VALUES ('github', $1, $1, 'user', 'pending', 5)
                   ON CONFLICT (source, target_id) DO NOTHING""",
                u,
            )
            inserted_targets += 1
            await conn.execute(
                """INSERT INTO github_spider_queue (target_type, target_identifier, source, priority, status)
                   VALUES ('user', $1, 'seed_1hop', 5, 'pending')
                   ON CONFLICT (target_type, target_identifier) DO NOTHING""",
                u,
            )
            inserted_queue += 1
    await pool.close()
    return inserted_targets, inserted_queue


async def main():
    sq = from_sqlite()
    print(f"[sqlite] 1-hop usernames: {len(sq)}")
    api = await from_live_api()
    print(f"[live]   followers+following: {len(api)}")
    merged = sq | api
    print(f"[merged] unique usernames: {len(merged)}  (sqlite-only={len(sq-api)}, api-only={len(api-sq)}, both={len(sq&api)})")
    t, q = await seed(merged)
    print(f"[pg] upserted into collection_targets: {t}")
    print(f"[pg] upserted into github_spider_queue: {q}")


if __name__ == "__main__":
    asyncio.run(main())
