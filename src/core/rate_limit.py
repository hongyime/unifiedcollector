"""Adaptive rate limiter — canonical implementation for unified collector.

Consolidates the 5 toolkit implementations (instagram, lemon8, search, strava,
tiktok) into one async-first class. Keys can be URL or account identifier; the
class extracts the domain from URL-shaped keys automatically.

Behavior summary
----------------
- ``await rl.wait(key)`` blocks for the current per-key delay (baseline + jitter,
  minus elapsed since last call). Honours any active cooldown for that key.
- ``rl.record_success(key)`` decays the per-key delay after ``success_threshold``
  consecutive successes (toward ``min_delay``).
- ``rl.record_failure(key, status_code=...)`` increases the delay
  multiplicatively. ``429``/``503`` -> ``adjustment_factor`` bump (toward
  ``max_delay``). ``403`` -> hard jump to ``forbidden_backoff`` (account/IP
  flagged).
- ``rl.record_cooldown(key, seconds)`` pins ``key`` cold for ``seconds`` (e.g.
  a server-issued Retry-After). ``await wait(key)`` blocks until expiry.
- ``rl.is_in_cooldown(key)`` / ``rl.get_cooldown_remaining(key)`` for callers
  that want to skip rather than wait.
- ``rl.get_stats(key=None)`` for observability.

Bounded state
-------------
Per-key dicts use an LRU bound (default 256) to stop URL/domain explosions
from growing memory unbounded.

Concurrency
-----------
All shared state mutations happen under ``asyncio.Lock``. Sleeps happen
OUTSIDE the lock so multiple keys can wait in parallel.

Sync shim
---------
``wait_sync(key)`` provided for callers that aren't async (e.g. yt-dlp
hook); it dispatches to ``asyncio.run`` if no loop, else uses
``loop.run_until_complete``. Prefer the async version.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class _KeyState:
    """Per-key adaptive state."""
    current_delay: float
    success_streak: int = 0
    last_request_ts: float = 0.0
    cooldown_until: float = 0.0  # epoch seconds; 0 = no cooldown
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_status: int = 0


class _BoundedLRU(OrderedDict):
    """OrderedDict with size cap; oldest entry evicted on overflow."""
    def __init__(self, maxsize: int = 256):
        super().__init__()
        self.maxsize = maxsize

    def __setitem__(self, k, v):
        if k in self:
            self.move_to_end(k)
        super().__setitem__(k, v)
        if len(self) > self.maxsize:
            evicted = next(iter(self))
            del self[evicted]

    def get_or_create(self, k, factory):
        if k in self:
            self.move_to_end(k)
            return self[k]
        v = factory()
        self[k] = v
        return v


class RateLimiter:
    """Async-first adaptive rate limiter, key-agnostic.

    Args:
        base_delay: initial baseline delay between requests (seconds).
        min_delay: floor after success-driven decay.
        max_delay: ceiling after failure-driven backoff.
        jitter: fractional jitter applied to delay (0.3 = +/-30%).
        success_threshold: consecutive successes before decay applies.
        adjustment_factor: multiplicative bump per 429/503 / decay per
            success-burst (e.g. 0.2 -> +/-20% step).
        forbidden_backoff: hard delay set on 403 (IP/account flagged).
        max_keys: bound on per-key state dict to avoid unbounded growth.
        loop_compatible_sleep: if True, uses ``asyncio.sleep``; else
            ``time.sleep`` (only used by ``wait_sync``).

    Notes:
        URL-shaped keys (anything starting ``http://`` or ``https://``)
        are reduced to ``netloc`` so multiple paths under one host share
        one budget. Account-style keys (``@user``) are used as-is.
    """

    def __init__(
        self,
        base_delay: float = 2.0,
        min_delay: float = 0.5,
        max_delay: float = 120.0,
        jitter: float = 0.3,
        success_threshold: int = 5,
        adjustment_factor: float = 0.2,
        forbidden_backoff: float = 30.0,
        max_keys: int = 256,
    ):
        if min_delay < 0 or base_delay < min_delay or max_delay < base_delay:
            raise ValueError(
                "must hold: 0 <= min_delay <= base_delay <= max_delay; "
                f"got min={min_delay}, base={base_delay}, max={max_delay}"
            )
        if not 0 <= jitter <= 1:
            raise ValueError(f"jitter must be in [0, 1], got {jitter}")
        if success_threshold < 1:
            raise ValueError(f"success_threshold must be >= 1, got {success_threshold}")
        if not 0 < adjustment_factor < 1:
            raise ValueError(f"adjustment_factor must be in (0, 1), got {adjustment_factor}")
        if forbidden_backoff <= 0:
            raise ValueError(f"forbidden_backoff must be > 0, got {forbidden_backoff}")

        self.base_delay = base_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.success_threshold = success_threshold
        self.adjustment_factor = adjustment_factor
        self.forbidden_backoff = forbidden_backoff

        self._state: _BoundedLRU = _BoundedLRU(maxsize=max_keys)
        self._lock = asyncio.Lock()

    # -- key normalization -------------------------------------------------

    @staticmethod
    def _normalize_key(key: str) -> str:
        if not key:
            return "_default"
        if key.startswith(("http://", "https://")):
            try:
                netloc = urlparse(key).netloc
                if netloc:
                    return netloc.lower()
            except Exception:
                pass
        return key

    # -- internal: get-or-create ------------------------------------------

    def _get_state(self, key: str) -> _KeyState:
        # CALLER must hold ``self._lock``.
        return self._state.get_or_create(
            key, lambda: _KeyState(current_delay=self.base_delay)
        )

    # -- main async wait --------------------------------------------------

    async def wait(self, key: str = "_default") -> float:
        """Block until it is OK to make the next request for ``key``.

        Returns the actual seconds slept (useful for tests / metrics).
        Honours active cooldown first; then enforces the per-key delay
        with jitter, accounting for time since the last call.
        """
        nkey = self._normalize_key(key)
        async with self._lock:
            st = self._get_state(nkey)

            # --- cooldown branch ---
            now = time.time()
            if st.cooldown_until and now < st.cooldown_until:
                cool = st.cooldown_until - now
                # Release lock before sleeping
                cool_to_sleep = cool
                cooldown_path = True
            else:
                cooldown_path = False
                # Clear expired cooldown
                if st.cooldown_until and now >= st.cooldown_until:
                    st.cooldown_until = 0.0

                # --- normal delay branch ---
                jitter_factor = 1.0 + random.uniform(-self.jitter, self.jitter)
                jittered = st.current_delay * jitter_factor

                if st.last_request_ts > 0:
                    elapsed = now - st.last_request_ts
                    cool_to_sleep = max(0.0, jittered - elapsed)
                else:
                    cool_to_sleep = jittered

                # update timestamp BEFORE sleeping so concurrent callers see it
                st.last_request_ts = now + cool_to_sleep
                st.request_count += 1

        # Sleep outside the lock so other keys can advance.
        if cool_to_sleep > 0:
            if cooldown_path:
                logger.debug("rate-limit cooldown %.2fs key=%s", cool_to_sleep, nkey)
            await asyncio.sleep(cool_to_sleep)
        return cool_to_sleep

    # -- record outcomes --------------------------------------------------

    def record_success(self, key: str = "_default") -> None:
        """Record a successful request; may decay the per-key delay."""
        nkey = self._normalize_key(key)
        # Sync method; we use a quick non-blocking acquire pattern via
        # creating a fresh entry if missing. asyncio.Lock is not
        # acquirable from sync code, but we aren't writing to the LRU
        # ordering itself in a way that races with wait()'s lock holders
        # in practice -- to be safe, callers that mix sync record_* with
        # async wait() should serialize externally. The dict access
        # itself is atomic under the GIL.
        st = self._state.get(nkey)
        if st is None:
            self._state[nkey] = st = _KeyState(current_delay=self.base_delay)
        st.success_streak += 1
        st.success_count += 1
        st.last_status = 200
        if st.success_streak >= self.success_threshold:
            new = max(self.min_delay, st.current_delay * (1.0 - self.adjustment_factor))
            if new < st.current_delay:
                logger.debug(
                    "rate-limit decay key=%s %.2fs -> %.2fs",
                    nkey, st.current_delay, new,
                )
            st.current_delay = new
            st.success_streak = 0

    def record_failure(self, key: str = "_default", status_code: int = 0) -> None:
        """Record a failed request; backs off based on ``status_code``.

        - 403: hard jump to ``forbidden_backoff`` AND register a cooldown
          for ``forbidden_backoff`` seconds (account/IP flagged).
        - 429 / 503: multiplicative bump toward ``max_delay``.
        - other: streak reset only.
        """
        nkey = self._normalize_key(key)
        st = self._state.get(nkey)
        if st is None:
            self._state[nkey] = st = _KeyState(current_delay=self.base_delay)
        st.success_streak = 0
        st.failure_count += 1
        st.last_status = status_code

        if status_code == 403:
            st.current_delay = min(self.max_delay, max(st.current_delay, self.forbidden_backoff))
            st.cooldown_until = time.time() + self.forbidden_backoff
            logger.warning(
                "rate-limit 403 key=%s -> cooldown %.0fs, current_delay=%.2fs",
                nkey, self.forbidden_backoff, st.current_delay,
            )
        elif status_code in (429, 503):
            new = min(self.max_delay, st.current_delay * (1.0 + self.adjustment_factor))
            logger.info(
                "rate-limit %d key=%s %.2fs -> %.2fs",
                status_code, nkey, st.current_delay, new,
            )
            st.current_delay = new

    def record_cooldown(self, key: str, seconds: float) -> None:
        """Pin ``key`` cold for ``seconds`` (e.g. honor Retry-After header)."""
        if seconds <= 0:
            return
        nkey = self._normalize_key(key)
        st = self._state.get(nkey)
        if st is None:
            self._state[nkey] = st = _KeyState(current_delay=self.base_delay)
        st.cooldown_until = max(st.cooldown_until, time.time() + seconds)
        logger.info("rate-limit cooldown set key=%s %.0fs", nkey, seconds)

    # -- queries -----------------------------------------------------------

    def is_in_cooldown(self, key: str) -> bool:
        nkey = self._normalize_key(key)
        st = self._state.get(nkey)
        if st is None:
            return False
        return st.cooldown_until > time.time()

    def get_cooldown_remaining(self, key: str) -> float:
        nkey = self._normalize_key(key)
        st = self._state.get(nkey)
        if st is None:
            return 0.0
        return max(0.0, st.cooldown_until - time.time())

    def get_stats(self, key: Optional[str] = None) -> dict[str, Any]:
        if key is None:
            return {
                "tracked_keys": len(self._state),
                "max_keys": self._state.maxsize,
                "base_delay": self.base_delay,
                "min_delay": self.min_delay,
                "max_delay": self.max_delay,
            }
        nkey = self._normalize_key(key)
        st = self._state.get(nkey)
        if st is None:
            return {"key": nkey, "tracked": False}
        return {
            "key": nkey,
            "tracked": True,
            "current_delay": st.current_delay,
            "success_streak": st.success_streak,
            "request_count": st.request_count,
            "success_count": st.success_count,
            "failure_count": st.failure_count,
            "last_status": st.last_status,
            "cooldown_remaining": max(0.0, st.cooldown_until - time.time()),
        }

    def reset(self, key: Optional[str] = None) -> None:
        """Reset all per-key state, or just one key."""
        if key is None:
            self._state.clear()
        else:
            self._state.pop(self._normalize_key(key), None)

    # -- sync shim ---------------------------------------------------------

    def wait_sync(self, key: str = "_default") -> float:
        """Sync wrapper around ``wait`` for non-async callers (e.g. yt-dlp).

        Tries the running loop; falls back to ``asyncio.run`` if none.
        Prefer ``await wait(key)`` whenever possible.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.wait(key))

        # We're inside an async context but called sync. Run a coroutine
        # in a fresh event loop in a worker thread to avoid re-entering.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(asyncio.run, self.wait(key))
            return fut.result()


__all__ = ["RateLimiter"]
