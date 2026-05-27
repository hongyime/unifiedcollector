"""Tests for src/config.py — account loading and helper functions."""
import os
from unittest.mock import patch

import pytest

from config import get_account_by_name, get_default_account


# We need to mock INSTAGRAM_ACCOUNTS since the real .env isn't present in CI
MOCK_ACCOUNTS = [
    {"name": "alpha", "username": "alpha_user", "password": "pw1"},
    {"name": "beta", "username": "beta_user", "password": "pw2"},
]


class TestGetAccountByName:
    """Tests for get_account_by_name()."""

    @patch("config.INSTAGRAM_ACCOUNTS", MOCK_ACCOUNTS)
    def test_finds_existing_account(self):
        acct = get_account_by_name("beta")
        assert acct is not None
        assert acct["username"] == "beta_user"

    @patch("config.INSTAGRAM_ACCOUNTS", MOCK_ACCOUNTS)
    def test_returns_none_for_unknown_name(self):
        assert get_account_by_name("nonexistent") is None

    @patch("config.INSTAGRAM_ACCOUNTS", MOCK_ACCOUNTS)
    def test_none_name_returns_default(self):
        acct = get_account_by_name(None)
        assert acct is not None
        assert acct["name"] == "alpha"  # first = default

    @patch("config.INSTAGRAM_ACCOUNTS", [])
    def test_empty_accounts_returns_none(self):
        assert get_account_by_name(None) is None
        assert get_account_by_name("anything") is None


class TestGetDefaultAccount:
    """Tests for get_default_account()."""

    @patch("config.INSTAGRAM_ACCOUNTS", MOCK_ACCOUNTS)
    def test_returns_first_account(self):
        acct = get_default_account()
        assert acct["name"] == "alpha"

    @patch("config.INSTAGRAM_ACCOUNTS", [])
    def test_returns_none_when_empty(self):
        assert get_default_account() is None


# ══════════════════════════════════════════════════════════════
#  _load_accounts_from_env
# ══════════════════════════════════════════════════════════════

class TestLoadAccountsFromEnv:
    """Tests for _load_accounts_from_env()."""

    def test_returns_empty_when_dotenv_missing(self, monkeypatch):
        """If python-dotenv is not importable, returns []."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "dotenv":
                raise ImportError("no dotenv")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from config import _load_accounts_from_env
        result = _load_accounts_from_env()
        assert result == []

    def test_returns_empty_when_env_file_missing(self, monkeypatch, tmp_path):
        """If .env file doesn't exist, returns []."""
        monkeypatch.setattr("config.os.path.dirname", lambda _: str(tmp_path / "lib"))
        from config import _load_accounts_from_env

        # Patch dotenv_values to simulate no .env
        with patch("config.os.path.exists", return_value=False):
            result = _load_accounts_from_env()
        assert result == []

    def test_parses_valid_accounts(self, monkeypatch):
        """Successfully parses well-formed .env entries."""
        env = {
            "INSTA_ACCOUNT_1_NAME": "alpha",
            "INSTA_ACCOUNT_1_USER": "alpha_user",
            "INSTA_ACCOUNT_1_PASS": "alpha_pw",
            "INSTA_ACCOUNT_2_NAME": "beta",
            "INSTA_ACCOUNT_2_USER": "beta_user",
            "INSTA_ACCOUNT_2_PASS": "beta_pw",
        }
        from config import _load_accounts_from_env

        with patch("config.os.path.exists", return_value=True), \
             patch("dotenv.dotenv_values", return_value=env):
            result = _load_accounts_from_env()

        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["username"] == "beta_user"

    def test_skips_incomplete_accounts(self, monkeypatch, capsys):
        """Accounts missing name/user/pass are skipped with a warning."""
        env = {
            "INSTA_ACCOUNT_1_NAME": "alpha",
            "INSTA_ACCOUNT_1_USER": "alpha_user",
            # missing password
        }
        from config import _load_accounts_from_env

        with patch("config.os.path.exists", return_value=True), \
             patch("dotenv.dotenv_values", return_value=env):
            result = _load_accounts_from_env()

        assert len(result) == 0
        output = capsys.readouterr().out
        assert "Incomplete" in output or "skipping" in output.lower()

    def test_returns_empty_with_no_accounts(self, monkeypatch, capsys):
        """Empty .env returns []."""
        from config import _load_accounts_from_env

        with patch("config.os.path.exists", return_value=True), \
             patch("dotenv.dotenv_values", return_value={}):
            result = _load_accounts_from_env()

        assert result == []


# ══════════════════════════════════════════════════════════════
#  get_downloads_directory
# ══════════════════════════════════════════════════════════════

class TestGetDownloadsDirectory:
    @patch("config.get_session_path", return_value="/cached/path")
    def test_returns_session_path_when_set(self, mock_session):
        from config import get_downloads_directory
        result = get_downloads_directory()
        assert result == "/cached/path"

    @patch("config.get_session_path", return_value=None)
    @patch("config.prompt_for_download_path", return_value="/new/path")
    def test_prompts_when_no_session(self, mock_prompt, mock_session):
        from config import get_downloads_directory
        result = get_downloads_directory()
        assert result == "/new/path"
        mock_prompt.assert_called_once()


# ══════════════════════════════════════════════════════════════
#  _load_proxy_config
# ══════════════════════════════════════════════════════════════

class TestLoadProxyConfig:
    def test_returns_empty_when_no_env(self, monkeypatch):
        from config import _load_proxy_config
        with patch("config.os.path.exists", return_value=False):
            result = _load_proxy_config()
        assert result == {}

    def test_loads_global_proxy(self, monkeypatch):
        env = {"PROXY_URL": "socks5://proxy:1080"}
        from config import _load_proxy_config
        with patch("config.os.path.exists", return_value=True), \
             patch("dotenv.dotenv_values", return_value=env):
            result = _load_proxy_config()
        assert result.get("__global__") == "socks5://proxy:1080"

    def test_loads_per_account_proxy(self, monkeypatch):
        env = {
            "INSTA_ACCOUNT_1_NAME": "alpha",
            "INSTA_ACCOUNT_1_PROXY": "socks5://proxy1:1080",
        }
        from config import _load_proxy_config
        with patch("config.os.path.exists", return_value=True), \
             patch("dotenv.dotenv_values", return_value=env):
            result = _load_proxy_config()
        assert result.get("alpha") == "socks5://proxy1:1080"

    def test_ignores_empty_proxy(self, monkeypatch):
        env = {
            "INSTA_ACCOUNT_1_NAME": "alpha",
            "INSTA_ACCOUNT_1_PROXY": "   ",
        }
        from config import _load_proxy_config
        with patch("config.os.path.exists", return_value=True), \
             patch("dotenv.dotenv_values", return_value=env):
            result = _load_proxy_config()
        assert "alpha" not in result


# ══════════════════════════════════════════════════════════════
#  Constants validation
# ══════════════════════════════════════════════════════════════

class TestConstants:
    def test_rate_limit_phrases_are_tuple(self):
        from config import RATE_LIMIT_PHRASES
        assert isinstance(RATE_LIMIT_PHRASES, tuple)
        assert len(RATE_LIMIT_PHRASES) > 0

    def test_challenge_phrases_are_tuple(self):
        from config import CHALLENGE_PHRASES
        assert isinstance(CHALLENGE_PHRASES, tuple)
        assert len(CHALLENGE_PHRASES) > 0

    def test_account_switch_phrases_are_tuple(self):
        from config import ACCOUNT_SWITCH_PHRASES
        assert isinstance(ACCOUNT_SWITCH_PHRASES, tuple)
        assert len(ACCOUNT_SWITCH_PHRASES) > 0

    def test_all_phrases_lowercase(self):
        from config import RATE_LIMIT_PHRASES, CHALLENGE_PHRASES, ACCOUNT_SWITCH_PHRASES
        for phrases in (RATE_LIMIT_PHRASES, CHALLENGE_PHRASES, ACCOUNT_SWITCH_PHRASES):
            for p in phrases:
                assert p == p.lower(), f"Phrase '{p}' should be lowercase for case-insensitive matching"
