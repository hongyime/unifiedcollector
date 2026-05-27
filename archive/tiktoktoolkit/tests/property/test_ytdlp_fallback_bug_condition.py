"""Bug condition exploration test for yt-dlp fallback.

Property 1: Bug Condition — Gallery-DL 403/Anti-Bot Triggers yt-dlp Fallback

This test MUST FAIL on unfixed code — failure confirms the bug exists.
DO NOT attempt to fix the test or the code when it fails.

The bug: when gallery-dl raises a ProviderError matching a 403/anti-bot pattern,
the pipeline falls back directly to Playwright (_download_with_browser_fallback)
without first attempting yt-dlp (_download_with_ytdlp_fallback).

Expected failure on unfixed code:
  - AttributeError: 'GalleryDLProvider' object has no attribute '_download_with_ytdlp_fallback'
  - OR: _download_with_browser_fallback is called without _download_with_ytdlp_fallback being called first

Validates: Requirements 1.1, 1.2
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.errors import ProviderError
from src.provider import GalleryDLProvider


# ---------------------------------------------------------------------------
# Trigger patterns that should route through yt-dlp before Playwright
# ---------------------------------------------------------------------------

TRIGGER_PATTERNS = [
    "403",
    "forbidden",
    "javascript challenge",
    "extraction error",
    "could not extract rehydration data",
]


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def _make_provider(monkeypatch_or_patch=None) -> GalleryDLProvider:
    """Create a GalleryDLProvider with all subprocess calls mocked out."""
    def fake_run(args, **_kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, 0, stdout="1.31.0", stderr="")
        # Suppress --list-urls probe
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("src.provider.subprocess.run", side_effect=fake_run):
        with patch("src.provider.create_tracker", return_value=SimpleNamespace(
            count_for_user=lambda _u: 0,
            is_downloaded_in_folder=lambda *_a: False,
            mark_downloaded=lambda *_a, **_kw: None,
        )):
            provider = GalleryDLProvider({"gallerydl": {
                "retries": 0,
                "sleep": 0,
                "tracker_required": False,
            }})
    return provider


# ---------------------------------------------------------------------------
# Property 1: Bug Condition
# ---------------------------------------------------------------------------

class TestYtdlpFallbackBugCondition:
    """Property 1: Bug Condition — Gallery-DL 403/Anti-Bot Triggers yt-dlp Fallback.

    For each of the five trigger patterns, when _run_gallery_dl raises a ProviderError
    containing that pattern, download_user MUST call _download_with_ytdlp_fallback
    BEFORE calling _download_with_browser_fallback.

    On UNFIXED code: _download_with_ytdlp_fallback does not exist → AttributeError,
    OR the method exists but is never called before _download_with_browser_fallback.

    On FIXED code: _download_with_ytdlp_fallback is called first for all five patterns.

    Validates: Requirements 1.1, 1.2
    """

    @given(trigger=st.sampled_from(TRIGGER_PATTERNS))
    @settings(max_examples=len(TRIGGER_PATTERNS), deadline=None)
    def test_ytdlp_fallback_called_before_playwright(self, trigger):
        """Assert _download_with_ytdlp_fallback is called BEFORE _download_with_browser_fallback.

        **Validates: Requirements 1.1, 1.2**

        On UNFIXED code this test FAILS because:
          - _download_with_ytdlp_fallback does not exist on GalleryDLProvider, OR
          - _download_with_browser_fallback is called without _download_with_ytdlp_fallback first

        On FIXED code this test PASSES because the pipeline is:
          gallery-dl → yt-dlp fallback → Playwright fallback
        """
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())

        provider = _make_provider()

        call_order = []

        # Mock _run_gallery_dl to raise ProviderError with the trigger pattern
        def fake_run_gallery_dl(*_args, **_kwargs):
            raise ProviderError(f"Download failed: {trigger}")

        # Mock _download_with_ytdlp_fallback to record call and return failed results
        def fake_ytdlp_fallback(username, limit, target_dir):
            call_order.append("ytdlp")
            # Return failed results so the pipeline continues to Playwright
            from src.models import DownloadResult
            return [DownloadResult(ok=False, url=f"https://www.tiktok.com/@{username}",
                                   status="failed", reason="yt-dlp unavailable (test)")]

        # Mock _download_with_browser_fallback to record call and return a result
        def fake_browser_fallback(username, limit, target_dir, download_type):
            call_order.append("playwright")
            from src.models import DownloadResult
            return [DownloadResult(ok=True, url=f"https://www.tiktok.com/@{username}",
                                   status="downloaded")]

        provider._run_gallery_dl = fake_run_gallery_dl
        provider._download_with_browser_fallback = fake_browser_fallback

        # On UNFIXED code: _download_with_ytdlp_fallback does not exist.
        # We assert it exists AND is called before Playwright.
        assert hasattr(provider, "_download_with_ytdlp_fallback"), (
            f"BUG CONFIRMED: GalleryDLProvider has no '_download_with_ytdlp_fallback' method. "
            f"Trigger pattern '{trigger}' routes directly to Playwright with no yt-dlp attempt. "
            f"The yt-dlp intermediate fallback step is missing."
        )

        provider._download_with_ytdlp_fallback = fake_ytdlp_fallback

        # Run download_user — this exercises the anti-bot fallback path
        provider.download_user("testuser", 5, tmp_path)

        # Assert yt-dlp was called before Playwright
        assert "ytdlp" in call_order, (
            f"BUG CONFIRMED: _download_with_ytdlp_fallback was never called for trigger '{trigger}'. "
            f"Call order: {call_order}. "
            f"The pipeline skipped yt-dlp and went straight to Playwright."
        )

        assert "playwright" in call_order or call_order == ["ytdlp"], (
            f"Unexpected call order for trigger '{trigger}': {call_order}"
        )

        ytdlp_idx = call_order.index("ytdlp")
        if "playwright" in call_order:
            playwright_idx = call_order.index("playwright")
            assert ytdlp_idx < playwright_idx, (
                f"BUG CONFIRMED: _download_with_browser_fallback (Playwright) was called at index "
                f"{playwright_idx} BEFORE _download_with_ytdlp_fallback at index {ytdlp_idx} "
                f"for trigger pattern '{trigger}'. "
                f"Full call order: {call_order}"
            )
