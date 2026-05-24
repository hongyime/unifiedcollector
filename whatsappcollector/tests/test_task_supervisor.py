"""
Property test for TaskSupervisor — Property 8: CancelledError Never Restarts

**Validates: Requirements 5.3**

FOR ALL supervised tasks `t`, IF `t` raises `asyncio.CancelledError`,
THEN the restart count of `t` SHALL NOT increase.
"""

import asyncio
import sys
import os

# Ensure workspace root is on sys.path so `shared.task_supervisor` is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from shared.task_supervisor import TaskSupervisor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_supervisor_briefly(supervisor: TaskSupervisor, duration: float = 0.1) -> None:
    """Start the supervisor, let it run for `duration` seconds, then stop it."""
    await supervisor.start()
    await asyncio.sleep(duration)
    await supervisor.stop()


# ---------------------------------------------------------------------------
# Property 8: CancelledError Never Restarts
# ---------------------------------------------------------------------------

@given(
    # Generate a small positive delay to vary timing slightly across examples
    delay=st.floats(min_value=0.0, max_value=0.05),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,  # 5 s per example — generous for async tests
)
def test_cancelled_error_never_restarts(delay: float) -> None:
    """
    **Validates: Requirements 5.3**

    Property 8: FOR ALL supervised tasks `t`, IF `t` raises
    `asyncio.CancelledError`, THEN the restart count of `t` SHALL NOT increase.
    """

    async def _inner() -> None:
        async def cancelled_coro() -> None:
            # Simulate a tiny bit of work then raise CancelledError
            await asyncio.sleep(delay)
            raise asyncio.CancelledError()

        supervisor = TaskSupervisor(
            name="test_cancelled",
            coro_fn=cancelled_coro,
            restart_delay=0.0,  # no delay so the test stays fast
        )

        await _run_supervisor_briefly(supervisor, duration=max(delay + 0.05, 0.1))

        # Core property: CancelledError must never trigger a restart
        assert supervisor.restart_count == 0, (
            f"restart_count should be 0 after CancelledError, got {supervisor.restart_count}"
        )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Concrete / example-based test (complements the property test)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_error_does_not_increment_restart_count() -> None:
    """
    Concrete example: a coroutine that immediately raises CancelledError
    must leave restart_count == 0.
    """
    call_count = 0

    async def immediate_cancel() -> None:
        nonlocal call_count
        call_count += 1
        raise asyncio.CancelledError()

    supervisor = TaskSupervisor(
        name="immediate_cancel",
        coro_fn=immediate_cancel,
        restart_delay=0.0,
    )

    await supervisor.start()
    await asyncio.sleep(0.05)
    await supervisor.stop()

    assert supervisor.restart_count == 0, (
        f"CancelledError must not increment restart_count; got {supervisor.restart_count}"
    )


@pytest.mark.asyncio
async def test_regular_exception_does_increment_restart_count() -> None:
    """
    Sanity check: a regular exception SHOULD increment restart_count,
    confirming the CancelledError path is genuinely different.
    """
    async def crashing_coro() -> None:
        raise RuntimeError("boom")

    supervisor = TaskSupervisor(
        name="crashing",
        coro_fn=crashing_coro,
        restart_delay=0.01,  # very short so we get at least one restart
    )

    await supervisor.start()
    await asyncio.sleep(0.15)  # enough time for at least one restart cycle
    await supervisor.stop()

    assert supervisor.restart_count >= 1, (
        f"RuntimeError should have triggered at least one restart; got {supervisor.restart_count}"
    )


# ---------------------------------------------------------------------------
# Property 9: Exception Always Restarts with Delay
# ---------------------------------------------------------------------------

@given(
    exc_type=st.sampled_from([ValueError, RuntimeError, OSError, TypeError, KeyError]),
    # Use 50ms minimum to stay well above Windows' ~15ms timer resolution
    restart_delay=st.floats(min_value=0.05, max_value=0.10),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=10000,  # 10 s per example — generous for async tests with delays
)
def test_exception_always_restarts_with_delay(
    exc_type: type, restart_delay: float
) -> None:
    """
    **Validates: Requirements 5.1, 5.6**

    Property 9: FOR ALL supervised tasks `t` and exceptions `e` where `e` is
    not `asyncio.CancelledError`, IF `t` raises `e`, THEN `t` SHALL be
    restarted and the restart count of `t` SHALL increase by 1.

    Also: the time between task termination and task re-launch SHALL be at
    least `restart_delay` seconds.
    """

    async def _inner() -> None:
        crashed_at: list[float] = []
        restarted_at: list[float] = []
        call_count = 0

        async def flaky_coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: record crash time and raise the generated exception
                crashed_at.append(asyncio.get_event_loop().time())
                raise exc_type("test exception")
            else:
                # Subsequent calls: record restart time and complete normally
                restarted_at.append(asyncio.get_event_loop().time())
                # Sleep long enough that the supervisor doesn't loop again
                await asyncio.sleep(10)

        supervisor = TaskSupervisor(
            name="test_exception_restart",
            coro_fn=flaky_coro,
            restart_delay=restart_delay,
        )

        # Run long enough for: crash + restart_delay + restart execution
        # Use a generous multiplier (5x) to account for Windows scheduler jitter
        run_duration = restart_delay * 5 + 0.3
        await supervisor.start()
        await asyncio.sleep(run_duration)
        await supervisor.stop()

        # Core property: exception must trigger a restart
        assert supervisor.restart_count >= 1, (
            f"restart_count should be >= 1 after {exc_type.__name__}, "
            f"got {supervisor.restart_count}"
        )

        # Delay property: restart must not happen before restart_delay has elapsed
        assert len(crashed_at) >= 1, "Coroutine should have crashed at least once"
        assert len(restarted_at) >= 1, (
            f"Coroutine should have been restarted at least once "
            f"(restart_delay={restart_delay:.3f}s, run_duration={run_duration:.3f}s)"
        )

        elapsed = restarted_at[0] - crashed_at[0]
        # Use 50% tolerance to accommodate Windows scheduler granularity (~15ms)
        assert elapsed >= restart_delay * 0.5, (
            f"Restart happened too soon: elapsed={elapsed:.4f}s, "
            f"restart_delay={restart_delay:.4f}s"
        )

    asyncio.run(_inner())


# ---------------------------------------------------------------------------
# Property 10: Flap Detection Threshold
# ---------------------------------------------------------------------------

from unittest.mock import patch


@given(
    restart_count=st.integers(min_value=11, max_value=20),
)
@settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=30000,  # 30 s — we may run up to 20 restart cycles
)
def test_flap_detection_triggers_warning_above_threshold(restart_count: int) -> None:
    """
    **Validates: Requirements 5.6**

    Property 10: FOR ALL supervised tasks `t`, WHEN `t` has been restarted
    more than 10 times within a 10-minute window, a WARNING
    `supervised_task_flapping` SHALL be logged.
    """

    async def _inner() -> None:
        call_counter = [0]

        async def always_fails() -> None:
            call_counter[0] += 1
            raise RuntimeError("deliberate failure")

        supervisor = TaskSupervisor(
            name="flapping_task",
            coro_fn=always_fails,
            restart_delay=0.0,
        )

        warning_calls: list[str] = []

        # Patch structlog's warning on the module-level logger used by TaskSupervisor
        with patch("shared.task_supervisor.logger") as mock_logger:
            await supervisor.start()

            # Wait until we have accumulated enough restarts
            # Each restart cycle: coro raises → _record_restart → sleep(0) → repeat
            # With restart_delay=0.0 this is very fast
            deadline = asyncio.get_event_loop().time() + 10.0
            while supervisor.restart_count < restart_count:
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.01)

            await supervisor.stop()

            # Collect all warning calls
            warning_calls = [
                str(c)
                for c in mock_logger.warning.call_args_list
                if "supervised_task_flapping" in str(c)
            ]

        # Core property: flapping warning must have been emitted at least once
        assert len(warning_calls) >= 1, (
            f"Expected at least one 'supervised_task_flapping' WARNING after "
            f"{supervisor.restart_count} restarts (threshold=10), but got none. "
            f"All warning calls: {mock_logger.warning.call_args_list}"
        )

    asyncio.run(_inner())


@given(
    restart_count=st.integers(min_value=1, max_value=10),
)
@settings(
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=15000,
)
def test_flap_detection_does_not_trigger_below_threshold(restart_count: int) -> None:
    """
    **Validates: Requirements 5.6**

    Negative case for Property 10: WHEN a task has been restarted 10 or fewer
    times within a 10-minute window, the `supervised_task_flapping` WARNING
    SHALL NOT be emitted.
    """

    async def _inner() -> None:
        call_counter = [0]
        stop_event = asyncio.Event()

        async def fails_then_blocks() -> None:
            call_counter[0] += 1
            if call_counter[0] <= restart_count:
                raise RuntimeError("deliberate failure")
            # After reaching the target restart count, block until stopped
            await stop_event.wait()

        supervisor = TaskSupervisor(
            name="non_flapping_task",
            coro_fn=fails_then_blocks,
            restart_delay=0.0,
        )

        with patch("shared.task_supervisor.logger") as mock_logger:
            await supervisor.start()

            # Wait until we've hit exactly restart_count restarts
            deadline = asyncio.get_event_loop().time() + 10.0
            while supervisor.restart_count < restart_count:
                if asyncio.get_event_loop().time() > deadline:
                    break
                await asyncio.sleep(0.01)

            # Give the loop one more tick to settle into the blocking coro
            await asyncio.sleep(0.02)
            stop_event.set()
            await supervisor.stop()

            flapping_warnings = [
                c for c in mock_logger.warning.call_args_list
                if "supervised_task_flapping" in str(c)
            ]

        # Negative property: no flapping warning for <= 10 restarts
        assert len(flapping_warnings) == 0, (
            f"Expected NO 'supervised_task_flapping' WARNING for {restart_count} restarts "
            f"(threshold=10), but got {len(flapping_warnings)}: {flapping_warnings}"
        )

    asyncio.run(_inner())
