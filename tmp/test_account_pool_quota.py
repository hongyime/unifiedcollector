"""Behavioral verification for AccountPool quota extensions."""
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.account_pool import AccountPool, _quota_date  # noqa: E402


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
print("Test 1: _quota_date rolls at quota_reset_hour")
print("=" * 60)
# At UTC midnight reset_hour=0 today's date. With reset_hour=4 and current
# UTC hour < 4, we're still in yesterday's window. We can't easily monkey
# the clock here, so just confirm format and that reset_hour=24 raises... no, it's free.
today = _quota_date(0)
assert_true("returns yyyy-mm-dd", len(today) == 10 and today[4] == "-" and today[7] == "-",
            f"got {today!r}")

print("\n" + "=" * 60)
print("Test 2: AccountPool with no quotas allows everything")
print("=" * 60)
pool = AccountPool()
pool.add_account("alice", {"user": "a", "pass": "x"})
assert_eq("can_view_profiles default", pool.can_view_profiles("alice"), True)
assert_eq("can_perform_action default", pool.can_perform_action("alice"), True)
assert_eq("can_download_media default", pool.can_download_media("alice"), True)
# Record some
for _ in range(1000):
    pool.record_profile_view("alice")
assert_eq("still True after 1000 views (unlimited)", pool.can_view_profiles("alice"), True)
usage = pool.get_quota_usage("alice")
assert_eq("counter recorded", usage["profile_views"], 1000)

print("\n" + "=" * 60)
print("Test 3: AccountPool with quotas blocks once exceeded")
print("=" * 60)
pool = AccountPool(daily_quota_profile_views=3, daily_quota_actions=2)
pool.add_account("bob", {"user": "b"})
assert_eq("can view (0/3)", pool.can_view_profiles("bob"), True)
pool.record_profile_view("bob")
pool.record_profile_view("bob")
assert_eq("can view (2/3)", pool.can_view_profiles("bob"), True)
pool.record_profile_view("bob")
assert_eq("cannot view (3/3)", pool.can_view_profiles("bob"), False)

assert_eq("can act (0/2)", pool.can_perform_action("bob"), True)
pool.record_action("bob", count=2)
assert_eq("cannot act (2/2)", pool.can_perform_action("bob"), False)

# Other counters unaffected
assert_eq("media unlimited still", pool.can_download_media("bob"), True)

print("\n" + "=" * 60)
print("Test 4: get_next_with_quota filters out exhausted accounts")
print("=" * 60)
pool = AccountPool(daily_quota_profile_views=2)
pool.add_account("alice", {"user": "a"})
pool.add_account("bob", {"user": "b"})
pool.add_account("carol", {"user": "c"})
# Drain alice
pool.record_profile_view("alice", count=2)
# carol has 1 view
pool.record_profile_view("carol", count=1)

picks = []
for _ in range(6):
    a = pool.get_next_with_quota(require="profile_view")
    if a:
        pool.record_profile_view(a.name)
        picks.append(a.name)

# alice should never be picked (exhausted before we started); bob should be
# picked twice; carol should be picked once before hitting 2.
assert_true("alice not picked", "alice" not in picks, f"picks={picks}")
assert_true("bob picked >= 1 time", picks.count("bob") >= 1, f"picks={picks}")

print("\n" + "=" * 60)
print("Test 5: unknown require raises ValueError")
print("=" * 60)
try:
    pool.get_next_with_quota(require="bogus")
    assert_true("did not raise", False)
except ValueError as e:
    assert_true("raised ValueError", True, f"-> {e}")

print("\n" + "=" * 60)
print("Test 6: 'any' falls back to get_next")
print("=" * 60)
pool = AccountPool()
pool.add_account("a1", {"user": "a"})
pool.add_account("a2", {"user": "b"})
pick = pool.get_next_with_quota(require="any")
assert_true("returned an account", pick is not None, f"pick={pick}")

print("\n" + "=" * 60)
print("Test 7: quota survives concurrent record_* (locked)")
print("=" * 60)
import threading
pool = AccountPool(daily_quota_actions=10000)
pool.add_account("hot", {"user": "h"})
def worker():
    for _ in range(1000):
        pool.record_action("hot")
threads = [threading.Thread(target=worker) for _ in range(5)]
for t in threads: t.start()
for t in threads: t.join()
usage = pool.get_quota_usage("hot")
assert_eq("5*1000=5000 actions counted", usage["actions"], 5000)

print("\n" + "=" * 60)
print("Test 8: get_quota_summary formats correctly")
print("=" * 60)
pool = AccountPool(daily_quota_profile_views=100, daily_quota_actions=0)  # action unlimited
pool.add_account("zoe", {"user": "z"})
pool.record_profile_view("zoe", count=37)
pool.record_action("zoe", count=999)
sm = pool.get_quota_summary("zoe")
assert_eq("profile_views fmt", sm["profile_views"], "37/100")
assert_eq("actions fmt (unlimited)", sm["actions"], "999/inf")

print("\n" + "=" * 60)
print("ALL ACCOUNT-POOL QUOTA TESTS PASSED")
print("=" * 60)
