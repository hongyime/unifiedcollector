"""Tests for cli_helpers.py — resolve_account_config, get_account_username."""
import os
from unittest.mock import patch

import pytest

from cli_helpers import resolve_account_config, get_account_username

_MOCK_ACCOUNTS = [
    {"name": "alpha", "username": "alpha_user", "password": "pw1"},
    {"name": "beta", "username": "beta_user", "password": "pw2"},
]


# ══════════════════════════════════════════════════════════════
#  resolve_account_config
# ══════════════════════════════════════════════════════════════

class TestResolveAccountConfig:

    @patch("cli_helpers.get_account_by_name")
    def test_resolves_by_name(self, mock_get):
        mock_get.return_value = _MOCK_ACCOUNTS[1]
        result = resolve_account_config("beta")
        assert result["username"] == "beta_user"
        mock_get.assert_called_once_with("beta")

    @patch("cli_helpers.get_default_account")
    def test_resolves_default_when_none(self, mock_default):
        mock_default.return_value = _MOCK_ACCOUNTS[0]
        result = resolve_account_config(None)
        assert result["name"] == "alpha"

    @patch("cli_helpers.get_account_by_name", return_value=None)
    def test_returns_none_for_unknown(self, mock_get):
        result = resolve_account_config("nonexistent")
        assert result is None

    @patch("cli_helpers.get_default_account", return_value=None)
    def test_returns_none_when_no_default(self, mock_default):
        result = resolve_account_config(None)
        assert result is None


# ══════════════════════════════════════════════════════════════
#  get_account_username
# ══════════════════════════════════════════════════════════════

class TestGetAccountUsername:

    @patch("cli_helpers.get_account_by_name")
    def test_returns_username(self, mock_get):
        mock_get.return_value = _MOCK_ACCOUNTS[0]
        result = get_account_username("alpha")
        assert result == "alpha_user"

    @patch("cli_helpers.get_default_account", return_value=None)
    def test_returns_none_when_no_account(self, mock_default):
        result = get_account_username(None)
        assert result is None
