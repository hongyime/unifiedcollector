"""Tests for src/progress_manager.py — progress tracking logic.

All file I/O uses tmp directories so no real data/ files are touched.
"""
import json
import os
import signal
from unittest.mock import patch, MagicMock

import pytest

from progress_manager import ProgressManager


# ── Helpers ──────────────────────────────────────────────────

@pytest.fixture
def pm(tmp_path, monkeypatch):
    """Create a ProgressManager with isolated in-memory DB and temp directory."""
    import progress_manager as _pm_mod
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    monkeypatch.setattr("progress_manager.DATA_DIR", data_dir)
    monkeypatch.setattr("progress_manager.SPIDER_PROGRESS_FILE", os.path.join(data_dir, "spider_progress.json"))
    monkeypatch.setattr("progress_manager.DOWNLOAD_PROGRESS_FILE", os.path.join(data_dir, "download_progress.json"))
    monkeypatch.setattr("progress_manager.BATCH_STATE_FILE", os.path.join(data_dir, "batch_state.json"))

    # Isolate DB — use in-memory SQLite so tests don't see real data
    monkeypatch.setenv("DATABASE_URL", ":memory:")
    _pm_mod._get_db._instance = None  # reset singleton

    mgr = ProgressManager(operation_type="spider")
    yield mgr
    _pm_mod._get_db._instance = None  # reset after test


# ══════════════════════════════════════════════════════════════
#  Extraction / migration
# ══════════════════════════════════════════════════════════════

class TestExtractUsername:
    def test_string_passthrough(self):
        assert ProgressManager._extract_username("alice") == "alice"

    def test_dict_extraction(self):
        entry = {"username": "bob", "extra": 123}
        assert ProgressManager._extract_username(entry) == "bob"

    def test_dict_missing_key(self):
        entry = {"notusername": "x"}
        # Falls back to str(entry)
        assert isinstance(ProgressManager._extract_username(entry), str)


class TestMigrateProgressData:
    def test_converts_legacy_dicts(self, pm):
        # Simulate legacy dict entries that arrived via an old JSON import
        pm.progress_data["completed"] = [{"username": "alice"}, {"username": "bob"}]
        pm.progress_data["failed"] = [{"username": "charlie"}]
        pm._migrate_progress_data()

        assert pm.progress_data["completed"] == ["alice", "bob"]
        assert pm.progress_data["failed"] == ["charlie"]


# ══════════════════════════════════════════════════════════════
#  Progress file routing
# ══════════════════════════════════════════════════════════════

class TestGetProgressFile:
    def test_spider_type(self, pm):
        assert "spider_progress" in pm.progress_file

    def test_download_type(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path / "data2")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setattr("progress_manager.DATA_DIR", data_dir)
        monkeypatch.setattr("progress_manager.SPIDER_PROGRESS_FILE", os.path.join(data_dir, "sp.json"))
        monkeypatch.setattr("progress_manager.DOWNLOAD_PROGRESS_FILE", os.path.join(data_dir, "dl.json"))
        monkeypatch.setattr("progress_manager.BATCH_STATE_FILE", os.path.join(data_dir, "batch.json"))
        pm2 = ProgressManager(operation_type="download")
        assert "dl.json" in pm2.progress_file


# ══════════════════════════════════════════════════════════════
#  Core CRUD
# ══════════════════════════════════════════════════════════════

class TestMarkCompleted:
    def test_adds_to_completed(self, pm):
        pm.mark_completed("alice")
        assert "alice" in pm.progress_data["completed"]
        assert pm.progress_data["statistics"]["successful"] == 1

    def test_removes_from_pending(self, pm):
        pm.mark_pending("bob")
        pm.mark_completed("bob")
        assert "bob" not in pm.progress_data["pending"]
        assert "bob" in pm.progress_data["completed"]

    def test_idempotent(self, pm):
        pm.mark_completed("alice")
        pm.mark_completed("alice")
        assert pm.progress_data["completed"].count("alice") == 1


class TestMarkFailed:
    def test_adds_to_failed(self, pm):
        pm.mark_failed("charlie", error_msg="timeout")
        assert "charlie" in pm.progress_data["failed"]
        assert pm.progress_data["statistics"]["failed"] == 1

    def test_stores_error_message(self, pm):
        pm.mark_failed("charlie", error_msg="rate limited")
        assert pm.progress_data["errors"]["charlie"]["error"] == "rate limited"

    def test_removes_from_pending(self, pm):
        pm.mark_pending("diana")
        pm.mark_failed("diana")
        assert "diana" not in pm.progress_data["pending"]


class TestIsCompleted:
    def test_true_for_completed(self, pm):
        pm.mark_completed("alice")
        assert pm.is_completed("alice") is True

    def test_false_for_unknown(self, pm):
        assert pm.is_completed("unknown") is False


class TestGetRemainingUsers:
    def test_excludes_completed_and_failed(self, pm):
        pm.mark_completed("alice")
        pm.mark_failed("bob")
        remaining = pm.get_remaining_users(["alice", "bob", "charlie", "diana"])
        assert remaining == ["charlie", "diana"]

    def test_returns_all_when_nothing_done(self, pm):
        remaining = pm.get_remaining_users(["x", "y"])
        assert remaining == ["x", "y"]


class TestClearFailedUsers:
    def test_clears_list(self, pm):
        pm.mark_failed("alice")
        pm.mark_failed("bob")
        pm.clear_failed_users()
        assert pm.progress_data["failed"] == []


class TestGetProgressSummary:
    def test_summary_counts(self, pm):
        pm.mark_completed("a")
        pm.mark_completed("b")
        pm.mark_failed("c")
        summary = pm.get_progress_summary()
        assert summary["completed"] == 2
        assert summary["failed"] == 1


class TestCanResume:
    def test_false_when_empty(self, pm):
        assert pm.can_resume() is False

    def test_true_with_completed(self, pm):
        pm.mark_completed("a")
        assert pm.can_resume() is True

    def test_true_with_failed(self, pm):
        pm.mark_failed("a")
        assert pm.can_resume() is True


class TestSaveAndLoad:
    def test_round_trip(self, pm):
        pm.mark_completed("round")
        pm.save_progress()

        # Load fresh — reuse the same DB connection and operation_id
        pm2 = ProgressManager.__new__(ProgressManager)
        pm2.operation_type = pm.operation_type
        pm2.progress_file = pm.progress_file
        pm2.batch_state_file = pm.batch_state_file
        pm2._repo = pm._repo
        pm2._operation_id = pm._operation_id
        pm2.progress_data = pm2._load_progress()
        pm2._migrate_progress_data()
        assert "round" in pm2.progress_data["completed"]


# ══════════════════════════════════════════════════════════════
#  Progress file routing — additional types
# ══════════════════════════════════════════════════════════════

class TestGetProgressFileAdditional:
    def test_following_media_download_type(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path / "data_fmd")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setattr("progress_manager.DATA_DIR", data_dir)
        monkeypatch.setattr("progress_manager.SPIDER_PROGRESS_FILE", os.path.join(data_dir, "sp.json"))
        monkeypatch.setattr("progress_manager.DOWNLOAD_PROGRESS_FILE", os.path.join(data_dir, "dl.json"))
        monkeypatch.setattr("progress_manager.BATCH_STATE_FILE", os.path.join(data_dir, "batch.json"))
        pm = ProgressManager(operation_type="following_media_download")
        assert "following_media_download_progress" in pm.progress_file

    def test_general_type(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path / "data_gen")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setattr("progress_manager.DATA_DIR", data_dir)
        monkeypatch.setattr("progress_manager.SPIDER_PROGRESS_FILE", os.path.join(data_dir, "sp.json"))
        monkeypatch.setattr("progress_manager.DOWNLOAD_PROGRESS_FILE", os.path.join(data_dir, "dl.json"))
        monkeypatch.setattr("progress_manager.BATCH_STATE_FILE", os.path.join(data_dir, "batch.json"))
        pm = ProgressManager(operation_type="general")
        assert "general_progress" in pm.progress_file


# ══════════════════════════════════════════════════════════════
#  Batch state
# ══════════════════════════════════════════════════════════════

class TestBatchState:
    def test_save_and_load(self, pm):
        pm.update_batch_state(current_operation="spider", total_users=10)
        assert pm.batch_state["current_operation"] == "spider"
        assert pm.batch_state["total_users"] == 10

        # Verify it persisted to DB
        pm2 = ProgressManager.__new__(ProgressManager)
        pm2.batch_state_file = pm.batch_state_file
        pm2._repo = pm._repo
        pm2._operation_id = pm._operation_id
        state = pm2._load_batch_state()
        assert state["current_operation"] == "spider"

    def test_load_returns_defaults_when_missing(self, pm):
        # batch_state_file doesn't exist yet (tmp_path), but __init__ already
        # loads defaults — just verify it doesn't crash
        assert pm.batch_state["current_operation"] is None

    def test_update_merges_keys(self, pm):
        pm.update_batch_state(current_user_index=3)
        pm.update_batch_state(operation_count=7)
        assert pm.batch_state["current_user_index"] == 3
        assert pm.batch_state["operation_count"] == 7

    def test_save_returns_true(self, pm):
        assert pm.save_batch_state() is True


# ══════════════════════════════════════════════════════════════
#  get_failed_users
# ══════════════════════════════════════════════════════════════

class TestGetFailedUsers:
    def test_returns_failed_list(self, pm):
        pm.mark_failed("alice")
        pm.mark_failed("bob", "timeout")
        failed = pm.get_failed_users()
        assert set(failed) == {"alice", "bob"}

    def test_empty_when_none_failed(self, pm):
        assert pm.get_failed_users() == []


# ══════════════════════════════════════════════════════════════
#  mark_completed with details
# ══════════════════════════════════════════════════════════════

class TestMarkCompletedDetails:
    def test_stores_details_dict(self, pm):
        pm.mark_completed("alice", details={"account_used": "acct1", "post_limit": 10})
        assert pm.progress_data["details"]["alice"]["account_used"] == "acct1"

    def test_removes_from_failed(self, pm):
        pm.mark_failed("alice")
        pm.mark_completed("alice")
        assert "alice" not in pm.progress_data["failed"]
        assert "alice" in pm.progress_data["completed"]


# ══════════════════════════════════════════════════════════════
#  cleanup_progress
# ══════════════════════════════════════════════════════════════

class TestCleanupProgress:
    def test_archives_progress_file(self, pm):
        pm.mark_completed("alice")
        assert pm.can_resume() is True

        pm.cleanup_progress()
        # After archival, operation rows are deleted — nothing left to resume
        assert pm.can_resume() is False

    def test_removes_batch_state(self, pm):
        pm.update_batch_state(current_operation="spider")
        assert pm.batch_state["current_operation"] == "spider"

        pm.cleanup_progress()
        # Batch-state rows are deleted on archive
        fresh_state = pm._load_batch_state()
        assert fresh_state.get("current_operation") is None


# ══════════════════════════════════════════════════════════════
#  mark_media_download_completed / failed
# ══════════════════════════════════════════════════════════════

class TestMediaDownloadTracking:
    def test_completed_adds_to_completed(self, pm):
        pm.mark_media_download_completed("alice", {"photos": 5, "videos": 2})
        assert "alice" in pm.progress_data["completed"]
        assert pm.progress_data["media_stats"]["alice"]["photos"] == 5

    def test_completed_removes_from_failed(self, pm):
        pm.mark_failed("alice")
        pm.mark_media_download_completed("alice")
        assert "alice" not in pm.progress_data["failed"]

    def test_failed_adds_to_failed(self, pm):
        pm.mark_media_download_failed("bob", error="network timeout")
        assert "bob" in pm.progress_data["failed"]
        assert pm.progress_data["errors"]["bob"]["error"] == "network timeout"

    def test_failed_removes_from_completed(self, pm):
        pm.mark_completed("bob")
        pm.mark_media_download_failed("bob")
        assert "bob" not in pm.progress_data["completed"]
        assert "bob" in pm.progress_data["failed"]

    def test_idempotent_completed(self, pm):
        pm.mark_media_download_completed("alice")
        pm.mark_media_download_completed("alice")
        assert pm.progress_data["completed"].count("alice") == 1


# ══════════════════════════════════════════════════════════════
#  get_remaining_accounts
# ══════════════════════════════════════════════════════════════

class TestGetRemainingAccounts:
    def test_excludes_completed_and_failed(self, pm):
        pm.mark_completed("alice")
        pm.mark_failed("bob")
        remaining = pm.get_remaining_accounts(["alice", "bob", "charlie", "diana"])
        assert set(remaining) == {"charlie", "diana"}

    def test_returns_all_when_nothing_processed(self, pm):
        remaining = pm.get_remaining_accounts(["x", "y", "z"])
        assert remaining == ["x", "y", "z"]


# ══════════════════════════════════════════════════════════════
#  get_media_download_stats
# ══════════════════════════════════════════════════════════════

class TestGetMediaDownloadStats:
    def test_aggregates_completed_count(self, pm):
        pm.mark_media_download_completed("alice", {"photos": 10, "videos": 3})
        pm.mark_media_download_completed("bob", {"photos": 5, "videos": 1})
        stats = pm.get_media_download_stats()
        assert stats["accounts_completed"] == 2
        assert "media_downloaded" in stats  # structure present

    def test_empty_stats(self, pm):
        stats = pm.get_media_download_stats()
        assert stats["accounts_completed"] == 0
        assert "media_downloaded" in stats


# ══════════════════════════════════════════════════════════════
#  save_progress returns False on error
# ══════════════════════════════════════════════════════════════

class TestSaveProgressFailure:
    def test_returns_false_on_write_error(self, pm):
        # Make upsert_progress raise on all calls
        pm._repo = MagicMock()
        pm._repo.upsert_progress.side_effect = OSError("db full")
        pm.mark_completed("alice")  # adds to progress_data['completed']
        result = pm.save_progress()
        assert result is False


# ══════════════════════════════════════════════════════════════
#  handle_graceful_exit decorator
# ══════════════════════════════════════════════════════════════

class TestHandleGracefulExit:
    def test_normal_execution(self):
        from progress_manager import handle_graceful_exit

        @handle_graceful_exit()
        def good_func():
            return 42

        assert good_func() == 42

    def test_reraises_keyboard_interrupt(self):
        from progress_manager import handle_graceful_exit

        @handle_graceful_exit()
        def interrupted():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            interrupted()

    def test_reraises_generic_exception(self):
        from progress_manager import handle_graceful_exit

        @handle_graceful_exit()
        def broken():
            raise ValueError("oops")

        with pytest.raises(ValueError, match="oops"):
            broken()
