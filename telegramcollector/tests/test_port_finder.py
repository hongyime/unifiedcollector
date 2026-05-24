"""
P2.1 Bug-Condition Exploration Test — Hardcoded Dashboard Port Causes Crash-Loop

Validates: Requirements 1.5, 2.5, 3.7

Bug condition:
    port IN [8501..8505] AND host_state.port_bound(port) = True

The current docker-compose.yml uses hardcoded --server.port=850X for each
Streamlit dashboard. There is no shared/port_finder.py. When the primary port
is already bound by another process, the streamlit command exits with a bind
error and the container enters a crash-loop restart, never attempting the next
available port.

EXPECTED OUTCOME (Task 11 — bug-condition test, on unfixed code): FAILS
  — shared/port_finder.py does not exist, so importing find_free_port raises
    ModuleNotFoundError, confirming the bug: no fallback mechanism exists.

EXPECTED OUTCOME (Task 12 — preservation test, on unfixed code): PASSES
  — when the primary port is free, raw socket.bind() succeeds immediately,
    confirming the free-port path works correctly without any fix needed.

Documented counterexample (Task 11):
    pre_bound_ports = {8502}   (8501 may be occupied on this host)
    Attempt: socket.bind(("0.0.0.0", 8502)) — first bind succeeds (we hold it)
    Attempt: socket.bind(("0.0.0.0", 8502)) — second bind raises OSError
    Expected (correct behavior): find_free_port(8502, 4) returns 8503
    Actual (buggy behavior):     ModuleNotFoundError: No module named 'shared.port_finder'
                                 — no fallback, process would crash-loop

    Root cause: docker-compose.yml uses hardcoded --server.port=850X with no
    fallback logic. shared/port_finder.py does not exist yet (created in Task 13).
"""

import socket
import sys
import os

import pytest
from hypothesis import given, settings as h_settings, HealthCheck, assume
from hypothesis import strategies as st

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _try_bind_ports(ports: set) -> tuple[list[socket.socket], set]:
    """
    Attempt to bind each port in `ports` on 0.0.0.0 (no SO_REUSEADDR).
    Returns (successfully_bound_sockets, successfully_bound_ports).
    Ports already occupied by external processes are silently skipped.
    """
    sockets = []
    bound = set()
    for port in sorted(ports):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            sockets.append(s)
            bound.add(port)
        except OSError:
            s.close()
            # Port already occupied externally — skip it
    return sockets, bound


def _release_sockets(sockets: list[socket.socket]) -> None:
    """Close all sockets in the list, ignoring errors."""
    for s in sockets:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Task 11 — Property 1: Bug Condition
# Hardcoded port binding raises OSError with no fallback
# ---------------------------------------------------------------------------

@given(
    pre_bound_ports=st.sets(st.integers(8501, 8505), min_size=1)
)
@h_settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_hardcoded_port_raises_oserror_no_fallback(pre_bound_ports):
    """
    **Validates: Requirements 1.5, 2.5**

    Bug condition:
        port IN [8501..8505] AND host_state.port_bound(port) = True

    For each set of pre-bound ports (1–5 ports from [8501, 8505]):
      1. Bind those ports with real sockets (simulating another process holding them).
      2. Attempt socket.bind(("0.0.0.0", primary_port)) directly (simulating
         the hardcoded --server.port=850X approach in docker-compose.yml).
      3. Assert OSError is raised (confirming the bug — no fallback exists).
      4. Assert that find_free_port from shared.port_finder returns a free port
         (the CORRECT expected behavior) — this FAILS because shared/port_finder.py
         does not exist yet (created in Task 13).

    EXPECTED OUTCOME on unfixed code: FAILS
      — ModuleNotFoundError: No module named 'shared.port_finder'

    EXPECTED OUTCOME on fixed code: PASSES
      — find_free_port(primary_port, 4) returns the first free port in range.

    Documented counterexample:
        pre_bound_ports = {8502}
        socket.bind(("0.0.0.0", 8502)) [first]  → succeeds (we hold it)
        socket.bind(("0.0.0.0", 8502)) [second] → OSError (address in use)
        find_free_port(8502, 4) → ModuleNotFoundError (module doesn't exist yet)
        BUG CONFIRMED: no fallback mechanism, process would crash-loop.
    """
    bound_sockets, actually_bound = _try_bind_ports(pre_bound_ports)

    try:
        # Skip if we couldn't bind any ports (all occupied externally)
        assume(len(actually_bound) > 0)

        primary_port = min(actually_bound)

        # Simulate the hardcoded approach: try to bind the same port again
        # (no SO_REUSEADDR — plain socket like streamlit uses)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_raised_oserror = False
        try:
            probe.bind(("0.0.0.0", primary_port))
            probe.close()
        except OSError:
            probe.close()
            bind_raised_oserror = True

        # OSError must be raised — confirms the hardcoded approach fails
        assert bind_raised_oserror, (
            f"Expected OSError when binding already-bound port {primary_port}, "
            f"but bind succeeded. actually_bound={actually_bound}."
        )

        # Ensure at least one port in [primary_port, primary_port+4] is free;
        # otherwise find_free_port correctly raises RuntimeError (not a bug).
        search_range = range(primary_port, primary_port + 5)
        assume(any(p not in actually_bound for p in search_range))

        # Now assert the CORRECT behavior: find_free_port should return a free port.
        # On unfixed code this raises ModuleNotFoundError → test FAILS → bug confirmed.
        # On fixed code (Task 13) this returns the first free port in range.
        from shared.port_finder import find_free_port  # noqa: F401 — expected to fail

        chosen = find_free_port(primary_port, 4)
        assert chosen not in actually_bound, (
            f"find_free_port({primary_port}, 4) returned {chosen} which is "
            f"already bound. actually_bound={actually_bound}"
        )

    finally:
        _release_sockets(bound_sockets)


# ---------------------------------------------------------------------------
# Task 12 — Property 2: Preservation
# Primary port binding unchanged when port is free
# ---------------------------------------------------------------------------

@given(
    primary_port=st.sampled_from([8501, 8502, 8503, 8504, 8505])
)
@h_settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_primary_port_binding_succeeds_when_free(primary_port):
    """
    **Validates: Requirements 3.7**

    Preservation: when the primary port is free, socket.bind(("0.0.0.0", primary_port))
    succeeds immediately — no pre-binding, no interference.

    For all primary ports in [8501, 8502, 8503, 8504, 8505] with no pre-binding:
      1. Attempt socket.bind(("0.0.0.0", primary_port)) directly.
      2. Assert it succeeds (no OSError).

    EXPECTED OUTCOME on unfixed code: PASSES
      — free-port path works correctly without any fix needed.

    EXPECTED OUTCOME on fixed code: PASSES
      — free primary port must still be returned as-is (no regression).

    Non-bug condition: host_state.port_bound(primary_port) = False
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            s.bind(("0.0.0.0", primary_port))
        except OSError as e:
            # Port is already in use by something on the host — skip this example
            # (not a test failure; the port is legitimately occupied externally)
            pytest.skip(
                f"Port {primary_port} is already in use on this host: {e}. "
                "Skipping — this is a host-state issue, not a code bug."
            )
        # Bind succeeded — preservation confirmed: free port binds without error
    finally:
        s.close()
