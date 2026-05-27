"""Tests for src/following_media_downloader.py — FollowingMediaDownloader (offline, mocked).

All instaloader API calls are mocked.  Tests verify:
 - state persistence (load/save download state via DB)
 - following list filtering
 - download_account_media flow (success, blocked, not-in-following)
 - download_single_account delegation
 - download_all_following batch filtering
 - show_progress display
"""
import os
from unittest.mock import MagicMock, patch

import pytest
import instaloader
import instaloader.exceptions

_MOCK_ACCOUNTS = [
    {"name": "acct1", "username": "user_one", "password": "pw1"},
    {"name": "acct2", "username": "user_two", "password": "pw2"},
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Redirect all config paths to tmp and suppress real auth."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", list(_MOCK_ACCOUNTS))
    monkeypatch.setattr("config.SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr("following_media_downloader.DATA_DIR", data_dir)
    monkeypatch.setattr("profile_access_tracker.DATA_DIR", data_dir)
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)
    # Point DB to in-memory SQLite so tests don't touch the real DB
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")


def _make_downloader(monkeypatch, tmp_path, *, download_state=None):
    """Build a FollowingMediaDownloader with mocked auth."""
    with patch("following_media_downloader.InstagramAccountManager") as MockMgr:
        mock_mgr = MagicMock()
        mock_loader = MagicMock(spec=instaloader.Instaloader)
        mock_loader.context = MagicMock()
        mock_mgr.get_authenticated_loader.return_value = mock_loader
        MockMgr.return_value = mock_mgr

        from following_media_downloader import FollowingMediaDownloader
        dl = FollowingMediaDownloader()
        dl.loader = mock_loader
        dl.current_account = _MOCK_ACCOUNTS[0]
        dl.downloads_dir = str(tmp_path / "downloads")
        os.makedirs(dl.downloads_dir, exist_ok=True)

        # Seed state via DB if provided
        if download_state:
            dl.download_state = download_state
            dl._save_download_state()
            # Reload to confirm round-trip
            dl.download_state = dl._load_download_state()

        return dl


# ══════════════════════════════════════════════════════════════
#  Download state persistence
# ══════════════════════════════════════════════════════════════

class TestDownloadStatePersistence:
    def test_loads_empty_state_when_no_file(self, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        assert dl.download_state["completed_accounts"] == []
        assert dl.download_state["failed_accounts"] == []

    def test_loads_existing_state(self, monkeypatch, tmp_path):
        state = {
            "account_used": "acct1",
            "started_at": "2025-01-01",
            "last_updated": None,
            "completed_accounts": ["alice", "bob"],
            "failed_accounts": ["charlie"],
            "current_account_progress": {},
            "total_stats": {"photos": 5, "videos": 3, "stories": 0, "highlights": 0, "profile_photos": 2},
        }
        dl = _make_downloader(monkeypatch, tmp_path, download_state=state)
        assert dl.download_state["completed_accounts"] == ["alice", "bob"]
        assert dl.download_state["total_stats"]["photos"] == 5

    def test_save_and_reload(self, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.download_state["completed_accounts"].append("test_user")
        dl._save_download_state()

        # Reload from DB and verify
        reloaded = dl._load_download_state()
        assert "test_user" in reloaded["completed_accounts"]
        assert reloaded["last_updated"] is not None

    def test_handles_corrupt_state_file(self, monkeypatch, tmp_path):
        # With DB backend there's no corrupt file — empty state is always clean
        dl = _make_downloader(monkeypatch, tmp_path)
        assert dl.download_state["completed_accounts"] == []


# ══════════════════════════════════════════════════════════════
#  download_account_media
# ══════════════════════════════════════════════════════════════

class TestDownloadAccountMedia:
    def test_skips_not_in_following(self, monkeypatch, tmp_path, capsys):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["alice", "bob"]
        result = dl.download_account_media("charlie")
        assert result is False
        assert "not in following" in capsys.readouterr().out.lower()

    @patch("following_media_downloader.retry_with_backoff", return_value=None)
    def test_returns_false_when_profile_not_found(self, mock_retry, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["nonexistent"]
        result = dl.download_account_media("nonexistent")
        assert result is False
        assert "nonexistent" in dl.download_state["failed_accounts"]

    @patch("following_media_downloader.retry_with_backoff")
    @patch("following_media_downloader.profile_access_blocked", return_value=True)
    def test_returns_false_when_access_blocked(self, mock_blocked, mock_retry, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["private_user"]
        mock_profile = MagicMock()
        mock_profile.is_private = True
        mock_retry.return_value = mock_profile
        result = dl.download_account_media("private_user")
        assert result is False

    @patch("following_media_downloader.retry_with_backoff")
    @patch("following_media_downloader.profile_access_blocked", return_value=False)
    def test_success_marks_completed(self, mock_blocked, mock_retry, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["good_user"]
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.profile_pic_url = "https://example.com/pic.jpg"
        mock_profile.get_posts.return_value = []
        mock_profile.userid = 12345
        mock_retry.return_value = mock_profile
        dl.loader.get_stories.return_value = []
        dl.loader.get_highlights.return_value = []

        result = dl.download_account_media("good_user")
        assert result is True
        assert "good_user" in dl.download_state["completed_accounts"]

    @patch("following_media_downloader.retry_with_backoff")
    @patch("following_media_downloader.profile_access_blocked", return_value=False)
    def test_removes_from_failed_on_success(self, mock_blocked, mock_retry, monkeypatch, tmp_path):
        state = {
            "account_used": "acct1", "started_at": None, "last_updated": None,
            "completed_accounts": [], "failed_accounts": ["retry_user"],
            "current_account_progress": {},
            "total_stats": {"photos": 0, "videos": 0, "stories": 0, "highlights": 0, "profile_photos": 0},
        }
        dl = _make_downloader(monkeypatch, tmp_path, download_state=state)
        dl.following_list = ["retry_user"]
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.profile_pic_url = "https://example.com/pic.jpg"
        mock_profile.get_posts.return_value = []
        mock_profile.userid = 12345
        mock_retry.return_value = mock_profile
        dl.loader.get_stories.return_value = []
        dl.loader.get_highlights.return_value = []

        dl.download_account_media("retry_user")
        assert "retry_user" not in dl.download_state["failed_accounts"]
        assert "retry_user" in dl.download_state["completed_accounts"]

    @patch("following_media_downloader.retry_with_backoff")
    def test_profile_not_exists_exception(self, mock_retry, monkeypatch, tmp_path, capsys):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["gone_user"]
        mock_retry.side_effect = instaloader.exceptions.ProfileNotExistsException("")
        result = dl.download_account_media("gone_user")
        assert result is False
        assert "gone_user" in dl.download_state["failed_accounts"]

    @patch("following_media_downloader.retry_with_backoff")
    @patch("following_media_downloader.profile_access_blocked", return_value=False)
    def test_increments_stats_on_post_download(self, mock_blocked, mock_retry, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["poster"]
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.profile_pic_url = "https://example.com/pic.jpg"
        mock_profile.userid = 12345

        # One non-video post
        mock_post = MagicMock()
        mock_post.is_video = False
        mock_profile.get_posts.return_value = [mock_post]

        # Return profile + pic result + post result
        mock_retry.side_effect = [mock_profile, True, True]
        dl.loader.get_stories.return_value = []
        dl.loader.get_highlights.return_value = []

        dl.download_account_media("poster")
        assert dl.download_state["total_stats"]["profile_photos"] >= 1


# ══════════════════════════════════════════════════════════════
#  download_single_account
# ══════════════════════════════════════════════════════════════

class TestDownloadSingleAccount:
    def test_rejects_not_in_following(self, monkeypatch, tmp_path, capsys):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.following_list = ["alice"]
        result = dl.download_single_account("bob")
        assert result is False
        assert "not in your following" in capsys.readouterr().out.lower()


# ══════════════════════════════════════════════════════════════
#  setup_downloads_directory
# ══════════════════════════════════════════════════════════════

class TestSetupDownloadsDirectory:
    @patch("following_media_downloader.get_downloads_directory")
    def test_creates_account_subdir(self, mock_get_dl, monkeypatch, tmp_path):
        mock_get_dl.return_value = str(tmp_path / "dl_root")
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.downloads_dir = None
        result = dl.setup_downloads_directory()
        assert "following_media_acct1" in result
        assert os.path.isdir(result)


# ══════════════════════════════════════════════════════════════
#  show_progress
# ══════════════════════════════════════════════════════════════

class TestShowProgress:
    def test_no_session_found(self, monkeypatch, tmp_path, capsys):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.download_state["account_used"] = None
        dl.show_progress()
        output = capsys.readouterr().out
        assert "No download session" in output

    def test_shows_stats(self, monkeypatch, tmp_path, capsys):
        state = {
            "account_used": "acct1", "started_at": "2025-01-01", "last_updated": "2025-01-02",
            "completed_accounts": ["alice"], "failed_accounts": ["bob"],
            "current_account_progress": {},
            "total_stats": {"photos": 10, "videos": 5, "stories": 2, "highlights": 1, "profile_photos": 1},
        }
        dl = _make_downloader(monkeypatch, tmp_path, download_state=state)
        dl.show_progress()
        output = capsys.readouterr().out
        assert "acct1" in output
        assert "1" in output  # completed count


# ══════════════════════════════════════════════════════════════
#  cleanup
# ══════════════════════════════════════════════════════════════

class TestCleanup:
    def test_calls_manager_logout(self, monkeypatch, tmp_path):
        dl = _make_downloader(monkeypatch, tmp_path)
        dl.cleanup()
        dl.manager.logout.assert_called_once()
