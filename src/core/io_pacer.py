"""Global media-I/O pacer — one shared bytes/sec ceiling for writes to Z.

P2 review §5: the real throughput ceiling is Z-drive media write bandwidth
shared by ~11 collectors, not CPU. Today ~8 independent per-source rate knobs
(telegram msg/s, beeper pages, whatsapp req/min, ...) are hand-tuned against that
one shared resource with NO coordination — turning one up re-saturates Z and
re-breaks another (the compose comments literally document this whack-a-mole:
"saturated the Z media drive -> load 43").

This replaces that with a SINGLE global token bucket in Redis, keyed on bytes.
Every collector's media path (all tiers of media_download.download) consumes
tokens equal to the bytes it just wrote; when the shared bucket runs dry the next
writer waits. Set one ceiling (MEDIA_IO_BYTES_PER_SEC) and aggregate Z write rate
converges to it regardless of how many collectors are active.

Properties:
  * Cross-process: the bucket lives in Redis (already running); all collectors
    share it. The refill+deduct is a single atomic Lua eval (no races).
  * Fail-OPEN: any Redis error / missing client → acquire() returns immediately.
    Pacing must NEVER block collection when Redis is down.
  * Dormant by default: MEDIA_IO_PACER_ENABLED=0 ships it off so it can be rolled
    out and enabled deliberately after observing baseline Z load.
  * Virtual-scheduling bucket: tokens may go negative (debt); the caller sleeps
    for the deficit / rate. Smooths bursts without dropping any work.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("MEDIA_IO_PACER_ENABLED", "0").lower() in ("1", "true", "yes")


# Atomic refill-and-deduct. Returns the number of seconds the caller should sleep
# (0 if tokens were available). Tokens may go negative — the deficit is the wait.
_LUA = """
local key   = KEYS[1]
local rate  = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local n     = tonumber(ARGV[3])
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000.0
local tokens = tonumber(redis.call('HGET', key, 'tokens') or burst)
local ts     = tonumber(redis.call('HGET', key, 'ts') or now)
tokens = math.min(burst, tokens + (now - ts) * rate)
tokens = tokens - n
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)
if tokens >= 0 then return '0' end
return tostring(-tokens / rate)
"""


class MediaIOPacer:
    """Singleton pacer. Use module-level :func:`get_pacer`."""

    KEY = "media_io_bucket"

    def __init__(self) -> None:
        self._redis: Optional[Any] = None
        self._script_sha: Optional[str] = None
        self._init_lock = asyncio.Lock()
        self._warned = False
        self.rate = float(os.getenv("MEDIA_IO_BYTES_PER_SEC", str(8_000_000)))   # 8 MB/s
        self.burst = float(os.getenv("MEDIA_IO_BURST_BYTES", str(self.rate * 4)))  # 4s burst
        # Never sleep longer than this in one acquire (guards misconfiguration).
        self.max_sleep = float(os.getenv("MEDIA_IO_MAX_SLEEP", "30"))

    async def _client(self) -> Optional[Any]:
        if self._redis is not None:
            return self._redis
        async with self._init_lock:
            if self._redis is not None:
                return self._redis
            url = os.getenv("REDIS_URL", "")
            if not url:
                host = os.getenv("REDIS_HOST", "redis")
                pw = os.getenv("REDIS_PASSWORD", "")
                auth = f":{pw}@" if pw else ""
                url = f"redis://{auth}{host}:6379/0"
            try:
                import redis.asyncio as aioredis
                client = aioredis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
                self._script_sha = await client.script_load(_LUA)
                self._redis = client
            except Exception as e:
                if not self._warned:
                    logger.warning("io_pacer: Redis unavailable, pacing disabled (fail-open): %s", e)
                    self._warned = True
                return None
        return self._redis

    async def acquire(self, nbytes: int) -> None:
        """Block until `nbytes` fit under the global rate. Fail-open on any error."""
        if not _enabled() or nbytes <= 0:
            return
        client = await self._client()
        if client is None:
            return  # fail-open
        try:
            res = await client.evalsha(
                self._script_sha, 1, self.KEY, self.rate, self.burst, nbytes
            )
            wait = float(res)
        except Exception as e:
            # NOSCRIPT after a Redis restart, connection blip, etc. → fail-open,
            # and drop the cached client so the next call reconnects + reloads.
            logger.debug("io_pacer: acquire failed, fail-open: %s", e)
            self._redis = None
            self._script_sha = None
            return
        if wait > 0:
            await asyncio.sleep(min(wait, self.max_sleep))


_PACER: Optional[MediaIOPacer] = None


def get_pacer() -> MediaIOPacer:
    global _PACER
    if _PACER is None:
        _PACER = MediaIOPacer()
    return _PACER


__all__ = ["MediaIOPacer", "get_pacer"]
