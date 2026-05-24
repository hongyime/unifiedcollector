"""Tests for src/download_media.py — MediaDownloader (offline, fully mocked).

All instaloader API calls are mocked.  These tests verify:
 - correct return values (True/False) for success/failure
 - that get_profile failures propagate correctly
 - download_all aggregation
"""
import os
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import instaloader
import instaloader.exceptions

# Patch out the account manager login before importing MediaDownloader
_MOCK_ACCOUNTS = [{"name": "test", "username": "testuser", "password": "pw"}]


@pytest.fixture(autouse=True)
def _isolate_download_media(monkeypatch, tmp_path):
    """Prevent MediaDownloader.__init__ from doing real login or disk I/O."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr("config.DATA_DIR", data_dir)
    monkeypatch.setattr("config.INSTAGRAM_ACCOUNTS", _MOCK_ACCOUNTS)
    monkeypatch.setattr("config.SESSIONS_DIR", str(tmp_path / "sessions"))
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)


def _make_downloader(tmp_path):
    """Build a MediaDownloader with a mocked manager & loader."""
    with patch("download_media.InstagramAccountManager") as MockMgr:
        mock_mgr_inst = MagicMock()
        mock_loader = MagicMock(spec=instaloader.Instaloader)
        mock_loader.context = MagicMock()
        mock_mgr_inst.get_authenticated_loader.return_value = mock_loader
        MockMgr.return_value = mock_mgr_inst

        from download_media import MediaDownloader
        dl = MediaDownloader("test")
        dl.downloads_dir = str(tmp_path / "downloads")
        os.makedirs(dl.downloads_dir, exist_ok=True)
        return dl


# ══════════════════════════════════════════════════════════════
#  download_profile_photo
# ══════════════════════════════════════════════════════════════

class TestDownloadProfilePhoto:

    @patch("download_media.MediaDownloader.verify_download", return_value=True)
    @patch("download_media.retry_with_backoff")
    def test_returns_true_on_success(self, mock_retry, mock_verify, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.profile_pic_url = "https://example.com/pic.jpg"
        # First call = get_profile, second = download_pic
        mock_retry.side_effect = [mock_profile, True]

        result = dl.download_profile_photo("therock")
        assert result is True

    @patch("download_media.retry_with_backoff")
    def test_returns_false_when_profile_not_found(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.return_value = None  # get_profile returns None

        result = dl.download_profile_photo("nonexistent_xyz")
        assert result is False

    def test_returns_false_for_empty_username(self, tmp_path):
        dl = _make_downloader(tmp_path)
        assert dl.download_profile_photo("") is False
        assert dl.download_profile_photo("  ") is False


# ══════════════════════════════════════════════════════════════
#  download_posts
# ══════════════════════════════════════════════════════════════

class TestDownloadPosts:

    @patch("download_media.MediaDownloader.verify_download", return_value=True)
    @patch("download_media.retry_with_backoff")
    def test_returns_true_on_success(self, mock_retry, mock_verify, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_post = MagicMock()
        mock_profile.get_posts.return_value = [mock_post]
        # First call = get_profile, subsequent = download_post
        mock_retry.side_effect = [mock_profile, True]

        result = dl.download_posts("therock", limit=1)
        assert result is True

    @patch("download_media.retry_with_backoff")
    def test_returns_false_when_profile_not_found(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.return_value = None

        result = dl.download_posts("nonexistent_xyz")
        assert result is False

    @patch("download_media.MediaDownloader.verify_download", return_value=True)
    @patch("download_media.retry_with_backoff")
    def test_respects_limit(self, mock_retry, mock_verify, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.get_posts.return_value = [MagicMock() for _ in range(10)]
        # get_profile + 3 post downloads
        mock_retry.side_effect = [mock_profile] + [True] * 3

        result = dl.download_posts("therock", limit=3)
        assert result is True
        # get_profile call + 3 download_post calls = 4 total
        assert mock_retry.call_count == 4

    def test_returns_false_for_empty_username(self, tmp_path):
        dl = _make_downloader(tmp_path)
        assert dl.download_posts("") is False

    @patch("download_media.MediaDownloader.verify_download", return_value=True)
    @patch("download_media.retry_with_backoff")
    def test_continues_on_individual_post_failure(self, mock_retry, mock_verify, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.get_posts.return_value = [MagicMock(), MagicMock(), MagicMock()]
        # get_profile, then fail-succeed-succeed
        mock_retry.side_effect = [mock_profile, None, True, True]

        result = dl.download_posts("therock")
        assert result is True  # overall success because some posts downloaded


# ══════════════════════════════════════════════════════════════
#  download_stories
# ══════════════════════════════════════════════════════════════

class TestDownloadStories:

    @patch("download_media.retry_with_backoff")
    def test_returns_true_when_no_stories(self, mock_retry, tmp_path):
        """No active stories is still success (0 stories is valid)."""
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_retry.return_value = mock_profile
        dl.loader.get_stories.return_value = []

        result = dl.download_stories("therock")
        assert result is True

    @patch("download_media.retry_with_backoff")
    def test_returns_false_when_profile_not_found(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.return_value = None

        result = dl.download_stories("nonexistent_xyz")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_for_blocked_private(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = True
        mock_profile.followed_by_viewer = False
        mock_retry.return_value = mock_profile

        result = dl.download_stories("private_user")
        assert result is False


# ══════════════════════════════════════════════════════════════
#  download_highlights
# ══════════════════════════════════════════════════════════════

class TestDownloadHighlights:

    @patch("download_media.retry_with_backoff")
    def test_returns_true_when_no_highlights(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_retry.return_value = mock_profile
        dl.loader.get_highlights.return_value = []

        result = dl.download_highlights("therock")
        assert result is True

    @patch("download_media.retry_with_backoff")
    def test_returns_false_when_profile_not_found(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.return_value = None

        result = dl.download_highlights("nonexistent_xyz")
        assert result is False


# ══════════════════════════════════════════════════════════════
#  download_all
# ══════════════════════════════════════════════════════════════

class TestDownloadAll:

    @patch("download_media.MediaDownloader.download_highlights", return_value=True)
    @patch("download_media.MediaDownloader.download_stories", return_value=True)
    @patch("download_media.MediaDownloader.download_posts", return_value=True)
    @patch("download_media.MediaDownloader.download_profile_photo", return_value=True)
    def test_returns_summary_with_all_true(self, m_pfp, m_posts, m_stories, m_hl, tmp_path):
        dl = _make_downloader(tmp_path)
        result = dl.download_all("therock")
        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["partial_success"] is False
        assert result["success_count"] == 4
        assert result["results"]["profile_photo"] is True
        assert result["results"]["posts"] is True
        assert result["results"]["stories"] is True
        assert result["results"]["highlights"] is True

    @patch("download_media.MediaDownloader.download_highlights", return_value=False)
    @patch("download_media.MediaDownloader.download_stories", return_value=True)
    @patch("download_media.MediaDownloader.download_posts", return_value=False)
    @patch("download_media.MediaDownloader.download_profile_photo", return_value=True)
    def test_partial_failure(self, m_pfp, m_posts, m_stories, m_hl, tmp_path):
        dl = _make_downloader(tmp_path)
        result = dl.download_all("therock")
        assert result["success"] is False
        assert result["partial_success"] is True
        assert result["success_count"] == 2
        assert result["results"]["profile_photo"] is True
        assert result["results"]["posts"] is False
        assert result["results"]["stories"] is True
        assert result["results"]["highlights"] is False


# ══════════════════════════════════════════════════════════════
#  cleanup
# ══════════════════════════════════════════════════════════════

class TestMediaDownloaderCleanup:

    def test_calls_manager_logout(self, tmp_path):
        dl = _make_downloader(tmp_path)
        dl.cleanup()
        dl.manager.logout.assert_called_once()


# ══════════════════════════════════════════════════════════════
#  download_profile_photo — additional edge cases
# ══════════════════════════════════════════════════════════════

class TestDownloadProfilePhotoEdgeCases:
    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_profile_not_exists_exception(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.side_effect = instaloader.exceptions.ProfileNotExistsException("")
        result = dl.download_profile_photo("ghost_user")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_when_download_pic_returns_none(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.profile_pic_url = "https://example.com/pic.jpg"
        # get_profile succeeds, download_pic returns None (retry exhaustion)
        mock_retry.side_effect = [mock_profile, None]
        result = dl.download_profile_photo("therock")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_generic_exception(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.side_effect = Exception("network error")
        result = dl.download_profile_photo("therock")
        assert result is False


# ══════════════════════════════════════════════════════════════
#  download_posts — additional edge cases
# ══════════════════════════════════════════════════════════════

class TestDownloadPostsEdgeCases:
    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_profile_not_exists(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.side_effect = instaloader.exceptions.ProfileNotExistsException("")
        result = dl.download_posts("ghost")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_when_access_blocked(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = True
        mock_profile.followed_by_viewer = False
        mock_retry.return_value = mock_profile
        result = dl.download_posts("private_user")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_private_during_iteration(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_profile.get_posts.side_effect = instaloader.exceptions.PrivateProfileNotFollowedException("")
        mock_retry.return_value = mock_profile
        result = dl.download_posts("sneaky_private")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_true_with_zero_posts(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.followed_by_viewer = True
        mock_profile.get_posts.return_value = []
        mock_retry.return_value = mock_profile
        result = dl.download_posts("no_posts_user")
        assert result is True


# ══════════════════════════════════════════════════════════════
#  download_stories — additional edge cases
# ══════════════════════════════════════════════════════════════

class TestDownloadStoriesEdgeCases:
    @patch("download_media.MediaDownloader.verify_download", return_value=True)
    @patch("download_media.retry_with_backoff")
    def test_downloads_story_items(self, mock_retry, mock_verify, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_profile.userid = 12345
        mock_retry.side_effect = [mock_profile, True, True]

        mock_item1 = MagicMock()
        mock_item2 = MagicMock()
        mock_story = MagicMock()
        mock_story.get_items.return_value = [mock_item1, mock_item2]
        dl.loader.get_stories.return_value = [mock_story]

        result = dl.download_stories("story_user")
        assert result is True

    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_profile_not_exists(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.side_effect = instaloader.exceptions.ProfileNotExistsException("")
        result = dl.download_stories("ghost")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_generic_exception(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.side_effect = Exception("connection reset")
        result = dl.download_stories("bad_user")
        assert result is False

    def test_returns_false_for_empty_username(self, tmp_path):
        dl = _make_downloader(tmp_path)
        assert dl.download_stories("") is False
        assert dl.download_stories("   ") is False


# ══════════════════════════════════════════════════════════════
#  download_highlights — additional edge cases
# ══════════════════════════════════════════════════════════════

class TestDownloadHighlightsEdgeCases:
    @patch("download_media.retry_with_backoff")
    def test_returns_false_for_blocked_private(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = True
        mock_profile.followed_by_viewer = False
        mock_retry.return_value = mock_profile
        result = dl.download_highlights("private_user")
        assert result is False

    @patch("download_media.MediaDownloader.verify_download", return_value=True)
    @patch("download_media.retry_with_backoff")
    def test_downloads_highlight_items(self, mock_retry, mock_verify, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_retry.side_effect = [mock_profile, True, True]

        mock_item1 = MagicMock()
        mock_item2 = MagicMock()
        mock_highlight = MagicMock()
        mock_highlight.title = "Travel"
        mock_highlight.get_items.return_value = [mock_item1, mock_item2]
        dl.loader.get_highlights.return_value = [mock_highlight]

        result = dl.download_highlights("hl_user")
        assert result is True

    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_profile_not_exists(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_retry.side_effect = instaloader.exceptions.ProfileNotExistsException("")
        result = dl.download_highlights("ghost")
        assert result is False

    @patch("download_media.retry_with_backoff")
    def test_returns_false_on_private_exception(self, mock_retry, tmp_path):
        dl = _make_downloader(tmp_path)
        mock_profile = MagicMock()
        mock_profile.is_private = False
        mock_retry.return_value = mock_profile
        dl.loader.get_highlights.side_effect = instaloader.exceptions.PrivateProfileNotFollowedException("")
        result = dl.download_highlights("sneaky_private")
        assert result is False

    def test_returns_false_for_empty_username(self, tmp_path):
        dl = _make_downloader(tmp_path)
        assert dl.download_highlights("") is False


# ══════════════════════════════════════════════════════════════
#  _setup_target_directory
# ══════════════════════════════════════════════════════════════

class TestSetupTargetDirectory:
    def test_creates_user_directory(self, tmp_path):
        dl = _make_downloader(tmp_path)
        target = dl._setup_target_directory("alice")
        assert os.path.isdir(target)
        assert target.endswith("user_alice")

    def test_sets_dirname_pattern(self, tmp_path):
        dl = _make_downloader(tmp_path)
        dl._setup_target_directory("bob")
        assert dl.loader.dirname_pattern is not None


# ══════════════════════════════════════════════════════════════
#  P1 Logic - Return Contract Mismatch Exploration Tests
#  Task 5.1: Test mocking download_all() to return dict with
#  {'success': False, 'partial_success': True}
# ══════════════════════════════════════════════════════════════

class TestReturnContractMismatchExploration:
    """**Validates: Requirements 2.1**
    
    Exploration tests to demonstrate MediaDownloader.download_all() return
    contract bugs BEFORE implementing fixes. These tests should FAIL on
    unfixed code to confirm the bug exists.
    """

    @patch("download_media.MediaDownloader.download_highlights", return_value=False)
    @patch("download_media.MediaDownloader.download_stories", return_value=False)
    @patch("download_media.MediaDownloader.download_posts", return_value=True)
    @patch("download_media.MediaDownloader.download_profile_photo", return_value=True)
    def test_download_all_returns_dict_not_boolean(self, m_pfp, m_posts, m_stories, m_hl, tmp_path):
        """Test that download_all() returns a dict structure, not a boolean.
        
        This test verifies the return contract of download_all(). The function
        should return a dict with keys: success, partial_success, success_count,
        total_count, results.
        
        Expected on UNFIXED code: This test should PASS (dict is returned).
        Expected on FIXED code: This test should PASS (dict is still returned).
        """
        dl = _make_downloader(tmp_path)
        result = dl.download_all("testuser")
        
        # Verify return type is dict
        assert isinstance(result, dict), "download_all() should return dict, not boolean"
        
        # Verify dict structure
        assert "success" in result
        assert "partial_success" in result
        assert "success_count" in result
        assert "total_count" in result
        assert "results" in result
        
        # Verify partial success case
        assert result["success"] is False
        assert result["partial_success"] is True
        assert result["success_count"] == 2
        assert result["total_count"] == 4
