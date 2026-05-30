import asyncio
import logging
import os
import ssl as _ssl

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


def _dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://collector:***@localhost:5432/unifiedcollector",
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
    elif mode == "require-noverify":
        # Explicit opt-in for no-verify (legacy/local-dev only).  Loud log.
        logger.warning(
            "POSTGRES_SSL_MODE=require-noverify — TLS is on but the certificate "
            "is NOT being verified. Use only for local dev."
        )
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    # Otherwise: keep default verify_mode=CERT_REQUIRED (fail closed if no CA).
    return ctx


async def get_pool() -> asyncpg.Pool:
    global _pool
    # Fast path without contention.
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            ssl = _ssl_context()
            kwargs = dict(min_size=2, max_size=20, command_timeout=60,
                          max_inactive_connection_lifetime=300)
            if ssl is not None:
                kwargs["ssl"] = ssl
            _pool = await asyncpg.create_pool(_dsn(), **kwargs)
            logger.info("Database pool created")
    return _pool


async def close_pool():
    global _pool
    async with _pool_lock:
        if _pool:
            await _pool.close()
            _pool = None
            logger.info("Database pool closed")
