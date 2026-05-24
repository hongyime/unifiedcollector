"""
Tests for shared/config_manager.py — ConfigManager class.
Tasks 4.1 through 4.8 of the universal-config-control spec.
"""
import argparse
import logging
import os
import tempfile

import pytest
from hypothesis import given, settings as h_settings
import hypothesis.strategies as st

from shared.config_manager import ConfigManager, SETTING_GROUPS, all_setting_definitions


# ---------------------------------------------------------------------------
# Task 4.1 — Unit tests for read_env and write_setting
# ---------------------------------------------------------------------------

class TestReadEnv:
    def test_read_existing_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        cm = ConfigManager(str(env))
        assert cm.read_env("FOO") == "bar"

    def test_read_absent_key_returns_none(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=bar\n")
        cm = ConfigManager(str(env))
        assert cm.read_env("MISSING") is None

    def test_read_missing_file_returns_none(self, tmp_path):
        cm = ConfigManager(str(tmp_path / "nonexistent.env"))
        assert cm.read_env("FOO") is None


class TestWriteSetting:
    def test_write_new_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        cm = ConfigManager(str(env))
        cm.write_setting("NEW_KEY", "hello")
        assert cm.read_env("NEW_KEY") == "hello"

    def test_update_existing_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("FOO=old\n")
        cm = ConfigManager(str(env))
        cm.write_setting("FOO", "new")
        assert cm.read_env("FOO") == "new"
        # Only one FOO line
        content = env.read_text()
        assert content.count("FOO=") == 1

    def test_preserves_comments_and_blank_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# comment\n\nFOO=bar\n")
        cm = ConfigManager(str(env))
        cm.write_setting("BAZ", "qux")
        content = env.read_text()
        assert "# comment" in content
        assert "FOO=bar" in content
        assert "BAZ=qux" in content

    def test_creates_env_when_missing(self, tmp_path):
        env = tmp_path / ".env"
        cm = ConfigManager(str(env))
        cm.write_setting("X", "1")
        assert env.exists()
        assert cm.read_env("X") == "1"

    def test_raises_ioerror_on_rename_failure(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("")
        cm = ConfigManager(str(env))
        monkeypatch.setattr("os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(IOError):
            cm.write_setting("X", "1")

    def test_secret_key_value_not_logged(self, tmp_path, caplog):
        env = tmp_path / ".env"
        env.write_text("")
        cm = ConfigManager(str(env))
        with caplog.at_level(logging.DEBUG):
            cm.write_setting("DB_PASSWORD", "supersecret")
        assert "supersecret" not in caplog.text
        assert "<redacted>" in caplog.text


# ---------------------------------------------------------------------------
# Task 4.2 — Property test: write_setting / read_env round-trip (Property 1)
# Validates: Requirements 1.1, 1.2
# ---------------------------------------------------------------------------

@given(
    key=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", min_size=1, max_size=30),
    value=st.text(
        alphabet=st.characters(
            blacklist_characters="\r\n",
            blacklist_categories=("Cs",),
        ),
        max_size=200,
    ),
)
def test_write_read_roundtrip(key, value):
    """**Validates: Requirements 1.1, 1.2**"""
    with tempfile.TemporaryDirectory() as d:
        env_path = os.path.join(d, ".env")
        with open(env_path, "w") as f:
            f.write("")
        cm = ConfigManager(env_path)
        cm.write_setting(key, value)
        assert cm.read_env(key) == str(value)


# ---------------------------------------------------------------------------
# Task 4.3 — Property test: write_setting preserves all other keys (Property 2)
# Validates: Requirements 1.3
# ---------------------------------------------------------------------------

@given(
    pairs=st.dictionaries(
        keys=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=20),
        values=st.text(
            alphabet=st.characters(
                blacklist_characters="\r\n",
                blacklist_categories=("Cs",),
            ),
            max_size=100,
        ),
        min_size=2,
        max_size=10,
    ),
)
@h_settings(deadline=None)
def test_write_preserves_other_keys(pairs):
    """**Validates: Requirements 1.3**"""
    with tempfile.TemporaryDirectory() as d:
        env_path = os.path.join(d, ".env")
        cm = ConfigManager(env_path)
        # Write all pairs
        for k, v in pairs.items():
            cm.write_setting(k, v)
        # Pick one key to overwrite
        target_key = next(iter(pairs))
        cm.write_setting(target_key, "OVERWRITTEN")
        # All other keys must be unchanged
        for k, v in pairs.items():
            if k != target_key:
                assert cm.read_env(k) == str(v), f"Key {k} was modified"


# ---------------------------------------------------------------------------
# Task 4.4 — Property test: write_setting preserves comments and blank lines (Property 3)
# Validates: Requirements 1.3
# ---------------------------------------------------------------------------

@given(
    comments=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", max_size=50),
        min_size=1,
        max_size=5,
    ),
    blank_count=st.integers(min_value=1, max_value=3),
)
def test_write_preserves_comments_and_blanks(comments, blank_count):
    """**Validates: Requirements 1.3**"""
    with tempfile.TemporaryDirectory() as d:
        env_path = os.path.join(d, ".env")
        lines = []
        for c in comments:
            lines.append(f"# {c}")
        lines.extend([""] * blank_count)
        lines.append("EXISTING=value")
        with open(env_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        cm = ConfigManager(env_path)
        cm.write_setting("NEW_SETTING", "42")
        content = open(env_path).read()
        for c in comments:
            assert f"# {c}" in content
        assert "EXISTING=value" in content
        assert "NEW_SETTING=42" in content


# ---------------------------------------------------------------------------
# Task 4.5 — Property test: apply_cli_overrides sets only non-None args (Property 4)
# Validates: Requirements 4.6, 4.7
# ---------------------------------------------------------------------------

_SAFE_ATTR_CHARS = "abcdefghijklmnopqrstuvwxyz"
_SAFE_ATTR_STRATEGY = st.text(
    alphabet=_SAFE_ATTR_CHARS + "_",
    min_size=1,
    max_size=20,
).filter(lambda s: s not in ("__dict__", "__class__", "__module__") and not s.startswith("__"))
# os.environ values must not contain null bytes (platform restriction)
_SAFE_VALUE_STRATEGY = st.text(
    alphabet=st.characters(blacklist_characters="\x00\r\n"),
    min_size=1,
    max_size=50,
)


@given(
    non_none_pairs=st.dictionaries(
        keys=_SAFE_ATTR_STRATEGY,
        values=_SAFE_VALUE_STRATEGY,
        min_size=0,
        max_size=5,
    ),
    none_keys=st.lists(
        _SAFE_ATTR_STRATEGY,
        min_size=0,
        max_size=5,
    ),
)
def test_apply_cli_overrides_only_non_none(non_none_pairs, none_keys):
    """**Validates: Requirements 4.6, 4.7**"""
    # Ensure no overlap between non_none and none keys
    none_keys = [k for k in none_keys if k not in non_none_pairs]

    namespace_dict = {**non_none_pairs, **{k: None for k in none_keys}}
    args = argparse.Namespace(**namespace_dict)
    mapping = {k: f"ENV_{k.upper()}" for k in namespace_dict}

    # Remove any pre-existing env vars
    for env_key in mapping.values():
        os.environ.pop(env_key, None)

    with tempfile.TemporaryDirectory() as d:
        cm = ConfigManager(os.path.join(d, ".env"))
        cm.apply_cli_overrides(args, mapping)

    for attr, env_key in mapping.items():
        if namespace_dict[attr] is not None:
            assert os.environ.get(env_key) == str(namespace_dict[attr])
        else:
            assert env_key not in os.environ

    # Cleanup
    for env_key in mapping.values():
        os.environ.pop(env_key, None)


# ---------------------------------------------------------------------------
# Task 4.6 — Unit tests for list_settings
# ---------------------------------------------------------------------------

class TestListSettings:
    def test_returns_all_keys_for_valid_group(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        cm = ConfigManager(str(env))
        result = cm.list_settings("face_recognition")
        expected_keys = {d.key for d in SETTING_GROUPS["face_recognition"]}
        assert set(result.keys()) == expected_keys

    def test_falls_back_to_default_for_absent_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        cm = ConfigManager(str(env))
        result = cm.list_settings("bulk_sender")
        for defn in SETTING_GROUPS["bulk_sender"]:
            assert result[defn.key] == defn.default

    def test_raises_key_error_for_unknown_group(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("")
        cm = ConfigManager(str(env))
        with pytest.raises(KeyError):
            cm.list_settings("nonexistent_group")


# ---------------------------------------------------------------------------
# Task 4.7 — Property test: SETTING_GROUPS type consistency (Property 8)
# Validates: Requirements 3.2
# ---------------------------------------------------------------------------

def test_setting_groups_type_consistency():
    """Every SettingDefinition.python_type matches the actual Settings field type.
    **Validates: Requirements 3.2**
    """
    from shared.config import Settings

    settings = Settings()

    for group_name, definitions in SETTING_GROUPS.items():
        for defn in definitions:
            attr_name = defn.key.lower() if hasattr(settings, defn.key.lower()) else defn.key
            if not hasattr(settings, attr_name):
                continue
            val = getattr(settings, attr_name, None)
            if val is not None:
                # bool must be checked before int (bool is subclass of int)
                if defn.python_type is bool:
                    assert isinstance(val, bool), (
                        f"{defn.key}: expected bool, got {type(val)}"
                    )
                else:
                    assert isinstance(val, defn.python_type), (
                        f"{defn.key}: expected {defn.python_type}, got {type(val)}"
                    )


# ---------------------------------------------------------------------------
# Task 4.8 — Property test: secret fields are explicitly marked sensitive
# Validates: Requirements 3.5
# ---------------------------------------------------------------------------

def test_secret_fields_are_marked_sensitive():
    """Keys that look secret-like must be explicitly marked sensitive.
    **Validates: Requirements 3.5**
    """
    import re
    pattern = re.compile(r"PASSWORD|TOKEN|HASH|SECRET|KEY", re.IGNORECASE)
    for defn in all_setting_definitions():
        if pattern.search(defn.key):
            assert defn.sensitive, f"Secret-like key {defn.key!r} must be marked sensitive"


def test_registry_covers_all_settings_fields():
    """All Settings model fields must be represented in SETTING_GROUPS."""
    from shared.config import Settings

    registry_keys = {defn.key for defn in all_setting_definitions()}
    settings_keys = {name for name in Settings.model_fields.keys() if name.isupper()}

    missing = settings_keys - registry_keys
    assert not missing, f"SETTING_GROUPS missing definitions for: {sorted(missing)}"
