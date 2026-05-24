from __future__ import annotations
import asyncpg
import os
import logging

logger = logging.getLogger(__name__)
_pool: asyncpg.Pool | None = None

DSN = (
    f"postgresql://{os.environ.get('DB_USER', 'collector_user')}"
    f":{os.environ.get('DB_PASSWORD', '')}"
    f"@{os.environ.get('DB_HOST', 'postgres')}"
    f":{os.environ.get('DB_PORT', '5432')}"
    f"/{os.environ.get('DB_NAME', 'telegramcollector')}"
)

async def init_pool() -> None:
    global _pool
    try:
        _pool = await asyncpg.create_pool(dsn=DSN, min_size=2, max_size=10)
        logger.info("DB pool initialized")
    except Exception as e:
        logger.error("DB pool failed: %s", e)

async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

def _serialize(record) -> dict:
    if record is None:
        return {}
    d = dict(record)
    for k, v in d.items():
        if hasattr(v, 'isoformat'):
            d[k] = v.isoformat()
        elif isinstance(v, (bytes, memoryview)):
            d[k] = None
    return d

async def fetchall(sql: str, *args) -> list[dict]:
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [_serialize(r) for r in rows]

async def fetchone(sql: str, *args) -> dict | None:
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return _serialize(row) if row else None

async def fetchval(sql: str, *args):
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        return await conn.fetchval(sql, *args)

async def execute(sql: str, *args) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(sql, *args)
