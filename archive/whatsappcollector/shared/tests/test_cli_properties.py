"""
Property-based tests for tools/config_cli.py

Properties 8, 9, and 10.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Ensure tools/ is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from tools.config_cli import (  # noqa: E402
    build_parser,
    cmd_diff,
    cmd_export,
    cmd_set,
    coerce_value,
    validate_key,
    validate_service,
)
from shared.live_config import PARAMETER_REGISTRY, ParameterMeta  # noqa: E402

# ---------------------------------------------------------------------------
# Strategy helpers (mirrors those in test_live_config_properties.py)
# ---------------------------------------------------------------------------


def _invalid_int_strategy() -> st.SearchStrategy:
    return st.text().filter(
        lambda s: not (s.strip().lstrip("-").isdigit() and s.strip() != "")
    ).filter(lambda s: s != "")


def _invalid_float_strategy() -> st.SearchStrategy:
    return st.sampled_from(["abc", "xyz", "not_a_float", "--1", "1.2.3", "inf!", "nan!"])


def _invalid_bool_strategy() -> st.SearchStrategy:
    valid = {"true", "false", "1", "0", "yes", "no"}
    return st.text(min_size=1).filter(lambda s: s.lower() not in valid)


def _invalid_options_strategy(options: list) -> st.SearchStrategy:
    return st.text(min_size=1).filter(lambda s: s not in options)


def _out_of_range_str_strategy(meta: ParameterMeta) -> st.SearchStrategy | None:
    """Generate string representations of numeric values outside [min_value, max_value]."""
    if meta.min_value is None and meta.max_value is None:
        return None
    if meta.python_type is int:
        strategies = []
        if meta.min_value is not None:
            strategies.append(st.integers(max_value=int(meta.min_value) - 1).map(str))
        if meta.max_value is not None:
            strategies.append(st.integers(min_value=int(meta.max_value) + 1).map(str))
        return st.one_of(*strategies) if strategies else None
    elif meta.python_type is float:
        strategies = []
        if meta.min_value is not None:
            strategies.append(
                st.floats(
                    max_value=meta.min_value - 1e-9,
                    allow_nan=False,
                    allow_infinity=False,
                ).map(str)
            )
        if meta.max_value is not None:
            strategies.append(
                st.floats(
                    min_value=meta.max_value + 1e-9,
                    allow_nan=False,
                    allow_infinity=False,
                ).map(str)
            )
        return st.one_of(*strategies) if strategies else None
    return None


def _invalid_value_strategy(meta: ParameterMeta) -> st.SearchStrategy | None:
    """Return a strategy that generates invalid raw string values for meta, or None if not applicable."""
    if meta.python_type is int:
        return _invalid_int_strategy()
    elif meta.python_type is float:
        return _invalid_float_strategy()
    elif meta.python_type is bool:
        return _invalid_bool_strategy()
    elif meta.python_type is str and meta.options is not None:
        return _invalid_options_strategy(meta.options)
    # str with no options — any string is valid; no type violation possible
    return None


def _invalid_value_strategy_with_range(meta: ParameterMeta) -> st.SearchStrategy | None:
    """Combine type-violation and range-violation strategies."""
    strats = []
    type_strat = _invalid_value_strategy(meta)
    if type_strat is not None:
        strats.append(type_strat)
    range_strat = _out_of_range_str_strategy(meta)
    if range_strat is not None:
        strats.append(range_strat)
    if not strats:
        return None
    return st.one_of(*strats)


def _valid_raw_value_strategy(meta: ParameterMeta) -> st.SearchStrategy:
    """Generate valid string representations for a given ParameterMeta."""
    if meta.python_type is bool:
        return st.sampled_from(["true", "false", "1", "0"])
    elif meta.python_type is int:
        lo = int(meta.min_value) if meta.min_value is not None else -999
        hi = int(meta.max_value) if meta.max_value is not None else 999
        return st.integers(min_value=lo, max_value=hi).map(str)
    elif meta.python_type is float:
        lo = float(meta.min_value) if meta.min_value is not None else -999.0
        hi = float(meta.max_value) if meta.max_value is not None else 999.0
        return st.floats(
            min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False
        ).map(str)
    elif meta.python_type is str and meta.options is not None:
        return st.sampled_from(meta.options)
    else:
        return st.text(min_size=1)


# ---------------------------------------------------------------------------
# Collect all (service, meta) pairs that have at least one invalid strategy
# ---------------------------------------------------------------------------

_TESTABLE_PAIRS: list[tuple[str, ParameterMeta]] = [
    (service, meta)
    for service, params in PARAMETER_REGISTRY.items()
    for meta in params
    if _invalid_value_strategy_with_range(meta) is not None
]

# ---------------------------------------------------------------------------
# Property 8: CLI set validates before writing
#
# For any (service, key, value) where value violates constraints, assert
# sys.exit(1) is called and redis.hset is NOT called.
#
# **Validates: Requirements 6.9, 6.10, 6.11**
# ---------------------------------------------------------------------------


def _make_property8_test(service: str, meta: ParameterMeta):
    """Return a @given test for a single (service, key) pair."""
    invalid_strat = _invalid_value_strategy_with_range(meta)
    assert invalid_strat is not None

    @given(raw_value=invalid_strat)
    @h_settings(max_examples=30)
    def _test(raw_value: str) -> None:
        """
        **Validates: Requirements 6.9, 6.10, 6.11**
        """
        mock_redis = MagicMock()
        args = argparse.Namespace(service=service, key=meta.key, value=raw_value)

        with patch("tools.config_cli._get_redis", return_value=mock_redis):
            with pytest.raises(SystemExit) as exc_info:
                cmd_set(args)

        assert exc_info.value.code == 1, (
            f"[{service}.{meta.key}] expected exit code 1 for invalid value {raw_value!r}, "
            f"got {exc_info.value.code}"
        )
        mock_redis.hset.assert_not_called(), (
            f"[{service}.{meta.key}] Redis hset was called despite invalid value {raw_value!r}"
        )

    return _test


# Register dynamic tests for every testable (service, key) pair
for _service, _meta in _TESTABLE_PAIRS:
    _p8_test = _make_property8_test(_service, _meta)
    _p8_name = f"test_cli_set_validates_before_writing__{_service}__{_meta.key}"
    _p8_test.__name__ = _p8_name
    globals()[_p8_name] = _p8_test


# ---------------------------------------------------------------------------
# Property 9: CLI diff output is exactly the set of overridden parameters
#
# Generate arbitrary Redis states (dict of service → {key: value} overrides).
# Mock _get_redis().hgetall to return the generated state.
# Capture stdout from cmd_diff.
# Assert the output contains exactly the keys whose live value differs from
# the env default.
#
# **Validates: Requirements 6.7**
# ---------------------------------------------------------------------------

# Build a flat list of all (service, key) pairs for use in dict strategies
_ALL_PAIRS: list[tuple[str, ParameterMeta]] = [
    (service, meta)
    for service, params in PARAMETER_REGISTRY.items()
    for meta in params
]

# Strategy: generate a dict of {service: {key: raw_value}} overrides
# We use a small subset to keep hypothesis fast
_OVERRIDE_STRATEGY = st.dictionaries(
    keys=st.sampled_from(_ALL_PAIRS),
    values=st.none(),  # placeholder; we'll generate values per meta below
    min_size=0,
    max_size=8,
)


def _build_redis_state(overrides: dict[tuple[str, ParameterMeta], str]) -> dict[str, dict[str, str]]:
    """Convert flat {(service, meta): raw_value} to {service: {key: raw_value}}."""
    state: dict[str, dict[str, str]] = {}
    for (service, meta), raw_value in overrides.items():
        state.setdefault(service, {})[meta.key] = raw_value
    return state


@given(
    overrides=st.lists(
        st.tuples(
            st.sampled_from(_ALL_PAIRS),
            # Generate a valid raw value for the meta (we'll use defaults to guarantee
            # some entries differ and some don't)
            st.booleans(),  # True = use default (no diff), False = use a non-default value
        ),
        min_size=0,
        max_size=10,
    )
)
@h_settings(max_examples=50)
def test_cli_diff_output_is_exactly_overridden_parameters(
    overrides: list[tuple[tuple[str, ParameterMeta], bool]],
) -> None:
    """
    Property 9: CLI diff output is exactly the set of overridden parameters.

    **Validates: Requirements 6.7**
    """
    # Build a Redis state: for each (service, meta, use_default) triple,
    # either store the default (no diff) or a clearly different value.
    redis_state: dict[str, dict[str, str]] = {}
    expected_diffs: set[str] = set()  # "service.key" labels that should appear in diff

    for (service, meta), use_default in overrides:
        if use_default:
            # Store the default value — should NOT appear in diff
            raw = str(meta.default)
        else:
            # Store a value that is guaranteed to differ from the default.
            # We use a sentinel that is always different from the real default.
            # For bool, flip it. For numeric, add/subtract 0 if range allows, else use default.
            # Simplest approach: use a string that differs from str(meta.default).
            raw = _non_default_raw(meta)

        redis_state.setdefault(service, {})[meta.key] = raw

        label = f"{service}.{meta.key}"
        if raw != str(meta.default):
            expected_diffs.add(label)
        else:
            expected_diffs.discard(label)

    # Build mock redis: hgetall returns the per-service dict
    mock_redis = MagicMock()

    def _hgetall(redis_key: str) -> dict[str, str]:
        # redis_key is "live_config:{service}"
        service_name = redis_key.replace("live_config:", "", 1)
        return redis_state.get(service_name, {})

    mock_redis.hgetall.side_effect = _hgetall

    # Capture stdout
    buf = io.StringIO()
    args = argparse.Namespace()

    with patch("tools.config_cli._get_redis", return_value=mock_redis):
        with contextlib.redirect_stdout(buf):
            cmd_diff(args)

    output = buf.getvalue()

    # Parse the output lines to extract "service.key" labels
    # Plain-text format: "service.key: default → live_value"
    # Rich format: table rows — we disable rich by checking _RICH flag
    # Since tests run without rich (or with it), we need to handle both.
    # The plain-text format is: "{label}: {default} → {live_value}"
    # The rich format renders a table; we can't easily parse it, so we
    # patch _RICH to False to force plain-text output.
    # Re-run with _RICH forced to False:
    buf2 = io.StringIO()
    with patch("tools.config_cli._get_redis", return_value=mock_redis):
        with patch("tools.config_cli._RICH", False):
            with contextlib.redirect_stdout(buf2):
                cmd_diff(args)

    output = buf2.getvalue()

    if not expected_diffs:
        # No diffs — output should say "No live overrides differ from defaults."
        assert "No live overrides" in output or output.strip() == "", (
            f"Expected no-diff message, got: {output!r}"
        )
        return

    # Parse labels from plain-text output lines like "service.key: default → live_value"
    found_labels: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("★") or line.startswith("No live"):
            continue
        # Format: "service.key: default → live_value"
        if ": " in line and " → " in line:
            label = line.split(":")[0].strip()
            found_labels.add(label)

    assert found_labels == expected_diffs, (
        f"diff output mismatch:\n"
        f"  expected: {sorted(expected_diffs)}\n"
        f"  found:    {sorted(found_labels)}\n"
        f"  output:   {output!r}"
    )


def _non_default_raw(meta: ParameterMeta) -> str:
    """Return a raw string value that differs from str(meta.default)."""
    default_str = str(meta.default)
    if meta.python_type is bool:
        # Flip the bool
        return "false" if meta.default else "true"
    elif meta.python_type is int:
        # Try default + 1 if within range, else default - 1
        candidate = meta.default + 1
        if meta.max_value is not None and candidate > meta.max_value:
            candidate = meta.default - 1
        if meta.min_value is not None and candidate < meta.min_value:
            # Can't differ — fall back to default (test will skip this entry)
            return default_str
        return str(candidate)
    elif meta.python_type is float:
        candidate = meta.default + 0.001
        if meta.max_value is not None and candidate > meta.max_value:
            candidate = meta.default - 0.001
        if meta.min_value is not None and candidate < meta.min_value:
            return default_str
        return str(candidate)
    elif meta.python_type is str and meta.options is not None:
        # Pick a different option if available
        others = [o for o in meta.options if o != default_str]
        if others:
            return others[0]
        return default_str
    else:
        # Free-form str: append a suffix
        return default_str + "_OVERRIDE"


# ---------------------------------------------------------------------------
# Property 10: CLI export produces a re-importable .env snippet
#
# Generate arbitrary Redis states.
# Capture stdout from cmd_export.
# Parse the output as a .env file (split on '=', skip comment lines).
# Assert that the parsed values match the Redis state for all keys that were set.
#
# **Validates: Requirements 6.8**
# ---------------------------------------------------------------------------


@given(
    overrides=st.lists(
        st.tuples(
            st.sampled_from(_ALL_PAIRS),
            st.booleans(),  # True = use default, False = use non-default
        ),
        min_size=0,
        max_size=10,
    )
)
@h_settings(max_examples=50)
def test_cli_export_produces_reimportable_env_snippet(
    overrides: list[tuple[tuple[str, ParameterMeta], bool]],
) -> None:
    """
    Property 10: CLI export produces a re-importable .env snippet.

    **Validates: Requirements 6.8**
    """
    # Build Redis state
    redis_state: dict[str, dict[str, str]] = {}
    # Track what we expect to see in the export: {key: raw_value} for all set keys
    expected_exports: dict[str, str] = {}  # key → raw_value

    for (service, meta), use_default in overrides:
        raw = str(meta.default) if use_default else _non_default_raw(meta)
        redis_state.setdefault(service, {})[meta.key] = raw
        expected_exports[meta.key] = raw

    mock_redis = MagicMock()

    def _hgetall(redis_key: str) -> dict[str, str]:
        service_name = redis_key.replace("live_config:", "", 1)
        return redis_state.get(service_name, {})

    mock_redis.hgetall.side_effect = _hgetall

    args = argparse.Namespace()
    buf = io.StringIO()

    with patch("tools.config_cli._get_redis", return_value=mock_redis):
        with contextlib.redirect_stdout(buf):
            cmd_export(args)

    output = buf.getvalue()

    # Parse the .env output: skip comment lines (starting with #) and blank lines
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()

    # cmd_export iterates services in sorted order and outputs key=value without service prefix.
    # If the same key appears in multiple services, the last service (alphabetically) wins.
    # Build the expected parsed output by simulating the same iteration order.
    expected_parsed: dict[str, str] = {}
    for service in sorted(PARAMETER_REGISTRY.keys()):
        service_hash = redis_state.get(service, {})
        if not service_hash:
            continue
        for meta in PARAMETER_REGISTRY[service]:
            if meta.key in service_hash:
                expected_parsed[meta.key] = service_hash[meta.key]

    # Every key in expected_parsed must appear in parsed with the correct value
    for key, expected_raw in expected_parsed.items():
        assert key in parsed, (
            f"Key {key!r} not found in export output.\n"
            f"Output:\n{output}"
        )
        assert parsed[key] == expected_raw, (
            f"Export value mismatch for {key!r}: "
            f"expected {expected_raw!r}, got {parsed[key]!r}"
        )

    # No extra keys should appear in parsed beyond what's in expected_parsed
    extra_keys = set(parsed.keys()) - set(expected_parsed.keys())
    assert not extra_keys, (
        f"Unexpected keys in export output: {extra_keys}\nOutput:\n{output}"
    )
