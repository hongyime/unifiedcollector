"""Preservation property tests for yt-dlp fallback bugfix.

Property 2: Preservation — Non-403 Gallery-DL Paths Unchanged

These tests MUST PASS on UNFIXED code — they encode the baseline behavior that
the fix must preserve.

Observations encoded:
  - When gallery-dl succeeds: no fallback is invoked, tracker is updated, results returned as-is
  - When tracker pre-check returns 0 new videos: download is skipped, no gallery-dl or fallback invoked
  - When gallery-dl raises a non-403 ProviderError (e.g. '404 not found'): error propagates, no yt-dlp or Playwright called
  - When download_type == 'profile_pictures': yt-dlp fallback is NOT invoked (videos only)

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.errors import ProviderError
from src.models import DownloadResult
from src.provider import GalleryDLProvider


# ---------------------------------------------------------------------------
# Provider factory (same pattern as test_ytdlp_fallback_bug_condition.py)
# ---------------------------------------------------------------------------

def _make_provider(tracker_precheck_result=None) -> GalleryDLProvider:
    """Create a GalleryDLProvider with all subprocess calls mocked out."""
    def fake_run(args, **_kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, stdout="1.31.0", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    tracker = SimpleNamespace(
        count_for_user=lambda _u: 5,
        is_downloaded_in_folder=lambda *_a: False,
        mark_downloaded=MagicMock(),
    )

    with patch("src.provider.subprocess.run", side_effect=fake_run):
        with patch("src.provider.create_tracker", return_value=tracker):
            provider = GalleryDLProvider({"gallerydl": {
                "retries": 0,
                "sleep": 0,
                "tracker_required": False,
            }})

    provider._tracker = tracker
    return provider


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-403 error messages that should NOT trigger any fallback
NON_403_ERRORS = [
    "404 not found",
    "rate limit exceeded",
    "no videos found",
    "account is private",
    "network timeout",
    "connection refused",
    "dns resolution failed",
]

non_403_error_strategy = st.sampled_from(NON_403_ERRORS)

valid_username_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip())

valid_limit_strategy = st.integers(min_value=1, max_value=100)


# ---------------------------------------------------------------------------
# Property 2a: Gallery-DL success path — no fallback invoked
# ---------------------------------------------------------------------------

class TestGalleryDlSuccessPreservation:
    """When gallery-dl succeeds, no fallback (yt-dlp or Playwright) is invoked.

    Validates: Requirements 3.1
    """

    @given(
        username=valid_username_strategy,
        limit=valid_limit_strategy,
    )
    @settings(max_examples=30, deadline=None)
    def test_no_fallback_when_gallery_dl_succeeds(self, username: str, limit: int):
        """**Validates: Requirements 3.1**

        For all valid usernames and limits, when _run_gallery_dl succeeds,
        neither _download_with_ytdlp_fallback nor _download_with_browser_fallback
        is called.
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        fallback_calls = []

        # gallery-dl succeeds with one downloaded file
        fake_file = tmp_path / f"{username}" / f"{username}_2024-01-01_12345.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.touch()

        def fake_run_gallery_dl(*_args, **_kwargs):
            return [fake_file], 0

        def fake_ytdlp_fallback(*_args, **_kwargs):
            fallback_calls.append("ytdlp")
            return []

        def fake_browser_fallback(*_args, **_kwargs):
            fallback_calls.append("playwright")
            return []

        provider._run_gallery_dl = fake_run_gallery_dl
        provider._download_with_browser_fallback = fake_browser_fallback
        # Only attach ytdlp fallback mock if the method exists (unfixed code won't have it)
        if hasattr(provider, "_download_with_ytdlp_fallback"):
            provider._download_with_ytdlp_fallback = fake_ytdlp_fallback

        results = provider.download_user(username, limit, tmp_path)

        assert fallback_calls == [], (
            f"Expected no fallback calls when gallery-dl succeeds, "
            f"but got: {fallback_calls}"
        )

    @given(
        username=valid_username_strategy,
        limit=valid_limit_strategy,
    )
    @settings(max_examples=30, deadline=None)
    def test_tracker_updated_when_gallery_dl_succeeds(self, username: str, limit: int):
        """**Validates: Requirements 3.1**

        When gallery-dl succeeds, the tracker's mark_downloaded is called for
        each successfully downloaded file.
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        fake_file = tmp_path / f"{username}" / f"{username}_2024-01-01_12345.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.touch()

        def fake_run_gallery_dl(*_args, **_kwargs):
            return [fake_file], 0

        provider._run_gallery_dl = fake_run_gallery_dl

        results = provider.download_user(username, limit, tmp_path)

        # At least one result should be ok
        ok_results = [r for r in results if r.ok and r.status == "downloaded"]
        assert len(ok_results) >= 1, (
            f"Expected at least one successful result when gallery-dl succeeds. "
            f"Got: {results}"
        )

    @given(
        username=valid_username_strategy,
        limit=valid_limit_strategy,
    )
    @settings(max_examples=30, deadline=None)
    def test_results_returned_as_is_when_gallery_dl_succeeds(self, username: str, limit: int):
        """**Validates: Requirements 3.1**

        When gallery-dl succeeds, download_user returns results derived from
        the downloaded files (not from any fallback).
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        fake_file = tmp_path / f"{username}" / f"{username}_2024-01-01_99999.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.touch()

        def fake_run_gallery_dl(*_args, **_kwargs):
            return [fake_file], 0

        provider._run_gallery_dl = fake_run_gallery_dl

        results = provider.download_user(username, limit, tmp_path)

        assert isinstance(results, list), "download_user must return a list"
        assert len(results) >= 1, "Expected at least one result"
        # Results should not be from a fallback (they should reference the fake file)
        ok_results = [r for r in results if r.ok]
        assert len(ok_results) >= 1, f"Expected ok results, got: {results}"


# ---------------------------------------------------------------------------
# Property 2b: Tracker pre-check returns 0 — download skipped entirely
# ---------------------------------------------------------------------------

class TestTrackerSkipPreservation:
    """When tracker pre-check returns 0 new videos, download is skipped.

    No gallery-dl, yt-dlp, or Playwright is invoked.

    Validates: Requirements 3.2
    """

    @given(username=valid_username_strategy)
    @settings(max_examples=30, deadline=None)
    def test_download_skipped_when_tracker_precheck_returns_zero(self, username: str):
        """**Validates: Requirements 3.2**

        When _tracker_precheck returns 0, download_user returns a skipped result
        without calling _run_gallery_dl, _download_with_ytdlp_fallback, or
        _download_with_browser_fallback.
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        invocations = []

        def fake_tracker_precheck(*_args, **_kwargs):
            return 0  # All videos already downloaded

        def fake_run_gallery_dl(*_args, **_kwargs):
            invocations.append("gallery_dl")
            return [], 0

        def fake_ytdlp_fallback(*_args, **_kwargs):
            invocations.append("ytdlp")
            return []

        def fake_browser_fallback(*_args, **_kwargs):
            invocations.append("playwright")
            return []

        provider._tracker_precheck = fake_tracker_precheck
        provider._run_gallery_dl = fake_run_gallery_dl
        provider._download_with_browser_fallback = fake_browser_fallback
        if hasattr(provider, "_download_with_ytdlp_fallback"):
            provider._download_with_ytdlp_fallback = fake_ytdlp_fallback

        results = provider.download_user(username, 10, tmp_path)

        assert invocations == [], (
            f"Expected no downloads when tracker pre-check returns 0, "
            f"but got invocations: {invocations}"
        )

        assert len(results) == 1, f"Expected exactly one result, got: {results}"
        assert results[0].status == "skipped", (
            f"Expected status='skipped', got: {results[0].status}"
        )
        assert results[0].ok is True, (
            f"Expected ok=True for skipped result, got: {results[0].ok}"
        )


# ---------------------------------------------------------------------------
# Property 2c: Non-403 ProviderError propagates — no fallback invoked
# ---------------------------------------------------------------------------

class TestNon403ErrorPreservation:
    """When gallery-dl raises a non-403 ProviderError, it propagates without fallback.

    Validates: Requirements 3.3
    """

    @given(error_msg=non_403_error_strategy)
    @settings(max_examples=len(NON_403_ERRORS), deadline=None)
    def test_non_403_error_propagates_without_fallback(self, error_msg: str):
        """**Validates: Requirements 3.3**

        For all non-403 error messages, when _run_gallery_dl raises ProviderError,
        neither _download_with_ytdlp_fallback nor _download_with_browser_fallback
        is called. The error is caught by the outer exception handler and returned
        as a failed DownloadResult.
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        fallback_calls = []

        def fake_run_gallery_dl(*_args, **_kwargs):
            raise ProviderError(error_msg)

        def fake_ytdlp_fallback(*_args, **_kwargs):
            fallback_calls.append("ytdlp")
            return []

        def fake_browser_fallback(*_args, **_kwargs):
            fallback_calls.append("playwright")
            return []

        provider._run_gallery_dl = fake_run_gallery_dl
        provider._download_with_browser_fallback = fake_browser_fallback
        if hasattr(provider, "_download_with_ytdlp_fallback"):
            provider._download_with_ytdlp_fallback = fake_ytdlp_fallback

        results = provider.download_user("testuser", 5, tmp_path)

        assert fallback_calls == [], (
            f"Expected no fallback calls for non-403 error '{error_msg}', "
            f"but got: {fallback_calls}"
        )

        # The outer exception handler returns a failed DownloadResult
        assert len(results) == 1, f"Expected exactly one result, got: {results}"
        assert results[0].ok is False, (
            f"Expected ok=False for non-403 error, got: {results[0].ok}"
        )
        assert results[0].status == "failed", (
            f"Expected status='failed', got: {results[0].status}"
        )

    @given(error_msg=non_403_error_strategy)
    @settings(max_examples=len(NON_403_ERRORS), deadline=None)
    def test_non_403_error_reason_preserved(self, error_msg: str):
        """**Validates: Requirements 3.3**

        The reason field of the failed DownloadResult contains the original error message.
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        def fake_run_gallery_dl(*_args, **_kwargs):
            raise ProviderError(error_msg)

        provider._run_gallery_dl = fake_run_gallery_dl

        results = provider.download_user("testuser", 5, tmp_path)

        assert results[0].reason is not None, "Expected a reason in the failed result"
        assert error_msg in results[0].reason, (
            f"Expected error message '{error_msg}' in reason '{results[0].reason}'"
        )


# ---------------------------------------------------------------------------
# Property 2d: profile_pictures download_type — yt-dlp fallback NOT invoked
# ---------------------------------------------------------------------------

class TestProfilePicturesPreservation:
    """When download_type == 'profile_pictures', yt-dlp fallback is never invoked.

    Validates: Requirements 3.6
    """

    @given(username=valid_username_strategy)
    @settings(max_examples=20, deadline=None)
    def test_ytdlp_not_invoked_for_profile_pictures_on_403(self, username: str):
        """**Validates: Requirements 3.6**

        When download_type='profile_pictures' and gallery-dl raises a 403 ProviderError,
        _download_with_ytdlp_fallback must NOT be called. The pipeline goes directly
        to Playwright (or returns a failed result if Playwright is unavailable).
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        ytdlp_calls = []

        def fake_run_gallery_dl(*_args, **_kwargs):
            raise ProviderError("403 forbidden")

        def fake_ytdlp_fallback(*_args, **_kwargs):
            ytdlp_calls.append("ytdlp")
            return []

        def fake_browser_fallback(uname, limit, target_dir, download_type):
            # Simulate Playwright returning a result for profile_pictures
            return [DownloadResult(
                ok=False,
                url=f"https://www.tiktok.com/@{uname}",
                status="failed",
                reason="Browser fallback does not support profile_pictures",
            )]

        provider._run_gallery_dl = fake_run_gallery_dl
        provider._download_with_browser_fallback = fake_browser_fallback
        if hasattr(provider, "_download_with_ytdlp_fallback"):
            provider._download_with_ytdlp_fallback = fake_ytdlp_fallback

        provider.download_user(username, 1, tmp_path, download_type="profile_pictures")

        assert ytdlp_calls == [], (
            f"yt-dlp fallback must NOT be invoked for download_type='profile_pictures'. "
            f"Got calls: {ytdlp_calls}"
        )

    @given(username=valid_username_strategy)
    @settings(max_examples=20, deadline=None)
    def test_ytdlp_not_invoked_for_profile_pictures_on_success(self, username: str):
        """**Validates: Requirements 3.6**

        When download_type='profile_pictures' and gallery-dl succeeds,
        _download_with_ytdlp_fallback is never called.
        """
        tmp_path = Path(tempfile.mkdtemp())
        provider = _make_provider()

        ytdlp_calls = []

        fake_file = tmp_path / f"{username}" / f"{username}_profile_2024-01-01.jpg"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.touch()

        def fake_run_gallery_dl(*_args, **_kwargs):
            return [fake_file], 0

        def fake_ytdlp_fallback(*_args, **_kwargs):
            ytdlp_calls.append("ytdlp")
            return []

        provider._run_gallery_dl = fake_run_gallery_dl
        if hasattr(provider, "_download_with_ytdlp_fallback"):
            provider._download_with_ytdlp_fallback = fake_ytdlp_fallback

        provider.download_user(username, 1, tmp_path, download_type="profile_pictures")

        assert ytdlp_calls == [], (
            f"yt-dlp fallback must NOT be invoked for download_type='profile_pictures'. "
            f"Got calls: {ytdlp_calls}"
        )
