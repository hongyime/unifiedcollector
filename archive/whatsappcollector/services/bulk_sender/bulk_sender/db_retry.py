import asyncio
from functools import wraps


def with_db_retry(max_retries: int = 3, base_delay: float = 0.5):
    def decorator(func):
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
