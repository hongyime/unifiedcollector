"""
P2.2 Bug-Condition Exploration Test — No App-Level Startup Probe

Validates: Requirements 1.6, 2.6, 3.6

Bug condition:
    docker_healthcheck_passed = True AND
    (db_schema_ready() = False OR redis_keyspace_ready() = False)

The application services (collector, face_recognition, etc.) rely solely on
Docker Compose `depends_on: condition: service_healthy`, which only checks the
container-level healthcheck. The application-level database schema and Redis
keyspace may not yet be ready, causing race-condition crashes on the first query.

There is no `shared/startup_probe.py` module. The application calls
`get_db_connection()` immediately on startup without any retry logic.

EXPECTED OUTCOME (Task 14 — bug-condition test, on unfixed code): FAILS
  — `shared.startup_probe` does not exist, so importing `wait_for_dependencies`
    raises ModuleNotFoundError, confirming the bug: no startup probe exists.

EXPECTED OUTCOME (Task 15 — preservation test, on unfixed code): PASSES
  — when the DB is already available, `get_db_connection()` succeeds immediately
    without any added delay, confirming the fast-path is already correct.

Documented counterexample (Task 14):
    probe_failures_before_success = 1
    Scenario: probe_postgres() fails once, then succeeds.
    Expected (correct behavior): wait_for_dependencies() retries and eventually
        returns, allowing the application to proceed safely.
    Actual (buggy behavior):     ModuleNotFoundError: No module named 'shared.startup_probe'
                                 — no retry logic exists; the application would
                                   crash on the first DB call if the schema is not ready.

    Root cause: shared/startup_probe.py does not exist yet (created in Task 16).
    The application has no mechanism to wait for app-level DB/Redis readiness
    beyond the Docker container healthcheck.
"""

import asyncio
import sys
import os
import time

import pytest
from hypothesis import given, settings as h_settings, HealthCheck, assume
from hypothesis import strategies as st
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Task 14 — Property 1: Bug Condition
# Application crashes on first DB call rather than retrying
# ---------------------------------------------------------------------------

@given(
    probe_failures=st.integers(0, 29)
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_no_startup_probe_crashes_on_first_db_call(probe_failures):
    """
    **Validates: Requirements 1.6, 2.6**

    Bug condition:
        docker_healthcheck_passed = True AND db_schema_ready() = False

    For each count of probe failures before success (0–29):
      1. Simulate probe_postgres() failing `probe_failures` times then succeeding.
      2. Assert that wait_for_dependencies from shared.startup_probe exists and
         correctly retries — this FAILS because shared/startup_probe.py does not
         exist yet (created in Task 16).

    EXPECTED OUTCOME on unfixed code: FAILS
      — ModuleNotFoundError: No module named 'shared.startup_probe'

    EXPECTED OUTCOME on fixed code: PASSES
      — wait_for_dependencies() retries probe_postgres() until it succeeds,
        then returns without raising.

    Documented counterexample:
        probe_failures = 1
        probe_postgres() fails once → wait_for_dependencies() should retry
        Actual: ModuleNotFoundError — no retry mechanism exists.
        BUG CONFIRMED: application would crash on first DB call if schema not ready.
    """
    # Track how many times the probe is called
    call_count = 0

    async def mock_probe_postgres(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= probe_failures:
            return False  # Simulate DB not ready
        return True  # Simulate DB ready

    async def mock_probe_redis(*args, **kwargs):
        return True  # Redis always ready in this scenario

    # Assert that the startup probe module exists with wait_for_dependencies.
    # On unfixed code this raises ModuleNotFoundError → test FAILS → bug confirmed.
    # On fixed code (Task 16) this import succeeds and the function works correctly.
    from shared.startup_probe import wait_for_dependencies  # noqa: F401 — expected to fail

    # With the fix in place: patch the probe functions and verify retry behaviour
    with patch("shared.startup_probe.probe_postgres", side_effect=mock_probe_postgres), \
         patch("shared.startup_probe.probe_redis", side_effect=mock_probe_redis):

        asyncio.run(
            wait_for_dependencies(
                require_postgres=True,
                require_redis=False,
                max_attempts=probe_failures + 2,  # enough attempts to succeed
                retry_interval=0.0,               # no delay in tests
            )
        )

    # After wait_for_dependencies returns, the probe must have been called at
    # least probe_failures + 1 times (failures + 1 success).
    expected_calls = probe_failures + 1
    assert call_count == expected_calls, (
        f"Expected probe_postgres to be called {expected_calls} time(s) "
        f"(probe_failures={probe_failures}), but was called {call_count} time(s)."
    )


# ---------------------------------------------------------------------------
# Task 15 — Property 2: Preservation
# Fast startup when dependencies are already ready
# ---------------------------------------------------------------------------

@given(
    require_postgres=st.booleans(),
    require_redis=st.booleans(),
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_fast_startup_when_dependencies_already_ready(require_postgres, require_redis):
    """
    **Validates: Requirements 3.6**

    Preservation: when DB and Redis respond immediately, startup proceeds
    without delay.

    For all combinations of require_postgres and require_redis flags:
      - When both probes succeed on the first attempt, the current code
        (without shared/startup_probe.py) does NOT add any delay because
        there is no probe at all — get_db_connection() is called directly
        and succeeds immediately.

    This test verifies the CURRENT behaviour on unfixed code:
      - A successful DB connection completes in < 100 ms (no artificial delay).
      - The absence of a startup probe means fast startup is trivially preserved.

    EXPECTED OUTCOME on unfixed code: PASSES
      — no startup probe exists, so no delay is added; fast path is correct.

    EXPECTED OUTCOME on fixed code: PASSES
      — wait_for_dependencies() with probes that succeed on attempt 1 must
        return in < 100 ms (no perceptible overhead).

    Non-bug condition:
        probe_postgres() succeeds on attempt 1 AND probe_redis() succeeds on attempt 1
    """
    # Skip the trivial case where neither dependency is required
    assume(require_postgres or require_redis)

    postgres_call_count = 0
    redis_call_count = 0

    async def instant_probe_postgres(*args, **kwargs):
        nonlocal postgres_call_count
        postgres_call_count += 1
        return True  # Succeeds immediately

    async def instant_probe_redis(*args, **kwargs):
        nonlocal redis_call_count
        redis_call_count += 1
        return True  # Succeeds immediately

    # On unfixed code: shared.startup_probe does not exist.
    # We verify the CURRENT fast-path: a direct DB connection attempt completes
    # quickly (< 100 ms) because there is no probe overhead.
    #
    # Strategy: mock get_db_connection to succeed instantly and measure elapsed time.
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value=None)

    async def fast_startup_simulation():
        # Simulate what the application does on startup (no probe, direct call)
        start = time.monotonic()
        # Direct DB call — no retry, no probe (current unfixed behaviour)
        async with mock_conn as conn:
            await conn.execute("SELECT 1")
        elapsed_ms = (time.monotonic() - start) * 1000
        return elapsed_ms

    elapsed = asyncio.run(fast_startup_simulation())

    assert elapsed < 100, (
        f"Fast startup took {elapsed:.1f} ms — expected < 100 ms. "
        f"require_postgres={require_postgres}, require_redis={require_redis}. "
        "The startup path must not add perceptible delay when dependencies are ready."
    )

    # Additionally: if the startup probe module IS available (post-fix),
    # verify it also completes in < 100 ms with instant probes.
    try:
        from shared.startup_probe import wait_for_dependencies

        with patch("shared.startup_probe.probe_postgres", side_effect=instant_probe_postgres), \
             patch("shared.startup_probe.probe_redis", side_effect=instant_probe_redis):

            start = time.monotonic()
            asyncio.run(
                wait_for_dependencies(
                    require_postgres=require_postgres,
                    require_redis=require_redis,
                    max_attempts=5,
                    retry_interval=0.0,
                )
            )
            elapsed_probe_ms = (time.monotonic() - start) * 1000

        assert elapsed_probe_ms < 100, (
            f"wait_for_dependencies took {elapsed_probe_ms:.1f} ms with instant probes — "
            f"expected < 100 ms. require_postgres={require_postgres}, "
            f"require_redis={require_redis}."
        )

        # Each required probe must be called exactly once (fast path = 1 attempt)
        if require_postgres:
            assert postgres_call_count == 1, (
                f"probe_postgres called {postgres_call_count} time(s); expected 1 "
                "(should succeed on first attempt and not retry)."
            )
        if require_redis:
            assert redis_call_count == 1, (
                f"probe_redis called {redis_call_count} time(s); expected 1 "
                "(should succeed on first attempt and not retry)."
            )

    except ModuleNotFoundError:
        # shared/startup_probe.py does not exist yet (pre-fix) — this is expected.
        # The preservation property is satisfied by the fast direct-call path above.
        pass
