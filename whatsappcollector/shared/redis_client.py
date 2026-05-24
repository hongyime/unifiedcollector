"""
shared/redis_client.py — Async Redis connection helper with auto-reconnect.
"""
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError


def create_redis_client(url: str) -> Redis:
    """Return a redis.asyncio.Redis client with exponential-backoff retry.

    The client is not yet connected — use `await client.ping()` or any
    first command to establish the connection lazily.
    """
    retry = Retry(ExponentialBackoff(cap=10, base=0.5), retries=6)
    return Redis.from_url(
        url,
        decode_responses=True,
        retry=retry,
        retry_on_error=[ConnectionError, TimeoutError],
    )


async def get_logger():
    """Convenience import alias — use get_logger from observability instead."""
    from shared.observability import get_logger as _get_logger
    return _get_logger()
