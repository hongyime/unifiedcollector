"""Behavioral verification for AccountPool flood-wait + classified errors (Wave 2.2)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.account_pool import AccountPool  # noqa: E402


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


print("=" * 60)
print("Test 1: record_flood_wait pins account for exact seconds")
print("=" * 60)
pool = AccountPool(default_cooldown=10, error_cooldown=20, max_consecutive_errors=5)
pool.add_account("alice", {"user": "a"})
assert_true("initially available", pool.is_available("alice"))
pool.record_flood_wait("alice", seconds=2.0)
assert_true("locked after flood_wait", not pool.is_available("alice"))
acct = pool._find("alice")
remaining = acct.locked_until - time.monotonic()
assert_true(f"remaining ~2s (got {remaining:.2f})", 1.5 < remaining < 2.5)
assert_eq("cooldown_reason set", acct.cooldown_reason, "flood-wait")

print("\n" + "=" * 60)
print("Test 2: record_flood_wait with longer pin extends; shorter pin keeps existing")
print("=" * 60)
pool = AccountPool()
pool.add_account("bob", {"user": "b"})
pool.record_flood_wait("bob", seconds=10.0, reason="flood-wait")
acct = pool._find("bob")
first_until = acct.locked_until
pool.record_flood_wait("bob", seconds=2.0, reason="short-flood")
assert_eq("shorter pin does NOT shorten", acct.locked_until, first_until)
assert_eq("reason unchanged when not extended", acct.cooldown_reason, "flood-wait")

pool.record_flood_wait("bob", seconds=20.0, reason="long-flood")
assert_true("longer pin extends", acct.locked_until > first_until + 5)
assert_eq("reason updated to longer", acct.cooldown_reason, "long-flood")

print("\n" + "=" * 60)
print("Test 3: record_flood_wait with seconds <= 0 is a no-op")
print("=" * 60)
pool = AccountPool()
pool.add_account("c", {"user": "c"})
acct = pool._find("c")
before = acct.locked_until
pool.record_flood_wait("c", seconds=0)
pool.record_flood_wait("c", seconds=-5)
assert_eq("no-op for seconds<=0", acct.locked_until, before)

print("\n" + "=" * 60)
print("Test 4: record_error_classified — kind=rate_limit applies 60s")
print("=" * 60)
pool = AccountPool(error_cooldown=600)
pool.add_account("d", {"user": "d"})
pool.record_error_classified("d", "rate_limit")
acct = pool._find("d")
remaining = acct.locked_until - time.monotonic()
assert_true(f"~60s cooldown (got {remaining:.0f})", 50 < remaining < 65)
assert_eq("kind recorded", acct.last_error_kind, "rate_limit")
assert_eq("error_count incremented", acct.error_count, 1)

print("\n" + "=" * 60)
print("Test 5: record_error_classified — kind=auth is sticky (caps error_count)")
print("=" * 60)
pool = AccountPool(error_cooldown=300, max_consecutive_errors=5)
pool.add_account("e", {"user": "e"})
pool.record_error_classified("e", "auth")
acct = pool._find("e")
assert_true("error_count capped at threshold", acct.error_count >= 5)
assert_true("locked", acct.is_locked)
remaining = acct.locked_until - time.monotonic()
assert_true(f"~3600s cooldown (got {remaining:.0f})", 3500 < remaining < 3700)
assert_true("not healthy", not acct.is_healthy)

print("\n" + "=" * 60)
print("Test 6: record_error_classified — flood_wait uses retry_after seconds")
print("=" * 60)
pool = AccountPool(error_cooldown=10)
pool.add_account("f", {"user": "f"})
pool.record_error_classified("f", "flood_wait", retry_after=300)
acct = pool._find("f")
remaining = acct.locked_until - time.monotonic()
assert_true(f"~300s cooldown (got {remaining:.0f})", 290 < remaining < 310)
assert_eq("kind=flood_wait", acct.last_error_kind, "flood_wait")
assert_eq("cooldown_reason=flood_wait", acct.cooldown_reason, "flood_wait")

print("\n" + "=" * 60)
print("Test 7: record_error_classified — flood_wait without retry_after falls back")
print("=" * 60)
pool = AccountPool(error_cooldown=42)
pool.add_account("g", {"user": "g"})
pool.record_error_classified("g", "flood_wait")  # no retry_after
acct = pool._find("g")
remaining = acct.locked_until - time.monotonic()
assert_true(f"falls back to error_cooldown=42 (got {remaining:.0f})", 35 < remaining < 50)

print("\n" + "=" * 60)
print("Test 8: record_error_classified — unknown kind treated as 'unknown'")
print("=" * 60)
pool = AccountPool(error_cooldown=88)
pool.add_account("h", {"user": "h"})
pool.record_error_classified("h", "totally_made_up_kind")
acct = pool._find("h")
assert_eq("kind normalized to 'unknown'", acct.last_error_kind, "unknown")
remaining = acct.locked_until - time.monotonic()
assert_true(f"~88s cooldown (got {remaining:.0f})", 80 < remaining < 95)

print("\n" + "=" * 60)
print("Test 9: record_error_classified — retry_after overrides per-kind default")
print("=" * 60)
pool = AccountPool()
pool.add_account("i", {"user": "i"})
pool.record_error_classified("i", "rate_limit", retry_after=120)
acct = pool._find("i")
remaining = acct.locked_until - time.monotonic()
assert_true(f"~120s cooldown (overrides 60s default, got {remaining:.0f})",
            115 < remaining < 130)

print("\n" + "=" * 60)
print("Test 10: get_status surfaces cooldown_reason + last_error_kind")
print("=" * 60)
pool = AccountPool()
pool.add_account("j", {"user": "j"})
pool.add_account("k", {"user": "k"})
pool.record_error_classified("j", "rate_limit")
pool.record_flood_wait("k", 60, reason="manual-pin")
status = pool.get_status()
status_by = {s["name"]: s for s in status}
assert_eq("j last_error_kind", status_by["j"]["last_error_kind"], "rate_limit")
assert_eq("j cooldown_reason", status_by["j"]["cooldown_reason"], "rate_limit")
assert_eq("k cooldown_reason", status_by["k"]["cooldown_reason"], "manual-pin")

print("\n" + "=" * 60)
print("Test 11: is_available — null-safe for unknown account")
print("=" * 60)
pool = AccountPool()
assert_eq("unknown account => not available", pool.is_available("ghost"), False)
pool.add_account("real", {"user": "r"})
assert_eq("real account => available", pool.is_available("real"), True)

print("\n" + "=" * 60)
print("ALL ACCOUNT-POOL CLASSIFIED-ERROR TESTS PASSED")
print("=" * 60)
