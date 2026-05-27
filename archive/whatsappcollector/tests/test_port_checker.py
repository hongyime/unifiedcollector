"""
Property tests for PortChecker (check_ports.sh logic simulated in Python)

Property 1: Port Selection Invariant
**Validates: Requirements 2.4**

FOR ALL sets of in-use ports U and configured port p, IF auto_increment mode
is active and a free port q is selected, THEN q is not in U AND q >= p+1 AND
q <= p+10.

Property 2: No Duplicate Port Assignment
**Validates: Requirements 2.4**

FOR ALL pairs of services (s1, s2) where s1 ≠ s2, the resolved host port of
s1 SHALL NOT equal the resolved host port of s2.

Property 3: PortChecker Idempotence
**Validates: Requirements 2.4**

Running PortChecker twice with the same environment and no port conflicts SHALL
produce identical output both times.
"""

from __future__ import annotations

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Python simulation of check_ports.sh logic
# ---------------------------------------------------------------------------

def find_free_port(base: int, in_use: set[int]) -> int | None:
    """Simulate find_free_port(base): scan base+1..base+10, return first free."""
    for i in range(1, 11):
        candidate = base + i
        if candidate not in in_use:
            return candidate
    return None


def resolve_ports(
    port_vars: dict[str, int],
    in_use: set[int],
    strategy: str = "auto_increment",
) -> dict[str, int] | None:
    """
    Simulate the main loop of check_ports.sh.

    Returns a dict of {var_name: resolved_port} on success, or None if
    strategy=fail_fast and any conflict is found (or exhaustion occurs).
    """
    resolved: dict[str, int] = {}
    fatal = False

    for var, port in port_vars.items():
        if port in in_use:
            if strategy == "auto_increment":
                free = find_free_port(port, in_use)
                if free is None:
                    fatal = True
                    continue
                resolved[var] = free
                # The newly assigned port is now "taken" for subsequent services
                in_use = in_use | {free}
            else:
                fatal = True
        else:
            resolved[var] = port

    if fatal:
        return None
    return resolved


# ---------------------------------------------------------------------------
# Property 1: Port Selection Invariant
# ---------------------------------------------------------------------------

@given(
    base_port=st.integers(min_value=1024, max_value=65520),
    in_use_offsets=st.frozensets(st.integers(min_value=0, max_value=10), max_size=9),
)
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_port_selection_invariant(base_port: int, in_use_offsets: frozenset[int]) -> None:
    """
    **Validates: Requirements 2.4**

    Property 1: FOR ALL sets of in-use ports U and configured port p,
    IF auto_increment mode is active and a free port q is selected,
    THEN q is not in U AND q >= p+1 AND q <= p+10.
    """
    in_use = {base_port + offset for offset in in_use_offsets}
    # Ensure base_port itself is "in use" so auto_increment is triggered
    in_use.add(base_port)

    result = find_free_port(base_port, in_use)

    if result is not None:
        # Core invariant: selected port must not be in use
        assert result not in in_use, (
            f"Selected port {result} is still in use set {in_use}"
        )
        # Core invariant: selected port must be within [base+1, base+10]
        assert base_port + 1 <= result <= base_port + 10, (
            f"Selected port {result} is outside range [{base_port+1}, {base_port+10}]"
        )
    else:
        # All 10 candidates were in use — exhaustion is valid when all are taken
        candidates = set(range(base_port + 1, base_port + 11))
        assert candidates.issubset(in_use), (
            f"find_free_port returned None but not all candidates {candidates} "
            f"are in use set {in_use}"
        )


# ---------------------------------------------------------------------------
# Property 2: No Duplicate Port Assignment
# ---------------------------------------------------------------------------

@given(
    num_services=st.integers(min_value=2, max_value=15),
    base_ports_seed=st.integers(min_value=3000, max_value=9000),
    in_use_extra=st.frozensets(st.integers(min_value=3000, max_value=9100), max_size=20),
)
@settings(
    max_examples=150,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_no_duplicate_port_assignment(
    num_services: int,
    base_ports_seed: int,
    in_use_extra: frozenset[int],
) -> None:
    """
    **Validates: Requirements 2.4**

    Property 2: FOR ALL pairs of services (s1, s2) where s1 ≠ s2, the
    resolved host port of s1 SHALL NOT equal the resolved host port of s2.
    """
    # Build a set of distinct base ports for each service (spaced 20 apart
    # to avoid accidental overlap in the base assignments themselves)
    port_vars: dict[str, int] = {}
    for i in range(num_services):
        port_vars[f"SERVICE_{i}_PORT"] = base_ports_seed + i * 20

    in_use = set(in_use_extra)

    result = resolve_ports(port_vars, in_use, strategy="auto_increment")

    if result is None:
        # Exhaustion occurred — skip this example (not a property violation)
        return

    resolved_ports = list(result.values())

    # Core property: all resolved ports must be unique
    assert len(resolved_ports) == len(set(resolved_ports)), (
        f"Duplicate port assignments detected: {resolved_ports}"
    )


# ---------------------------------------------------------------------------
# Property 3: PortChecker Idempotence
# ---------------------------------------------------------------------------

@given(
    port_vars=st.fixed_dictionaries({
        "WA_CLIENT_1_HOST_PORT": st.just(3011),
        "WA_CLIENT_2_HOST_PORT": st.just(3012),
        "COLLECTOR_DASHBOARD_PORT": st.just(8501),
        "COLLECTOR_METRICS_PORT": st.just(9090),
        "MEDIA_ARCHIVAL_DASHBOARD_PORT": st.just(8502),
        "MEDIA_ARCHIVAL_METRICS_PORT": st.just(9091),
    }),
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=5000,
)
def test_port_checker_idempotence(port_vars: dict[str, int]) -> None:
    """
    **Validates: Requirements 2.4**

    Property 3: Running PortChecker twice with the same environment and no
    port conflicts SHALL produce identical output both times.
    """
    # No ports are in use — clean environment
    in_use: set[int] = set()

    result_1 = resolve_ports(dict(port_vars), in_use.copy(), strategy="auto_increment")
    result_2 = resolve_ports(dict(port_vars), in_use.copy(), strategy="auto_increment")

    assert result_1 is not None, "First run unexpectedly failed"
    assert result_2 is not None, "Second run unexpectedly failed"

    # Core property: both runs must produce identical resolved port mappings
    assert result_1 == result_2, (
        f"PortChecker is not idempotent:\n  run1={result_1}\n  run2={result_2}"
    )

    # Additionally: when no conflicts exist, resolved ports equal configured ports
    for var, port in port_vars.items():
        assert result_1[var] == port, (
            f"Expected {var}={port} (no conflict), got {result_1[var]}"
        )
