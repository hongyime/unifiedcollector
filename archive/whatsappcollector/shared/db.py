"""
shared/db.py — asyncpg connection pool factory for all whatsappcollector services.

Fixes BUG-05: asyncpg validates connections on checkout by default, which eliminates
the stale-connection failure mode that the SQLAlchemy pool (without pool_pre_ping) had.
SQLAlchemy is retired in all new services; raw asyncpg is used directly.
"""
import asyncpg


def _normalize_asyncpg_dsn(dsn: str) -> str:
    value = (dsn or "").strip()
    if value.startswith("postgresql+asyncpg://"):
        return "postgresql://" + value[len("postgresql+asyncpg://"):]
    if value.startswith("postgres+asyncpg://"):
        return "postgres://" + value[len("postgres+asyncpg://"):]
    return value


async def create_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
    command_timeout: float = 30.0,
    **kwargs,
) -> asyncpg.Pool:
    """Create and return an asyncpg connection pool.

    asyncpg checks connection health on checkout by default — no extra
    pool_pre_ping configuration is required.
    """
    return await asyncpg.create_pool(
        _normalize_asyncpg_dsn(dsn),
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        **kwargs,
    )
