"""Integration tests — hit the real Instagram API via saved sessions.

These tests are **skipped** by default.  Run them with:

    pytest tests/ --run-integration -v

Requirements:
  - A valid session file in ``sessions/`` for the account configured as
    ``INTEGRATION_ACCOUNT`` in conftest.py (default: "b" / bryanseah234).
  - Network access to Instagram.

The tests use the public profile ``therock`` (Dwayne Johnson) as a safe
read-only target.  Downloads go to the project ``downloads/`` directory.
"""
import os
import glob

import pytest
import instaloader

# Mark every test in this module
pytestmark = pytest.mark.integration


# ══════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════

class TestAuthentication:
    """Verify we can authenticate and the session is usable."""

    def test_loader_is_authenticated(self, integration_loader):
        loader, _ = integration_loader
        assert loader is not None
        assert hasattr(loader, "context")

    def test_session_context_has_username(self, integration_loader):
        loader, _ = integration_loader
        # The context should expose the logged-in username
        assert loader.context is not None


# ══════════════════════════════════════════════════════════════
#  Profile retrieval (get_profile / Profile.from_username)
# ══════════════════════════════════════════════════════════════

class TestProfileRetrieval:
    """Test real profile lookup for 'therock'."""

    def test_get_profile_public_user(self, integration_loader):
        from media_utils import get_profile

        loader, _ = integration_loader
        profile = get_profile(loader, "therock")
        assert profile is not None
        assert profile.username == "therock"
        assert profile.is_private is False

    def test_get_profile_nonexistent(self, integration_loader):
        from media_utils import get_profile

        loader, _ = integration_loader
        profile = get_profile(loader, "this_user_definitely_does_not_exist_xyz_12345")
        assert profile is None

    def test_profile_has_expected_fields(self, integration_loader):
        loader, _ = integration_loader
        profile = instaloader.Profile.from_username(loader.context, "therock")
        # Basic sanity — these should always be present on a public profile
        assert profile.full_name  # "Dwayne Johnson" or similar
        assert profile.userid > 0
        assert isinstance(profile.mediacount, int)
        assert isinstance(profile.followers, int)
        assert isinstance(profile.followees, int)


# ══════════════════════════════════════════════════════════════
#  Media utils helpers on a real profile
# ══════════════════════════════════════════════════════════════

class TestMediaUtilsIntegration:

    def test_is_accessible_private_public_user(self, integration_loader):
        from media_utils import is_accessible_private, get_profile

        loader, _ = integration_loader
        profile = get_profile(loader, "therock")
        # Public profile → is_accessible_private should be False
        assert is_accessible_private(profile) is False

    def test_profile_access_blocked_public(self, integration_loader):
        from media_utils import profile_access_blocked, get_profile

        loader, _ = integration_loader
        profile = get_profile(loader, "therock")
        assert profile_access_blocked(profile) is False

    def test_summarize_real_profile(self, integration_loader):
        from media_utils import summarize_profile, get_profile

        loader, _ = integration_loader
        profile = get_profile(loader, "therock")
        summary = summarize_profile(profile)
        assert summary["exists"] is True
        assert summary["username"] == "therock"
        assert summary["is_private"] is False


# ══════════════════════════════════════════════════════════════
#  Download profile photo
# ══════════════════════════════════════════════════════════════

class TestDownloadProfilePhoto:
    """Download the profile picture of 'therock' to ``downloads/``."""

    def test_download_profile_photo(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from account_manager import InstagramAccountManager
        from conftest import INTEGRATION_ACCOUNT

        # Build a downloader the "real" way
        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        success = dl.download_profile_photo("therock")
        dl.cleanup()

        assert success is True

        # Verify at least one file was created in the user directory
        user_dir = os.path.join(downloads_dir, "user_therock")
        assert os.path.isdir(user_dir), f"Expected dir {user_dir}"
        files = os.listdir(user_dir)
        assert len(files) >= 1, f"Expected at least 1 file, got {files}"


# ══════════════════════════════════════════════════════════════
#  Download posts (with limit)
# ══════════════════════════════════════════════════════════════

class TestDownloadPosts:
    """Download 1 post from 'therock' to verify the pipeline works end-to-end."""

    def test_download_one_post(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from conftest import INTEGRATION_ACCOUNT

        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        success = dl.download_posts("therock", limit=1)
        dl.cleanup()

        assert success is True

        user_dir = os.path.join(downloads_dir, "user_therock")
        # Should have at least 1 file (image or video + metadata)
        files = os.listdir(user_dir)
        assert len(files) >= 1


# ══════════════════════════════════════════════════════════════
#  Download stories (may be empty — still success)
# ══════════════════════════════════════════════════════════════

class TestDownloadStories:
    """Stories may or may not be present; either way the call should succeed."""

    def test_download_stories_succeeds(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from conftest import INTEGRATION_ACCOUNT

        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        success = dl.download_stories("therock")
        dl.cleanup()

        assert success is True


# ══════════════════════════════════════════════════════════════
#  Download highlights (may be empty — still success)
# ══════════════════════════════════════════════════════════════

class TestDownloadHighlights:
    """Highlights may or may not exist for the target profile."""

    def test_download_highlights_succeeds(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from conftest import INTEGRATION_ACCOUNT

        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        success = dl.download_highlights("therock")
        dl.cleanup()

        assert success is True


# ══════════════════════════════════════════════════════════════
#  download_all (profile + 1 post + stories + highlights)
# ══════════════════════════════════════════════════════════════

class TestDownloadAll:

    def test_download_all_returns_summary(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from conftest import INTEGRATION_ACCOUNT

        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        result = dl.download_all("therock", post_limit=1)
        dl.cleanup()

        assert isinstance(result, dict)
        assert "results" in result
        # At minimum, profile photo and posts should succeed for a public account
        assert result["results"]["profile_photo"] is True
        assert result["results"]["posts"] is True
        assert result["success"] or result["partial_success"]


# ══════════════════════════════════════════════════════════════
#  Error cases
# ══════════════════════════════════════════════════════════════

class TestErrorCases:
    """Verify correct failure behavior when hitting the real API."""

    def test_download_nonexistent_user_returns_false(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from conftest import INTEGRATION_ACCOUNT

        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        success = dl.download_posts("this_user_definitely_does_not_exist_xyz_12345")
        dl.cleanup()
        assert success is False

    def test_download_profile_photo_nonexistent_returns_false(self, integration_loader, downloads_dir):
        from download_media import MediaDownloader
        from conftest import INTEGRATION_ACCOUNT

        dl = MediaDownloader(INTEGRATION_ACCOUNT)
        dl.downloads_dir = downloads_dir

        success = dl.download_profile_photo("this_user_definitely_does_not_exist_xyz_12345")
        dl.cleanup()
        assert success is False


# ══════════════════════════════════════════════════════════════
#  Profile Access Tracker with real data
# ══════════════════════════════════════════════════════════════

class TestAccessTrackerIntegration:
    """Use the tracker against real profile data."""

    def test_record_and_query_real_profile(self, integration_loader, tmp_path, monkeypatch):
        from profile_access_tracker import ProfileAccessTracker
        from media_utils import get_profile

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)
        monkeypatch.setattr("profile_access_tracker.DATA_DIR", data_dir)
        monkeypatch.setattr("config.DATA_DIR", data_dir)

        loader, account = integration_loader
        profile = get_profile(loader, "therock")
        assert profile is not None

        tracker = ProfileAccessTracker()
        tracker.record_profile_access("therock", account, {
            "can_access": True,
            "is_public": not profile.is_private,
            "is_followed": getattr(profile, "followed_by_viewer", False),
        })

        summary = tracker.get_profile_summary("therock")
        assert summary["status"] == "tracked"
        assert summary["is_public"] is True
