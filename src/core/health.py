import asyncio
import sys

from src.db.connection import get_pool


async def _check():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")


def health_check():
    try:
        asyncio.run(_check())
    except Exception as e:
        print(f"UNHEALTHY: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    health_check()
