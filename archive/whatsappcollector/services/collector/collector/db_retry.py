import asyncio
from collections.abc import Callable
from functools import wraps


def with_db_retry(max_retries: int = 3, base_delay: float = 0.5):
    """Retry async DB operations with exponential backoff."""

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except (ConnectionRefusedError, ConnectionResetError, OSError, asyncio.TimeoutError):
                    attempt += 1
                    if attempt > max_retries:
                        raise
                    await asyncio.sleep(base_delay * (2 ** (attempt - 1)))

        return wrapper

    return decorator
