"""
Property-based tests for shared/live_config.py

**Validates: Requirements 1.2, 1.3, 2.2**
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

import asyncio
from unittest.mock import AsyncMock

from shared.live_config import (
    PARAMETER_REGISTRY,
    ConfigOverlay,
    ConfigValidationError,
    ParameterMeta,
)

# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

_TYPE_STRATEGIES: dict[type, st.SearchStrategy] = {
    bool: st.booleans(),
    int: st.integers(),
    float: st.floats(allow_nan=False, allow_infinity=False),
    str: st.text(),
}


def strategy_for(python_type: type) -> st.SearchStrategy:
    return _TYPE_STRATEGIES[python_type]


# ---------------------------------------------------------------------------
# Helper: build a minimal mock settings object for a service
# ---------------------------------------------------------------------------

def make_mock_settings(service_name: str) -> SimpleNamespace:
    """Return a SimpleNamespace with all defaults from PARAMETER_REGISTRY for service_name."""
    params = PARAMETER_REGISTRY.get(service_name, [])
    attrs = {meta.key: meta.default for meta in params}
    return SimpleNamespace(**attrs)


def make_overlay(service_name: str) -> ConfigOverlay:
    """Return a ConfigOverlay with a mock settings object and no Redis connection."""
    settings = make_mock_settings(service_name)
    # Pass a dummy redis_url; the overlay will fail to connect but that's fine —
    # we only exercise the in-process _live dict and env-default path.
    return ConfigOverlay(
        settings=settings,
        service_name=service_name,
        redis_url="redis://localhost:0",  # unreachable — no Redis needed for these tests
    )


def test_get_env_default_falls_back_to_registry_default_when_settings_key_missing() -> None:
    """Missing settings attributes should fall back to registry defaults instead of crashing."""
    partial_settings = SimpleNamespace(LOG_LEVEL="DEBUG")
    overlay = ConfigOverlay(
        settings=partial_settings,
        service_name="collector",
        redis_url="redis://localhost:0",
    )

    assert overlay.get_env_default("LANGUAGE_WHITELIST") == ""
    assert overlay.get("LANGUAGE_WHITELIST") == ""


def test_get_all_uses_registry_defaults_for_missing_settings_keys() -> None:
    """get_all() should include registry defaults for keys absent on settings objects."""
    partial_settings = SimpleNamespace(LOG_LEVEL="WARNING")
    overlay = ConfigOverlay(
        settings=partial_settings,
        service_name="collector",
        redis_url="redis://localhost:0",
    )

    merged = overlay.get_all()

    assert merged["LOG_LEVEL"] == "WARNING"
    assert merged["LANGUAGE_WHITELIST"] == ""
    assert merged["SESSION_RISK_THRESHOLD"] == 0.8


# ---------------------------------------------------------------------------
# Property 1: get() always returns a type-valid value
#
# For every service and every registered parameter key K:
#   - When _live[K] is set to a generated value of the correct python_type,
#     type(overlay.get(K)) == meta.python_type
#   - When _live is empty, overlay.get(K) returns the env default which is
#     already type-valid.
# ---------------------------------------------------------------------------

def _make_type_valid_test(service_name: str, meta: ParameterMeta):
    """
    Return a Hypothesis @given test function for a single (service, key) pair.
    We generate values of the correct python_type (or None to simulate absent).
    """

    @given(live_value=st.one_of(st.none(), strategy_for(meta.python_type)))
    @h_settings(max_examples=50)
    def _test(live_value: Any) -> None:
        overlay = make_overlay(service_name)

        if live_value is None:
            # Simulate key absent from _live — get() should return env default
            overlay._live.clear()
            result = overlay.get(meta.key)
            assert type(result) == meta.python_type, (
                f"[{service_name}.{meta.key}] env default has wrong type: "
                f"expected {meta.python_type.__name__}, got {type(result).__name__} ({result!r})"
            )
        else:
            # Simulate a live Redis override already coerced to the correct type
            overlay._live[meta.key] = live_value
            result = overlay.get(meta.key)
            assert type(result) == meta.python_type, (
                f"[{service_name}.{meta.key}] live value has wrong type: "
                f"expected {meta.python_type.__name__}, got {type(result).__name__} ({result!r})"
            )

    return _test


# Dynamically generate one pytest test per (service, key) pair so failures are
# reported individually and are easy to triage.
for _service, _params in PARAMETER_REGISTRY.items():
    for _meta in _params:
        _test_fn = _make_type_valid_test(_service, _meta)
        _test_name = f"test_get_type_valid__{_service}__{_meta.key}"
        _test_fn.__name__ = _test_name
        globals()[_test_name] = _test_fn


# ---------------------------------------------------------------------------
# Explicit test: empty _live returns env default (type-valid)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service_name,meta",
    [
        (svc, meta)
        for svc, params in PARAMETER_REGISTRY.items()
        for meta in params
    ],
    ids=lambda x: f"{x.service}.{x.key}" if isinstance(x, ParameterMeta) else x,
)
def test_empty_live_returns_env_default_type_valid(service_name: str, meta: ParameterMeta) -> None:
    """When _live is empty, get() returns the env default which must be type-valid."""
    overlay = make_overlay(service_name)
    overlay._live.clear()

    result = overlay.get(meta.key)

    assert type(result) == meta.python_type, (
        f"[{service_name}.{meta.key}] env default type mismatch: "
        f"expected {meta.python_type.__name__}, got {type(result).__name__} ({result!r})"
    )


# ---------------------------------------------------------------------------
# Property 2: Env default is preserved when no Redis override exists
#
# For any registered parameter key K absent from _live:
#   overlay.get(K) == getattr(overlay._settings, K)
#
# **Validates: Requirements 1.3, 2.4**
# ---------------------------------------------------------------------------

@given(
    pair=st.sampled_from(
        [
            (service, meta)
            for service, params in PARAMETER_REGISTRY.items()
            for meta in params
        ]
    )
)
@h_settings(max_examples=100)
def test_env_default_preserved_when_no_redis_override(pair: tuple[str, ParameterMeta]) -> None:
    """
    Property 2: When _live is empty (no Redis override), overlay.get(K) must
    equal getattr(settings, K) exactly — value equality, not just type equality.

    **Validates: Requirements 1.3, 2.4**
    """
    service_name, meta = pair
    overlay = make_overlay(service_name)
    overlay._live.clear()

    result = overlay.get(meta.key)
    expected = getattr(overlay._settings, meta.key)

    assert result == expected, (
        f"[{service_name}.{meta.key}] env default not preserved: "
        f"expected {expected!r}, got {result!r}"
    )


@given(
    pair=st.sampled_from(
        [
            (service, meta)
            for service, params in PARAMETER_REGISTRY.items()
            for meta in params
        ]
    ),
    other_pairs=st.lists(
        st.sampled_from(
            [
                (service, meta)
                for service, params in PARAMETER_REGISTRY.items()
                for meta in params
            ]
        ),
        min_size=0,
        max_size=5,
    ),
)
@h_settings(max_examples=100)
def test_env_default_unaffected_by_other_key_overrides(
    pair: tuple[str, ParameterMeta],
    other_pairs: list[tuple[str, ParameterMeta]],
) -> None:
    """
    Property 2 (extended): Setting _live for OTHER keys in the same service
    must not affect the target key's env default when the target key is absent
    from _live.

    **Validates: Requirements 1.3, 2.4**
    """
    service_name, target_meta = pair
    overlay = make_overlay(service_name)
    overlay._live.clear()

    # Inject overrides for other keys in the same service (using their defaults
    # as stand-in values — we just need something in _live that isn't the target key).
    for other_service, other_meta in other_pairs:
        if other_service == service_name and other_meta.key != target_meta.key:
            overlay._live[other_meta.key] = other_meta.default

    # Target key must still be absent from _live
    overlay._live.pop(target_meta.key, None)

    result = overlay.get(target_meta.key)
    expected = getattr(overlay._settings, target_meta.key)

    assert result == expected, (
        f"[{service_name}.{target_meta.key}] env default changed by other-key overrides: "
        f"expected {expected!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Parametrized deterministic coverage: all (service, key) pairs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service_name,meta",
    [
        (svc, meta)
        for svc, params in PARAMETER_REGISTRY.items()
        for meta in params
    ],
    ids=lambda x: f"{x.service}.{x.key}" if isinstance(x, ParameterMeta) else x,
)
def test_env_default_exact_value_no_override(service_name: str, meta: ParameterMeta) -> None:
    """
    Deterministic coverage: for every (service, key) pair, verify that with an
    empty _live the returned value equals the env default exactly.

    **Validates: Requirements 1.3, 2.4**
    """
    overlay = make_overlay(service_name)
    overlay._live.clear()

    result = overlay.get(meta.key)
    expected = getattr(overlay._settings, meta.key)

    assert result == expected, (
        f"[{service_name}.{meta.key}] expected env default {expected!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Property 3: push() rejects invalid values without touching Redis
#
# For any parameter key K and any raw string value that fails type coercion,
# range validation, or options validation, overlay.push(K, raw_value) SHALL
# raise ConfigValidationError and the Redis hash SHALL remain unmodified.
#
# **Validates: Requirements 4.2, 4.3, 4.4**
# ---------------------------------------------------------------------------


def make_overlay_with_mock_redis(service_name: str) -> ConfigOverlay:
    """Return a ConfigOverlay with a mocked async Redis client (no real Redis needed)."""
    settings = make_mock_settings(service_name)
    overlay = ConfigOverlay(
        settings=settings,
        service_name=service_name,
        redis_url="redis://localhost:0",
    )
    mock_redis = AsyncMock()
    mock_redis.hset = AsyncMock(return_value=1)
    overlay._redis = mock_redis
    return overlay


# ---------------------------------------------------------------------------
# Helper: build invalid-value strategies per ParameterMeta
# ---------------------------------------------------------------------------

def _invalid_int_strategy() -> st.SearchStrategy:
    """Strings that cannot be coerced to int."""
    return st.text().filter(
        lambda s: not (s.strip().lstrip("-").isdigit() and s.strip() != "")
    ).filter(lambda s: s != "")


def _invalid_float_strategy() -> st.SearchStrategy:
    """Strings that cannot be coerced to float."""
    return st.sampled_from(["abc", "xyz", "not_a_float", "--1", "1.2.3", "inf!", "nan!"])


def _invalid_bool_strategy() -> st.SearchStrategy:
    """Strings not in the accepted bool set."""
    valid = {"true", "false", "1", "0", "yes", "no"}
    return st.text(min_size=1).filter(lambda s: s.lower() not in valid)


def _invalid_options_strategy(options: list) -> st.SearchStrategy:
    """Strings not in the options list."""
    return st.text(min_size=1).filter(lambda s: s not in options)


def _out_of_range_strategy(meta: ParameterMeta) -> st.SearchStrategy | None:
    """Generate numeric values outside [min_value, max_value] for the given meta."""
    if meta.min_value is None and meta.max_value is None:
        return None
    if meta.python_type is int:
        strategies = []
        if meta.min_value is not None:
            strategies.append(
                st.integers(max_value=int(meta.min_value) - 1)
            )
        if meta.max_value is not None:
            strategies.append(
                st.integers(min_value=int(meta.max_value) + 1)
            )
        return st.one_of(*strategies) if strategies else None
    elif meta.python_type is float:
        strategies = []
        if meta.min_value is not None:
            strategies.append(
                st.floats(
                    max_value=meta.min_value - 1e-9,
                    allow_nan=False,
                    allow_infinity=False,
                )
            )
        if meta.max_value is not None:
            strategies.append(
                st.floats(
                    min_value=meta.max_value + 1e-9,
                    allow_nan=False,
                    allow_infinity=False,
                )
            )
        return st.one_of(*strategies) if strategies else None
    return None


# ---------------------------------------------------------------------------
# Dynamic test generators for Property 3
# ---------------------------------------------------------------------------

def _make_type_violation_test(service_name: str, meta: ParameterMeta):
    """Return a @given test that asserts push() raises ConfigValidationError on type violations."""

    if meta.python_type is int:
        invalid_strat = _invalid_int_strategy()
    elif meta.python_type is float:
        invalid_strat = _invalid_float_strategy()
    elif meta.python_type is bool:
        invalid_strat = _invalid_bool_strategy()
    elif meta.python_type is str and meta.options is not None:
        invalid_strat = _invalid_options_strategy(meta.options)
    else:
        # str with no options — no type violation possible (any string is valid)
        return None

    @given(raw_value=invalid_strat)
    @h_settings(max_examples=30)
    def _test(raw_value: str) -> None:
        overlay = make_overlay_with_mock_redis(service_name)
        with pytest.raises(ConfigValidationError):
            asyncio.run(overlay.push(meta.key, raw_value))
        assert overlay._redis.hset.call_count == 0, (
            f"[{service_name}.{meta.key}] Redis hset was called despite invalid value {raw_value!r}"
        )

    return _test


def _make_range_violation_test(service_name: str, meta: ParameterMeta):
    """Return a @given test that asserts push() raises ConfigValidationError on range violations."""
    strat = _out_of_range_strategy(meta)
    if strat is None:
        return None

    @given(out_of_range=strat)
    @h_settings(max_examples=30)
    def _test(out_of_range) -> None:
        overlay = make_overlay_with_mock_redis(service_name)
        raw_value = str(out_of_range)
        with pytest.raises(ConfigValidationError):
            asyncio.run(overlay.push(meta.key, raw_value))
        assert overlay._redis.hset.call_count == 0, (
            f"[{service_name}.{meta.key}] Redis hset was called despite out-of-range value {raw_value!r}"
        )

    return _test


# Register dynamic tests for every (service, key) pair
for _service, _params in PARAMETER_REGISTRY.items():
    for _meta in _params:
        # Type violation test
        _type_test = _make_type_violation_test(_service, _meta)
        if _type_test is not None:
            _type_test_name = f"test_push_type_violation__{_service}__{_meta.key}"
            _type_test.__name__ = _type_test_name
            globals()[_type_test_name] = _type_test

        # Range violation test
        _range_test = _make_range_violation_test(_service, _meta)
        if _range_test is not None:
            _range_test_name = f"test_push_range_violation__{_service}__{_meta.key}"
            _range_test.__name__ = _range_test_name
            globals()[_range_test_name] = _range_test


# ---------------------------------------------------------------------------
# Parametrized deterministic coverage: unknown key raises ConfigValidationError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("service_name", list(PARAMETER_REGISTRY.keys()))
def test_push_unknown_key_raises_and_no_redis_write(service_name: str) -> None:
    """push() with an unregistered key raises ConfigValidationError without touching Redis."""
    overlay = make_overlay_with_mock_redis(service_name)
    with pytest.raises(ConfigValidationError):
        asyncio.run(overlay.push("__NONEXISTENT_KEY__", "value"))
    assert overlay._redis.hset.call_count == 0


# ---------------------------------------------------------------------------
# Parametrized deterministic coverage: options violation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "service_name,meta",
    [
        (svc, meta)
        for svc, params in PARAMETER_REGISTRY.items()
        for meta in params
        if meta.options is not None
    ],
    ids=lambda x: f"{x.service}.{x.key}" if isinstance(x, ParameterMeta) else x,
)
def test_push_options_violation_no_redis_write(service_name: str, meta: ParameterMeta) -> None:
    """push() with a value not in options raises ConfigValidationError without touching Redis."""
    overlay = make_overlay_with_mock_redis(service_name)
    invalid_value = "__NOT_IN_OPTIONS__"
    with pytest.raises(ConfigValidationError):
        asyncio.run(overlay.push(meta.key, invalid_value))
    assert overlay._redis.hset.call_count == 0, (
        f"[{service_name}.{meta.key}] Redis hset was called despite options violation"
    )


# ---------------------------------------------------------------------------
# Coerce helper (module-level, used by Properties 4 and 5)
# ---------------------------------------------------------------------------

def _coerce(raw_value: str, python_type: type) -> Any:
    """Coerce a raw string to python_type using the same logic as _poll_once."""
    if python_type is bool:
        if raw_value.lower() in ("true", "1", "yes"):
            return True
        elif raw_value.lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"cannot coerce {raw_value!r} to bool")
    elif python_type is int:
        return int(raw_value)
    elif python_type is float:
        return float(raw_value)
    else:
        return raw_value


# ---------------------------------------------------------------------------
# Valid raw-value strategy helper (used by Property 4)
# ---------------------------------------------------------------------------

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
# Property 4: push() / poll round-trip preserves value
#
# For valid (key, raw_value) pairs, after push() + simulated poll,
# overlay.get(K) == coerced(raw_value).
#
# **Validates: Requirements 4.1, 2.2**
# ---------------------------------------------------------------------------

def _make_push_poll_roundtrip_test(service_name: str, meta: ParameterMeta):
    """Return a @given test for the push/poll round-trip for a single (service, key) pair."""
    # Skip requires_restart keys — poll loop never applies them to _live
    if meta.requires_restart:
        return None

    raw_strat = _valid_raw_value_strategy(meta)

    @given(raw_value=raw_strat)
    @h_settings(max_examples=30)
    def _test(raw_value: str) -> None:
        overlay = make_overlay_with_mock_redis(service_name)

        # Step 1: push() — writes to Redis (mocked)
        asyncio.run(overlay.push(meta.key, raw_value))

        # Step 2: simulate poll — hgetall returns {key: raw_value}
        overlay._redis.hgetall = AsyncMock(return_value={meta.key: raw_value})
        asyncio.run(overlay._poll_once())

        # Step 3: assert get() returns the coerced value
        expected = _coerce(raw_value, meta.python_type)
        result = overlay.get(meta.key)
        assert result == expected, (
            f"[{service_name}.{meta.key}] round-trip mismatch: "
            f"pushed {raw_value!r}, expected {expected!r}, got {result!r}"
        )

    return _test


# Register dynamic tests for every non-restart (service, key) pair
for _service, _params in PARAMETER_REGISTRY.items():
    for _meta in _params:
        _rt_test = _make_push_poll_roundtrip_test(_service, _meta)
        if _rt_test is not None:
            _rt_test_name = f"test_push_poll_roundtrip__{_service}__{_meta.key}"
            _rt_test.__name__ = _rt_test_name
            globals()[_rt_test_name] = _rt_test


# ---------------------------------------------------------------------------
# Property 5: reset() / poll round-trip restores env default
#
# After reset(K) + simulated poll (hgetall returns empty dict),
# overlay.get(K) == getattr(settings, K).
#
# **Validates: Requirements 4.5**
# ---------------------------------------------------------------------------

@given(
    pair=st.sampled_from(
        [
            (service, meta)
            for service, params in PARAMETER_REGISTRY.items()
            for meta in params
            if not meta.requires_restart
        ]
    )
)
@h_settings(max_examples=50)
def test_reset_poll_roundtrip_restores_env_default(pair: tuple) -> None:
    """
    Property 5: After reset(K) + simulated poll with empty Redis hash,
    overlay.get(K) must equal the env default.

    **Validates: Requirements 4.5**
    """
    service_name, meta = pair
    overlay = make_overlay_with_mock_redis(service_name)

    # Pre-populate _live with a non-default sentinel so we can confirm it's cleared
    overlay._live[meta.key] = meta.default  # any value — just needs to be present

    # Step 1: reset() — calls HDEL on Redis (mocked)
    asyncio.run(overlay.reset(meta.key))

    # Step 2: simulate poll — hgetall returns empty dict (key was deleted)
    overlay._redis.hgetall = AsyncMock(return_value={})
    asyncio.run(overlay._poll_once())

    # Step 3: assert get() returns the env default
    expected = getattr(overlay._settings, meta.key)
    result = overlay.get(meta.key)
    assert result == expected, (
        f"[{service_name}.{meta.key}] reset/poll did not restore env default: "
        f"expected {expected!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Property 6: Graceful degradation preserves last-known values
#
# After at least one successful poll, simulate ConnectionError;
# assert all _live values unchanged and no exception raised.
#
# **Validates: Requirements 3.2, 3.3**
# ---------------------------------------------------------------------------

@given(
    service_name=st.sampled_from(list(PARAMETER_REGISTRY.keys())),
)
@h_settings(max_examples=20)
def test_graceful_degradation_preserves_live_values(service_name: str) -> None:
    """
    Property 6: When hgetall raises ConnectionError, _poll_once() must not raise
    and must leave _live unchanged.

    **Validates: Requirements 3.2, 3.3**
    """
    overlay = make_overlay_with_mock_redis(service_name)

    # Pre-populate _live with defaults as stand-in "last-known" values
    params = PARAMETER_REGISTRY[service_name]
    for meta in params:
        if not meta.requires_restart:
            overlay._live[meta.key] = meta.default

    snapshot = dict(overlay._live)

    # Simulate ConnectionError from Redis
    overlay._redis.hgetall = AsyncMock(side_effect=ConnectionError("Redis down"))

    # _poll_once() must NOT raise
    asyncio.run(overlay._poll_once())

    # _live must be unchanged
    assert overlay._live == snapshot, (
        f"[{service_name}] _live changed after ConnectionError: "
        f"before={snapshot!r}, after={overlay._live!r}"
    )


# ---------------------------------------------------------------------------
# Property 7: _live never contains unregistered keys
#
# Feed arbitrary Redis hash contents (including unknown keys) through
# _poll_once; assert _live keys are always a subset of PARAMETER_REGISTRY[service].
#
# **Validates: Requirements 2.3**
# ---------------------------------------------------------------------------

@given(
    service_name=st.sampled_from(list(PARAMETER_REGISTRY.keys())),
    redis_hash=st.dictionaries(
        keys=st.text(min_size=1, max_size=40),
        values=st.text(min_size=1, max_size=40),
        min_size=0,
        max_size=20,
    ),
)
@h_settings(max_examples=100)
def test_live_never_contains_unregistered_keys(
    service_name: str, redis_hash: dict
) -> None:
    """
    Property 7: After _poll_once() with arbitrary Redis hash contents,
    _live keys must be a subset of the registered keys for the service.

    **Validates: Requirements 2.3**
    """
    overlay = make_overlay_with_mock_redis(service_name)
    overlay._redis.hgetall = AsyncMock(return_value=redis_hash)

    asyncio.run(overlay._poll_once())

    registered_keys = set(PARAMETER_REGISTRY[service_name][i].key
                          for i in range(len(PARAMETER_REGISTRY[service_name])))
    live_keys = set(overlay._live.keys())

    assert live_keys <= registered_keys, (
        f"[{service_name}] _live contains unregistered keys: "
        f"{live_keys - registered_keys!r}"
    )
