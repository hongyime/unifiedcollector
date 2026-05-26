"""Behavioral verification for AccountStateRepository (Wave 2.4)."""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if ENV_FILE.exists() and "DATABASE_URL" not in os.environ:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1]
        elif line.startswith("POSTGRES_SSL_MODE="):
            os.environ["POSTGRES_SSL_MODE"] = line.split("=", 1)[1]

from src.db.connection import get_pool, close_pool  # noqa: E402
from src.core.account_pool import AccountPool, _quota_date  # noqa: E402
from src.core.account_state_repository import AccountStateRepository  # noqa: E402


def assert_eq(name, got, want):
    if got == want:
        print(f"  OK    {name}: {got!r}")
    else:
        print(f"  FAIL  {name}: got={got!r} want={want!r}")
        raise SystemExit(1)


def assert_true(name, cond, detail=""):
    print(f"  {'OK' if cond else 'FAIL'}    {name}{(' ' + detail) if detail else ''}")
    if not cond:
        raise SystemExit(1)


# Unique test prefix so we don't pollute real account state
TEST_PREFIX = f"_t_state_{os.getpid()}_"


async def cleanup(pool):
    await pool.execute(
        "DELETE FROM account_state WHERE account_name LIKE $1", TEST_PREFIX + "%"
    )


async def main():
    db_pool = await get_pool()
    schema = (Path(__file__).resolve().parent.parent / "src" / "db" / "schemas"
              / "account_state.sql").read_text(encoding="utf-8")
    async with db_pool.acquire() as conn:
        await conn.execute(schema)

    repo = AccountStateRepository(db_pool)
    await cleanup(db_pool)

    try:
        print("=" * 60)
        print("Test 1: argument validation")
        print("=" * 60)
        try:
            AccountStateRepository(None)  # type: ignore[arg-type]
            assert_true("None pool rejected", False)
        except ValueError:
            assert_true("None pool rejected", True)

        print("\n" + "=" * 60)
        print("Test 2: flush_pool persists snapshot of all accounts")
        print("=" * 60)
        acct_pool = AccountPool(
            default_cooldown=10, error_cooldown=20, max_consecutive_errors=5,
            daily_quota_profile_views=200, daily_quota_actions=100,
        )
        n1 = TEST_PREFIX + "alice"
        n2 = TEST_PREFIX + "bob"
        acct_pool.add_account(n1, {"user": "a"})
        acct_pool.add_account(n2, {"user": "b"})

        # Touch some state
        acct_pool.record_profile_view(n1)
        acct_pool.record_profile_view(n1)
        acct_pool.record_action(n1)
        acct_pool.record_flood_wait(n2, seconds=300, reason="flood-wait")
        acct_pool.record_error_classified(n1, "rate_limit")

        n_flushed = await repo.flush_pool(acct_pool)
        assert_eq("count flushed", n_flushed, 2)

        # Read back via fetch()
        row1 = await repo.fetch(n1)
        assert_true("alice persisted", row1 is not None)
        assert_eq("alice profile_views", row1["profile_views_today"], 2)
        assert_eq("alice actions", row1["actions_today"], 1)
        assert_eq("alice last_error_kind", row1["last_error_kind"], "rate_limit")
        assert_true("alice locked", row1["locked_until_wall"] is not None)

        row2 = await repo.fetch(n2)
        assert_true("bob persisted", row2 is not None)
        assert_eq("bob cooldown_reason", row2["cooldown_reason"], "flood-wait")
        assert_true("bob locked ~5min", row2["locked_until_wall"] is not None)

        print("\n" + "=" * 60)
        print("Test 3: load_into_pool restores quota for SAME-DAY window")
        print("=" * 60)
        # Spawn a fresh pool with same accounts (zero state). Then load.
        fresh = AccountPool(
            default_cooldown=10, error_cooldown=20, max_consecutive_errors=5,
            daily_quota_profile_views=200, daily_quota_actions=100,
        )
        fresh.add_account(n1, {"user": "a"})
        fresh.add_account(n2, {"user": "b"})

        # Verify zero baseline
        a_fresh = fresh._find(n1)
        assert_eq("baseline profile_views", a_fresh.quota.profile_views, 0)
        assert_eq("baseline locked_until", a_fresh.locked_until, 0)

        n_loaded = await repo.load_into_pool(fresh)
        assert_eq("count loaded", n_loaded, 2)

        a_after = fresh._find(n1)
        assert_eq("alice profile_views restored", a_after.quota.profile_views, 2)
        assert_eq("alice actions restored", a_after.quota.actions, 1)
        assert_eq("alice last_error_kind restored",
                  a_after.last_error_kind, "rate_limit")
        assert_true("alice locked restored", a_after.locked_until > 0)
        # Verify cooldown deadline is in the future (i.e. monotonic was set)
        remaining = a_after.locked_until - time.monotonic()
        assert_true(f"alice cooldown still pending ({remaining:.0f}s)",
                    remaining > 0)

        b_after = fresh._find(n2)
        assert_eq("bob cooldown_reason restored",
                  b_after.cooldown_reason, "flood-wait")
        b_remaining = b_after.locked_until - time.monotonic()
        assert_true(f"bob cooldown ~5min ({b_remaining:.0f}s)",
                    250 < b_remaining < 310)

        print("\n" + "=" * 60)
        print("Test 4: only_existing=True skips unknown accounts")
        print("=" * 60)
        # Pool with only alice — bob exists in DB but not pool
        partial = AccountPool()
        partial.add_account(n1, {"user": "a"})
        n_loaded = await repo.load_into_pool(partial)
        assert_eq("only alice loaded", n_loaded, 1)
        assert_true("bob not in partial pool",
                    partial._find(n2) is None)

        print("\n" + "=" * 60)
        print("Test 5: stale quota window is reset on load")
        print("=" * 60)
        # Manually upsert a row with YESTERDAY's quota_window_start
        from datetime import datetime, timezone, timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2))
        yest_str = yesterday.strftime("%Y-%m-%d")
        await repo.upsert(
            n1,
            quota_date=yest_str,
            profile_views=999,
            actions=999,
            locked_until_wall=None,
            cooldown_reason="",
            last_error_kind="",
            error_count=0, success_count=0, total_requests=0,
        )
        fresh3 = AccountPool(daily_quota_profile_views=200)
        fresh3.add_account(n1, {"user": "a"})
        await repo.load_into_pool(fresh3)
        a3 = fresh3._find(n1)
        today = _quota_date()
        assert_eq("quota window reset to today", a3.quota.quota_date, today)
        assert_eq("stale profile_views NOT restored", a3.quota.profile_views, 0)
        assert_eq("stale actions NOT restored", a3.quota.actions, 0)

        print("\n" + "=" * 60)
        print("Test 6: expired cooldown clamps to 0 on load")
        print("=" * 60)
        # Persist a cooldown that's ALREADY in the past
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        await repo.upsert(
            n1,
            quota_date=_quota_date(),
            profile_views=0, actions=0,
            locked_until_wall=past,
            cooldown_reason="expired",
            last_error_kind="",
            error_count=0, success_count=0, total_requests=0,
        )
        fresh4 = AccountPool()
        fresh4.add_account(n1, {"user": "a"})
        await repo.load_into_pool(fresh4)
        a4 = fresh4._find(n1)
        assert_eq("expired cooldown clamps to 0", a4.locked_until, 0)
        assert_true("not locked after load", not a4.is_locked)

        print("\n" + "=" * 60)
        print("Test 7: clear_expired_cooldowns drops past deadlines")
        print("=" * 60)
        # Add another expired row directly via upsert
        await repo.upsert(
            TEST_PREFIX + "expired_test",
            quota_date=_quota_date(),
            profile_views=0, actions=0,
            locked_until_wall=datetime.now(timezone.utc) - timedelta(seconds=60),
            cooldown_reason="old",
            last_error_kind="",
            error_count=0, success_count=0, total_requests=0,
        )
        cleared = await repo.clear_expired_cooldowns()
        assert_true(f"cleared {cleared} expired cooldown(s)", cleared >= 1)

        # Verify the row's cooldown is now NULL
        row = await repo.fetch(TEST_PREFIX + "expired_test")
        assert_eq("locked_until_wall cleared", row["locked_until_wall"], None)
        assert_eq("cooldown_reason cleared", row["cooldown_reason"], "")

        print("\n" + "=" * 60)
        print("Test 8: round-trip preserves counters")
        print("=" * 60)
        rt_pool = AccountPool()
        rt_name = TEST_PREFIX + "roundtrip"
        rt_pool.add_account(rt_name, {"user": "rt"})
        # Fake some counters via direct attr (no public setter for these)
        a = rt_pool._find(rt_name)
        a.error_count = 7
        a.success_count = 42
        a.total_requests = 100
        await repo.flush_pool(rt_pool)

        # Fresh pool, load
        rt_fresh = AccountPool()
        rt_fresh.add_account(rt_name, {"user": "rt"})
        await repo.load_into_pool(rt_fresh)
        a2 = rt_fresh._find(rt_name)
        assert_eq("error_count round-trip", a2.error_count, 7)
        assert_eq("success_count round-trip", a2.success_count, 42)
        assert_eq("total_requests round-trip", a2.total_requests, 100)

        print("\n" + "=" * 60)
        print("Test 9: flush_pool when pool is empty")
        print("=" * 60)
        empty = AccountPool()
        n = await repo.flush_pool(empty)
        assert_eq("0 to flush", n, 0)

        print("\n" + "=" * 60)
        print("Test 10: idempotent upsert")
        print("=" * 60)
        target = TEST_PREFIX + "idem"
        for i in range(3):
            await repo.upsert(
                target,
                quota_date=_quota_date(),
                profile_views=10 + i, actions=20 + i,
                locked_until_wall=None,
                cooldown_reason="", last_error_kind="",
                error_count=0, success_count=0, total_requests=0,
            )
        row = await repo.fetch(target)
        assert_eq("final profile_views", row["profile_views_today"], 12)
        assert_eq("final actions", row["actions_today"], 22)

        print("\n" + "=" * 60)
        print("ALL ACCOUNT-STATE-REPOSITORY TESTS PASSED")
        print("=" * 60)

    finally:
        await cleanup(db_pool)
        await close_pool()


asyncio.run(main())
