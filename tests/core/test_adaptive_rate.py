"""Tests for src.core.adaptive_rate.

Coverage targets (from task spec):
  1. AST parse — implicit (import success).
  2. Per-account isolation — Account A cooldown does NOT affect Account B.
  3. Redis state survives restart — write events, simulate restart, verify
     bucket state preserved.
  4. AIMD math — 10 successes grow multiplier, 3 429s drop it.
  5. Token-bucket basic acquire/refill correctness.
  6. Emergency cooldown blocks acquire.
  7. Circuit breaker trips after N consecutive failures.
  8. Telemetry hook receives metric on each acquire.
  9. Backward-compat: old human_rate_limiter still importable & untouched.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.core.adaptive_rate import (
    AdaptiveRateLimiter,
    CircuitOpenError,
    RateMetric,
)


# ---------- helpers ---------------------------------------------------------


def make_limiter(**overrides):
    defaults = dict(
        base_rate=10.0,             # 10 tok/s — fast for tests
        min_rate=1.0,
        max_rate=50.0,
        capacity=5.0,
        additive_increase=1.0,      # big steps so AIMD math is observable
        multiplicative_decrease=0.5,
        emergency_cooldown_s=2.0,
        failure_threshold=3,
        recovery_timeout_s=1.0,
    )
    defaults.update(overrides)
    return AdaptiveRateLimiter(**defaults)


# ---------- 1. import / construction ---------------------------------------


def test_constructor_validates_inputs():
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(base_rate=0.0)
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(base_rate=1, min_rate=2, max_rate=10)
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(multiplicative_decrease=1.5)
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(failure_threshold=0)


def test_constructor_defaults_ok():
    rl = AdaptiveRateLimiter()
    assert rl.base_rate > 0
    assert rl.min_rate <= rl.base_rate <= rl.max_rate


# ---------- 2. per-account isolation (CRITICAL) -----------------------------


@pytest.mark.asyncio
async def test_per_account_isolation_cooldown():
    """Cooldown on account A must NOT block account B on same domain."""
    rl = make_limiter()

    # Trigger emergency cooldown on account A only
    rl.trigger_emergency_cooldown("instagram.com", "account_A", duration_s=10.0)

    assert rl.is_in_cooldown("instagram.com", "account_A") is True
    assert rl.is_in_cooldown("instagram.com", "account_B") is False

    # Account B must acquire immediately
    t0 = time.monotonic()
    await asyncio.wait_for(
        rl.acquire("instagram.com", "account_B"),
        timeout=1.0,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"Account B should not be throttled, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_per_account_isolation_failures():
    """429 on account A drops A's multiplier but not B's."""
    rl = make_limiter()

    # Prime both buckets
    await rl.acquire("tiktok.com", "tt_acc1")
    await rl.acquire("tiktok.com", "tt_acc2")

    # Hammer 429s on acc1
    for _ in range(3):
        rl.record_failure("tiktok.com", "tt_acc1", status_code=429)

    s1 = rl.get_stats("tiktok.com", "tt_acc1")
    s2 = rl.get_stats("tiktok.com", "tt_acc2")
    assert s1["aimd_multiplier"] < 1.0, "acc1 multiplier should be reduced"
    assert s2["aimd_multiplier"] == 1.0, "acc2 must not be affected"


# ---------- 3. AIMD math ----------------------------------------------------


@pytest.mark.asyncio
async def test_aimd_increase_then_decrease():
    """10 successes increase multiplier, then 3x 429 decrease it."""
    rl = make_limiter(
        additive_increase=0.5,         # rate += 0.5 per success
        multiplicative_decrease=0.5,   # rate /= 2 on 429
    )
    domain, account = "lemon8.com", "l8_acc1"

    await rl.acquire(domain, account)
    initial_mult = rl.get_stats(domain, account)["aimd_multiplier"]
    assert initial_mult == 1.0

    for _ in range(10):
        rl.record_success(domain, account)

    grown_mult = rl.get_stats(domain, account)["aimd_multiplier"]
    assert grown_mult > initial_mult, (
        f"Multiplier should grow after 10 successes; was {initial_mult}, now {grown_mult}"
    )

    # Now 3x 429
    for _ in range(3):
        rl.record_failure(domain, account, status_code=429)

    dropped_mult = rl.get_stats(domain, account)["aimd_multiplier"]
    expected = grown_mult * (0.5 ** 3)
    # Bounded by min_rate / base_rate
    floor = rl.min_rate / rl.base_rate
    assert dropped_mult == pytest.approx(max(expected, floor), rel=1e-6), (
        f"Expected ~{expected} (or floor {floor}); got {dropped_mult}"
    )


def test_aimd_floor_min_rate():
    """Multiplier never goes below min_rate / base_rate."""
    rl = make_limiter()
    # trigger_emergency_cooldown(0.0) is a cheap way to lazily create a bucket
    rl.trigger_emergency_cooldown("d", "a", duration_s=0.0)
    for _ in range(50):
        rl.record_failure("d", "a", status_code=429)
    mult = rl.get_stats("d", "a")["aimd_multiplier"]
    assert mult >= rl.min_rate / rl.base_rate - 1e-9


def test_aimd_ceiling_max_rate():
    rl = make_limiter()
    rl.trigger_emergency_cooldown("d", "a", duration_s=0.0)  # init bucket
    for _ in range(1000):
        rl.record_success("d", "a")
    mult = rl.get_stats("d", "a")["aimd_multiplier"]
    assert mult <= rl.max_rate / rl.base_rate + 1e-9


# ---------- 4. Token-bucket basics -----------------------------------------


@pytest.mark.asyncio
async def test_acquire_blocks_when_empty():
    """Bucket drains -> next acquire must wait."""
    rl = make_limiter(base_rate=2.0, capacity=2.0)  # 2 tok/s, cap 2

    # Drain the bucket — first 2 are free
    await rl.acquire("d", "a")
    await rl.acquire("d", "a")

    t0 = time.monotonic()
    await asyncio.wait_for(rl.acquire("d", "a"), timeout=2.0)
    elapsed = time.monotonic() - t0
    # Need 1 more token at 2 tok/s -> ~0.5s
    assert 0.3 <= elapsed <= 1.0, f"Expected ~0.5s wait, got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_acquire_weight_too_large_raises():
    rl = make_limiter(capacity=5.0)
    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        await rl.acquire("d", "a", weight=10)


@pytest.mark.asyncio
async def test_acquire_weight_consumes_n_tokens():
    rl = make_limiter(base_rate=1.0, capacity=5.0)
    await rl.acquire("d", "a", weight=3)
    stats = rl.get_stats("d", "a")
    assert stats["tokens"] == pytest.approx(2.0, abs=0.1)


# ---------- 5. Emergency cooldown ------------------------------------------


@pytest.mark.asyncio
async def test_emergency_cooldown_blocks_then_clears():
    rl = make_limiter()
    rl.trigger_emergency_cooldown("d", "a", duration_s=0.5)
    t0 = time.monotonic()
    await rl.acquire("d", "a")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.4, f"Cooldown should have blocked ~0.5s; got {elapsed:.2f}s"


def test_emergency_cooldown_403_status():
    rl = make_limiter()
    rl.trigger_emergency_cooldown("d", "a", duration_s=0.0)  # init
    rl.record_failure("d", "a", status_code=403)
    assert rl.is_in_cooldown("d", "a") is True


# ---------- 6. Circuit breaker trip ----------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_n_failures():
    rl = make_limiter(failure_threshold=3, recovery_timeout_s=10.0)
    await rl.acquire("d", "a")  # init
    for _ in range(3):
        rl.record_failure("d", "a", status_code=500)

    stats = rl.get_stats("d", "a")
    assert stats["circuit_state"] == "open"

    # Next acquire raises CircuitOpenError fast
    with pytest.raises(CircuitOpenError):
        await asyncio.wait_for(rl.acquire("d", "a"), timeout=0.5)


# ---------- 7. Telemetry ----------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_hook_receives_metric():
    metrics: list[RateMetric] = []
    rl = make_limiter(telemetry=metrics.append)

    await rl.acquire("d", "a", weight=2)
    await rl.acquire("d", "a")

    assert len(metrics) == 2
    m = metrics[0]
    assert m.domain == "d"
    assert m.account == "a"
    assert m.weight == 2
    assert m.tokens_remaining >= 0
    assert m.current_rate > 0
    assert m.aimd_multiplier == 1.0
    assert m.latency_to_acquire_s >= 0


@pytest.mark.asyncio
async def test_telemetry_hook_failure_does_not_break_acquire():
    def bad_hook(_metric):
        raise RuntimeError("boom")

    rl = make_limiter(telemetry=bad_hook)
    # Should NOT raise
    await rl.acquire("d", "a")


# ---------- 8. Redis persistence (fakeredis) -------------------------------


@pytest.mark.asyncio
async def test_redis_persistence_survives_restart():
    """5 acquire events, restart limiter, verify state preserved."""
    fakeredis = pytest.importorskip("fakeredis")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    rl1 = make_limiter(redis_client=redis, capacity=10.0)
    for _ in range(5):
        await rl1.acquire("instagram.com", "ig_acc1")
    # Drive the AIMD multiplier away from 1.0 so we have something to verify
    for _ in range(10):
        rl1.record_success("instagram.com", "ig_acc1")
    saved_mult = rl1.get_stats("instagram.com", "ig_acc1")["aimd_multiplier"]
    saved_tokens = rl1.get_stats("instagram.com", "ig_acc1")["tokens"]
    saved_acquires = rl1.get_stats("instagram.com", "ig_acc1")["total_acquires"]
    await rl1.flush_to_redis()

    # Simulate process restart: brand-new limiter, same Redis
    rl2 = make_limiter(redis_client=redis, capacity=10.0)
    # First acquire should auto-load state from Redis
    await rl2.acquire("instagram.com", "ig_acc1")

    stats = rl2.get_stats("instagram.com", "ig_acc1")
    assert stats["aimd_multiplier"] == pytest.approx(saved_mult, rel=1e-3)
    # tokens may have refilled slightly between save & load — within capacity
    assert stats["tokens"] <= stats["capacity"]
    # total_acquires preserved + 1 for the load-acquire
    assert stats["total_acquires"] == saved_acquires + 1


@pytest.mark.asyncio
async def test_redis_unavailable_does_not_break():
    """A flaky/missing Redis must NOT crash acquire."""
    class FakeBrokenRedis:
        async def hset(self, *_a, **_k):
            raise ConnectionError("redis down")
        async def hgetall(self, *_a, **_k):
            raise ConnectionError("redis down")
        async def expire(self, *_a, **_k):
            raise ConnectionError("redis down")

    rl = make_limiter(redis_client=FakeBrokenRedis())
    # Should not raise
    await rl.acquire("d", "a")
    assert rl.get_stats("d", "a")["tracked"] is True


# ---------- 9. Backward compat ---------------------------------------------


def test_legacy_human_rate_limiter_still_importable():
    """Wave 0 must NOT modify human_rate_limiter; verify still works."""
    from src.core.human_rate_limiter import HumanLikeRateLimiter, OperationType
    rl = HumanLikeRateLimiter()
    rl.trigger_emergency_cooldown("instagram.com", account="acc1")
    assert rl.is_in_cooldown("instagram.com", account="acc1")
    # Sibling account NOT affected
    assert not rl.is_in_cooldown("instagram.com", account="acc2")


def test_reset_clears_state():
    rl = make_limiter()
    rl.trigger_emergency_cooldown("d", "a", duration_s=10.0)
    rl.trigger_emergency_cooldown("d", "b", duration_s=10.0)
    rl.reset("d", "a")
    assert rl.is_in_cooldown("d", "a") is False
    assert rl.is_in_cooldown("d", "b") is True
    rl.reset()
    assert rl.is_in_cooldown("d", "b") is False


def test_key_validation():
    rl = make_limiter()
    with pytest.raises(ValueError):
        rl.get_stats("", "acc")
    with pytest.raises(ValueError):
        rl.get_stats("d", "")
