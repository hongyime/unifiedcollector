"""Initialize only the isolated development schema, without starting workers."""
import asyncio

from src.db.connection import close_pool, get_pool
from src.db.migrate import apply_all


async def initialize() -> None:
    pool = await get_pool()
    try:
        result = await apply_all(pool)
        if result.get("deferred") or not result.get("schemas"):
            raise RuntimeError("Development schema initialization did not complete")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(initialize())
