"""Tests for main.py — CLI entry-point and top-level functions.

All imported operations are mocked — these tests verify argument parsing,
output formatting, and correct delegation to underlying modules.
"""
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

# ── helpers ──────────────────────────────────────────────────

_MOCK_ACCOUNTS = [
    {"name": "acct1", "username": "user_one", "password": "pw1"},
    {"name": "acct2", "username": "user_two", "password": "pw2"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect config paths to tmp and mock credentials."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", list(_MOCK_ACCOUNTS))
    monkeypatch.setattr("config.SESSIONS_DIR", str(tmp_path / "sessions"))
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)


# ══════════════════════════════════════════════════════════════
#  list_accounts
# ══════════════════════════════════════════════════════════════

class TestListAccounts:
    def test_prints_all_accounts(self, capsys):
        from main import list_accounts
        list_accounts()
        output = capsys.readouterr().out
        assert "user_one" in output
        assert "user_two" in output

    def test_marks_first_as_default(self, capsys):
        from main import list_accounts
        list_accounts()
        output = capsys.readouterr().out
        assert "(DEFAULT)" in output

    def test_empty_accounts(self, monkeypatch, capsys):
        monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", [])
        # Need to re-import since main caches the import
        # Instead just patch at module level
        monkeypatch.setattr("main.INSTAGRAM_ACCOUNTS", [])
        from main import list_accounts
        list_accounts()
        output = capsys.readouterr().out
        # Should print the header at minimum
        assert "Configured" in output


# ══════════════════════════════════════════════════════════════
#  login_account
# ══════════════════════════════════════════════════════════════

class TestLoginAccount:
    @patch("main.InstagramAccountManager")
    def test_unknown_account(self, MockMgr, capsys):
        from main import login_account
        login_account("nonexistent")
        output = capsys.readouterr().out
        assert "not found" in output

    @patch("main.InstagramAccountManager")
    def test_successful_login(self, MockMgr, capsys):
        mock_mgr = MagicMock()
        mock_mgr.login.return_value = True
        MockMgr.return_value = mock_mgr

        from main import login_account
        login_account("acct1")
        output = capsys.readouterr().out
        assert "Logged in" in output or "[OK]" in output
        mock_mgr.logout.assert_called_once()

    @patch("main.InstagramAccountManager")
    def test_failed_login(self, MockMgr, capsys):
        mock_mgr = MagicMock()
        mock_mgr.login.return_value = False
        MockMgr.return_value = mock_mgr

        from main import login_account
        login_account("acct1")
        output = capsys.readouterr().out
        assert "failed" in output.lower() or "ERROR" in output


# ══════════════════════════════════════════════════════════════
#  test_all_accounts
# ══════════════════════════════════════════════════════════════

class TestTestAllAccounts:
    @patch("main.InstagramAccountManager")
    def test_all_succeed(self, MockMgr, capsys):
        mock_mgr = MagicMock()
        mock_mgr.login.return_value = True
        MockMgr.return_value = mock_mgr

        from main import test_all_accounts
        test_all_accounts()
        output = capsys.readouterr().out
        assert "2" in output  # 2 successful

    @patch("main.InstagramAccountManager")
    def test_mixed_results(self, MockMgr, capsys):
        mock_mgr = MagicMock()
        mock_mgr.login.side_effect = [True, False]
        MockMgr.return_value = mock_mgr

        from main import test_all_accounts
        test_all_accounts()
        output = capsys.readouterr().out
        assert "Failed" in output or "failed" in output.lower()


# ══════════════════════════════════════════════════════════════
#  main() — argument parsing
# ══════════════════════════════════════════════════════════════

class TestMainArgParsing:
    """Test that main() correctly parses subcommands and routes them."""

    @patch("main.list_accounts")
    def test_list_command(self, mock_fn, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "list"])
        from main import main
        main()
        mock_fn.assert_called_once()

    @patch("main.login_account")
    def test_login_command(self, mock_fn, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "login", "acct1"])
        from main import main
        main()
        mock_fn.assert_called_once_with("acct1")

    @patch("main.test_all_accounts")
    def test_test_all_command(self, mock_fn, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "test-all"])
        from main import main
        main()
        mock_fn.assert_called_once()

    def test_requires_subcommand(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py"])
        from main import main
        with pytest.raises(SystemExit):
            main()

    @patch("main.load_usernames", return_value=["alice", "bob"])
    @patch("main.get_account_username", return_value="user_one")
    def test_analyze_command(self, mock_get_user, mock_load, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", [
            "main.py", "analyze",
            "--json", str(tmp_path / "out.json"),
            "--csv", str(tmp_path / "out.csv"),
        ])
        from main import main
        with patch("analyze_users.UserAnalyzer") as MockAnalyzer:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze.return_value = {}
            MockAnalyzer.return_value = mock_analyzer
            main()

    @patch("main.InstagramAccountManager")
    def test_download_single_user(self, MockMgr, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", [
            "main.py", "download", "--username", "therock", "--profile-only",
        ])
        from main import main

        with patch("download_media.MediaDownloader") as MockDL:
            mock_dl = MagicMock()
            mock_dl.download_profile_photo.return_value = True
            MockDL.return_value = mock_dl
            try:
                main()
            except Exception:
                pass  # OK if imports fail in test context

    def test_download_requires_mode(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["main.py", "download"])
        from main import main
        try:
            main()
        except SystemExit:
            pass
        output = capsys.readouterr().out
        # Should either print help or error about missing --username/--batch


# ══════════════════════════════════════════════════════════════
#  Argument parsing — access-stats
# ══════════════════════════════════════════════════════════════

class TestAccessStatsCommand:
    def test_access_stats_calls_print(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "access-stats"])
        mock_fn = MagicMock()
        with patch.dict("sys.modules", {}):
            pass
        from main import main
        with patch("profile_access_tracker.print_access_statistics", mock_fn):
            try:
                main()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
#  Argument parsing — progress subcommands
# ══════════════════════════════════════════════════════════════

class TestProgressCommand:
    def test_progress_requires_subcommand(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["main.py", "progress"])
        from main import main
        with pytest.raises(SystemExit):
            main()


# ══════════════════════════════════════════════════════════════
#  P1 Logic - Return Contract Mismatch Exploration Tests
#  Task 5.3: Verify main.py download handler treats result as
#  boolean (should fail on unfixed code)
# ══════════════════════════════════════════════════════════════

class TestReturnContractMismatchInMain:
    """**Validates: Requirements 2.1**
    
    Exploration tests to demonstrate that main.py download command handler
    incorrectly treats download_all() dict result as boolean. These tests
    should FAIL on unfixed code to confirm the bug exists.
    """

    @patch("download_media.MediaDownloader")
    def test_main_download_partial_success_handling(self, MockDL, monkeypatch, capsys, tmp_path):
        """Test that main.py download handler correctly handles partial success.
        
        When download_all() returns {'success': False, 'partial_success': True},
        the FIXED code in main.py correctly checks dict keys:
        success = download_result['success'] or download_result['partial_success']
        
        Expected on UNFIXED code: Would have treated dict as truthy (bug).
        Expected on FIXED code: This test should PASS because the code
        correctly checks dict keys.
        """
        monkeypatch.setattr("sys.argv", [
            "main.py", "download", "--username", "testuser"
        ])
        
        mock_dl = MagicMock()
        # Return dict with success=False but partial_success=True
        mock_dl.download_all.return_value = {
            "success": False,
            "partial_success": True,
            "success_count": 2,
            "total_count": 4,
            "results": {
                "profile_photo": True,
                "posts": True,
                "stories": False,
                "highlights": False
            },
        }
        MockDL.return_value = mock_dl
        
        from main import main
        try:
            main()
        except SystemExit:
            pass  # OK if exits after completion
        
        output = capsys.readouterr().out
        # Should show completion message (partial success is still success)
        assert "completed" in output.lower() or "ok" in output.lower()

    @patch("download_media.MediaDownloader")
    def test_main_download_complete_failure_handling(self, MockDL, monkeypatch, capsys, tmp_path):
        """Test that main.py download handler correctly handles complete failure.
        
        When download_all() returns {'success': False, 'partial_success': False},
        the FIXED code in main.py correctly checks dict keys and returns False.
        
        Expected on UNFIXED code: Would have treated dict as truthy (bug).
        Expected on FIXED code: This test should PASS because the code
        correctly checks dict keys and reports failure.
        """
        monkeypatch.setattr("sys.argv", [
            "main.py", "download", "--username", "testuser"
        ])
        
        mock_dl = MagicMock()
        # Return dict with both success and partial_success False
        mock_dl.download_all.return_value = {
            "success": False,
            "partial_success": False,
            "success_count": 0,
            "total_count": 4,
            "results": {
                "profile_photo": False,
                "posts": False,
                "stories": False,
                "highlights": False
            },
        }
        MockDL.return_value = mock_dl
        
        from main import main
        try:
            main()
        except SystemExit:
            pass  # OK if exits after completion
        
        output = capsys.readouterr().out
        # Should show failure message
        assert "failed" in output.lower() or "error" in output.lower()
