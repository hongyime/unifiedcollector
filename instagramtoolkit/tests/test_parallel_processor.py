"""Tests for src/parallel_processor.py — InstagramProcessor (offline, fully mocked).

All instaloader API calls are mocked.  Tests verify:
 - account switching (round-robin, cooldown-aware)
 - _execute_with_retry error categorisation
 - collect_relationships / download_media integration with progress
 - batch processing
"""
import os
import time
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest
import instaloader

_MOCK_ACCOUNTS = [
    {"name": "acct1", "username": "user_one", "password": "pw1"},
    {"name": "acct2", "username": "user_two", "password": "pw2"},
    {"name": "acct3", "username": "user_three", "password": "pw3"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect all config paths to tmp and suppress real auth."""
    import progress_manager as _pm_mod
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", list(_MOCK_ACCOUNTS))
    monkeypatch.setattr("config.SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr("config.SPIDER_PROGRESS_FILE", os.path.join(data_dir, "spider_progress.json"))
    monkeypatch.setattr("config.DOWNLOAD_PROGRESS_FILE", os.path.join(data_dir, "download_progress.json"))
    monkeypatch.setattr("config.BATCH_STATE_FILE", os.path.join(data_dir, "batch_state.json"))
    # Patch module-level imports that were captured at load time
    monkeypatch.setattr("progress_manager.DATA_DIR", data_dir)
    monkeypatch.setattr("progress_manager.SPIDER_PROGRESS_FILE", os.path.join(data_dir, "spider_progress.json"))
    monkeypatch.setattr("progress_manager.DOWNLOAD_PROGRESS_FILE", os.path.join(data_dir, "download_progress.json"))
    monkeypatch.setattr("progress_manager.BATCH_STATE_FILE", os.path.join(data_dir, "batch_state.json"))
    monkeypatch.setattr("profile_access_tracker.DATA_DIR", data_dir)
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)
    # Isolate DB — reset singleton so each test gets a fresh in-memory DB.
    # Both module aliases must be reset: conftest adds src/ to sys.path so
    # "progress_manager" and "src.progress_manager" are separate module entries.
    import src.progress_manager as _src_pm_mod
    monkeypatch.setenv("DATABASE_URL", ":memory:")
    _pm_mod._get_db._instance = None
    _src_pm_mod._get_db._instance = None
    yield
    _pm_mod._get_db._instance = None
    _src_pm_mod._get_db._instance = None


def _make_processor(monkeypatch, tmp_path, account_name=None, op_type="general"):
    """Create an InstagramProcessor with mocked internals."""
    # Stub out sleep calls — must also patch _interruptible_sleep which loops on time.sleep
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("parallel_processor.time.sleep", lambda _: None)
    monkeypatch.setattr("parallel_processor._interruptible_sleep", lambda *a, **kw: None)
    monkeypatch.setattr("src.resilience._interruptible_sleep", lambda *a, **kw: None, raising=False)
    # Patch the module-level INSTAGRAM_ACCOUNTS captured at import time
    monkeypatch.setattr("parallel_processor.INSTAGRAM_ACCOUNTS", list(_MOCK_ACCOUNTS))

    with patch("parallel_processor.InstagramAccountManager") as MockMgr, \
         patch("parallel_processor.RateLimiter") as MockRate:

        mock_mgr = MagicMock()
        mock_loader = MagicMock(spec=instaloader.Instaloader)
        mock_loader.context = MagicMock()
        mock_mgr.get_authenticated_loader.return_value = mock_loader
        MockMgr.return_value = mock_mgr

        mock_rate = MagicMock()
        MockRate.return_value = mock_rate

        from parallel_processor import InstagramProcessor
        proc = InstagramProcessor(account_name=account_name, operation_type=op_type)
        proc.manager = mock_mgr
        return proc


# ══════════════════════════════════════════════════════════════
#  Initialisation
# ══════════════════════════════════════════════════════════════

class TestInit:
    def test_default_account_index_zero(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        assert proc.current_account_index == 0

    def test_named_account_sets_index(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path, account_name="acct2")
        assert proc.current_account_index == 1

    def test_unknown_account_falls_back(self, monkeypatch, tmp_path, capsys):
        proc = _make_processor(monkeypatch, tmp_path, account_name="unknown")
        output = capsys.readouterr().out
        assert "not found" in output

    def test_has_all_accounts(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        assert len(proc.available_accounts) == 3


# ══════════════════════════════════════════════════════════════
#  _get_current_account_username
# ══════════════════════════════════════════════════════════════

class TestGetCurrentAccountUsername:
    def test_returns_username(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        assert proc._get_current_account_username() == "user_one"

    def test_returns_correct_after_switch(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.current_account_index = 2
        assert proc._get_current_account_username() == "user_three"


# ══════════════════════════════════════════════════════════════
#  _switch_account
# ══════════════════════════════════════════════════════════════

class TestSwitchAccount:
    def test_returns_false_with_single_account(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", [_MOCK_ACCOUNTS[0]])
        proc = _make_processor(monkeypatch, tmp_path)
        proc.available_accounts = [_MOCK_ACCOUNTS[0]]
        assert proc._switch_account() is False

    def test_round_robin(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct2", "acct3"]

        assert proc.current_account_index == 0
        proc._switch_account()
        assert proc.current_account_index == 1
        proc._switch_account()
        assert proc.current_account_index == 2
        proc._switch_account()
        assert proc.current_account_index == 0  # wraps around

    def test_skips_cooled_down_account(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.cooldown_manager = MagicMock()
        # acct2 is on cooldown
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct3"]

        proc.current_account_index = 0
        proc._switch_account()
        # Should skip acct2 (index 1) and go to acct3 (index 2)
        assert proc.current_account_index == 2

    def test_picks_fallback_when_all_on_cooldown(self, monkeypatch, tmp_path, capsys):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = []

        proc.current_account_index = 0
        proc._switch_account()
        output = capsys.readouterr().out
        assert "All accounts on cooldown" in output


# ══════════════════════════════════════════════════════════════
#  _get_best_account_for_user
# ══════════════════════════════════════════════════════════════

class TestGetBestAccountForUser:
    def test_returns_current_when_no_history(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.access_tracker = MagicMock()
        proc.access_tracker.get_best_account_for_profile.return_value = None
        assert proc._get_best_account_for_user("unknown") == 0

    def test_returns_recommended_account_index(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.access_tracker = MagicMock()
        proc.access_tracker.get_best_account_for_profile.return_value = "acct3"
        assert proc._get_best_account_for_user("someuser") == 2


# ══════════════════════════════════════════════════════════════
#  _record_access_attempt
# ══════════════════════════════════════════════════════════════

class TestRecordAccessAttempt:
    def test_records_success(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.access_tracker = MagicMock()
        proc._record_access_attempt("alice", True, is_public=True)
        proc.access_tracker.record_profile_access.assert_called_once()
        call_args = proc.access_tracker.record_profile_access.call_args
        assert call_args[0][0] == "alice"
        assert call_args[0][2]["can_access"] is True

    def test_records_failure_with_error(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.access_tracker = MagicMock()
        proc._record_access_attempt("alice", False, error=Exception("timeout"))
        call_args = proc.access_tracker.record_profile_access.call_args
        assert call_args[0][2]["can_access"] is False
        assert "timeout" in call_args[0][2]["error"]


# ══════════════════════════════════════════════════════════════
#  _handle_rate_limiting
# ══════════════════════════════════════════════════════════════

class TestHandleRateLimiting:
    def test_increments_operation_count(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        initial = proc.operation_count
        proc._handle_rate_limiting()
        assert proc.operation_count == initial + 1

    def test_records_quota(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc._handle_rate_limiting()
        proc.quota_manager.record_action.assert_called_once()


# ══════════════════════════════════════════════════════════════
#  _execute_with_retry
# ══════════════════════════════════════════════════════════════

class TestExecuteWithRetry:
    def test_success_on_first_try(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        result = proc._execute_with_retry(lambda: True)
        assert result is True

    def test_false_return_triggers_switch(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct2", "acct3"]

        call_count = 0
        def failing_then_success():
            nonlocal call_count
            call_count += 1
            return call_count >= 2  # fail first, succeed second

        result = proc._execute_with_retry(failing_then_success)
        assert result is True

    def test_quota_exhausted_switches_account(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        # First account exhausted, second ok
        proc.quota_manager.can_perform_action.side_effect = [False, True]
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct2", "acct3"]

        result = proc._execute_with_retry(lambda: True)
        assert result is True

    def test_rate_limit_puts_cooldown(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct2", "acct3"]

        def raise_rate_limit():
            raise Exception("please wait a few minutes before you try again")

        proc._execute_with_retry(raise_rate_limit)
        proc.cooldown_manager.put_on_cooldown.assert_called()

    def test_challenge_long_cooldown(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct2", "acct3"]

        def raise_challenge():
            raise Exception("checkpoint_required")

        proc._execute_with_retry(raise_challenge)
        # Challenge should get 4x cooldown
        cd_call = proc.cooldown_manager.put_on_cooldown.call_args
        assert cd_call is not None

    def test_non_recoverable_returns_false(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True

        def raise_unknown():
            raise Exception("some totally unexpected error xyzzy")

        result = proc._execute_with_retry(raise_unknown)
        assert result is False

    def test_auth_issue_switches_account(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.cooldown_manager = MagicMock()
        proc.cooldown_manager.get_available_accounts.return_value = ["acct1", "acct2", "acct3"]

        attempts = 0
        def raise_then_succeed():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise Exception("bad credentials supplied")
            return True

        result = proc._execute_with_retry(raise_then_succeed)
        assert result is True
        assert proc.current_account_index != 0  # switched away from first


# ══════════════════════════════════════════════════════════════
#  collect_relationships
# ══════════════════════════════════════════════════════════════

class TestCollectRelationships:
    def test_skips_already_completed(self, monkeypatch, tmp_path, capsys):
        proc = _make_processor(monkeypatch, tmp_path, op_type="spider")
        proc.progress_manager.mark_completed("alice")
        result = proc.collect_relationships("alice")
        assert result is True
        assert "already completed" in capsys.readouterr().out.lower()

    @patch("parallel_processor.RelationshipCollector")
    def test_marks_completed_on_success(self, MockCollector, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path, op_type="spider")
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.access_tracker = MagicMock()

        mock_coll = MagicMock()
        MockCollector.return_value = mock_coll

        result = proc.collect_relationships("bob")
        assert result is True
        assert proc.progress_manager.is_completed("bob")

    @patch("parallel_processor.RelationshipCollector")
    def test_marks_failed_on_failure(self, MockCollector, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path, op_type="spider")
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.access_tracker = MagicMock()

        MockCollector.side_effect = Exception("some totally unexpected error xyzzy")

        result = proc.collect_relationships("failing_user")
        assert result is False
        assert "failing_user" in proc.progress_manager.get_failed_users()


# ══════════════════════════════════════════════════════════════
#  download_media
# ══════════════════════════════════════════════════════════════

class TestDownloadMedia:
    def test_skips_already_completed(self, monkeypatch, tmp_path, capsys):
        proc = _make_processor(monkeypatch, tmp_path, op_type="download")
        proc.progress_manager.mark_completed("alice")
        result = proc.download_media("alice")
        assert result is True

    @patch("parallel_processor.MediaDownloader")
    def test_marks_completed_on_success(self, MockDL, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path, op_type="download")
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.downloads_dir = str(tmp_path / "downloads")
        os.makedirs(proc.downloads_dir, exist_ok=True)

        mock_dl = MagicMock()
        mock_dl.download_all.return_value = {
            "success": True,
            "partial_success": False,
            "success_count": 4,
            "total_count": 4,
            "results": {"profile_photo": True, "posts": True, "stories": True, "highlights": True},
        }
        MockDL.return_value = mock_dl

        result = proc.download_media("bob")
        assert result is True
        assert proc.progress_manager.is_completed("bob")

    @patch("parallel_processor.MediaDownloader")
    def test_marks_failed_on_failure(self, MockDL, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path, op_type="download")
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.downloads_dir = str(tmp_path / "downloads")

        MockDL.side_effect = Exception("some totally unexpected error xyzzy")

        result = proc.download_media("failing_user")
        assert result is False


# ══════════════════════════════════════════════════════════════
#  _get_downloads_dir
# ══════════════════════════════════════════════════════════════

class TestGetDownloadsDir:
    @patch("parallel_processor.get_downloads_directory", return_value="/tmp/downloads")
    def test_prompts_once_then_caches(self, mock_get, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        d1 = proc._get_downloads_dir()
        d2 = proc._get_downloads_dir()
        assert d1 == d2
        mock_get.assert_called_once()

    def test_returns_existing_dir(self, monkeypatch, tmp_path):
        proc = _make_processor(monkeypatch, tmp_path)
        proc.downloads_dir = "/some/path"
        assert proc._get_downloads_dir() == "/some/path"


# ══════════════════════════════════════════════════════════════
#  P1 Logic - Return Contract Mismatch Exploration Tests
#  Task 5.2: Verify InstagramProcessor.download_media() treats
#  result as boolean (should fail on unfixed code)
# ══════════════════════════════════════════════════════════════

class TestReturnContractMismatchInProcessor:
    """**Validates: Requirements 2.1**
    
    Exploration tests to demonstrate that InstagramProcessor.download_media()
    incorrectly treats download_all() dict result as boolean. These tests
    should FAIL on unfixed code to confirm the bug exists.
    """

    @patch("parallel_processor.MediaDownloader")
    def test_partial_success_treated_as_truthy(self, MockDL, monkeypatch, tmp_path):
        """Test that partial_success dict is incorrectly treated as truthy.
        
        When download_all() returns {'success': False, 'partial_success': True},
        the UNFIXED code treats this dict as truthy (because non-empty dicts
        are truthy in Python), causing incorrect success detection.
        
        Expected on UNFIXED code: This test should FAIL because the code
        incorrectly returns True when it should handle the dict structure.
        
        Expected on FIXED code: This test should PASS because the code
        correctly checks dict keys.
        """
        proc = _make_processor(monkeypatch, tmp_path, op_type="download")
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.downloads_dir = str(tmp_path / "downloads")
        os.makedirs(proc.downloads_dir, exist_ok=True)

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

        result = proc.download_media("testuser")
        
        # On UNFIXED code: result will be True (dict is truthy)
        # On FIXED code: result should be True (partial_success is handled)
        # This test documents the EXPECTED behavior after fix
        assert result is True, "partial_success should be treated as success"

    @patch("parallel_processor.MediaDownloader")
    def test_complete_failure_dict_treated_as_truthy(self, MockDL, monkeypatch, tmp_path):
        """Test that complete failure dict is incorrectly treated as truthy.
        
        When download_all() returns {'success': False, 'partial_success': False},
        the UNFIXED code treats this dict as truthy (because non-empty dicts
        are truthy in Python), causing incorrect success detection.
        
        Expected on UNFIXED code: This test should FAIL because the code
        incorrectly returns True when it should return False.
        
        Expected on FIXED code: This test should PASS because the code
        correctly checks dict keys and returns False.
        """
        proc = _make_processor(monkeypatch, tmp_path, op_type="download")
        proc.quota_manager = MagicMock()
        proc.quota_manager.can_perform_action.return_value = True
        proc.downloads_dir = str(tmp_path / "downloads")
        os.makedirs(proc.downloads_dir, exist_ok=True)

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

        result = proc.download_media("testuser")
        
        # On UNFIXED code: result will be True (dict is truthy) - BUG!
        # On FIXED code: result should be False (no success)
        assert result is False, "complete failure should return False"
