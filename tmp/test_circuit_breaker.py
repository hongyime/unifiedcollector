"""Behavioral verification for src/core/circuit_breaker.CircuitBreaker."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError  # noqa: E402


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


async def main():
    print("=" * 60)
    print("Test 1: argument validation")
    print("=" * 60)
    bad_cases = [
        {"failure_threshold": 0},
        {"failure_threshold": -1},
        {"recovery_timeout": 0},
        {"recovery_timeout": -1},
    ]
    for kw in bad_cases:
        try:
            CircuitBreaker(**kw)
            assert_true(f"reject {kw}", False)
        except ValueError as e:
            assert_true(f"reject {kw}", True, f"-> {e}")

    print("\n" + "=" * 60)
    print("Test 2: closed circuit allows calls; success keeps closed")
    print("=" * 60)
    cb = CircuitBreaker("t2", failure_threshold=3, recovery_timeout=10.0)
    assert_eq("initial state", cb.state, CircuitBreaker.CLOSED)
    for i in range(5):
        result = await cb.call(lambda: asyncio.sleep(0, result=42))
        assert_eq(f"call {i} result", result, 42)
    assert_eq("state after 5 successes", cb.state, CircuitBreaker.CLOSED)
    assert_eq("failure count", cb.failure_count, 0)

    print("\n" + "=" * 60)
    print("Test 3: failures trip the circuit")
    print("=" * 60)
    cb = CircuitBreaker("t3", failure_threshold=3, recovery_timeout=10.0)
    async def bad():
        raise ValueError("boom")
    for i in range(3):
        try:
            await cb.call(bad)
        except ValueError:
            pass
        assert_eq(f"after fail {i+1}", cb.failure_count, i + 1)
    assert_eq("state after threshold reached", cb.state, CircuitBreaker.OPEN)

    # Subsequent calls should fail-fast with CircuitOpenError, NOT execute fn.
    fn_called = [0]
    async def counted_fn():
        fn_called[0] += 1
        return "ok"
    try:
        await cb.call(counted_fn)
        assert_true("fail-fast", False)
    except CircuitOpenError:
        assert_true("fail-fast on OPEN raises CircuitOpenError", True)
    assert_eq("fn NOT called while open", fn_called[0], 0)

    print("\n" + "=" * 60)
    print("Test 4: recovery_timeout transitions to HALF_OPEN")
    print("=" * 60)
    cb = CircuitBreaker("t4", failure_threshold=2, recovery_timeout=0.3)
    async def bad():
        raise RuntimeError("bad")
    for _ in range(2):
        try: await cb.call(bad)
        except RuntimeError: pass
    assert_eq("state OPEN", cb.state, CircuitBreaker.OPEN)
    # Wait past recovery_timeout
    await asyncio.sleep(0.35)
    # Next call should transition to HALF_OPEN, then CLOSE on success
    result = await cb.call(lambda: asyncio.sleep(0, result="probe-ok"))
    assert_eq("probe success", result, "probe-ok")
    assert_eq("state CLOSED after probe success", cb.state, CircuitBreaker.CLOSED)
    assert_eq("failure_count reset", cb.failure_count, 0)

    print("\n" + "=" * 60)
    print("Test 5: HALF_OPEN probe failure -> OPEN with fresh timer")
    print("=" * 60)
    cb = CircuitBreaker("t5", failure_threshold=2, recovery_timeout=0.3)
    async def bad():
        raise RuntimeError("still bad")
    for _ in range(2):
        try: await cb.call(bad)
        except RuntimeError: pass
    await asyncio.sleep(0.35)
    # HALF_OPEN probe fails
    try: await cb.call(bad)
    except RuntimeError: pass
    assert_eq("state OPEN after failed probe", cb.state, CircuitBreaker.OPEN)
    # Should NOT pass through immediately
    try:
        await cb.call(lambda: asyncio.sleep(0, result="x"))
        assert_true("should have raised", False)
    except CircuitOpenError:
        assert_true("OPEN gates calls again", True)

    print("\n" + "=" * 60)
    print("Test 6: concurrent HALF_OPEN — only ONE probe runs at a time")
    print("=" * 60)
    cb = CircuitBreaker("t6", failure_threshold=2, recovery_timeout=0.2)
    # Trip it
    async def bad():
        raise RuntimeError("nope")
    for _ in range(2):
        try: await cb.call(bad)
        except RuntimeError: pass
    await asyncio.sleep(0.25)

    # Now spawn 5 concurrent calls. Only one should be the probe; the
    # other 4 should fail-fast because half-open in-flight.
    in_flight_seen = []
    probe_count = [0]
    async def slow_probe():
        probe_count[0] += 1
        await asyncio.sleep(0.2)
        return "probed"
    async def attempt(i):
        try:
            r = await cb.call(slow_probe)
            return ("ok", r)
        except CircuitOpenError as e:
            return ("rejected", str(e))

    tasks = [asyncio.create_task(attempt(i)) for i in range(5)]
    results = await asyncio.gather(*tasks)
    ok_count = sum(1 for r in results if r[0] == "ok")
    rej_count = sum(1 for r in results if r[0] == "rejected")
    assert_eq("exactly 1 probe ran", probe_count[0], 1)
    assert_eq("1 succeeded", ok_count, 1)
    assert_eq("4 rejected as half-open in-flight", rej_count, 4)
    assert_eq("state CLOSED after probe success", cb.state, CircuitBreaker.CLOSED)

    print("\n" + "=" * 60)
    print("Test 7: expected_exception filter")
    print("=" * 60)
    cb = CircuitBreaker("t7", failure_threshold=2, recovery_timeout=10.0,
                        expected_exception=ValueError)
    async def runtime_err():
        raise RuntimeError("wrong type")
    async def value_err():
        raise ValueError("counted")
    # 5 RuntimeErrors should NOT trip the breaker
    for _ in range(5):
        try: await cb.call(runtime_err)
        except RuntimeError: pass
    assert_eq("RuntimeErrors don't count", cb.failure_count, 0)
    assert_eq("state still CLOSED", cb.state, CircuitBreaker.CLOSED)
    # 2 ValueErrors trip it
    for _ in range(2):
        try: await cb.call(value_err)
        except ValueError: pass
    assert_eq("ValueErrors counted", cb.failure_count, 2)
    assert_eq("state OPEN", cb.state, CircuitBreaker.OPEN)

    print("\n" + "=" * 60)
    print("Test 8: reset() forces CLOSED")
    print("=" * 60)
    cb = CircuitBreaker("t8", failure_threshold=2, recovery_timeout=100.0)
    async def bad():
        raise RuntimeError("x")
    for _ in range(2):
        try: await cb.call(bad)
        except RuntimeError: pass
    assert_eq("state OPEN", cb.state, CircuitBreaker.OPEN)
    await cb.reset()
    assert_eq("state CLOSED after reset", cb.state, CircuitBreaker.CLOSED)
    assert_eq("failure_count zeroed", cb.failure_count, 0)
    # Should accept calls again immediately
    result = await cb.call(lambda: asyncio.sleep(0, result=99))
    assert_eq("post-reset call works", result, 99)

    print("\n" + "=" * 60)
    print("Test 9: stats() snapshot")
    print("=" * 60)
    cb = CircuitBreaker("t9", failure_threshold=3, recovery_timeout=5.0)
    s = cb.stats()
    assert_eq("name", s["name"], "t9")
    assert_eq("state", s["state"], "closed")
    assert_eq("failure_count", s["failure_count"], 0)
    assert_eq("failure_threshold", s["failure_threshold"], 3)
    assert_eq("opened_at", s["opened_at"], None)
    # After tripping
    async def bad():
        raise RuntimeError("x")
    for _ in range(3):
        try: await cb.call(bad)
        except RuntimeError: pass
    s = cb.stats()
    assert_eq("state OPEN", s["state"], "open")
    assert_true("opened_at set", s["opened_at"] is not None)
    assert_true("elapsed >= 0", s["elapsed_since_open"] is not None and s["elapsed_since_open"] >= 0)

    print("\n" + "=" * 60)
    print("Test 10: CancelledError doesn't trip default breaker")
    print("=" * 60)
    cb = CircuitBreaker("t10", failure_threshold=2, recovery_timeout=10.0)
    async def cancelled():
        raise asyncio.CancelledError()
    for _ in range(5):
        try: await cb.call(cancelled)
        except asyncio.CancelledError: pass
        except BaseException: pass
    # CancelledError IS a BaseException but inherits from BaseException only
    # in 3.8+. Default expected_exception=Exception should NOT count it.
    # Actually CancelledError inherits from BaseException directly in 3.8+
    # so isinstance(exc, Exception) is False. Good.
    assert_eq("CancelledError not counted", cb.failure_count, 0)
    assert_eq("state still CLOSED", cb.state, CircuitBreaker.CLOSED)

    print("\n" + "=" * 60)
    print("ALL CIRCUIT-BREAKER TESTS PASSED")
    print("=" * 60)


asyncio.run(main())
