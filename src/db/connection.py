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


def _env_float(name: str, default: float, *, min_value: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(min_value, value)


def _is_retryable_connect_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            OSError,
            ConnectionError,
            TimeoutError,
            asyncio.TimeoutError,
            asyncpg.InterfaceError,
        ),
    ):
        return True
    name = exc.__class__.__name__.lower()
    return any(
        token in name
        for token in (
            "cannotconnectnow",
            "connectiondoesnotexist",
            "connectionfailure",
            "connectionrejected",
            "connectionreset",
            "connectionrefused",
            "toomanyconnections",
        )
    )


async def _create_pool_with_retry(kwargs: dict) -> asyncpg.Pool:
    max_wait = _env_float("DB_CONNECT_RETRY_TIMEOUT_SECONDS", 180.0, min_value=0.0)
    delay = _env_float("DB_CONNECT_RETRY_INITIAL_SECONDS", 5.0, min_value=0.1)
    max_delay = _env_float("DB_CONNECT_RETRY_MAX_SECONDS", 30.0, min_value=0.1)
    deadline = asyncio.get_running_loop().time() + max_wait
    attempt = 0

    while True:
        try:
            return await asyncpg.create_pool(_dsn(), **kwargs)
        except Exception as exc:
            if not _is_retryable_connect_error(exc):
                raise
            now = asyncio.get_running_loop().time()
            if attempt > 0 and now >= deadline:
                raise
            sleep_for = min(delay, max(0.1, deadline - now))
            logger.warning(
                "Database pool connect failed (%s); retrying in %.0fs",
                exc,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 1.5, max_delay)
            attempt += 1


async def get_pool() -> asyncpg.Pool:
    global _pool
    # Fast path without contention.
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            ssl = _ssl_context()
            # Per-container pool sizing. With ~14 pools (11 collectors +
            # dashboard + scheduler + worker) sharing one Postgres, the product
            # of max_size × pools must stay under the server's max_connections,
            # or bursts exhaust the slots and asyncpg raises TimeoutError on
            # connect (observed crashing beeper/whatsapp/lemon8). Defaults:
            # min_size=1 (small idle footprint), max_size=10 (14×10=140 < the
            # server's 200 ceiling). Override per-container via env if needed.
            import os as _os
            _min = int(_os.getenv("DB_POOL_MIN_SIZE", "1"))
            _max = int(_os.getenv("DB_POOL_MAX_SIZE", "10"))
            kwargs = dict(min_size=_min, max_size=_max, command_timeout=60,
                          max_inactive_connection_lifetime=300)
            if ssl is not None:
                kwargs["ssl"] = ssl
            _pool = await _create_pool_with_retry(kwargs)
            logger.info("Database pool created")
    return _pool


async def close_pool():
    global _pool
    async with _pool_lock:
        if _pool:
            await _pool.close()
            _pool = None
            logger.info("Database pool closed")
