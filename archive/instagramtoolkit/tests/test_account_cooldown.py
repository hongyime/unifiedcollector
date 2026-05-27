"""Tests for src/account_cooldown.py — cooldown and quota managers.

All tests use tmp directories; time.time is patched for deterministic results.
"""
import json
import os
import time
from unittest.mock import patch

import pytest

from account_cooldown import AccountCooldownManager, AccountQuotaManager


# ── Helpers ──────────────────────────────────────────────────

@pytest.fixture
def cooldown_mgr(monkeypatch):
    """AccountCooldownManager backed by an isolated in-memory DB."""
    import account_cooldown as _ac
    monkeypatch.setenv("DATABASE_URL", ":memory:")
    _ac._get_db._instance = None  # force fresh in-memory DB
    mgr = AccountCooldownManager()
    yield mgr
    _ac._get_db._instance = None  # reset after test


@pytest.fixture
def quota_mgr(monkeypatch):
    """AccountQuotaManager backed by an isolated in-memory DB."""
    import account_cooldown as _ac
    monkeypatch.setenv("DATABASE_URL", ":memory:")
    _ac._get_db._instance = None  # force fresh in-memory DB
    mgr = AccountQuotaManager()
    yield mgr
    _ac._get_db._instance = None  # reset after test


# ══════════════════════════════════════════════════════════════
#  AccountCooldownManager
# ══════════════════════════════════════════════════════════════

class TestCooldownManager:

    def test_not_on_cooldown_by_default(self, cooldown_mgr):
        assert cooldown_mgr.is_on_cooldown("acct1") is False

    def test_put_on_cooldown(self, cooldown_mgr):
        cooldown_mgr.put_on_cooldown("acct1", minutes=10)
        assert cooldown_mgr.is_on_cooldown("acct1") is True

    def test_cooldown_expires(self, cooldown_mgr):
        cooldown_mgr.put_on_cooldown("acct1", minutes=10)
        # Advance time past cooldown
        with patch("account_cooldown.time.time", return_value=time.time() + 700):
            assert cooldown_mgr.is_on_cooldown("acct1") is False

    def test_get_cooldown_remaining(self, cooldown_mgr):
        cooldown_mgr.put_on_cooldown("acct1", minutes=5)
        remaining = cooldown_mgr.get_cooldown_remaining("acct1")
        assert 200 < remaining <= 300  # roughly 5 min

    def test_remaining_zero_when_not_set(self, cooldown_mgr):
        assert cooldown_mgr.get_cooldown_remaining("acct1") == 0.0

    def test_clear_cooldown(self, cooldown_mgr):
        cooldown_mgr.put_on_cooldown("acct1", minutes=60)
        cooldown_mgr.clear_cooldown("acct1")
        assert cooldown_mgr.is_on_cooldown("acct1") is False

    def test_get_available_accounts(self, cooldown_mgr):
        cooldown_mgr.put_on_cooldown("acct2", minutes=60)
        available = cooldown_mgr.get_available_accounts(["acct1", "acct2", "acct3"])
        assert "acct1" in available
        assert "acct2" not in available
        assert "acct3" in available

    def test_reason_stored(self, cooldown_mgr):
        # Reason is stored in DB; verify put_on_cooldown doesn't raise
        cooldown_mgr.put_on_cooldown("acct1", reason="429 rate-limit")
        # account should be on cooldown — verify via public API
        assert cooldown_mgr.is_on_cooldown("acct1") is True


# ══════════════════════════════════════════════════════════════
#  AccountQuotaManager
# ══════════════════════════════════════════════════════════════

class TestQuotaManager:

    def test_starts_with_no_usage(self, quota_mgr):
        assert quota_mgr.can_view_profiles("acct1") is True
        assert quota_mgr.can_perform_action("acct1") is True

    def test_record_profile_view(self, quota_mgr):
        quota_mgr.record_profile_view("acct1", count=5)
        summary = quota_mgr.get_usage_summary("acct1")
        assert summary["profile_views"].startswith("5/")

    def test_record_action(self, quota_mgr):
        quota_mgr.record_action("acct1", count=10)
        summary = quota_mgr.get_usage_summary("acct1")
        assert summary["actions"].startswith("10/")

    @patch("account_cooldown.DAILY_QUOTA_PROFILE_VIEWS", 5)
    def test_profile_view_quota_exceeded(self, quota_mgr):
        for _ in range(6):
            quota_mgr.record_profile_view("acct1")
        assert quota_mgr.can_view_profiles("acct1") is False

    @patch("account_cooldown.DAILY_QUOTA_ACTIONS", 10)
    def test_action_quota_exceeded(self, quota_mgr):
        for _ in range(11):
            quota_mgr.record_action("acct1")
        assert quota_mgr.can_perform_action("acct1") is False

    @patch("account_cooldown.DAILY_QUOTA_PROFILE_VIEWS", 0)
    def test_zero_quota_means_unlimited(self, quota_mgr):
        # Record a moderate amount (avoid 1000-write file locking on Windows)
        quota_mgr.record_profile_view("acct1", count=999)
        assert quota_mgr.can_view_profiles("acct1") is True

    def test_multiple_accounts_independent(self, quota_mgr):
        quota_mgr.record_profile_view("acct1", count=50)
        quota_mgr.record_action("acct2", count=100)
        s1 = quota_mgr.get_usage_summary("acct1")
        s2 = quota_mgr.get_usage_summary("acct2")
        assert s1["profile_views"].startswith("50/")
        assert s2["actions"].startswith("100/")

    def test_usage_summary_has_date(self, quota_mgr):
        quota_mgr.record_action("acct1")
        summary = quota_mgr.get_usage_summary("acct1")
        assert "date" in summary


# ══════════════════════════════════════════════════════════════
#  AccountCooldownManager — additional edge cases
# ══════════════════════════════════════════════════════════════

class TestCooldownPersistence:

    def test_clear_non_cooled_account_is_noop(self, cooldown_mgr):
        cooldown_mgr.clear_cooldown("never_set")
        assert cooldown_mgr.is_on_cooldown("never_set") is False

    def test_all_accounts_unavailable(self, cooldown_mgr):
        cooldown_mgr.put_on_cooldown("a", minutes=60)
        cooldown_mgr.put_on_cooldown("b", minutes=60)
        available = cooldown_mgr.get_available_accounts(["a", "b"])
        assert available == []

    def _DEAD_test_corrupt_cooldown_file_loads_empty(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        cf = os.path.join(data_dir, "cooldowns.json")
        with open(cf, "w") as f:
            f.write("{not valid json")
        monkeypatch.setattr("account_cooldown.DATA_DIR", data_dir)
        monkeypatch.setattr("account_cooldown._COOLDOWN_FILE", cf)
        mgr = AccountCooldownManager()
        assert mgr._cooldowns == {}


# ══════════════════════════════════════════════════════════════
#  AccountQuotaManager — additional edge cases
# ══════════════════════════════════════════════════════════════

class TestQuotaEdgeCases:

    @patch("account_cooldown.DAILY_QUOTA_ACTIONS", 0)
    def test_zero_action_quota_means_unlimited(self, quota_mgr):
        quota_mgr.record_action("acct1", count=999)
        assert quota_mgr.can_perform_action("acct1") is True

    def test_ensure_account_can_be_called_without_error(self, quota_mgr):
        """_ensure_account is callable and doesn't raise."""
        quota_mgr.record_action("acct1", count=5)
        # Should not raise
        quota_mgr._ensure_account("acct1")
        summary = quota_mgr.get_usage_summary("acct1")
        # Actions should still be tracked (date hasn't changed in this session)
        assert "actions" in summary
        assert "date" in summary

    def test_print_all_usage_shows_all_accounts(self, quota_mgr, capsys):
        quota_mgr.record_action("acct1", count=5)
        quota_mgr.record_profile_view("acct2", count=10)
        quota_mgr.print_all_usage()
        output = capsys.readouterr().out
        assert "acct1" in output
        assert "acct2" in output

    def test_print_all_usage_empty(self, quota_mgr, capsys):
        quota_mgr.print_all_usage()
        output = capsys.readouterr().out
        assert "No usage data yet" in output

    # test_corrupt_quota_file_loads_empty removed — quota is DB-backed, no JSON file


# TestFileLockExploration removed — cooldown/quota now DB-backed, no JSON file locking

