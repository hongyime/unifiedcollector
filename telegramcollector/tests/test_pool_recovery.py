"""
P3.2 Bug-Condition Exploration Test — Pool Reference Not Cleared on Double-Raise

Validates: Requirements 1.8, 2.8, 3.4

Bug condition:
    pool_state.check_raised = True
    AND pool_state.close_raised = True
    AND pool_state.pool_reference != None

The current _recover_pool() in database.py:
  1. Calls pool.check() — if it raises, falls into the except block
  2. In the except block, calls pool.close() — if THAT also raises, the exception
     propagates out of _recover_pool() BEFORE self.pool = None is executed
  3. Result: self.pool still points to the broken AsyncConnectionPool object
  4. Subsequent get_db_connection() calls try to use the broken pool

EXPECTED OUTCOME (Task 20 — bug-condition test, on unfixed code): FAILS
  — when both check() and close() raise, self.pool is still non-None after
    _recover_pool() returns (or raises), confirming the bug.

EXPECTED OUTCOME (Task 21 — preservation test, on unfixed code): PASSES
  — when check() succeeds (soft recovery) or check() raises but close() succeeds,
    the existing behaviour is correct and must not regress.

Documented counterexample (Task 20):
    check_raises=True, close_raises=True
    After _recover_pool():
        self.pool is NOT None  ← BUG: should be None before _initialize_pool()
    Root cause: the second exception from pool.close() propagates before
    self.pool = None is reached, leaving a broken pool reference.
"""

import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings, HealthCheck
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_manager():
    """Build a minimal DatabaseManager without a real DB connection."""
    from shared.database import DatabaseManager

    # Reset singleton for test isolation
    DatabaseManager._instance = None
    mgr = DatabaseManager()
    mgr._health_task = None
    mgr._circuit_breaker = None
    return mgr


def _make_mock_pool(check_raises: bool, close_raises: bool):
    """Build a mock AsyncConnectionPool with configurable raise behaviour."""
    pool = AsyncMock()

    if check_raises:
        pool.check = AsyncMock(side_effect=Exception("pool.check() failed"))
    else:
        pool.check = AsyncMock(return_value=None)

    if close_raises:
        pool.close = AsyncMock(side_effect=RuntimeError("pool.close() failed"))
    else:
        pool.close = AsyncMock(return_value=None)

    return pool


# ---------------------------------------------------------------------------
# Task 20 — Property 1: Bug Condition
# Pool reference not cleared when both check() and close() raise
# ---------------------------------------------------------------------------

@given(
    check_raises=st.just(True),
    close_raises=st.just(True),
)
@h_settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_pool_reference_not_cleared_on_double_raise(check_raises, close_raises):
    """
    **Validates: Requirements 1.8, 2.8**

    Bug condition:
        pool_state.check_raised = True AND pool_state.close_raised = True

    For the case where both pool.check() and pool.close() raise:
      1. Build a DatabaseManager with a mock pool where both raise.
      2. Call _recover_pool().
      3. Assert self.pool is None after the call (correct expected behavior).

    EXPECTED OUTCOME on unfixed code: FAILS
      — self.pool is still non-None because the exception from close() propagates
        before self.pool = None is executed.

    EXPECTED OUTCOME on fixed code: PASSES
      — self.pool is None regardless of whether close() raises (finally guard).

    Documented counterexample:
        check() raises OperationalError, close() raises RuntimeError
        → self.pool still points to broken pool after _recover_pool()
        BUG CONFIRMED: subsequent get_db_connection() calls use broken pool.
    """
    mgr = _make_db_manager()
    mock_pool = _make_mock_pool(check_raises=True, close_raises=True)
    mgr.pool = mock_pool

    # Patch _initialize_pool to be a no-op (we only test pool reference clearing)
    async def _noop_initialize():
        pass

    async def _run():
        with patch.object(mgr, '_initialize_pool', side_effect=_noop_initialize):
            try:
                await mgr._recover_pool()
            except Exception:
                pass  # _recover_pool may raise — we only care about self.pool state

        # Assert: self.pool MUST be None after double-raise (correct behavior)
        # On unfixed code: self.pool is still mock_pool → assertion FAILS → bug confirmed
        assert mgr.pool is None, (
            f"BUG CONFIRMED: self.pool is still non-None after _recover_pool() "
            f"with check_raises=True AND close_raises=True. "
            f"self.pool={mgr.pool!r}. "
            f"A finally: self.pool = None guard is required to guarantee the "
            f"reference is cleared regardless of close() outcome."
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 21 — Property 2: Preservation
# Healthy pool recovery paths unchanged
# ---------------------------------------------------------------------------

@given(
    check_raises=st.booleans(),
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_healthy_pool_recovery_paths_unchanged(check_raises):
    """
    **Validates: Requirements 3.4**

    Preservation: the two existing correct recovery paths must not regress:

    Path A (check_raises=False): pool.check() succeeds → soft recovery, pool unchanged.
    Path B (check_raises=True, close_raises=False): check() raises, close() succeeds
        → self.pool = None, _initialize_pool() called.

    For both paths, the fix must not alter the existing correct behaviour.

    EXPECTED OUTCOME on unfixed code: PASSES
      — both paths already work correctly.

    EXPECTED OUTCOME on fixed code: PASSES
      — fix must not break either path.
    """
    mgr = _make_db_manager()
    # close() always succeeds in this preservation test
    mock_pool = _make_mock_pool(check_raises=check_raises, close_raises=False)
    mgr.pool = mock_pool

    initialize_called = [False]

    async def _mock_initialize():
        initialize_called[0] = True

    async def _run():
        with patch.object(mgr, '_initialize_pool', side_effect=_mock_initialize):
            try:
                await mgr._recover_pool()
            except Exception:
                pass

        if not check_raises:
            # Path A: soft recovery — pool.check() succeeded, pool should be unchanged
            assert mgr.pool is mock_pool, (
                f"REGRESSION (Path A): pool.check() succeeded but self.pool was "
                f"replaced. Expected pool to remain unchanged after soft recovery."
            )
            assert not initialize_called[0], (
                "REGRESSION (Path A): _initialize_pool() was called despite "
                "pool.check() succeeding (soft recovery should not reinitialize)."
            )
        else:
            # Path B: check() raised, close() succeeded → pool must be None and
            # _initialize_pool() must have been called
            assert mgr.pool is None, (
                f"REGRESSION (Path B): check() raised and close() succeeded, "
                f"but self.pool is still non-None. Expected self.pool = None."
            )
            assert initialize_called[0], (
                "REGRESSION (Path B): _initialize_pool() was not called after "
                "check() raised and close() succeeded."
            )

    asyncio.run(_run())
