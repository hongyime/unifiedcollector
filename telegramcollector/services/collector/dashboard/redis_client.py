"""Redis helper for the collector dashboard."""
from shared.config import Settings

settings = Settings()


def get_redis():
    """Return a redis.Redis instance, or None if unavailable."""
    try:
        import redis

        client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        client.ping()
        return client
    except ImportError:
        return None
    except Exception:
        return None
