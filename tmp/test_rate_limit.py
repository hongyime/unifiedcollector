"""Behavioral verification for src/core/rate_limit.RateLimiter.

Run: python tmp/test_rate_limit.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.rate_limit import RateLimiter, _BoundedLRU  # noqa: E402

PASS = "OK"
FAIL = "FAIL"


def assert_eq(name, got, want):
    if got == want:
        print(f"  {PASS}  {name}: {got!r}")
    else:
        print(f"  {FAIL}  {name}: got={got!r} want={want!r}")
        raise SystemExit(1)


def assert_true(name, cond, detail=""):
    if cond:
        print(f"  {PASS}  {name}{(' '+detail) if detail else ''}")
    else:
        print(f"  {FAIL}  {name}{(' '+detail) if detail else ''}")
        raise SystemExit(1)


async def main():
    print("=" * 60)
    print("Test 1: validation rejects bad arguments")
    print("=" * 60)
    for args, why in [
        (dict(min_delay=-1), "negative min"),
        (dict(min_delay=5, base_delay=2), "min > base"),
        (dict(base_delay=10, max_delay=5), "base > max"),
        (dict(jitter=2.0), "jitter > 1"),
        (dict(success_threshold=0), "threshold 0"),
        (dict(adjustment_factor=0), "adjustment 0"),
        (dict(adjustment_factor=1.5), "adjustment > 1"),
        (dict(forbidden_backoff=0), "forbidden_backoff 0"),
    ]:
        try:
            RateLimiter(**args)
            print(f"  {FAIL}  {why}: did NOT raise")
            raise SystemExit(1)
        except ValueError:
            print(f"  {PASS}  {why}: raised ValueError")

    print("\n" + "=" * 60)
    print("Test 2: URL key reduces to netloc; account key passes through")
    print("=" * 60)
    rl = RateLimiter(base_delay=0.01, min_delay=0.001, max_delay=1.0,
                     jitter=0, success_threshold=2, adjustment_factor=0.5,
                     forbidden_backoff=0.05)
    assert_eq("netloc 1", rl._normalize_key("https://www.tiktok.com/@user/video"), "www.tiktok.com")
    assert_eq("netloc 2", rl._normalize_key("http://lemon8-app.com/api"), "lemon8-app.com")
    assert_eq("account-style", rl._normalize_key("@bryanseah"), "@bryanseah")
    assert_eq("empty -> default", rl._normalize_key(""), "_default")

    print("\n" + "=" * 60)
    print("Test 3: wait() blocks for ~base_delay on first call")
    print("=" * 60)
    rl = RateLimiter(base_delay=0.10, min_delay=0.01, max_delay=1.0,
                     jitter=0, success_threshold=5, adjustment_factor=0.2,
                     forbidden_backoff=0.5)
    t0 = time.perf_counter()
    slept = await rl.wait("https://example.com/x")
    elapsed = time.perf_counter() - t0
    assert_true("first call sleeps near base_delay", 0.08 <= elapsed <= 0.20,
                f"elapsed={elapsed:.3f}s, reported_slept={slept:.3f}s")

    print("\n" + "=" * 60)
    print("Test 4: success threshold decays delay")
    print("=" * 60)
    rl = RateLimiter(base_delay=1.0, min_delay=0.1, max_delay=10.0,
                     jitter=0, success_threshold=3, adjustment_factor=0.5,
                     forbidden_backoff=0.5)
    key = "https://api.example.com"
    # 3 successes -> 1 decay (0.5x)
    rl.record_success(key)
    rl.record_success(key)
    rl.record_success(key)
    s = rl.get_stats(key)
    assert_eq("decayed to half", round(s["current_delay"], 4), 0.5)
    # 3 more -> 0.25, then -> 0.125, then floor 0.1
    for _ in range(3):
        rl.record_success(key)
    s = rl.get_stats(key)
    assert_eq("decayed to quarter", round(s["current_delay"], 4), 0.25)
    for _ in range(3):
        rl.record_success(key)
    s = rl.get_stats(key)
    assert_eq("decayed to eighth", round(s["current_delay"], 4), 0.125)
    for _ in range(3):
        rl.record_success(key)
    s = rl.get_stats(key)
    assert_eq("clamped to min_delay", round(s["current_delay"], 4), 0.1)

    print("\n" + "=" * 60)
    print("Test 5: 429 + 503 backoff, 403 hard cooldown")
    print("=" * 60)
    rl = RateLimiter(base_delay=1.0, min_delay=0.1, max_delay=10.0,
                     jitter=0, success_threshold=5, adjustment_factor=0.5,
                     forbidden_backoff=2.0)
    key = "https://api.example.com"
    rl.record_failure(key, status_code=429)
    s = rl.get_stats(key)
    assert_eq("429 bumped to 1.5x", round(s["current_delay"], 4), 1.5)
    rl.record_failure(key, status_code=503)
    s = rl.get_stats(key)
    assert_eq("503 bumped to 2.25x", round(s["current_delay"], 4), 2.25)
    # streak should reset
    assert_eq("streak reset on failure", s["success_streak"], 0)

    rl.record_failure(key, status_code=403)
    s = rl.get_stats(key)
    assert_true("403 sets cooldown_remaining > 0", s["cooldown_remaining"] > 0,
                f"cooldown={s['cooldown_remaining']:.2f}s")
    assert_true("cooldown <= forbidden_backoff", s["cooldown_remaining"] <= 2.0)
    assert_true("403 raised current_delay >= forbidden_backoff",
                s["current_delay"] >= 2.0, f"cd={s['current_delay']}")

    print("\n" + "=" * 60)
    print("Test 6: is_in_cooldown / record_cooldown / get_cooldown_remaining")
    print("=" * 60)
    rl = RateLimiter(base_delay=0.1, min_delay=0.01, max_delay=10.0,
                     jitter=0, success_threshold=5, adjustment_factor=0.5,
                     forbidden_backoff=0.5)
    assert_eq("untracked key not in cooldown", rl.is_in_cooldown("@new"), False)
    assert_eq("untracked remaining 0", rl.get_cooldown_remaining("@new"), 0.0)
    rl.record_cooldown("@user", 0.3)
    assert_eq("tracked: in cooldown", rl.is_in_cooldown("@user"), True)
    rem = rl.get_cooldown_remaining("@user")
    assert_true("remaining ~0.3s", 0.25 <= rem <= 0.35, f"rem={rem:.3f}")

    # wait() should block for that cooldown:
    t0 = time.perf_counter()
    await rl.wait("@user")
    el = time.perf_counter() - t0
    assert_true("wait honored cooldown", el >= 0.25, f"elapsed={el:.3f}s")

    print("\n" + "=" * 60)
    print("Test 7: bounded LRU evicts oldest beyond max_keys")
    print("=" * 60)
    rl = RateLimiter(max_keys=4, base_delay=0.1, min_delay=0.01, max_delay=10.0,
                     jitter=0, success_threshold=5, adjustment_factor=0.5,
                     forbidden_backoff=0.5)
    for i in range(10):
        rl.record_success(f"key{i}")
    s = rl.get_stats()
    assert_eq("tracked_keys capped", s["tracked_keys"], 4)
    # key0..key5 should be evicted; key6..key9 retained
    for evicted in range(6):
        assert_eq(f"key{evicted} evicted", rl.get_stats(f"key{evicted}")["tracked"], False)
    for kept in range(6, 10):
        assert_eq(f"key{kept} retained", rl.get_stats(f"key{kept}")["tracked"], True)

    print("\n" + "=" * 60)
    print("Test 8: per-key independence (different URLs don't share streak)")
    print("=" * 60)
    rl = RateLimiter(base_delay=1.0, min_delay=0.1, max_delay=10.0,
                     jitter=0, success_threshold=3, adjustment_factor=0.5,
                     forbidden_backoff=0.5)
    rl.record_success("https://a.com")
    rl.record_success("https://a.com")
    rl.record_success("https://a.com")  # decays
    rl.record_success("https://b.com")
    s_a = rl.get_stats("https://a.com")
    s_b = rl.get_stats("https://b.com")
    assert_eq("a.com decayed", round(s_a["current_delay"], 4), 0.5)
    assert_eq("b.com still at base", round(s_b["current_delay"], 4), 1.0)

    print("\n" + "=" * 60)
    print("Test 9: concurrent waits on same key serialize correctly")
    print("=" * 60)
    rl = RateLimiter(base_delay=0.1, min_delay=0.01, max_delay=1.0,
                     jitter=0, success_threshold=5, adjustment_factor=0.5,
                     forbidden_backoff=0.5)
    t0 = time.perf_counter()
    # Fire 3 in parallel for the same key
    await asyncio.gather(
        rl.wait("https://api.x.com/1"),
        rl.wait("https://api.x.com/2"),
        rl.wait("https://api.x.com/3"),
    )
    el = time.perf_counter() - t0
    # 3 calls @ base_delay 0.1s should be staggered to ~0.3s minimum
    # (because last_request_ts is updated under the lock, so subsequent
    # waiters see the updated timestamp).
    assert_true("3 same-key waits serialized to ~3*delay",
                0.25 <= el <= 0.5, f"elapsed={el:.3f}s")

    print("\n" + "=" * 60)
    print("Test 10: reset clears state")
    print("=" * 60)
    rl.reset("https://api.x.com/1")
    assert_eq("specific key gone", rl.get_stats("https://api.x.com/1")["tracked"], False)
    rl.reset()
    assert_eq("all keys gone", rl.get_stats()["tracked_keys"], 0)

    print("\n" + "=" * 60)
    print("ALL RATE-LIMIT TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
