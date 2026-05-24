"""Global token-bucket rate limiter for Telegram API requests."""

import asyncio
import random
import time


class RateLimiter:
    """Token-bucket rate limiter shared across all TelegramClientManager instances.

    Enforces a global budget of `rate` tokens/second. Each acquire() call
    consumes one token. If the bucket is empty the caller is suspended until
    enough tokens have accumulated.

    FloodWait support: set_flood_wait(account_id, seconds) blocks all
    acquire(account_id) calls for at least seconds + 10 seconds.
    """

    def __init__(self, rate: float = 30.0) -> None:
        """
        Args:
            rate: tokens per second (default 30 = Telegram global limit per IP)
        """
        self._rate: float = rate
        self._tokens: float = rate          # start full
        self._last_refill: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._flood_wait_until: dict[int, float] = {}  # account_id → monotonic deadline

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens proportional to elapsed time since last refill, capped at _rate."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
        self._last_refill = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, account_id: int | None = None) -> None:
        """Await until one token is available, then consume it.

        If account_id is currently under a FloodWait, sleeps until the
        deadline before attempting to acquire a token.

        The acquire loop retries after sleeping because another coroutine
        may have consumed the token in the meantime.
        """
        # 1. Honour per-account flood wait
        if account_id is not None:
            deadline = self._flood_wait_until.get(account_id)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)

        # 2. Token-bucket acquire loop
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Bucket empty — compute how long until one token is ready
                sleep_time = (1.0 - self._tokens) / self._rate

            # Sleep outside the lock so other coroutines can proceed
            await asyncio.sleep(sleep_time)
            # After waking, loop back and re-check under the lock; another
            # coroutine may have consumed the token while we slept.

    def set_flood_wait(self, account_id: int, seconds: int) -> None:
        """Record that account_id must not make requests for seconds + 10 s."""
        self._flood_wait_until[account_id] = time.monotonic() + seconds + 10

    def clear_flood_wait(self, account_id: int) -> None:
        """Remove the flood-wait entry for account_id."""
        self._flood_wait_until.pop(account_id, None)

    def jitter_sleep(self, base: float) -> float:
        """Return base + random.uniform(0, base * 0.3).

        Usage::

            await asyncio.sleep(rate_limiter.jitter_sleep(base))
        """
        return base + random.uniform(0, base * 0.3)
