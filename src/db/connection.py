import logging
import os
import ssl as _ssl

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://collector:collector@localhost:5432/unifiedcollector",
    )


def _ssl_context():
    mode = os.environ.get("POSTGRES_SSL_MODE", "")
    if not mode or mode == "disable":
        return None
    if mode == "prefer":
        return "prefer"
    ctx = _ssl.create_default_context()
    cert = os.environ.get("POSTGRES_SSL_CERT", "")
    if cert:
        ctx.load_verify_locations(cert)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    return ctx


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        ssl = _ssl_context()
        kwargs = dict(min_size=2, max_size=10, command_timeout=30)
        if ssl is not None:
            kwargs["ssl"] = ssl
        _pool = await asyncpg.create_pool(_dsn(), **kwargs)
        logger.info("Database pool created")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")
