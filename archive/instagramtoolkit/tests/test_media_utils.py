"""Tests for src/media_utils.py — profile retrieval and access helpers.

Covers get_profile, is_accessible_private, profile_access_blocked, summarize_profile.
"""
from unittest.mock import MagicMock, patch, PropertyMock
import pytest
import instaloader
import instaloader.exceptions

from media_utils import (
    get_profile,
    is_accessible_private,
    profile_access_blocked,
    summarize_profile,
)


# ══════════════════════════════════════════════════════════════
#  get_profile
# ══════════════════════════════════════════════════════════════

class TestGetProfile:
    """Tests for get_profile()."""

    def _make_loader(self):
        loader = MagicMock(spec=instaloader.Instaloader)
        loader.context = MagicMock()
        return loader

    @patch("media_utils.instaloader.Profile.from_username")
    def test_returns_profile_on_success(self, mock_from):
        loader = self._make_loader()
        mock_profile = MagicMock()
        mock_from.return_value = mock_profile

        result = get_profile(loader, "therock")
        assert result is mock_profile
        mock_from.assert_called_once_with(loader.context, "therock")

    @patch("media_utils.instaloader.Profile.from_username")
    def test_returns_none_for_nonexistent_profile(self, mock_from):
        loader = self._make_loader()
        mock_from.side_effect = instaloader.exceptions.ProfileNotExistsException("")

        result = get_profile(loader, "nonexistent_user_xyz")
        assert result is None

    @patch("media_utils.instaloader.Profile.from_username")
    def test_propagates_connection_exception(self, mock_from):
        """ConnectionException should NOT be caught — allows retry_with_backoff to retry."""
        loader = self._make_loader()
        mock_from.side_effect = instaloader.exceptions.ConnectionException("503")

        with pytest.raises(instaloader.exceptions.ConnectionException):
            get_profile(loader, "therock")

    @patch("media_utils.instaloader.Profile.from_username")
    def test_propagates_query_returned_bad_request(self, mock_from):
        loader = self._make_loader()
        mock_from.side_effect = instaloader.exceptions.QueryReturnedBadRequestException("400")

        with pytest.raises(instaloader.exceptions.QueryReturnedBadRequestException):
            get_profile(loader, "therock")

    @patch("media_utils.instaloader.Profile.from_username")
    def test_propagates_login_required_exception(self, mock_from):
        loader = self._make_loader()
        mock_from.side_effect = instaloader.exceptions.LoginRequiredException("")

        with pytest.raises(instaloader.exceptions.LoginRequiredException):
            get_profile(loader, "therock")

    def test_raises_on_none_loader(self):
        with pytest.raises(RuntimeError, match="Invalid loader"):
            get_profile(None, "therock")

    def test_raises_on_loader_without_context(self):
        loader = MagicMock(spec=[])  # No attributes
        with pytest.raises(RuntimeError, match="Invalid loader"):
            get_profile(loader, "therock")


# ══════════════════════════════════════════════════════════════
#  is_accessible_private
# ══════════════════════════════════════════════════════════════

class TestIsAccessiblePrivate:

    def test_true_for_private_followed(self):
        profile = MagicMock()
        profile.is_private = True
        profile.followed_by_viewer = True
        assert is_accessible_private(profile) is True

    def test_false_for_private_not_followed(self):
        profile = MagicMock()
        profile.is_private = True
        profile.followed_by_viewer = False
        assert is_accessible_private(profile) is False

    def test_false_for_public(self):
        profile = MagicMock()
        profile.is_private = False
        profile.followed_by_viewer = False
        assert is_accessible_private(profile) is False

    def test_false_for_none(self):
        assert is_accessible_private(None) is False


# ══════════════════════════════════════════════════════════════
#  profile_access_blocked
# ══════════════════════════════════════════════════════════════

class TestProfileAccessBlocked:

    def test_true_for_none(self):
        assert profile_access_blocked(None) is True

    def test_true_for_private_not_followed(self):
        profile = MagicMock()
        profile.is_private = True
        profile.followed_by_viewer = False
        assert profile_access_blocked(profile) is True

    def test_false_for_private_followed(self):
        profile = MagicMock()
        profile.is_private = True
        profile.followed_by_viewer = True
        assert profile_access_blocked(profile) is False

    def test_false_for_public(self):
        profile = MagicMock()
        profile.is_private = False
        profile.followed_by_viewer = False
        assert profile_access_blocked(profile) is False


# ══════════════════════════════════════════════════════════════
#  summarize_profile
# ══════════════════════════════════════════════════════════════

class TestSummarizeProfile:

    def test_none_returns_not_exists(self):
        result = summarize_profile(None)
        assert result == {"exists": False}

    def test_public_profile(self):
        profile = MagicMock()
        profile.username = "therock"
        profile.is_private = False
        profile.followed_by_viewer = False
        profile.has_blocked_viewer = False

        result = summarize_profile(profile)
        assert result["exists"] is True
        assert result["username"] == "therock"
        assert result["is_private"] is False

    def test_private_followed_profile(self):
        profile = MagicMock()
        profile.username = "someone"
        profile.is_private = True
        profile.followed_by_viewer = True
        profile.has_blocked_viewer = False

        result = summarize_profile(profile)
        assert result["exists"] is True
        assert result["is_private"] is True
        assert result["followed_by_viewer"] is True

    def test_handles_missing_attributes(self):
        """getattr fallback for has_blocked_viewer / followed_by_viewer."""
        profile = MagicMock(spec=["username", "is_private"])
        profile.username = "minimal"
        profile.is_private = False

        result = summarize_profile(profile)
        assert result["exists"] is True
        assert result["username"] == "minimal"
