import asyncio
import logging

logger = logging.getLogger(__name__)


async def probe_postgres(
    host: str, port: int, dbname: str, user: str, password: str,
    timeout: float = 5.0,
) -> bool:
    """Attempts a real psycopg connection and executes SELECT 1. Returns True on success."""
    try:
        import psycopg
        loop = asyncio.get_event_loop()
        conn = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: psycopg.connect(
                    host=host, port=port, dbname=dbname, user=user, password=password,
                    connect_timeout=int(timeout)
                )
            ),
            timeout=timeout
        )
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception as e:
        logger.debug(f"probe_postgres failed: {e}")
        return False


async def probe_redis(
    host: str, port: int, db: int = 0, password: str = None,
    timeout: float = 5.0,
) -> bool:
    """Attempts a Redis PING. Returns True on success."""
    try:
        import redis as redis_lib
        loop = asyncio.get_event_loop()
        client = redis_lib.Redis(
            host=host, port=port, db=db, password=password,
            socket_connect_timeout=timeout, socket_timeout=timeout
        )
        await loop.run_in_executor(None, client.ping)
        client.close()
        return True
    except Exception as e:
        logger.debug(f"probe_redis failed: {e}")
        return False


async def wait_for_dependencies(
    require_postgres: bool = True,
    require_redis: bool = False,
    max_attempts: int = 30,
    retry_interval: float = 2.0,
) -> None:
    """
    Loops until all required dependencies respond successfully.
    Raises RuntimeError if max_attempts is exhausted.
    Uses settings from shared.config.settings for connection parameters.
    Logs each attempt at DEBUG level; logs success at INFO level.
    """
    from shared.config import settings

    attempt = 0
    while True:
        attempt += 1
        pg_ok = True
        redis_ok = True

        if require_postgres:
            pg_ok = await probe_postgres(
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                dbname=settings.DB_NAME,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
            )

        if require_redis:
            redis_ok = await probe_redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
            )

        if pg_ok and redis_ok:
            logger.info(f"All dependencies ready after {attempt} attempt(s).")
            return

        logger.debug(f"Attempt {attempt}/{max_attempts}: postgres={pg_ok}, redis={redis_ok}")

        if max_attempts > 0 and attempt >= max_attempts:
            raise RuntimeError(
                f"Dependencies not ready after {max_attempts} attempts. "
                f"postgres={pg_ok}, redis={redis_ok}"
            )

        await asyncio.sleep(retry_interval)
