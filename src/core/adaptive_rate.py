"""Per-account adaptive rate limiter (Wave 0, cross-cutting).

This module adds a *per-account* token-bucket + AIMD layer on top of the
existing ``rate_limit.RateLimiter`` / ``human_rate_limiter.HumanLikeRateLimiter``.
Five collectors will consume it: instagram, lemon8, search, strava, tiktok.

Why per-account?
----------------
The legacy ``HumanLikeRateLimiter`` keys cooldowns on ``domain`` only, so a
single 429 against IG account A would freeze accounts B..E too. This module
keys on ``(domain, account)`` and only that bucket is throttled when the
account is the one being rate-limited.

What it does NOT do:
- Outbound message sending (this is read-only ingest).
- Account quota tracking — that's ``account_quota.py`` (Batch 2 Agent A).
- Replace ``human_rate_limiter`` — that module stays in place; this one
  composes / extends it. Migration of consumers is Wave 2 work.

Design
------
1. **Token bucket per (domain, account)** with adaptive ``rate`` (tokens/s).
2. **AIMD** on the rate:
     - ``record_success`` -> ``rate += additive_increase`` (capped at ``max_rate``).
     - ``record_failure(429|403|5xx)`` -> ``rate *= multiplicative_decrease``
       (floored at ``min_rate``).
3. **Persistence** (optional): bucket state + AIMD multiplier serialized to
   Redis with key prefix ``adaptive_rate:`` so state survives a process
   restart.
4. **Circuit breaker**: after ``failure_threshold`` consecutive failures on a
   bucket, an isolated ``CircuitBreaker`` opens and ``acquire`` raises
   ``CircuitOpenError`` until recovery — we hand off, don't reinvent.
5. **Telemetry**: optional callback receives ``RateMetric`` per acquire.
6. **Emergency cooldown**: hard freeze on a single ``(domain, account)``.

Usage
-----

    rl = AdaptiveRateLimiter()
    await rl.acquire("instagram.com", "ig_account_1")  # blocks until allowed
    try:
        result = await fetch(...)
        rl.record_success("instagram.com", "ig_account_1")
    except RateLimitedError as e:
        rl.record_failure("instagram.com", "ig_account_1", status_code=429)

Each ``acquire`` may be decorated with ``weight=N`` for heavier ops; weight
consumes more tokens.

Concurrency model
-----------------
- ``asyncio.Lock`` per bucket — ``acquire`` releases the lock before sleeping
  so multiple distinct buckets advance in parallel.
- Per-bucket circuit-breaker has its own lock (inside CircuitBreaker).

Author: Hermes (Wave 0 batch 1 agent C)
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from .circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

# ---------- types -----------------------------------------------------------

BucketKey = tuple[str, str]  # (domain, account)
TelemetryHook = Callable[["RateMetric"], None]
RedisLike = Any  # redis.asyncio.Redis — kept Any to avoid hard import dependency


@dataclass
class RateMetric:
    """Snapshot emitted on every successful ``acquire`` call.

    Consumers (e.g. Prometheus, Datadog) wire a ``TelemetryHook`` and
    consume these. Hook MUST NOT raise — wrapped in try/except by the
    limiter, but cheap-failing keeps the hot path fast.
    """
    domain: str
    account: str
    weight: int
    latency_to_acquire_s: float
    tokens_remaining: float
    current_rate: float
    aimd_multiplier: float


@dataclass
class _Bucket:
    """Mutable per-(domain, account) state.

    Token-bucket invariants:
        tokens in [0, capacity]
        rate = base_rate * aimd_multiplier
        rate in [min_rate, max_rate]

    ``cooldown_until`` is a monotonic deadline; ``acquire`` blocks until then
    regardless of token state.
    """
    tokens: float
    capacity: float
    base_rate: float            # nominal tokens/s before AIMD scaling
    aimd_multiplier: float      # >0; scales base_rate
    last_refill: float          # monotonic
    cooldown_until: float = 0.0  # monotonic deadline
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_acquires: int = 0
    total_failures: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    # ---------- serialization ----------
    def to_dict(self) -> dict[str, float]:
        # Persist *wall* time, not monotonic — different process boots have
        # different monotonic clocks. We translate on load.
        now_mono = time.monotonic()
        cooldown_remaining = max(0.0, self.cooldown_until - now_mono)
        return {
            "tokens": self.tokens,
            "capacity": self.capacity,
            "base_rate": self.base_rate,
            "aimd_multiplier": self.aimd_multiplier,
            "cooldown_remaining": cooldown_remaining,
            "consecutive_failures": float(self.consecutive_failures),
            "consecutive_successes": float(self.consecutive_successes),
            "total_acquires": float(self.total_acquires),
            "total_failures": float(self.total_failures),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_Bucket":
        now = time.monotonic()
        cooldown_remaining = float(d.get("cooldown_remaining", 0.0) or 0.0)
        return cls(
            tokens=float(d["tokens"]),
            capacity=float(d["capacity"]),
            base_rate=float(d["base_rate"]),
            aimd_multiplier=float(d["aimd_multiplier"]),
            last_refill=now,
            cooldown_until=now + cooldown_remaining if cooldown_remaining > 0 else 0.0,
            consecutive_failures=int(d.get("consecutive_failures", 0) or 0),
            consecutive_successes=int(d.get("consecutive_successes", 0) or 0),
            total_acquires=int(d.get("total_acquires", 0) or 0),
            total_failures=int(d.get("total_failures", 0) or 0),
        )

    @property
    def current_rate(self) -> float:
        return self.base_rate * self.aimd_multiplier


# ---------- main class ------------------------------------------------------


class AdaptiveRateLimiter:
    """Per-account token-bucket + AIMD limiter, async-first.

    All public methods are safe to call concurrently. Per-bucket locking
    means one slow account doesn't block sibling accounts.
    """

    REDIS_KEY_PREFIX = "adaptive_rate:"

    def __init__(
        self,
        base_rate: float = 1.0,           # tokens/s nominal
        min_rate: float = 0.1,
        max_rate: float = 10.0,
        capacity: float = 5.0,            # bucket size; allows short bursts
        additive_increase: float = 0.05,  # AIMD: rate += this on each success
        multiplicative_decrease: float = 0.5,  # AIMD: rate *= this on failure
        emergency_cooldown_s: float = 900.0,  # 15 min default
        failure_threshold: int = 5,       # circuit-breaker trips after N consecutive
        recovery_timeout_s: float = 60.0,
        telemetry: Optional[TelemetryHook] = None,
        redis_client: Optional[RedisLike] = None,
        max_buckets: int = 1024,
    ):
        if not 0 < min_rate <= base_rate <= max_rate:
            raise ValueError(
                f"must hold 0 < min_rate <= base_rate <= max_rate; "
                f"got min={min_rate} base={base_rate} max={max_rate}"
            )
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity}")
        if additive_increase <= 0:
            raise ValueError(f"additive_increase must be > 0, got {additive_increase}")
        if not 0 < multiplicative_decrease < 1:
            raise ValueError(
                f"multiplicative_decrease must be in (0, 1), got {multiplicative_decrease}"
            )
        if emergency_cooldown_s <= 0:
            raise ValueError(f"emergency_cooldown_s must be > 0, got {emergency_cooldown_s}")
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold}")

        self.base_rate = base_rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.capacity = capacity
        self.additive_increase = additive_increase
        self.multiplicative_decrease = multiplicative_decrease
        self.emergency_cooldown_s = emergency_cooldown_s
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.telemetry = telemetry
        self.redis = redis_client
        self.max_buckets = max_buckets

        self._buckets: dict[BucketKey, _Bucket] = {}
        self._breakers: dict[BucketKey, CircuitBreaker] = {}
        self._buckets_lock = asyncio.Lock()  # guards _buckets/_breakers dicts

    # ---------- key helpers ----------

    @staticmethod
    def _key(domain: str, account: str) -> BucketKey:
        if not domain:
            raise ValueError("domain must be non-empty")
        if not account:
            raise ValueError("account must be non-empty (use HumanLikeRateLimiter for domain-only)")
        return (domain.lower(), account)

    @classmethod
    def _redis_key(cls, key: BucketKey) -> str:
        return f"{cls.REDIS_KEY_PREFIX}{key[0]}:{key[1]}"

    # ---------- bucket lifecycle ----------

    async def _get_or_create_bucket(self, key: BucketKey) -> _Bucket:
        # Fast path under per-instance dict lock
        async with self._buckets_lock:
            existing = self._buckets.get(key)
            if existing is not None:
                return existing

            # Try Redis first if configured
            bucket: Optional[_Bucket] = None
            if self.redis is not None:
                bucket = await self._load_from_redis(key)

            if bucket is None:
                bucket = _Bucket(
                    tokens=self.capacity,
                    capacity=self.capacity,
                    base_rate=self.base_rate,
                    aimd_multiplier=1.0,
                    last_refill=time.monotonic(),
                )

            self._buckets[key] = bucket
            self._breakers[key] = CircuitBreaker(
                name=f"adaptive_rate:{key[0]}:{key[1]}",
                failure_threshold=self.failure_threshold,
                recovery_timeout=self.recovery_timeout_s,
            )

            # Bound dict — drop oldest if over budget. Iter order = insertion.
            if len(self._buckets) > self.max_buckets:
                oldest = next(iter(self._buckets))
                if oldest != key:
                    self._buckets.pop(oldest, None)
                    self._breakers.pop(oldest, None)

            return bucket

    def _refill(self, b: _Bucket, now: Optional[float] = None) -> None:
        """Add tokens accumulated since last refill. CALLER holds b.lock."""
        if now is None:
            now = time.monotonic()
        elapsed = max(0.0, now - b.last_refill)
        if elapsed > 0:
            added = elapsed * b.current_rate
            b.tokens = min(b.capacity, b.tokens + added)
            b.last_refill = now

    # ---------- public API ----------

    async def acquire(self, domain: str, account: str, weight: int = 1) -> None:
        """Block until ``weight`` tokens are available for ``(domain, account)``.

        Honours any active emergency cooldown and the per-bucket circuit
        breaker. Raises ``CircuitOpenError`` if the breaker is OPEN.
        """
        if weight < 1:
            raise ValueError(f"weight must be >= 1, got {weight}")
        if weight > self.capacity:
            raise ValueError(
                f"weight={weight} exceeds bucket capacity={self.capacity}; "
                "increase capacity or reduce weight"
            )

        key = self._key(domain, account)
        bucket = await self._get_or_create_bucket(key)
        breaker = self._breakers[key]

        if breaker.state == CircuitBreaker.OPEN:
            # Re-check inside the breaker; raises CircuitOpenError if still open.
            # We use a no-op probe coroutine so the breaker handles HALF_OPEN
            # transition timing for us.
            async def _noop() -> None:
                return None
            await breaker.call(_noop)

        t0 = time.monotonic()
        # Loop: we may need multiple refill-then-sleep cycles when other
        # waiters drain the bucket between our refill and our consume.
        while True:
            # Hold lock only across the math; sleeping happens outside.
            async with bucket.lock:
                now = time.monotonic()

                # Cooldown branch — overrides everything
                if bucket.cooldown_until and now < bucket.cooldown_until:
                    sleep_for = bucket.cooldown_until - now
                elif bucket.cooldown_until and now >= bucket.cooldown_until:
                    bucket.cooldown_until = 0.0
                    sleep_for = 0.0
                else:
                    sleep_for = 0.0

                if sleep_for == 0.0:
                    self._refill(bucket, now)
                    if bucket.tokens >= weight:
                        bucket.tokens -= weight
                        bucket.total_acquires += 1
                        # Snapshot for telemetry
                        snapshot_tokens = bucket.tokens
                        snapshot_rate = bucket.current_rate
                        snapshot_aimd = bucket.aimd_multiplier
                        # Persist asynchronously (best-effort)
                        if self.redis is not None:
                            asyncio.create_task(self._persist(key, bucket))
                        elapsed = time.monotonic() - t0
                        self._emit_telemetry(
                            domain, account, weight,
                            elapsed, snapshot_tokens, snapshot_rate, snapshot_aimd,
                        )
                        return
                    # Need (weight - tokens) more tokens at current_rate.
                    deficit = weight - bucket.tokens
                    if bucket.current_rate <= 0:
                        # Pathological — should be impossible due to min_rate.
                        sleep_for = 1.0
                    else:
                        sleep_for = deficit / bucket.current_rate

            # Sleep OUTSIDE the lock so other (domain, account) tuples and
            # even other waiters on this bucket can advance.
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    def record_success(self, domain: str, account: str) -> None:
        """AIMD additive-increase. Schedule-safe sync method."""
        key = self._key(domain, account)
        bucket = self._buckets.get(key)
        if bucket is None:
            return  # nothing to do; will lazily init on next acquire
        # No await-able lock from sync; mutate is GIL-safe enough.
        bucket.consecutive_successes += 1
        bucket.consecutive_failures = 0
        new_mult = min(
            self.max_rate / bucket.base_rate,
            bucket.aimd_multiplier + (self.additive_increase / bucket.base_rate),
        )
        bucket.aimd_multiplier = max(
            self.min_rate / bucket.base_rate,
            new_mult,
        )

    def record_failure(self, domain: str, account: str, status_code: int = 0) -> None:
        """AIMD multiplicative-decrease for 429/403/5xx; otherwise streak reset.

        On 403 we additionally trigger the emergency cooldown — a 403 means
        the account is flagged and hammering will only entrench the ban.
        """
        key = self._key(domain, account)
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        bucket.consecutive_successes = 0

        # Decide if this status counts as backoff-worthy.
        is_backoff = status_code in (429, 403) or (500 <= status_code < 600)

        if is_backoff:
            bucket.consecutive_failures += 1
            bucket.total_failures += 1
            # AIMD: multiplicative decrease
            new_mult = max(
                self.min_rate / bucket.base_rate,
                bucket.aimd_multiplier * self.multiplicative_decrease,
            )
            bucket.aimd_multiplier = new_mult

            if status_code == 403:
                # 403 -> emergency cooldown immediately. Don't wait for streak.
                bucket.cooldown_until = time.monotonic() + self.emergency_cooldown_s
                logger.warning(
                    "adaptive_rate: 403 on %s -> emergency cooldown %.0fs (acct=%s)",
                    domain, self.emergency_cooldown_s, account,
                )

            # Drive the per-bucket circuit breaker. We can't hit ``call``
            # from here without re-entering an event loop, so we mutate state
            # via an internal failure record. Easier: just count consecutive
            # failures and trip the breaker manually.
            breaker = self._breakers.get(key)
            if breaker is not None and bucket.consecutive_failures >= self.failure_threshold:
                # Manual trip — the breaker's own counter is independent
                # because we don't actually wrap the upstream call. This
                # mirrors the outcome (state=OPEN with timer) without the
                # async machinery.
                if breaker._state != CircuitBreaker.OPEN:
                    breaker._state = CircuitBreaker.OPEN
                    breaker._opened_at = time.monotonic()
                    breaker._failure_count = bucket.consecutive_failures
                    logger.warning(
                        "adaptive_rate: circuit %r tripped (%d consecutive failures)",
                        breaker.name, bucket.consecutive_failures,
                    )

    def trigger_emergency_cooldown(
        self,
        domain: str,
        account: str,
        duration_s: Optional[float] = None,
    ) -> None:
        """Pin a single ``(domain, account)`` cold for ``duration_s`` seconds.

        If ``duration_s`` is None, uses the limiter default
        (``emergency_cooldown_s``). Other accounts on the same domain are
        UNAFFECTED — that is the whole point of this module.
        """
        key = self._key(domain, account)
        bucket = self._buckets.get(key)
        if bucket is None:
            # Lazily create one so caller's intent is honored even before
            # the first acquire.
            bucket = _Bucket(
                tokens=self.capacity,
                capacity=self.capacity,
                base_rate=self.base_rate,
                aimd_multiplier=1.0,
                last_refill=time.monotonic(),
            )
            self._buckets[key] = bucket
            self._breakers[key] = CircuitBreaker(
                name=f"adaptive_rate:{key[0]}:{key[1]}",
                failure_threshold=self.failure_threshold,
                recovery_timeout=self.recovery_timeout_s,
            )

        d = self.emergency_cooldown_s if duration_s is None else duration_s
        if d <= 0:
            # Caller explicitly asked for no cooldown; bucket is still created
            # above so that subsequent record_success/record_failure have
            # somewhere to mutate.
            return
        bucket.cooldown_until = max(bucket.cooldown_until, time.monotonic() + d)
        logger.warning(
            "adaptive_rate: emergency cooldown set for %s/%s = %.0fs",
            domain, account, d,
        )

    def is_in_cooldown(self, domain: str, account: str) -> bool:
        key = self._key(domain, account)
        bucket = self._buckets.get(key)
        if bucket is None:
            return False
        return bucket.cooldown_until > time.monotonic()

    def get_stats(self, domain: str, account: str) -> dict[str, Any]:
        key = self._key(domain, account)
        bucket = self._buckets.get(key)
        if bucket is None:
            return {"tracked": False, "domain": domain, "account": account}
        breaker = self._breakers.get(key)
        return {
            "tracked": True,
            "domain": domain,
            "account": account,
            "tokens": bucket.tokens,
            "capacity": bucket.capacity,
            "current_rate": bucket.current_rate,
            "aimd_multiplier": bucket.aimd_multiplier,
            "consecutive_failures": bucket.consecutive_failures,
            "consecutive_successes": bucket.consecutive_successes,
            "total_acquires": bucket.total_acquires,
            "total_failures": bucket.total_failures,
            "cooldown_remaining": max(0.0, bucket.cooldown_until - time.monotonic()),
            "circuit_state": breaker.state if breaker else None,
        }

    def reset(self, domain: Optional[str] = None, account: Optional[str] = None) -> None:
        """Clear state for one bucket or all buckets."""
        if domain is None and account is None:
            self._buckets.clear()
            self._breakers.clear()
            return
        if domain is None or account is None:
            raise ValueError("domain and account must both be provided, or both None")
        key = self._key(domain, account)
        self._buckets.pop(key, None)
        self._breakers.pop(key, None)

    # ---------- redis persistence ----------

    async def _persist(self, key: BucketKey, bucket: _Bucket) -> None:
        if self.redis is None:
            return
        try:
            payload = bucket.to_dict()
            # HSET expects flat str/str. Stringify floats.
            flat = {k: repr(v) for k, v in payload.items()}
            await self.redis.hset(self._redis_key(key), mapping=flat)
            # Auto-expire idle buckets after 7 days so Redis doesn't bloat.
            await self.redis.expire(self._redis_key(key), 7 * 24 * 3600)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug("adaptive_rate: redis persist failed for %s: %r", key, exc)

    async def _load_from_redis(self, key: BucketKey) -> Optional[_Bucket]:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.hgetall(self._redis_key(key))
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug("adaptive_rate: redis load failed for %s: %r", key, exc)
            return None
        if not raw:
            return None
        try:
            decoded: dict[str, Any] = {}
            for k, v in raw.items():
                # redis-py returns bytes unless decode_responses=True. Handle both.
                kk = k.decode() if isinstance(k, (bytes, bytearray)) else k
                vv = v.decode() if isinstance(v, (bytes, bytearray)) else v
                decoded[kk] = float(vv)
            return _Bucket.from_dict(decoded)
        except (ValueError, KeyError) as exc:
            logger.warning(
                "adaptive_rate: corrupt redis entry %s, ignoring: %r",
                self._redis_key(key), exc,
            )
            return None

    async def flush_to_redis(self) -> int:
        """Force-persist every in-memory bucket. Returns count written."""
        if self.redis is None:
            return 0
        n = 0
        for key, bucket in list(self._buckets.items()):
            await self._persist(key, bucket)
            n += 1
        return n

    # ---------- telemetry ----------

    def _emit_telemetry(
        self,
        domain: str, account: str, weight: int,
        latency: float, tokens_remaining: float,
        current_rate: float, aimd_multiplier: float,
    ) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry(RateMetric(
                domain=domain,
                account=account,
                weight=weight,
                latency_to_acquire_s=latency,
                tokens_remaining=tokens_remaining,
                current_rate=current_rate,
                aimd_multiplier=aimd_multiplier,
            ))
        except Exception as exc:  # pragma: no cover — telemetry must not break callers
            logger.debug("adaptive_rate: telemetry hook raised %r", exc)


__all__ = [
    "AdaptiveRateLimiter",
    "RateMetric",
    "CircuitOpenError",
]
