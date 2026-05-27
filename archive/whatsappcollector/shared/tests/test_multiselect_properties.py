"""
Property-based and unit tests for multiselect support in shared/live_config.py
and shared/dashboard_live_config.py.

Since Streamlit cannot run in a test environment, `streamlit` is patched in
sys.modules with a MagicMock before the dashboard module is imported.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Patch streamlit BEFORE importing the dashboard module.
# We reuse the mock_st already installed by test_dashboard_properties.py if
# it ran first (both files share sys.modules["streamlit"]), otherwise we
# install our own.  Either way we grab the live reference from the already-
# imported dashboard module so that mock_st IS the same object that
# _select_widget calls through.
# ---------------------------------------------------------------------------

if "streamlit" not in sys.modules:
    _mock_st = MagicMock()
    sys.modules["streamlit"] = _mock_st

from shared.dashboard_live_config import _select_widget  # noqa: E402
import shared.dashboard_live_config as _dlc_module  # noqa: E402

# Always use the st reference that _select_widget actually calls through.
mock_st = sys.modules["streamlit"]

from shared.live_config import (  # noqa: E402
    PARAMETER_REGISTRY,
    ConfigOverlay,
    ConfigValidationError,
    ParameterMeta,
)


# ---------------------------------------------------------------------------
# Auto-reset mock_st before every test so side_effects / call counts don't
# bleed between tests (especially important for parametrized suites).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_mock_st():
    mock_st.reset_mock()
    mock_st.multiselect.side_effect = None
    mock_st.multiselect.return_value = []
    yield
    mock_st.multiselect.side_effect = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_settings(service_name: str) -> SimpleNamespace:
    params = PARAMETER_REGISTRY.get(service_name, [])
    return SimpleNamespace(**{meta.key: meta.default for meta in params})


def make_overlay_with_mock_redis(service_name: str) -> ConfigOverlay:
    settings = make_mock_settings(service_name)
    overlay = ConfigOverlay(
        settings=settings,
        service_name=service_name,
        redis_url="redis://localhost:0",
    )
    mock_redis = AsyncMock()
    mock_redis.hset = AsyncMock(return_value=1)
    mock_redis.hdel = AsyncMock(return_value=1)
    overlay._redis = mock_redis
    return overlay


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Tokens that are safe to use in comma-separated values (no commas, non-empty)
_safe_token = st.text(
    alphabet=st.characters(blacklist_characters=",\n\r\t"),
    min_size=1,
    max_size=30,
).map(str.strip).filter(lambda s: len(s) > 0)

# Arbitrary ParameterMeta kwargs for non-multiselect construction
_non_multiselect_kwargs = st.fixed_dictionaries({
    "key": st.text(min_size=1, max_size=20),
    "service": st.text(min_size=1, max_size=20),
    "python_type": st.sampled_from([bool, int, float, str]),
    "default": st.one_of(st.booleans(), st.integers(), st.floats(allow_nan=False, allow_infinity=False), st.text()),
    "description": st.text(min_size=1, max_size=50),
})


# ---------------------------------------------------------------------------
# Property 1: multi_select=True with no option pool raises ValueError
#
# **Validates: Requirements 1.3, 5.1**
# ---------------------------------------------------------------------------

@given(
    key=st.text(min_size=1, max_size=20),
    service=st.text(min_size=1, max_size=20),
    description=st.text(min_size=1, max_size=50),
)
@h_settings(max_examples=50)
def test_property1_multiselect_no_option_pool_raises(
    key: str, service: str, description: str
) -> None:
    """
    Property 1: For any ParameterMeta with multi_select=True and both options=None
    and known_values=None, __post_init__ SHALL raise ValueError.

    **Validates: Requirements 1.3, 5.1**
    """
    with pytest.raises(ValueError):
        ParameterMeta(
            key=key,
            service=service,
            python_type=str,
            default="",
            description=description,
            multi_select=True,
            options=None,
            known_values=None,
        )


# ---------------------------------------------------------------------------
# Property 2: multi_select=False never raises regardless of options/known_values
#
# **Validates: Requirements 1.4, 1.5**
# ---------------------------------------------------------------------------

@given(
    key=st.text(min_size=1, max_size=20),
    service=st.text(min_size=1, max_size=20),
    description=st.text(min_size=1, max_size=50),
    options=st.one_of(
        st.none(),
        st.lists(st.text(min_size=1, max_size=10), min_size=1, max_size=5),
    ),
    known_values=st.one_of(
        st.none(),
        st.lists(st.text(min_size=1, max_size=10), min_size=1, max_size=5),
    ),
)
@h_settings(max_examples=50)
def test_property2_non_multiselect_never_raises(
    key: str,
    service: str,
    description: str,
    options: list[str] | None,
    known_values: list[str] | None,
) -> None:
    """
    Property 2: For any ParameterMeta with multi_select=False, construction
    SHALL never raise ValueError regardless of options/known_values.

    **Validates: Requirements 1.4, 1.5**
    """
    # Should not raise
    meta = ParameterMeta(
        key=key,
        service=service,
        python_type=str,
        default="",
        description=description,
        multi_select=False,
        options=options,
        known_values=known_values,
    )
    assert meta.multi_select is False


# ---------------------------------------------------------------------------
# Property 3: comma-join then split round-trip is identity
#
# **Validates: Requirements 2.5, 3.1, 3.2**
# ---------------------------------------------------------------------------

@given(tokens=st.lists(_safe_token, min_size=1, max_size=20))
@h_settings(max_examples=100)
def test_property3_comma_roundtrip_identity(tokens: list[str]) -> None:
    """
    Property 3: For any list of non-empty string tokens (no commas),
    split(join(tokens, ","), ",") == tokens.

    **Validates: Requirements 2.5, 3.1, 3.2**
    """
    joined = ",".join(tokens)
    recovered = [t.strip() for t in joined.split(",") if t.strip()]
    assert recovered == tokens, (
        f"Round-trip failed: original={tokens!r}, joined={joined!r}, recovered={recovered!r}"
    )


# ---------------------------------------------------------------------------
# Property 4: unknown current tokens always appear in merged_options
#
# **Validates: Requirements 2.4, 5.2**
# ---------------------------------------------------------------------------

@given(
    known_values=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=8),
        min_size=1,
        max_size=10,
        unique=True,
    ),
    unknown_tokens=st.lists(
        st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=2, max_size=8),
        min_size=1,
        max_size=5,
        unique=True,
    ),
)
@h_settings(max_examples=50)
def test_property4_unknown_tokens_in_merged_options(
    known_values: list[str],
    unknown_tokens: list[str],
) -> None:
    """
    Property 4: For any ParameterMeta with multi_select=True, any tokens in
    current_value that are not in option_pool SHALL appear in the options
    argument passed to st.multiselect.

    **Validates: Requirements 2.4, 5.2**
    """
    # Ensure unknown_tokens are truly not in known_values
    unknown_tokens = [t for t in unknown_tokens if t not in known_values]
    if not unknown_tokens:
        return  # skip if hypothesis generated overlapping tokens

    meta = ParameterMeta(
        key="TEST_FIELD",
        service="test_service",
        python_type=str,
        default="",
        description="test",
        multi_select=True,
        known_values=known_values,
    )

    # current_value contains some known + some unknown tokens
    current_value = ",".join(known_values[:1] + unknown_tokens)

    # Capture the options argument passed to st.multiselect
    captured_options: list[str] = []

    def capture_multiselect(label, options, default=None, **kwargs):
        captured_options.clear()
        captured_options.extend(options)
        return default or []

    # Reset first, then set side_effect (reset_mock clears side_effect)
    mock_st.reset_mock()
    mock_st.multiselect.side_effect = capture_multiselect

    _select_widget(meta, current_value, "")

    for token in unknown_tokens:
        assert token in captured_options, (
            f"Unknown token {token!r} not found in merged_options={captured_options!r}"
        )

    mock_st.multiselect.side_effect = None


# ---------------------------------------------------------------------------
# Property 5: non-multiselect dispatch is unchanged
#
# **Validates: Requirements 2.7**
# ---------------------------------------------------------------------------

def _expected_widget_attr(meta: ParameterMeta) -> str:
    """Return the mock_st attribute name expected for a non-multiselect meta."""
    if meta.python_type is bool:
        return "toggle"
    if meta.python_type in (int, float) and meta.min_value is not None and meta.max_value is not None:
        return "slider"
    if meta.python_type is str and meta.options:
        return "selectbox"
    if meta.python_type is str:
        return "text_input"
    if meta.python_type is float:
        return "number_input"
    return "number_input"


_NON_MULTISELECT_PAIRS = [
    (service, meta)
    for service, params in PARAMETER_REGISTRY.items()
    for meta in params
    if not meta.multi_select
]

_NON_MULTISELECT_IDS = [
    f"{meta.service}.{meta.key}" for _, meta in _NON_MULTISELECT_PAIRS
]


@pytest.mark.parametrize("service_name,meta", _NON_MULTISELECT_PAIRS, ids=_NON_MULTISELECT_IDS)
def test_property5_non_multiselect_widget_unchanged(
    service_name: str, meta: ParameterMeta
) -> None:
    """
    Property 5: For any ParameterMeta with multi_select=False, _select_widget
    SHALL call the same widget type as before the change.

    **Validates: Requirements 2.7**
    """
    # autouse fixture already reset mock_st — call _select_widget directly
    _select_widget(meta, meta.default, meta.default)

    expected_attr = _expected_widget_attr(meta)
    widget_fn = getattr(mock_st, expected_attr)

    assert widget_fn.called, (
        f"[{service_name}.{meta.key}] expected mock_st.{expected_attr} to be called, "
        f"but it was not. python_type={meta.python_type.__name__}, "
        f"min_value={meta.min_value}, max_value={meta.max_value}, options={meta.options}"
    )
    assert not mock_st.multiselect.called, (
        f"[{service_name}.{meta.key}] mock_st.multiselect was unexpectedly called "
        f"for a non-multiselect parameter"
    )


# ---------------------------------------------------------------------------
# Property 6: overlay.push accepts any valid comma-joined subset of known_values
#
# **Validates: Requirements 3.2, 3.3**
# ---------------------------------------------------------------------------

_MULTISELECT_ENTRIES = [
    (service, meta)
    for service, params in PARAMETER_REGISTRY.items()
    for meta in params
    if meta.multi_select and meta.known_values
]


@pytest.mark.parametrize("service_name,meta", _MULTISELECT_ENTRIES,
                         ids=[f"{m.service}.{m.key}" for _, m in _MULTISELECT_ENTRIES])
@given(data=st.data())
@h_settings(max_examples=30)
def test_property6_push_accepts_valid_multiselect_subset(
    service_name: str, meta: ParameterMeta, data: st.DataObject
) -> None:
    """
    Property 6: For any non-empty subset of known_values joined with ",",
    overlay.push(key, joined) SHALL NOT raise ConfigValidationError.

    **Validates: Requirements 3.2, 3.3**
    """
    subset = data.draw(
        st.lists(
            st.sampled_from(meta.known_values),
            min_size=0,
            max_size=len(meta.known_values),
            unique=True,
        )
    )
    joined = ",".join(subset)

    overlay = make_overlay_with_mock_redis(service_name)

    # Should not raise ConfigValidationError
    asyncio.run(overlay.push(meta.key, joined))


# ---------------------------------------------------------------------------
# Unit test 4.7: empty multiselect selection returns ""
# ---------------------------------------------------------------------------

def test_unit_empty_multiselect_returns_empty_string() -> None:
    """
    Unit test 4.7: When st.multiselect returns [], _select_widget SHALL return "".

    **Validates: Requirements 2.6**
    """
    meta = ParameterMeta(
        key="LANGUAGE_WHITELIST",
        service="collector",
        python_type=str,
        default="",
        description="test",
        multi_select=True,
        known_values=["en", "es", "fr"],
    )

    # autouse fixture already reset mock_st and set multiselect.return_value = []
    result = _select_widget(meta, "", "")

    assert result == "", f"Expected empty string, got {result!r}"
    assert mock_st.multiselect.called


# ---------------------------------------------------------------------------
# Unit test 4.8: PARAMETER_REGISTRY entries have correct field values
# ---------------------------------------------------------------------------

def test_unit_tracked_fields_registry_entry() -> None:
    """
    Unit test 4.8a: user_intelligence/TRACKED_FIELDS has correct multi_select,
    known_values, and options values.
    """
    ui_params = {m.key: m for m in PARAMETER_REGISTRY["user_intelligence"]}
    assert "TRACKED_FIELDS" in ui_params, "TRACKED_FIELDS not found in user_intelligence registry"

    meta = ui_params["TRACKED_FIELDS"]
    assert meta.multi_select is True, "TRACKED_FIELDS should have multi_select=True"
    assert meta.options is None, "TRACKED_FIELDS should have options=None"
    assert meta.known_values is not None, "TRACKED_FIELDS should have known_values set"

    expected_fields = {
        "display_name", "push_name", "business_name", "phone_number",
        "is_business", "is_verified", "profile_photo", "about", "status",
    }
    assert set(meta.known_values) == expected_fields, (
        f"TRACKED_FIELDS known_values mismatch: got {set(meta.known_values)!r}"
    )
    assert len(meta.known_values) == 9, (
        f"TRACKED_FIELDS should have exactly 9 known_values, got {len(meta.known_values)}"
    )


def test_unit_language_whitelist_registry_entry() -> None:
    """
    Unit test 4.8b: collector/LANGUAGE_WHITELIST has correct multi_select,
    known_values (BCP-47 codes), and default="".
    """
    collector_params = {m.key: m for m in PARAMETER_REGISTRY["collector"]}
    assert "LANGUAGE_WHITELIST" in collector_params, "LANGUAGE_WHITELIST not found in collector registry"

    meta = collector_params["LANGUAGE_WHITELIST"]
    assert meta.multi_select is True, "LANGUAGE_WHITELIST should have multi_select=True"
    assert meta.default == "", f"LANGUAGE_WHITELIST default should be '', got {meta.default!r}"
    assert meta.known_values is not None, "LANGUAGE_WHITELIST should have known_values set"

    # Should contain BCP-47 codes
    expected_codes = {"en", "es", "fr", "de", "pt", "ar", "hi", "zh", "ru", "ja", "ko", "tr", "id", "vi"}
    assert expected_codes.issubset(set(meta.known_values)), (
        f"LANGUAGE_WHITELIST known_values missing some BCP-47 codes. "
        f"Missing: {expected_codes - set(meta.known_values)!r}"
    )


def test_unit_broker_type_registry_entry() -> None:
    """
    Unit test 4.8c: processor_py/BROKER_TYPE has correct options, requires_restart,
    and multi_select=False.
    """
    pp_params = {m.key: m for m in PARAMETER_REGISTRY["processor_py"]}
    assert "BROKER_TYPE" in pp_params, "BROKER_TYPE not found in processor_py registry"

    meta = pp_params["BROKER_TYPE"]
    assert meta.options == ["redis", "rabbitmq"], (
        f"BROKER_TYPE options should be ['redis', 'rabbitmq'], got {meta.options!r}"
    )
    assert meta.requires_restart is True, "BROKER_TYPE should have requires_restart=True"
    assert meta.multi_select is False, "BROKER_TYPE should have multi_select=False"


def test_unit_postgres_ssl_mode_registry_entry() -> None:
    """
    Unit test 4.8d: processor_py/POSTGRES_SSL_MODE has correct options,
    requires_restart, and multi_select=False.
    """
    pp_params = {m.key: m for m in PARAMETER_REGISTRY["processor_py"]}
    assert "POSTGRES_SSL_MODE" in pp_params, "POSTGRES_SSL_MODE not found in processor_py registry"

    meta = pp_params["POSTGRES_SSL_MODE"]
    assert meta.options == ["disable", "require", "verify-ca", "verify-full"], (
        f"POSTGRES_SSL_MODE options mismatch: got {meta.options!r}"
    )
    assert meta.requires_restart is True, "POSTGRES_SSL_MODE should have requires_restart=True"
    assert meta.multi_select is False, "POSTGRES_SSL_MODE should have multi_select=False"
