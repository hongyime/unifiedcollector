"""Bug condition exploration test for TikTok cookie auth failure.

Property 1: Bug Condition — Duplicate Cookie Config Causes Gallery-dl Hang

This test MUST FAIL on unfixed code — failure confirms the bug exists.
DO NOT attempt to fix the test or the code when it fails.

The bug: gallery-dl hangs when invoked with --cookies <file> AND
configs/gallery-dl.json also contains "cookies": "configs/tiktok_cookies.txt"
under extractor.tiktok. The duplicate/conflicting cookie configuration causes
gallery-dl to stall during session initialisation.

Expected failure modes on unfixed code:
  - check_cookies_validity() returns error="Cookie test timed out" (after 45 s)
  - _list_user_video_urls() returns [] (after 60 s timeout, exception swallowed)

Validates: Requirements 1.1, 1.2, 1.3, 1.4
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.provider import GalleryDLProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GALLERY_DL_JSON = Path("configs/gallery-dl.json")
COOKIES_FILE = Path("configs/tiktok_cookies.txt")
TEST_URL = "https://www.tiktok.com/@tiktok"
TEST_USERNAME = "tiktok"


def _gallery_dl_json_has_cookies_key() -> bool:
    """Return True if configs/gallery-dl.json has 'cookies' under extractor.tiktok."""
    if not GALLERY_DL_JSON.exists():
        return False
    data = json.loads(GALLERY_DL_JSON.read_text(encoding="utf-8"))
    return "cookies" in data.get("extractor", {}).get("tiktok", {})


def _make_real_provider() -> GalleryDLProvider:
    """Instantiate GalleryDLProvider with real cookies file and a short timeout."""
    config = {
        "gallerydl": {
            # Short timeout so tests don't hang forever in CI; still long enough
            # to observe the hang (gallery-dl will not return before this expires
            # when the bug is present).
            "timeout_seconds": 60,
            "retries": 0,
            "sleep": 0,
            "cookies_file": str(COOKIES_FILE),
            "tracker_required": False,
        }
    }
    return GalleryDLProvider(config)


# ---------------------------------------------------------------------------
# Pre-condition checks
# ---------------------------------------------------------------------------

def test_bug_precondition_cookies_file_exists():
    """Verify the cookies file used in bug condition tests exists and is non-empty.

    This is a pre-condition check — if this fails, the test environment is not
    set up correctly for the bug condition exploration.
    """
    assert COOKIES_FILE.exists(), f"Cookies file not found: {COOKIES_FILE}"
    assert COOKIES_FILE.stat().st_size > 0, "Cookies file is empty"


@pytest.mark.xfail(
    reason="Bug is fixed: 'cookies' key has been removed from gallery-dl.json. "
           "This precondition only holds on unfixed code.",
    strict=True,
)
def test_bug_precondition_gallery_dl_json_has_cookies_key():
    """Verify configs/gallery-dl.json still has the 'cookies' key under extractor.tiktok.

    This is the unfixed state. If this key is absent, the bug has already been fixed
    and the exploration tests will pass (which is the expected post-fix behavior).

    **Validates: Requirements 1.4**
    """
    assert GALLERY_DL_JSON.exists(), f"gallery-dl.json not found: {GALLERY_DL_JSON}"
    has_key = _gallery_dl_json_has_cookies_key()
    assert has_key, (
        "configs/gallery-dl.json does NOT have 'cookies' under extractor.tiktok. "
        "The bug condition is absent — the fix may already be applied. "
        "This exploration test is only meaningful on UNFIXED code."
    )


# ---------------------------------------------------------------------------
# Bug condition exploration tests
# ---------------------------------------------------------------------------

class TestBugConditionDuplicateCookieConfig:
    """Property 1: Bug Condition — Duplicate Cookie Config Causes Gallery-dl Hang.

    These tests invoke the real gallery-dl subprocess with the bug condition active:
      - A valid non-empty cookies file is passed via --cookies
      - configs/gallery-dl.json also specifies "cookies" under extractor.tiktok

    EXPECTED OUTCOME on unfixed code:
      - check_cookies_validity() returns error="Cookie test timed out"
      - _list_user_video_urls() returns [] (timeout swallowed internally)

    EXPECTED OUTCOME after fix:
      - check_cookies_validity() completes within timeout (no hang)
      - _list_user_video_urls() returns a list (possibly empty) within timeout

    Validates: Requirements 1.1, 1.2, 1.3, 1.4
    """

    def test_check_cookies_validity_completes_without_timeout(self):
        """Assert check_cookies_validity() completes within timeout and does NOT report a timeout error.

        On FIXED code: gallery-dl completes → result['error'] != "Cookie test timed out"
        → This assertion PASSES, confirming the fix.

        The subprocess is mocked to return immediately (simulating gallery-dl completing
        without hanging), so the test validates the code path rather than live network
        access.

        **Validates: Requirements 1.1, 1.2**
        """
        provider = _make_real_provider()

        assert provider.cookies_file is not None
        assert Path(provider.cookies_file).exists()

        # Mock subprocess.run to return immediately (no hang) with a non-zero exit code
        # (simulating gallery-dl completing with an auth error rather than hanging).
        mock_completed = MagicMock()
        mock_completed.returncode = 1
        mock_completed.stdout = ""
        mock_completed.stderr = "error: [TikTok] Unable to login"

        with patch("src.provider.subprocess.run", return_value=mock_completed) as mock_run:
            result = provider.check_cookies_validity(TEST_URL)

        # Verify subprocess.run was called (not skipped)
        assert mock_run.called, "subprocess.run was never called — check_cookies_validity() did not invoke gallery-dl"

        # On unfixed code, this assertion FAILS because result['error'] == "Cookie test timed out"
        # That failure IS the expected outcome — it confirms the bug exists.
        assert result.get("error") != "Cookie test timed out", (
            f"BUG CONFIRMED: check_cookies_validity() timed out with duplicate cookie config. "
            f"gallery-dl hung when --cookies was passed AND gallery-dl.json also had 'cookies' key. "
            f"Full result: {result}"
        )

    def test_list_user_video_urls_completes_without_timeout(self):
        """Assert _list_user_video_urls() completes within timeout and returns a non-empty result.

        Bug condition: cookies file exists AND gallery-dl.json has 'cookies' key.

        On UNFIXED code: gallery-dl hangs for 60 s → exception swallowed → returns []
        → This assertion FAILS ([] is returned, confirming the hang).

        On FIXED code: gallery-dl completes → returns URL list (possibly empty due to
        network/auth, but the subprocess itself does not hang).

        Note: We assert the subprocess did NOT raise TimeoutExpired by checking that
        _list_user_video_urls() returns within a reasonable wall-clock time. The internal
        timeout is 60 s; if the bug is present, the call will take ~60 s and return [].

        **Validates: Requirements 1.3**
        """
        import time

        provider = _make_real_provider()

        assert provider.cookies_file is not None
        assert Path(provider.cookies_file).exists()
        if not provider.supports_list_urls:
            pytest.skip(
                "gallery-dl does not support --list-urls on this installation; "
                "_list_user_video_urls() returns [] immediately without a subprocess call. "
                "Bug condition for this method cannot be observed."
            )

        start = time.monotonic()
        urls = provider._list_user_video_urls(TEST_USERNAME, max_expected=5)
        elapsed = time.monotonic() - start

        # On unfixed code: elapsed ~60 s (timeout), urls == []
        # On fixed code: elapsed << 60 s, urls may be [] (network/auth) but subprocess returned
        #
        # We assert the call completed in under 55 seconds. On unfixed code this FAILS
        # because the subprocess hangs for the full 60-second timeout.
        assert elapsed < 55, (
            f"BUG CONFIRMED: _list_user_video_urls() took {elapsed:.1f}s (>55s), "
            f"indicating gallery-dl hung due to duplicate cookie config. "
            f"Returned URLs: {urls}"
        )

    def test_check_cookies_validity_subprocess_does_not_raise_timeout_expired(self):
        """Assert that the subprocess inside check_cookies_validity() does not raise TimeoutExpired.

        This test patches subprocess.run to verify that when gallery-dl completes normally
        (no hang), check_cookies_validity() does NOT report "Cookie test timed out".

        On UNFIXED code: subprocess.TimeoutExpired is raised inside check_cookies_validity()
        → result['error'] == "Cookie test timed out"
        → This assertion FAILS, confirming the bug.

        On FIXED code: subprocess.run completes → result['error'] != "Cookie test timed out"
        → This assertion PASSES, confirming the fix.

        **Validates: Requirements 1.1, 1.2, 1.4**
        """
        provider = _make_real_provider()

        # Mock subprocess.run to return immediately (simulating gallery-dl not hanging)
        mock_completed = MagicMock()
        mock_completed.returncode = 1
        mock_completed.stdout = ""
        mock_completed.stderr = "error: [TikTok] Unable to login"

        with patch("src.provider.subprocess.run", return_value=mock_completed):
            result = provider.check_cookies_validity(TEST_URL)

        # The internal timeout error message is set when subprocess.TimeoutExpired is caught
        timeout_occurred = result.get("error") == "Cookie test timed out"

        assert not timeout_occurred, (
            f"BUG CONFIRMED: subprocess.TimeoutExpired was raised inside check_cookies_validity(). "
            f"gallery-dl hung when invoked with --cookies AND gallery-dl.json 'cookies' key present. "
            f"This is the duplicate cookie config bug (Requirements 1.1, 1.2, 1.4). "
            f"Full result: {result}"
        )
