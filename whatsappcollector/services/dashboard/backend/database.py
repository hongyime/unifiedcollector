"""
backend/database.py — Asyncpg connection pool for the dashboard service.

Usage:
    async with database.acquire() as conn:
        rows = await conn.fetch("SELECT ...")
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Initialize the shared asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return

    settings = get_settings()
    try:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=2,
            max_size=10,
            command_timeout=30,
            ssl="require",
        )
        logger.info("database_pool_initialized")
    except Exception as exc:
        logger.error("database_pool_init_failed: %s", exc)
        _pool = None


async def close_pool() -> None:
    """Close the shared connection pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("database_pool_closed")


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the pool. Raises if pool is unavailable."""
    global _pool
    if _pool is None:
        await init_pool()
    if _pool is None:
        raise RuntimeError("Database pool not available")
    async with _pool.acquire() as conn:
        yield conn


async def fetchall(query: str, *args) -> list[asyncpg.Record]:
    """Convenience: run a SELECT and return all rows."""
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchone(query: str, *args) -> asyncpg.Record | None:
    """Convenience: run a SELECT and return one row."""
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args):
    """Convenience: run a SELECT and return a single scalar."""
    async with acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args) -> str:
    """Convenience: run an INSERT/UPDATE/DELETE."""
    async with acquire() as conn:
        return await conn.execute(query, *args)
