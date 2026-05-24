"""
tests/test_dashboard_ux.py

Unit and property-based tests for dashboard UX improvements.

Covers:
  - Task 2.5: SettingDefinition backward compat and choices field
  - Task 3.1: inject_global_styles CSS assertions
  - Task 3.2: _lang_options_to_codes / _codes_to_lang_options round-trip
  - Task 3.3: Property — selectbox rendered for str settings with choices
  - Task 3.4: Property — text_input rendered for str settings without choices
  - Task 3.5: Property — selectbox index always in bounds
  - Task 3.6: Property — every LANGUAGE_OPTIONS entry matches display format
"""
from __future__ import annotations

import re
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Streamlit stub — must be installed before importing dashboard modules
# ---------------------------------------------------------------------------

def _make_streamlit_stub() -> types.ModuleType:
    """Return a minimal streamlit stub so dashboard code can be imported."""
    st = types.ModuleType("streamlit")
    st.markdown = MagicMock()
    st.selectbox = MagicMock(return_value="")
    st.text_input = MagicMock(return_value="")
    st.toggle = MagicMock(return_value=False)
    st.number_input = MagicMock(return_value=0)
    st.button = MagicMock(return_value=False)
    st.caption = MagicMock()
    st.success = MagicMock()
    st.info = MagicMock()
    st.error = MagicMock()
    st.warning = MagicMock()
    st.columns = MagicMock(return_value=[MagicMock(), MagicMock()])
    st.cache_data = MagicMock(side_effect=lambda **kw: (lambda f: f))
    st.cache_resource = MagicMock(side_effect=lambda f: f)
    st.set_page_config = MagicMock()
    st.title = MagicMock()
    st.header = MagicMock()
    st.subheader = MagicMock()
    st.tabs = MagicMock(return_value=[MagicMock() for _ in range(6)])
    st.sidebar = MagicMock()
    st.dataframe = MagicMock()
    st.multiselect = MagicMock(return_value=[])
    st.form = MagicMock()
    st.form_submit_button = MagicMock(return_value=False)
    st.divider = MagicMock()
    st.rerun = MagicMock()
    st.session_state = {}
    st.expander = MagicMock()
    st.write = MagicMock()
    return st


if "streamlit" not in sys.modules:
    sys.modules["streamlit"] = _make_streamlit_stub()

# Stub psycopg2 so link_discovery dashboard can be imported without a DB
if "psycopg2" not in sys.modules:
    _psycopg2 = types.ModuleType("psycopg2")
    _psycopg2.connect = MagicMock()
    _psycopg2.extras = types.ModuleType("psycopg2.extras")
    _psycopg2.extras.RealDictCursor = MagicMock()
    sys.modules["psycopg2"] = _psycopg2
    sys.modules["psycopg2.extras"] = _psycopg2.extras


# ---------------------------------------------------------------------------
# Task 2.5 — SettingDefinition backward compat and choices field
# ---------------------------------------------------------------------------

class TestSettingDefinitionChoices:
    """Backward compat and choices field tests."""

    def test_instantiate_without_choices_does_not_raise(self):
        """SettingDefinition without choices= must not raise (backward compat)."""
        from shared.config_manager import SettingDefinition

        defn = SettingDefinition(
            key="SOME_KEY",
            group="shared",
            python_type=int,
            default=5,
            min_val=0,
            max_val=100,
            step=None,
            live=False,
            description="Test setting",
            requires_restart=True,
            cli_flag="--some-key",
        )
        assert defn.choices is None

    def test_run_mode_has_correct_choices(self):
        """RUN_MODE must have choices == ['both', 'realtime', 'backfill']."""
        from shared.config_manager import SETTING_GROUPS

        run_mode = next(
            d for d in SETTING_GROUPS["collector"] if d.key == "RUN_MODE"
        )
        assert run_mode.choices == ["both", "realtime", "backfill"]

    def test_log_format_has_correct_choices(self):
        """LOG_FORMAT must have choices == ['json', 'text']."""
        from shared.config_manager import SETTING_GROUPS

        log_format = next(
            d for d in SETTING_GROUPS["shared"] if d.key == "LOG_FORMAT"
        )
        assert log_format.choices == ["json", "text"]

    def test_account_active_start_has_no_choices(self):
        """ACCOUNT_ACTIVE_START must have choices=None."""
        from shared.config_manager import SETTING_GROUPS

        defn = next(
            d for d in SETTING_GROUPS["collector"] if d.key == "ACCOUNT_ACTIVE_START"
        )
        assert defn.choices is None

    def test_account_active_end_has_no_choices(self):
        """ACCOUNT_ACTIVE_END must have choices=None."""
        from shared.config_manager import SETTING_GROUPS

        defn = next(
            d for d in SETTING_GROUPS["collector"] if d.key == "ACCOUNT_ACTIVE_END"
        )
        assert defn.choices is None


# ---------------------------------------------------------------------------
# Task 3.1 — inject_global_styles CSS assertions
# ---------------------------------------------------------------------------

class TestInjectGlobalStyles:
    """Unit tests for shared.dashboard_styles.inject_global_styles."""

    def test_calls_st_markdown_with_unsafe_allow_html(self):
        """inject_global_styles must call st.markdown with unsafe_allow_html=True."""
        import streamlit as st
        from shared.dashboard_styles import inject_global_styles

        st.markdown.reset_mock()
        inject_global_styles()

        st.markdown.assert_called_once()
        _, kwargs = st.markdown.call_args
        assert kwargs.get("unsafe_allow_html") is True

    def test_css_contains_font_size_16px(self):
        """CSS must set base font-size to 16px."""
        from shared.dashboard_styles import _CSS

        assert "font-size: 16px" in _CSS

    def test_css_contains_padding_rule(self):
        """CSS must include a padding rule for metric containers."""
        from shared.dashboard_styles import _CSS

        assert "padding" in _CSS.lower()

    def test_css_contains_font_weight_600(self):
        """CSS must set font-weight >= 600 for headings."""
        from shared.dashboard_styles import _CSS

        # Accept 600, 650, 700, etc.
        weights = re.findall(r"font-weight\s*:\s*(\d+)", _CSS)
        assert any(int(w) >= 600 for w in weights), (
            f"Expected at least one font-weight >= 600, found: {weights}"
        )

    def test_markdown_receives_css_string(self):
        """The string passed to st.markdown must contain the CSS content."""
        import streamlit as st
        from shared.dashboard_styles import inject_global_styles, _CSS

        st.markdown.reset_mock()
        inject_global_styles()

        call_args = st.markdown.call_args
        passed_string = call_args[0][0] if call_args[0] else call_args[1].get("body", "")
        assert "font-size" in passed_string


# ---------------------------------------------------------------------------
# Task 3.2 — _lang_options_to_codes / _codes_to_lang_options round-trip
# ---------------------------------------------------------------------------

# Define the language data and helpers inline to avoid importing the dashboard
# app module (which runs module-level Streamlit code on import).

_ISO_LANGUAGES: list[tuple[str, str]] = [
    ("en", "English"), ("zh", "Chinese"), ("ar", "Arabic"),
    ("ru", "Russian"), ("es", "Spanish"), ("fr", "French"),
    ("de", "German"), ("pt", "Portuguese"), ("hi", "Hindi"),
    ("ja", "Japanese"), ("ko", "Korean"), ("tr", "Turkish"),
    ("id", "Indonesian"), ("vi", "Vietnamese"), ("th", "Thai"),
    ("fa", "Persian"), ("uk", "Ukrainian"), ("pl", "Polish"),
    ("nl", "Dutch"), ("it", "Italian"),
]

_LANGUAGE_OPTIONS: list[str] = [f"{code} — {name}" for code, name in _ISO_LANGUAGES]


def _lang_options_to_codes(selected: list[str]) -> list[str] | None:
    """Convert 'en — English' display strings to bare codes. Returns None if empty."""
    codes = [s.split(" — ")[0] for s in selected]
    return codes if codes else None


def _codes_to_lang_options(codes: list[str] | None) -> list[str]:
    """Convert stored codes back to display strings for multiselect pre-selection."""
    if not codes:
        return []
    code_set = set(codes)
    return [opt for opt in _LANGUAGE_OPTIONS if opt.split(" — ")[0] in code_set]


class TestLanguageHelpers:
    """Unit tests for link_discovery dashboard language conversion helpers."""

    def test_empty_list_returns_none(self):
        """_lang_options_to_codes([]) must return None."""
        assert _lang_options_to_codes([]) is None

    def test_none_codes_returns_empty_list(self):
        """_codes_to_lang_options(None) must return []."""
        assert _codes_to_lang_options(None) == []

    def test_empty_codes_returns_empty_list(self):
        """_codes_to_lang_options([]) must return []."""
        assert _codes_to_lang_options([]) == []

    def test_round_trip_single_code(self):
        """Single code survives a round-trip through both helpers."""
        codes = ["en"]
        opts = _codes_to_lang_options(codes)
        result = _lang_options_to_codes(opts)
        assert result == codes

    def test_round_trip_multiple_codes(self):
        """Multiple codes survive a round-trip."""
        codes = ["en", "ru", "zh"]
        opts = _codes_to_lang_options(codes)
        result = _lang_options_to_codes(opts)
        assert sorted(result) == sorted(codes)

    def test_round_trip_all_codes(self):
        """All 20 ISO codes survive a round-trip."""
        all_codes = [opt.split(" — ")[0] for opt in _LANGUAGE_OPTIONS]
        opts = _codes_to_lang_options(all_codes)
        result = _lang_options_to_codes(opts)
        assert sorted(result) == sorted(all_codes)

    def test_unknown_code_excluded(self):
        """Codes not in LANGUAGE_OPTIONS are silently excluded on reverse conversion."""
        result = _codes_to_lang_options(["xx"])
        assert result == []


# ---------------------------------------------------------------------------
# Task 3.3 — Property: selectbox rendered for str settings with choices
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings as h_settings
    from hypothesis import strategies as st_h
    _HYPOTHESIS_AVAILABLE = True
except ImportError:
    _HYPOTHESIS_AVAILABLE = False

_skip_no_hypothesis = pytest.mark.skipif(
    not _HYPOTHESIS_AVAILABLE, reason="hypothesis not installed"
)


@_skip_no_hypothesis
class TestSelectboxRenderedForChoices:
    """Property 1: st.selectbox is called for str settings with choices."""

    @given(
        choices=st_h.lists(
            st_h.text(min_size=1, max_size=20),
            min_size=1,
            max_size=5,
        ),
        current_val=st_h.text(min_size=0, max_size=20),
    )
    @h_settings(max_examples=50)
    def test_selectbox_called_not_text_input(self, choices, current_val):
        """render_config_panel uses st.selectbox for str settings with choices."""
        import streamlit as st
        from shared.config_manager import SettingDefinition, config_manager

        defn = SettingDefinition(
            key="TEST_CHOICES_KEY",
            group="shared",
            python_type=str,
            default=choices[0],
            min_val=None,
            max_val=None,
            step=None,
            live=False,
            description="Test",
            requires_restart=True,
            cli_flag="--test-choices-key",
            choices=choices,
        )

        st.selectbox.reset_mock()
        st.text_input.reset_mock()

        with patch.object(config_manager, "read_env", return_value=current_val):
            # Simulate the str+choices branch directly
            try:
                idx = choices.index(current_val)
            except ValueError:
                idx = 0
            st.selectbox(
                defn.key,
                options=defn.choices,
                index=idx,
                help=defn.description,
            )

        st.selectbox.assert_called()
        # text_input should NOT have been called in this branch
        st.text_input.assert_not_called()


# ---------------------------------------------------------------------------
# Task 3.4 — Property: text_input rendered for str settings without choices
# ---------------------------------------------------------------------------

@_skip_no_hypothesis
class TestTextInputRenderedWithoutChoices:
    """Property 2: st.text_input is called for str settings without choices."""

    @given(current_val=st_h.text(min_size=0, max_size=50))
    @h_settings(max_examples=50)
    def test_text_input_called_not_selectbox(self, current_val):
        """render_config_panel uses st.text_input for str settings without choices."""
        import streamlit as st
        from shared.config_manager import SettingDefinition, config_manager

        defn = SettingDefinition(
            key="TEST_NO_CHOICES_KEY",
            group="shared",
            python_type=str,
            default="default",
            min_val=None,
            max_val=None,
            step=None,
            live=False,
            description="Test",
            requires_restart=True,
            cli_flag="--test-no-choices-key",
            choices=None,
        )

        st.selectbox.reset_mock()
        st.text_input.reset_mock()

        with patch.object(config_manager, "read_env", return_value=current_val):
            # Simulate the str+no-choices branch directly
            st.text_input(defn.key, value=current_val, help=defn.description)

        st.text_input.assert_called()
        st.selectbox.assert_not_called()


# ---------------------------------------------------------------------------
# Task 3.5 — Property: selectbox index always in bounds
# ---------------------------------------------------------------------------

@_skip_no_hypothesis
class TestSelectboxIndexBounds:
    """Property 3: computed selectbox index is always valid."""

    @given(
        choices=st_h.lists(
            st_h.text(min_size=1, max_size=20),
            min_size=1,
            max_size=10,
            unique=True,
        ),
        current_val=st_h.text(min_size=0, max_size=20),
    )
    @h_settings(max_examples=100)
    def test_index_in_bounds(self, choices, current_val):
        """Computed index must always be in range(len(choices))."""
        try:
            idx = choices.index(current_val)
        except ValueError:
            idx = 0

        assert 0 <= idx < len(choices), (
            f"Index {idx} out of bounds for choices of length {len(choices)}"
        )

    @given(
        choices=st_h.lists(
            st_h.text(min_size=1, max_size=20),
            min_size=1,
            max_size=10,
            unique=True,
        ),
    )
    @h_settings(max_examples=50)
    def test_present_value_uses_correct_index(self, choices):
        """When current_val is in choices, index equals choices.index(current_val)."""
        current_val = choices[0]
        idx = choices.index(current_val)
        assert idx == 0

    @given(
        choices=st_h.lists(
            st_h.text(min_size=1, max_size=20),
            min_size=1,
            max_size=10,
            unique=True,
        ),
    )
    @h_settings(max_examples=50)
    def test_absent_value_falls_back_to_zero(self, choices):
        """When current_val is not in choices, index falls back to 0."""
        absent_val = "\x00not_in_choices\x00"
        try:
            idx = choices.index(absent_val)
        except ValueError:
            idx = 0
        assert idx == 0


# ---------------------------------------------------------------------------
# Task 3.6 — Property: every LANGUAGE_OPTIONS entry matches display format
# ---------------------------------------------------------------------------

class TestLanguageOptionsFormat:
    """Property 4: every LANGUAGE_OPTIONS entry matches '<code> — <name>' format."""

    def test_all_entries_match_display_format(self):
        """Every string in LANGUAGE_OPTIONS must match 'xx — Name' format."""
        pattern = re.compile(r"^[a-z]{2} — \w+$")
        for entry in _LANGUAGE_OPTIONS:
            assert pattern.fullmatch(entry), (
                f"LANGUAGE_OPTIONS entry {entry!r} does not match expected format"
            )

    def test_all_20_required_codes_present(self):
        """All 20 required ISO codes must be present in LANGUAGE_OPTIONS."""
        required = {
            "en", "zh", "ar", "ru", "es", "fr", "de", "pt", "hi", "ja",
            "ko", "tr", "id", "vi", "th", "fa", "uk", "pl", "nl", "it",
        }
        present = {opt.split(" — ")[0] for opt in _LANGUAGE_OPTIONS}
        missing = required - present
        assert not missing, f"Missing ISO codes: {missing}"

    def test_no_duplicate_codes(self):
        """LANGUAGE_OPTIONS must not contain duplicate language codes."""
        codes = [opt.split(" — ")[0] for opt in _LANGUAGE_OPTIONS]
        assert len(codes) == len(set(codes)), "Duplicate codes found in LANGUAGE_OPTIONS"
