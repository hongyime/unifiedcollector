"""Preservation property tests for TikTok cookie auth bugfix.

Property 2: Preservation — No-Cookie and Non-TikTok Invocations Unchanged

These tests MUST PASS on UNFIXED code — they encode the baseline behavior that
the fix must preserve. They use mocked subprocess.run so they do NOT invoke
gallery-dl and run quickly.

Observations encoded:
  - _build_gallery_dl_args(url, dir, use_cookies=False) does NOT include --cookies
  - _build_gallery_dl_args(url, dir, use_cookies=True) with cookies_file=None does NOT include --cookies
  - provider.timeout_seconds is passed as the `timeout` kwarg to subprocess.run in _run_gallery_dl
  - all other args (retries, sleep, user-agent, range, dest, config) are present and unchanged

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.provider import GalleryDLProvider


# ---------------------------------------------------------------------------
# Helpers / Factories
# ---------------------------------------------------------------------------

GALLERY_DL_JSON = Path("configs/gallery-dl.json")
TEST_URL = "https://www.tiktok.com/@tiktok"
TEST_DIR = Path("/tmp/test_downloads")


def _make_provider(
    timeout_seconds: int = 30,
    retries: int = 3,
    sleep: int = 1,
    cookies_file: Optional[str] = None,
    cookies_browser: Optional[str] = None,
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
) -> GalleryDLProvider:
    """Create a GalleryDLProvider with mocked gallery-dl installation check."""
    config = {
        "gallerydl": {
            "timeout_seconds": timeout_seconds,
            "retries": retries,
            "sleep": sleep,
            "cookies_file": cookies_file,
            "cookies_browser": cookies_browser,
            "user_agent": user_agent,
            "tracker_required": False,
        }
    }
    with patch("subprocess.run") as mock_run:
        # Mock gallery-dl --version
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="1.26.0",
            stderr="",
        )
        # Also mock the --list-urls support check (second call)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="1.26.0", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        provider = GalleryDLProvider(config)
    return provider


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

valid_timeout_seconds = st.integers(min_value=1, max_value=86400)

valid_retries = st.integers(min_value=0, max_value=10)

valid_sleep = st.integers(min_value=0, max_value=30)

valid_user_agent = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Zs")),
    min_size=1,
    max_size=200,
).filter(lambda s: s.strip())

valid_url = st.one_of(
    st.just("https://www.tiktok.com/@tiktok"),
    st.just("https://www.tiktok.com/@someuser"),
    st.just("https://www.instagram.com/someuser"),
    st.just("https://twitter.com/someuser"),
)

valid_browser = st.one_of(
    st.just("chrome"),
    st.just("firefox"),
    st.just("safari"),
    st.just("edge"),
)


# ---------------------------------------------------------------------------
# Property 2a: timeout_seconds is passed to subprocess.run as `timeout`
# ---------------------------------------------------------------------------

class TestTimeoutPreservation:
    """For all valid timeout_seconds values, subprocess.run receives exactly that value."""

    @given(timeout_seconds=valid_timeout_seconds)
    @settings(max_examples=50)
    def test_timeout_passed_to_subprocess_run(self, timeout_seconds: int):
        """**Validates: Requirements 3.1, 3.2**

        For all valid timeout_seconds values (1–86400), _run_gallery_dl passes
        exactly that value as the `timeout` kwarg to subprocess.run.
        """
        provider = _make_provider(timeout_seconds=timeout_seconds)
        assert provider.timeout_seconds == timeout_seconds

        captured_kwargs: List[Dict[str, Any]] = []

        def fake_run(args, **kwargs):
            captured_kwargs.append(kwargs)
            return MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

        with patch("subprocess.run", side_effect=fake_run):
            try:
                provider._run_gallery_dl(TEST_URL, TEST_DIR, limit=1)
            except Exception:
                pass  # We only care about the kwargs captured

        assert captured_kwargs, "subprocess.run was never called"
        # The first call is the actual gallery-dl invocation
        first_call_kwargs = captured_kwargs[0]
        assert "timeout" in first_call_kwargs, (
            f"subprocess.run was not called with a `timeout` kwarg. "
            f"Got kwargs: {first_call_kwargs}"
        )
        assert first_call_kwargs["timeout"] == timeout_seconds, (
            f"Expected timeout={timeout_seconds}, "
            f"got timeout={first_call_kwargs['timeout']}"
        )


# ---------------------------------------------------------------------------
# Property 2b: --cookies absent when cookies_file is None or file does not exist
# ---------------------------------------------------------------------------

class TestNoCookiesPreservation:
    """--cookies must be absent from arg list when cookies_file is None or file absent."""

    @given(url=valid_url)
    @settings(max_examples=30)
    def test_no_cookies_flag_when_cookies_file_is_none(self, url: str):
        """**Validates: Requirements 3.1**

        When cookies_file is None, _build_gallery_dl_args must NOT include --cookies.
        """
        provider = _make_provider(cookies_file=None)
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=True)
        assert "--cookies" not in args, (
            f"--cookies should not be in args when cookies_file is None. "
            f"Got args: {args}"
        )

    @given(url=valid_url)
    @settings(max_examples=30)
    def test_no_cookies_flag_when_use_cookies_false(self, url: str):
        """**Validates: Requirements 3.1**

        When use_cookies=False, _build_gallery_dl_args must NOT include --cookies
        regardless of whether cookies_file is set.
        """
        # Even if a cookies_file path is set, use_cookies=False must suppress it
        provider = _make_provider(cookies_file="configs/tiktok_cookies.txt")
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)
        assert "--cookies" not in args, (
            f"--cookies should not be in args when use_cookies=False. "
            f"Got args: {args}"
        )

    def test_no_cookies_flag_when_file_does_not_exist(self):
        """**Validates: Requirements 3.1**

        When cookies_file is set but the file does not exist on disk,
        _build_gallery_dl_args must NOT include --cookies.
        """
        provider = _make_provider(cookies_file="/nonexistent/path/cookies.txt")
        args = provider._build_gallery_dl_args(TEST_URL, TEST_DIR, use_cookies=True)
        assert "--cookies" not in args, (
            f"--cookies should not be in args when cookies file does not exist. "
            f"Got args: {args}"
        )


# ---------------------------------------------------------------------------
# Property 2c: --cookies-from-browser present when cookies_browser set and file absent
# ---------------------------------------------------------------------------

class TestBrowserCookiesPreservation:
    """--cookies-from-browser must be present when cookies_browser is set and file absent."""

    @given(browser=valid_browser, url=valid_url)
    @settings(max_examples=30)
    def test_cookies_from_browser_present_when_browser_set(self, browser: str, url: str):
        """**Validates: Requirements 3.4**

        When cookies_browser is set and cookies_file is absent (or file does not exist),
        _build_gallery_dl_args must include --cookies-from-browser <browser>.
        """
        provider = _make_provider(cookies_file=None, cookies_browser=browser)
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=True)
        assert "--cookies-from-browser" in args, (
            f"--cookies-from-browser should be in args when cookies_browser='{browser}'. "
            f"Got args: {args}"
        )
        idx = args.index("--cookies-from-browser")
        assert args[idx + 1] == browser, (
            f"Expected --cookies-from-browser {browser}, "
            f"got --cookies-from-browser {args[idx + 1]}"
        )

    @given(browser=valid_browser, url=valid_url)
    @settings(max_examples=30)
    def test_cookies_from_browser_absent_when_use_cookies_false(self, browser: str, url: str):
        """**Validates: Requirements 3.1**

        When use_cookies=False, --cookies-from-browser must NOT be included even if
        cookies_browser is set.
        """
        provider = _make_provider(cookies_file=None, cookies_browser=browser)
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)
        assert "--cookies-from-browser" not in args, (
            f"--cookies-from-browser should not be in args when use_cookies=False. "
            f"Got args: {args}"
        )


# ---------------------------------------------------------------------------
# Property 2d: All other argument fields preserved unchanged
# ---------------------------------------------------------------------------

class TestArgPreservation:
    """All other argument fields (retries, sleep, user-agent, dest, config) are preserved."""

    @given(
        retries=valid_retries,
        sleep=valid_sleep,
        url=valid_url,
    )
    @settings(max_examples=50)
    def test_retries_and_sleep_preserved(self, retries: int, sleep: int, url: str):
        """**Validates: Requirements 3.2**

        For all valid retries and sleep values, _build_gallery_dl_args includes
        --retries <retries> and --sleep <sleep> (when sleep > 0).
        """
        provider = _make_provider(retries=retries, sleep=sleep)
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)

        assert "--retries" in args, f"--retries missing from args: {args}"
        retries_idx = args.index("--retries")
        assert args[retries_idx + 1] == str(retries), (
            f"Expected --retries {retries}, got {args[retries_idx + 1]}"
        )

        if sleep > 0:
            assert "--sleep" in args, f"--sleep missing from args when sleep={sleep}: {args}"
            sleep_idx = args.index("--sleep")
            assert args[sleep_idx + 1] == str(sleep), (
                f"Expected --sleep {sleep}, got {args[sleep_idx + 1]}"
            )

    @given(url=valid_url)
    @settings(max_examples=30)
    def test_dest_is_absolute_path(self, url: str):
        """**Validates: Requirements 3.2**

        _build_gallery_dl_args always includes --dest with an absolute path.
        """
        provider = _make_provider()
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)

        assert "--dest" in args, f"--dest missing from args: {args}"
        dest_idx = args.index("--dest")
        dest_path = Path(args[dest_idx + 1])
        assert dest_path.is_absolute(), (
            f"--dest should be an absolute path, got: {dest_path}"
        )

    @given(url=valid_url)
    @settings(max_examples=30)
    def test_config_path_included_when_file_exists(self, url: str):
        """**Validates: Requirements 3.2**

        When configs/gallery-dl.json exists, --config is included in the arg list.
        """
        assume(GALLERY_DL_JSON.exists())
        provider = _make_provider()
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)

        assert "--config" in args, (
            f"--config should be in args when gallery-dl.json exists. Got: {args}"
        )
        config_idx = args.index("--config")
        config_path = Path(args[config_idx + 1])
        assert config_path.is_absolute(), (
            f"--config path should be absolute, got: {config_path}"
        )

    @given(url=valid_url)
    @settings(max_examples=30)
    def test_url_is_last_argument(self, url: str):
        """**Validates: Requirements 3.2**

        The URL is always the last argument in the gallery-dl arg list.
        """
        provider = _make_provider()
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)

        assert args[-1] == url, (
            f"URL should be the last argument. Got last arg: {args[-1]}, expected: {url}"
        )

    @given(url=valid_url)
    @settings(max_examples=30)
    def test_gallery_dl_is_first_argument(self, url: str):
        """**Validates: Requirements 3.2**

        'gallery-dl' is always the first element of the arg list.
        """
        provider = _make_provider()
        args = provider._build_gallery_dl_args(url, TEST_DIR, use_cookies=False)

        assert args[0] == "gallery-dl", (
            f"First arg should be 'gallery-dl', got: {args[0]}"
        )

    @given(
        limit=st.integers(min_value=1, max_value=1000),
        url=valid_url,
    )
    @settings(max_examples=30)
    def test_range_included_when_limit_set(self, limit: int, url: str):
        """**Validates: Requirements 3.2**

        When limit > 0, --range 1-<limit> is included in the arg list.
        """
        provider = _make_provider()
        args = provider._build_gallery_dl_args(url, TEST_DIR, limit=limit, use_cookies=False)

        assert "--range" in args, f"--range missing from args when limit={limit}: {args}"
        range_idx = args.index("--range")
        assert args[range_idx + 1] == f"1-{limit}", (
            f"Expected --range 1-{limit}, got {args[range_idx + 1]}"
        )
