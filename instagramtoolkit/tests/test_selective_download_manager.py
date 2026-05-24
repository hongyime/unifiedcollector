"""Tests for selective_download_manager.py — selective list management.

All tests use temp directories. get_available_usernames() is mocked to avoid DB.
"""
import json
import os
from unittest.mock import patch, MagicMock

import pytest

from selective_download_manager import SelectiveDownloadManager

_AVAILABLE = ["alice", "bob", "charlie", "diana", "eve"]


@pytest.fixture
def sdm(tmp_path, monkeypatch):
    """SelectiveDownloadManager with temp data dir; get_available_usernames mocked."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)

    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("selective_download_manager.DATA_DIR", data_dir)

    mgr = SelectiveDownloadManager()
    # Patch instance method so DB is not queried in tests that use add/remove
    mgr.get_available_usernames = lambda: list(_AVAILABLE)
    return mgr


# ══════════════════════════════════════════════════════════════
#  Initial state
# ══════════════════════════════════════════════════════════════

class TestSelectiveInit:

    def test_starts_empty(self, sdm):
        assert sdm.selective_list == []
        assert sdm.has_selection() is False

    def test_loads_existing_list(self, tmp_path, monkeypatch):
        data_dir = str(tmp_path / "data2")
        os.makedirs(data_dir, exist_ok=True)
        list_file = os.path.join(data_dir, "selective_download_list.json")
        with open(list_file, "w") as f:
            json.dump({"usernames": ["alice", "bob"], "total_count": 2, "last_updated": ""}, f)

        monkeypatch.setattr("config.DATA_DIR", data_dir)
        monkeypatch.setattr("selective_download_manager.DATA_DIR", data_dir)

        mgr = SelectiveDownloadManager()
        assert mgr.selective_list == ["alice", "bob"]
        assert mgr.has_selection() is True


# ══════════════════════════════════════════════════════════════
#  get_available_usernames — DB-backed
# ══════════════════════════════════════════════════════════════

class TestGetAvailableUsernames:

    def test_returns_list_from_db(self, tmp_path, monkeypatch):
        """get_available_usernames() queries UsernameRepository from DB."""
        monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"), raising=False)
        monkeypatch.setattr("selective_download_manager.DATA_DIR", str(tmp_path / "data"), raising=False)

        mock_repo = MagicMock()
        mock_repo.get_all.return_value = [
            {"username": "alice"}, {"username": "bob"}, {"username": "charlie"},
        ]
        with patch("db.repositories.username_repository.UsernameRepository", return_value=mock_repo), \
             patch("db.manager.DatabaseManager"):
            mgr = SelectiveDownloadManager()
            result = mgr.get_available_usernames()
        assert result == ["alice", "bob", "charlie"]

    def test_returns_empty_on_db_error(self, tmp_path, monkeypatch):
        """Returns empty list when DB raises."""
        monkeypatch.setattr("config.DATA_DIR", str(tmp_path / "data"), raising=False)
        monkeypatch.setattr("selective_download_manager.DATA_DIR", str(tmp_path / "data"), raising=False)

        with patch("db.manager.DatabaseManager", side_effect=Exception("db gone")):
            mgr = SelectiveDownloadManager()
            result = mgr.get_available_usernames()
        assert result == []


# ══════════════════════════════════════════════════════════════
#  add_username / remove_username
# ══════════════════════════════════════════════════════════════

class TestAddRemoveUsername:

    def test_add_valid_username(self, sdm):
        result = sdm.add_username("alice")
        assert result is True
        assert "alice" in sdm.selective_list

    def test_add_duplicate_returns_true(self, sdm):
        sdm.add_username("alice")
        result = sdm.add_username("alice")
        assert result is True
        assert sdm.selective_list.count("alice") == 1

    def test_add_unknown_username_returns_false(self, sdm):
        result = sdm.add_username("nonexistent_user")
        assert result is False
        assert "nonexistent_user" not in sdm.selective_list

    def test_remove_existing(self, sdm):
        sdm.add_username("bob")
        result = sdm.remove_username("bob")
        assert result is True
        assert "bob" not in sdm.selective_list

    def test_remove_nonexistent_returns_false(self, sdm):
        result = sdm.remove_username("zzz")
        assert result is False


# ══════════════════════════════════════════════════════════════
#  clear_list
# ══════════════════════════════════════════════════════════════

class TestClearList:

    def test_clear(self, sdm):
        sdm.add_username("alice")
        sdm.add_username("bob")
        sdm.clear_list()
        assert sdm.selective_list == []
        assert sdm.has_selection() is False


# ══════════════════════════════════════════════════════════════
#  get_selected_usernames
# ══════════════════════════════════════════════════════════════

class TestGetSelectedUsernames:

    def test_returns_copy(self, sdm):
        sdm.add_username("alice")
        selected = sdm.get_selected_usernames()
        selected.append("extra")
        assert "extra" not in sdm.selective_list

    def test_contains_all_added(self, sdm):
        sdm.add_username("alice")
        sdm.add_username("charlie")
        assert set(sdm.get_selected_usernames()) == {"alice", "charlie"}


# ══════════════════════════════════════════════════════════════
#  _handle_number_selection
# ══════════════════════════════════════════════════════════════

class TestHandleNumberSelection:

    def test_single_number(self, sdm):
        available = ["alice", "bob", "charlie"]
        sdm._handle_number_selection("2", available)
        assert "bob" in sdm.selective_list

    def test_comma_separated_numbers(self, sdm):
        available = ["alice", "bob", "charlie", "diana"]
        sdm._handle_number_selection("1,3", available)
        assert "alice" in sdm.selective_list
        assert "charlie" in sdm.selective_list

    def test_range_selection(self, sdm):
        available = ["alice", "bob", "charlie", "diana", "eve"]
        sdm._handle_number_selection("2-4", available)
        assert "bob" in sdm.selective_list
        assert "charlie" in sdm.selective_list
        assert "diana" in sdm.selective_list

    def test_out_of_range_ignored(self, sdm):
        available = ["alice", "bob"]
        sdm._handle_number_selection("5", available)
        assert len(sdm.selective_list) == 0


# ══════════════════════════════════════════════════════════════
#  _handle_username_selection
# ══════════════════════════════════════════════════════════════

class TestHandleUsernameSelection:

    def test_single_username(self, sdm):
        available = ["alice", "bob"]
        sdm._handle_username_selection("alice", available)
        assert "alice" in sdm.selective_list

    def test_comma_separated_usernames(self, sdm):
        available = ["alice", "bob", "charlie"]
        sdm._handle_username_selection("alice,charlie", available)
        assert "alice" in sdm.selective_list
        assert "charlie" in sdm.selective_list

    def test_unknown_username_skipped(self, sdm):
        available = ["alice"]
        sdm._handle_username_selection("unknown", available)
        assert len(sdm.selective_list) == 0


# ══════════════════════════════════════════════════════════════
#  Persistence
# ══════════════════════════════════════════════════════════════

class TestPersistence:

    def test_save_and_reload(self, sdm, tmp_path, monkeypatch):
        sdm.add_username("alice")
        sdm.add_username("bob")

        # Recreate — should load from disk
        data_dir = str(tmp_path / "data")
        monkeypatch.setattr("config.DATA_DIR", data_dir)
        monkeypatch.setattr("selective_download_manager.DATA_DIR", data_dir)
        mgr2 = SelectiveDownloadManager()
        assert set(mgr2.selective_list) == {"alice", "bob"}
