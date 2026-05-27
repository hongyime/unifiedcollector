"""Tests for src.core.account_quota.

Coverage targets:
  1. AST parse — implicit (import success).
  2. Backward compat — no config registered ⇒ has_quota always True, consume no-op.
  3. Daily limit enforcement.
  4. Weekly limit enforcement.
  5. Hourly limit enforcement (with bucket rollover).
  6. SGT day boundary (UTC midnight is NOT day boundary).
  7. ISO week computation.
  8. has_quota refuses on weight that would breach.
  9. consume increments counters monotonically.
 10. consume_strict raises on breach.
 11. QuotaConfig validation rejects negatives.
 12. Telemetry hook receives metric on consume.
 13. Concurrent consume across asyncio tasks doesn't lose increments
     (in-memory mode, single process).
 14. reset() clears state.
 15. Module singleton get/set + autouse reset between tests.

DB-backed tests (require ACCOUNT_QUOTA_TEST_DSN) cover:
 16. Persistence across "process restart" (drop + recreate tracker, re-read).
 17. Concurrent upsert across two trackers sharing the same DB pool — counts
     converge under ON CONFLICT.

Pure-unit tests (no DB) make up the bulk; they use the in-memory fallback
that's built into the tracker for ``pool=None``.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
import pytest_asyncio

from src.core.account_quota import (
    AccountQuotaTracker,
    QuotaConfig,
    QuotaExhaustedError,
    QuotaMetric,
    _sgt_day,
    _sgt_hour_bucket,
    _sgt_week_iso,
    get_default_tracker,
    set_default_tracker,
)


# ── module-singleton hygiene ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_default_tracker():
    set_default_tracker(None)
    yield
    set_default_tracker(None)


# ── helpers ─────────────────────────────────────────────────────────────────


class FrozenClock:
    """Stub clock returning a fixed UTC datetime; mutable for rollover tests."""
    def __init__(self, ts: datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self.ts = ts

    def __call__(self) -> datetime:
        return self.ts

    def advance(self, **kwargs) -> None:
        self.ts = self.ts + timedelta(**kwargs)


def make_tracker(**overrides) -> AccountQuotaTracker:
    defaults = {"pool": None, "cache_ttl_s": 0.0}
    defaults.update(overrides)
    return AccountQuotaTracker(**defaults)


# ── 1. construction / config validation ────────────────────────────────────


def test_quota_config_rejects_negative():
    with pytest.raises(ValueError):
        QuotaConfig(daily_limit=-1)
    with pytest.raises(ValueError):
        QuotaConfig(weekly_limit=-5)
    with pytest.raises(ValueError):
        QuotaConfig(hourly_limit=-3)


def test_quota_config_zero_axes_ok():
    # All-zero is permitted (no-op limits).
    c = QuotaConfig()
    assert c.daily_limit == 0
    assert c.weekly_limit == 0
    assert c.hourly_limit == 0


def test_tracker_rejects_bad_cache_ttl():
    with pytest.raises(ValueError):
        AccountQuotaTracker(cache_ttl_s=-1.0)


def test_register_rejects_empty_platform():
    t = make_tracker()
    with pytest.raises(ValueError):
        t.register("", QuotaConfig(daily_limit=10))


# ── 2. backward compat: no config ⇒ no-op ──────────────────────────────────


@pytest.mark.asyncio
async def test_no_config_has_quota_always_true():
    t = make_tracker()
    assert await t.has_quota("instagram", "acct1") is True
    # Even at huge weight
    assert await t.has_quota("instagram", "acct1", weight=10_000) is True


@pytest.mark.asyncio
async def test_no_config_consume_returns_none():
    t = make_tracker()
    metric = await t.consume("instagram", "acct1", weight=3)
    assert metric is None


# ── 3. daily limit enforcement ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_limit_exhaustion():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=5))

    for _ in range(5):
        assert await t.has_quota("ig", "a") is True
        await t.consume("ig", "a")

    # Next request would exceed
    assert await t.has_quota("ig", "a") is False


@pytest.mark.asyncio
async def test_daily_limit_weight_aware():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=10))
    await t.consume("ig", "a", weight=8)
    assert await t.has_quota("ig", "a", weight=2) is True
    assert await t.has_quota("ig", "a", weight=3) is False


@pytest.mark.asyncio
async def test_per_account_isolation():
    """Account A maxed out should not affect Account B."""
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=2))
    await t.consume("ig", "a"); await t.consume("ig", "a")
    assert await t.has_quota("ig", "a") is False
    assert await t.has_quota("ig", "b") is True


@pytest.mark.asyncio
async def test_per_platform_isolation():
    """Platform X exhaustion shouldn't affect platform Y for same acct."""
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=1))
    t.register("tt", QuotaConfig(daily_limit=1))
    await t.consume("ig", "a")
    assert await t.has_quota("ig", "a") is False
    assert await t.has_quota("tt", "a") is True


# ── 4. weekly limit ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_limit_within_one_day():
    t = make_tracker()
    t.register("ig", QuotaConfig(weekly_limit=3))
    for _ in range(3):
        await t.consume("ig", "a")
    assert await t.has_quota("ig", "a") is False


# ── 5. hourly limit + rollover ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hourly_limit_blocks_within_hour():
    clk = FrozenClock(datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc))  # 12:00 SGT
    t = make_tracker(clock=clk)
    t.register("gh", QuotaConfig(hourly_limit=2))
    await t.consume("gh", "a")
    await t.consume("gh", "a")
    assert await t.has_quota("gh", "a") is False


@pytest.mark.asyncio
async def test_hourly_limit_resets_on_bucket_rollover():
    clk = FrozenClock(datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc))  # 12:00 SGT
    t = make_tracker(clock=clk)
    t.register("gh", QuotaConfig(hourly_limit=2))
    await t.consume("gh", "a")
    await t.consume("gh", "a")
    assert await t.has_quota("gh", "a") is False
    # Roll into next hour bucket
    clk.advance(hours=1)
    assert await t.has_quota("gh", "a") is True
    await t.consume("gh", "a")
    usage = await t.get_usage("gh", "a")
    assert usage["requests_hour"] == 1


# ── 6. SGT day boundary (NOT UTC midnight) ────────────────────────────────


def test_sgt_day_at_utc_midnight_is_already_next_day_in_sgt():
    # 2026-05-26 00:00 UTC == 2026-05-26 08:00 SGT ⇒ day = 2026-05-26 SGT
    utc = datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc)
    assert _sgt_day(utc).isoformat() == "2026-05-26"
    # 2026-05-25 16:00 UTC == 2026-05-26 00:00 SGT ⇒ flips here
    utc2 = datetime(2026, 5, 25, 16, 0, tzinfo=timezone.utc)
    assert _sgt_day(utc2).isoformat() == "2026-05-26"
    # 2026-05-25 15:59 UTC == 2026-05-25 23:59 SGT ⇒ still 25
    utc3 = datetime(2026, 5, 25, 15, 59, tzinfo=timezone.utc)
    assert _sgt_day(utc3).isoformat() == "2026-05-25"


def test_sgt_week_iso_format():
    utc = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)
    s = _sgt_week_iso(utc)
    assert s.startswith("2026-W")
    assert len(s) == 8


def test_sgt_hour_bucket_format():
    utc = datetime(2026, 5, 26, 4, 30, tzinfo=timezone.utc)  # 12:30 SGT
    assert _sgt_hour_bucket(utc) == "2026-05-26 12:00"


# ── 9. consume increments monotonically ────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_increments_counters():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=100))
    m1 = await t.consume("ig", "a", weight=1)
    m2 = await t.consume("ig", "a", weight=2)
    m3 = await t.consume("ig", "a", weight=3)
    assert m1.requests_today == 1
    assert m2.requests_today == 3
    assert m3.requests_today == 6
    # Hour and week parallel
    assert m3.requests_hour == 6


@pytest.mark.asyncio
async def test_consume_rejects_zero_weight():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=10))
    with pytest.raises(ValueError):
        await t.consume("ig", "a", weight=0)
    with pytest.raises(ValueError):
        await t.has_quota("ig", "a", weight=0)


# ── 10. consume_strict raises ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_strict_raises_on_breach():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=2))
    await t.consume_strict("ig", "a")
    await t.consume_strict("ig", "a")
    with pytest.raises(QuotaExhaustedError) as ei:
        await t.consume_strict("ig", "a")
    assert ei.value.axis == "daily"
    assert ei.value.platform == "ig"
    assert ei.value.account == "a"


@pytest.mark.asyncio
async def test_consume_strict_no_config_is_safe():
    t = make_tracker()
    metric = await t.consume_strict("nope", "a")
    assert isinstance(metric, QuotaMetric)
    assert metric.daily_limit == 0


# ── 12. telemetry hook ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telemetry_hook_receives_metric():
    received: list[QuotaMetric] = []
    t = make_tracker(telemetry=received.append)
    t.register("ig", QuotaConfig(daily_limit=10))
    await t.consume("ig", "a")
    await t.consume("ig", "a", weight=2)
    assert len(received) == 2
    assert received[-1].requests_today == 3
    assert received[-1].daily_limit == 10
    assert received[-1].platform == "ig"


@pytest.mark.asyncio
async def test_telemetry_raise_does_not_break_consume():
    def raising_hook(_m):
        raise RuntimeError("boom")

    t = make_tracker(telemetry=raising_hook)
    t.register("ig", QuotaConfig(daily_limit=10))
    # Should NOT raise out
    metric = await t.consume("ig", "a")
    assert metric is not None


# ── 13. concurrent consume (in-mem) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_consume_no_lost_updates():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=10_000))
    await asyncio.gather(*[t.consume("ig", "a") for _ in range(50)])
    usage = await t.get_usage("ig", "a")
    assert usage["requests_today"] == 50


# ── 14. reset ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_clears_state():
    t = make_tracker()
    t.register("ig", QuotaConfig(daily_limit=2))
    await t.consume("ig", "a"); await t.consume("ig", "a")
    assert await t.has_quota("ig", "a") is False
    await t.reset("ig", "a")
    assert await t.has_quota("ig", "a") is True


# ── 15. module singleton ──────────────────────────────────────────────────-


def test_default_tracker_get_set():
    assert get_default_tracker() is None
    t = make_tracker()
    set_default_tracker(t)
    assert get_default_tracker() is t
    set_default_tracker(None)
    assert get_default_tracker() is None


# ── DB-backed tests (skipped unless DSN provided) ──────────────────────────


DSN = os.environ.get("ACCOUNT_QUOTA_TEST_DSN")
RUN_DB_TESTS = bool(DSN)
skip_no_db = pytest.mark.skipif(
    not RUN_DB_TESTS, reason="ACCOUNT_QUOTA_TEST_DSN not set"
)


@pytest_asyncio.fixture
async def db_pool():
    if not RUN_DB_TESTS:
        pytest.skip("ACCOUNT_QUOTA_TEST_DSN not set")
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM account_quota_usage WHERE platform LIKE 'test_%'"
        )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM account_quota_usage WHERE platform LIKE 'test_%'"
        )
    await pool.close()


@skip_no_db
@pytest.mark.asyncio
async def test_db_persistence_across_tracker_restart(db_pool):
    t1 = AccountQuotaTracker(pool=db_pool, cache_ttl_s=0.0)
    t1.register("test_ig", QuotaConfig(daily_limit=100))
    for _ in range(7):
        await t1.consume("test_ig", "acct_persist")
    # New tracker, same DB
    t2 = AccountQuotaTracker(pool=db_pool, cache_ttl_s=0.0)
    t2.register("test_ig", QuotaConfig(daily_limit=100))
    usage = await t2.get_usage("test_ig", "acct_persist")
    assert usage["requests_today"] == 7


@skip_no_db
@pytest.mark.asyncio
async def test_db_concurrent_consume_two_trackers(db_pool):
    """Two trackers sharing one pool, hammering same key — no lost updates."""
    t1 = AccountQuotaTracker(pool=db_pool, cache_ttl_s=0.0)
    t2 = AccountQuotaTracker(pool=db_pool, cache_ttl_s=0.0)
    t1.register("test_ig", QuotaConfig(daily_limit=10_000))
    t2.register("test_ig", QuotaConfig(daily_limit=10_000))
    await asyncio.gather(
        *[t1.consume("test_ig", "acct_concurrent") for _ in range(25)],
        *[t2.consume("test_ig", "acct_concurrent") for _ in range(25)],
    )
    usage = await t1.get_usage("test_ig", "acct_concurrent")
    assert usage["requests_today"] == 50


@skip_no_db
@pytest.mark.asyncio
async def test_db_daily_limit_enforced(db_pool):
    t = AccountQuotaTracker(pool=db_pool, cache_ttl_s=0.0)
    t.register("test_ig", QuotaConfig(daily_limit=3))
    for _ in range(3):
        assert await t.has_quota("test_ig", "acct_limit") is True
        await t.consume("test_ig", "acct_limit")
    assert await t.has_quota("test_ig", "acct_limit") is False
