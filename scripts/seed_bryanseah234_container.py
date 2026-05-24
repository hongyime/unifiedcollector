"""Container-side: read /tmp/bryanseah234_1hop.json, fetch live followers/following, seed PG."""
import asyncio
import json
import os
import sys

import asyncpg
import httpx

SEED = "bryanseah234"
PG_DSN = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://")
GH_TOKEN = os.getenv("GITHUB_TOKEN", "")


def from_file() -> set[str]:
    with open("/tmp/bryanseah234_1hop.json") as f:
        return set(json.load(f))


async def from_live_api() -> set[str]:
    if not GH_TOKEN:
        print("[live] no GITHUB_TOKEN; skipping", file=sys.stderr)
        return set()
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
                    print(f"[live] {kind} page {page}: HTTP {r.status_code} {r.text[:200]}", file=sys.stderr)
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
    t = q = 0
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO collection_targets (source, target_id, target_name, target_type, status, priority)
               VALUES ('github', $1, $1, 'user', 'pending', 10)
               ON CONFLICT (source, target_id) DO UPDATE SET priority=GREATEST(collection_targets.priority, 10)""",
            SEED,
        )
        t += 1
        await conn.execute(
            """INSERT INTO github_spider_queue (target_type, target_identifier, source, priority, status)
               VALUES ('user', $1, 'manual', 10, 'pending')
               ON CONFLICT (target_type, target_identifier) DO NOTHING""",
            SEED,
        )
        q += 1
        for u in usernames:
            if u == SEED:
                continue
            await conn.execute(
                """INSERT INTO collection_targets (source, target_id, target_name, target_type, status, priority)
                   VALUES ('github', $1, $1, 'user', 'pending', 5)
                   ON CONFLICT (source, target_id) DO NOTHING""",
                u,
            )
            t += 1
            await conn.execute(
                """INSERT INTO github_spider_queue (target_type, target_identifier, source, priority, status)
                   VALUES ('user', $1, 'seed_1hop', 5, 'pending')
                   ON CONFLICT (target_type, target_identifier) DO NOTHING""",
                u,
            )
            q += 1
    await pool.close()
    return t, q


async def main():
    sq = from_file()
    print(f"[file]   1-hop usernames: {len(sq)}")
    api = await from_live_api()
    print(f"[live]   followers+following: {len(api)}")
    merged = sq | api
    print(
        f"[merged] unique: {len(merged)}  (file-only={len(sq-api)}, api-only={len(api-sq)}, both={len(sq&api)})"
    )
    t, q = await seed(merged)
    print(f"[pg] collection_targets upserts attempted: {t}")
    print(f"[pg] github_spider_queue upserts attempted: {q}")


if __name__ == "__main__":
    asyncio.run(main())
