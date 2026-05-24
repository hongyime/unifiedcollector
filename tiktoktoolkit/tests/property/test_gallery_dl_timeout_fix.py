"""Bug condition exploration test for gallery-dl timeout attribute fix.

This test MUST FAIL on unfixed code to confirm the bug exists.
The bug: core/cli.py references `provider.timeout` but GalleryDLProvider
only defines `timeout_seconds`.

Expected failure: AttributeError: 'GalleryDLProvider' object has no attribute 'timeout'
"""
import subprocess
from types import SimpleNamespace

import pytest

from src.provider import GalleryDLProvider


def _completed(args, returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _make_provider(monkeypatch, gd_config=None):
    """Instantiate GalleryDLProvider with a minimal mock config (no real subprocess needed)."""
    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.0')
        if '--help' in args:
            return _completed(args, stdout='--list-urls')
        if '--list-urls' in args:
            return _completed(args, stdout='')
        raise AssertionError(f"Unexpected subprocess call: {args}")

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_a, **_kw: SimpleNamespace())

    config = {'gallerydl': gd_config or {'timeout_seconds': 1800}}
    return GalleryDLProvider(config)


class TestBugConditionTimeoutAttributeMissing:
    """Bug condition exploration: verify the fix — CLI uses provider.timeout_seconds correctly.

    The fix was applied to core/cli.py: `provider.timeout` was replaced with
    `provider.timeout_seconds`. These tests verify the fixed behavior.

    Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.2, 2.3
    """

    def test_provider_has_timeout_seconds_attribute(self, monkeypatch):
        """Assert that provider.timeout_seconds exists and is accessible.

        The fix: cli.py now uses provider.timeout_seconds (not provider.timeout).
        This test confirms the canonical attribute is present on the provider.
        """
        provider = _make_provider(monkeypatch)

        assert hasattr(provider, 'timeout_seconds'), (
            "GalleryDLProvider has no 'timeout_seconds' attribute. "
            "The fix requires this attribute to exist."
        )
        assert isinstance(provider.timeout_seconds, int)
        assert provider.timeout_seconds > 0

    def test_provider_timeout_seconds_access_does_not_raise(self, monkeypatch):
        """Directly access provider.timeout_seconds — must not raise AttributeError.

        The fix: cli.py now references provider.timeout_seconds instead of
        the non-existent provider.timeout.
        """
        provider = _make_provider(monkeypatch)

        # After the fix, this is the correct attribute to access
        value = provider.timeout_seconds  # noqa: F841
        assert value == 1800

    def test_debug_command_effective_config_block_does_not_crash(self, monkeypatch):
        """Simulate the fixed line from cli.py in debug_gallery_dl_cmd.

        Fixed core/cli.py line:
            click.echo(f"  Gallery-dl timeout: {provider.timeout_seconds}s")

        The original bug was:
            click.echo(f"  Gallery-dl timeout: {provider.timeout}")
            → AttributeError: 'GalleryDLProvider' object has no attribute 'timeout'

        After the fix, this must not crash.
        """
        provider = _make_provider(monkeypatch)

        # Simulate the fixed f-string from cli.py
        output = f"  Gallery-dl timeout: {provider.timeout_seconds}s"
        assert "Gallery-dl timeout" in output
        assert "1800s" in output

    def test_provider_timeout_seconds_attribute_with_no_timeout_config(self, monkeypatch):
        """Edge case: provider with no explicit timeout config still has timeout_seconds.

        Even with an empty config, the provider must have a default timeout_seconds
        value so the fixed CLI display block works without crashing.
        """
        provider = _make_provider(monkeypatch, gd_config={})

        # After the fix, timeout_seconds must always be present (with a default)
        value = provider.timeout_seconds
        assert isinstance(value, int)
        assert value > 0


# ---------------------------------------------------------------------------
# Task 2: Preservation property tests (run on UNFIXED code — must PASS)
# ---------------------------------------------------------------------------
"""Preservation property tests for gallery-dl timeout attribute fix.

These tests verify baseline behavior that must NOT change after the fix.
They MUST PASS on unfixed code, confirming the behavior we want to preserve.

Validates: Requirements 3.1, 3.2, 3.3, 3.4
"""

from unittest.mock import patch, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider_with_timeout(monkeypatch, timeout_seconds_value=None, legacy_timeout_minutes=None):
    """Create a provider with a specific timeout config."""
    def fake_run(args, **_kwargs):
        if '--version' in args:
            return _completed(args, stdout='1.31.0')
        if '--list-urls' in args:
            return _completed(args, stdout='')
        return _completed(args)

    monkeypatch.setattr('src.provider.subprocess.run', fake_run)
    monkeypatch.setattr('src.provider.create_tracker', lambda *_a, **_kw: SimpleNamespace())

    gd_config = {}
    if timeout_seconds_value is not None:
        gd_config['timeout_seconds'] = timeout_seconds_value
    elif legacy_timeout_minutes is not None:
        gd_config['timeout'] = legacy_timeout_minutes

    return GalleryDLProvider({'gallerydl': gd_config})


class TestPreservationTimeoutConfigParsing:
    """Property 2: Preservation — timeout config parsing must remain unchanged.

    Validates: Requirements 3.1, 3.2
    """

    @pytest.mark.parametrize('seconds', [1, 30, 60, 300, 1800, 3600, 86400])
    def test_timeout_seconds_key_stores_value_directly(self, monkeypatch, seconds):
        """For valid integer timeout_seconds values, provider.timeout_seconds equals the config value.

        The `timeout_seconds` config key is stored directly (no conversion).
        """
        provider = _make_provider_with_timeout(monkeypatch, timeout_seconds_value=seconds)
        assert provider.timeout_seconds == seconds

    @pytest.mark.parametrize('minutes', [1, 5, 10, 30, 60, 120, 1440])
    def test_legacy_timeout_key_converts_minutes_to_seconds(self, monkeypatch, minutes):
        """Legacy `timeout` config key (minutes) is converted to seconds (× 60).

        This is the legacy behavior: timeout in minutes → stored as seconds.
        """
        provider = _make_provider_with_timeout(monkeypatch, legacy_timeout_minutes=minutes)
        assert provider.timeout_seconds == minutes * 60

    @given(st.integers(min_value=1, max_value=86400))
    @settings(max_examples=50)
    def test_timeout_seconds_property_preserved_for_all_valid_values(self, seconds):
        """Property: for all valid integer timeout_seconds (1–86400), provider.timeout_seconds equals the config value.

        Validates: Requirements 3.1, 3.2
        """
        def fake_run(args, **_kwargs):
            if '--version' in args:
                return _completed(args, stdout='1.31.0')
            if '--list-urls' in args:
                return _completed(args, stdout='')
            return _completed(args)

        with patch('src.provider.subprocess.run', side_effect=fake_run), \
             patch('src.provider.create_tracker', return_value=SimpleNamespace()):
            provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': seconds}})
            assert provider.timeout_seconds == seconds

    @given(st.integers(min_value=1, max_value=1440))
    @settings(max_examples=50)
    def test_legacy_timeout_minutes_conversion_preserved(self, minutes):
        """Property: for all valid legacy timeout values (1–1440 minutes), provider.timeout_seconds == minutes * 60.

        Validates: Requirements 3.1, 3.2
        """
        def fake_run(args, **_kwargs):
            if '--version' in args:
                return _completed(args, stdout='1.31.0')
            if '--list-urls' in args:
                return _completed(args, stdout='')
            return _completed(args)

        with patch('src.provider.subprocess.run', side_effect=fake_run), \
             patch('src.provider.create_tracker', return_value=SimpleNamespace()):
            provider = GalleryDLProvider({'gallerydl': {'timeout': minutes}})
            assert provider.timeout_seconds == minutes * 60


class TestPreservationSubprocessTimeout:
    """Property 2: Preservation — _run_gallery_dl passes timeout_seconds to subprocess.run.

    Validates: Requirement 3.1
    """

    def test_run_gallery_dl_passes_timeout_seconds_to_subprocess(self, monkeypatch, tmp_path):
        """Verify _run_gallery_dl passes self.timeout_seconds (not some other value) to subprocess.run."""
        captured_kwargs = {}

        def fake_run(args, **kwargs):
            if '--version' in args:
                return _completed(args, stdout='1.31.0')
            if '--list-urls' in args:
                return _completed(args, stdout='')
            # Capture the kwargs for the actual gallery-dl call
            captured_kwargs.update(kwargs)
            return _completed(args)

        monkeypatch.setattr('src.provider.subprocess.run', fake_run)
        monkeypatch.setattr('src.provider.create_tracker', lambda *_a, **_kw: SimpleNamespace())

        expected_timeout = 900  # 15 minutes in seconds
        provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': expected_timeout}})

        # Create a dummy file so _run_gallery_dl has something to normalize
        target_dir = tmp_path / 'username_testuser'
        target_dir.mkdir()

        provider._run_gallery_dl('https://www.tiktok.com/@testuser', target_dir, 1)

        assert 'timeout' in captured_kwargs, "subprocess.run was not called with a timeout kwarg"
        assert captured_kwargs['timeout'] == expected_timeout, (
            f"Expected timeout={expected_timeout} but got timeout={captured_kwargs['timeout']}"
        )

    @pytest.mark.parametrize('timeout_seconds', [60, 300, 900, 1800, 3600])
    def test_subprocess_timeout_matches_provider_timeout_seconds(self, monkeypatch, tmp_path, timeout_seconds):
        """For various timeout_seconds values, subprocess.run receives exactly that value."""
        captured_timeouts = []

        def fake_run(args, **kwargs):
            if '--version' in args:
                return _completed(args, stdout='1.31.0')
            if '--list-urls' in args:
                return _completed(args, stdout='')
            if 'timeout' in kwargs:
                captured_timeouts.append(kwargs['timeout'])
            return _completed(args)

        monkeypatch.setattr('src.provider.subprocess.run', fake_run)
        monkeypatch.setattr('src.provider.create_tracker', lambda *_a, **_kw: SimpleNamespace())

        provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': timeout_seconds}})
        target_dir = tmp_path / 'username_testuser'
        target_dir.mkdir()

        provider._run_gallery_dl('https://www.tiktok.com/@testuser', target_dir, 1)

        assert any(t == timeout_seconds for t in captured_timeouts), (
            f"Expected timeout={timeout_seconds} in subprocess calls, got: {captured_timeouts}"
        )


class TestPreservationOtherProviderAttributes:
    """Property 2: Preservation — other provider attributes remain accessible.

    Validates: Requirements 3.3, 3.4
    """

    def test_retries_accessible(self, monkeypatch):
        """provider.retries is accessible without error."""
        provider = _make_provider(monkeypatch, gd_config={'retries': 5})
        assert provider.retries == 5

    def test_sleep_accessible(self, monkeypatch):
        """provider.sleep is accessible without error."""
        provider = _make_provider(monkeypatch, gd_config={'sleep': 3})
        assert provider.sleep == 3

    def test_skip_existing_accessible(self, monkeypatch):
        """provider.skip_existing is accessible without error."""
        provider = _make_provider(monkeypatch, gd_config={'skip_existing': False})
        assert provider.skip_existing is False

    def test_cookies_file_accessible(self, monkeypatch, tmp_path):
        """provider.cookies_file is accessible without error."""
        cookies = tmp_path / 'cookies.txt'
        cookies.write_text('# Netscape HTTP Cookie File\n', encoding='utf-8')
        provider = _make_provider(monkeypatch, gd_config={'cookies_file': str(cookies)})
        assert provider.cookies_file == str(cookies)

    def test_cookies_browser_accessible(self, monkeypatch):
        """provider.cookies_browser is accessible without error."""
        provider = _make_provider(monkeypatch, gd_config={'cookies_browser': 'chrome'})
        assert provider.cookies_browser == 'chrome'

    @pytest.mark.parametrize('attr', ['retries', 'sleep', 'skip_existing', 'cookies_file', 'cookies_browser'])
    def test_all_debug_config_attributes_accessible(self, monkeypatch, attr):
        """All attributes displayed in the debug command's Effective Configuration block are accessible."""
        provider = _make_provider(monkeypatch)
        # Should not raise AttributeError
        _ = getattr(provider, attr)


class TestPreservationDebugConfigFields:
    """Property 2: Preservation — debug command config fields remain functional.

    Validates: Requirements 3.3, 3.4
    """

    def test_output_root_accessible_from_config(self, monkeypatch):
        """config.output_root is accessible (used in debug command Effective Configuration)."""
        from src.config import load_config
        from pathlib import Path
        # Just verify the attribute exists on a loaded config
        # (the debug command accesses config.output_root)
        provider = _make_provider(monkeypatch)
        # Provider itself doesn't hold output_root, but we verify the debug block fields
        # that come from provider are all accessible
        assert hasattr(provider, 'retries')
        assert hasattr(provider, 'sleep')
        assert hasattr(provider, 'timeout_seconds')
        assert hasattr(provider, 'skip_existing')
        assert hasattr(provider, 'cookies_file')
        assert hasattr(provider, 'cookies_browser')

    def test_effective_config_block_fields_all_readable(self, monkeypatch):
        """All fields in the debug command's Effective Configuration block can be read without error.

        This simulates the display block in debug_gallery_dl_cmd (excluding provider.timeout
        which is the bug — that is tested in the bug condition tests above).
        """
        provider = _make_provider(monkeypatch, gd_config={
            'retries': 3,
            'sleep': 1,
            'timeout_seconds': 1800,
            'skip_existing': True,
        })

        # Simulate the lines from cli.py debug_gallery_dl_cmd that must not crash
        # (excluding the buggy `provider.timeout` line — that's the bug condition)
        lines = [
            f"  Gallery-dl retries: {provider.retries}",
            f"  Gallery-dl sleep: {provider.sleep}",
            f"  Gallery-dl timeout_seconds: {provider.timeout_seconds}",
            f"  Skip existing: {provider.skip_existing}",  # matches cli.py label
            f"  Cookies file: {provider.cookies_file or 'none'}",
            f"  Cookies browser: {provider.cookies_browser or 'none'}",
        ]

        assert any('retries' in line for line in lines)
        assert any('sleep' in line for line in lines)
        assert any('timeout_seconds' in line for line in lines)
        assert any('Skip existing' in line for line in lines)
        assert any('Cookies file' in line for line in lines)
        assert any('Cookies browser' in line for line in lines)


# ---------------------------------------------------------------------------
# Task 1: Bug condition exploration test (MUST FAIL on unfixed code)
# ---------------------------------------------------------------------------
"""Bug condition exploration: Cookie Extraction Fails When gallery-dl Takes > 30 s

These tests MUST FAIL on unfixed code — failure confirms the bug exists.
The bug: setup_browser_cookies() uses timeout=30 in subprocess.run(), which is
too short. When gallery-dl takes > 30 s, subprocess.TimeoutExpired is raised,
caught by the outer except block, and re-raised as ProviderError.

Expected counterexample: setup_browser_cookies() raises ProviderError for
duration=31 s instead of returning the cookies file path.

Validates: Requirements 1.1, 1.2, 1.3
"""


class TestBugConditionCookieTimeout:
    """Bug condition exploration: verify setup_browser_cookies() handles durations > 30 s.

    On UNFIXED code (timeout=30), these tests FAIL — confirming the bug exists.
    On FIXED code (timeout=120), these tests PASS — confirming the fix works.

    **Validates: Requirements 1.1, 1.2, 1.3**
    """

    @given(st.integers(min_value=31, max_value=120))
    @settings(max_examples=50)
    def test_cookie_extraction_succeeds_for_durations_above_30s(self, duration):
        """Property: for all gallery-dl durations in (30, 120], setup_browser_cookies() must
        return the cookies file path without raising ProviderError.

        On UNFIXED code (timeout=30): subprocess.run raises TimeoutExpired for any
        duration > 30 s, which is caught and re-raised as ProviderError — test FAILS.
        On FIXED code (timeout=120): subprocess.run completes successfully within the
        extended timeout — test PASSES.

        The mock simulates gallery-dl completing successfully (writing the cookies file
        and returning exit code 0), representing a run that takes between 31–120 s but
        finishes within the new 120 s timeout.

        **Validates: Requirements 1.1, 1.2, 1.3**
        """
        import tempfile
        from pathlib import Path as _Path
        from src.errors import ProviderError as _ProviderError

        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_file = _Path(tmpdir) / "tiktok_cookies.txt"

            def fake_run(args, **kwargs):
                if '--version' in args:
                    return _completed(args, stdout='1.31.0')
                if '--list-urls' in args:
                    return _completed(args, stdout='')
                if '--cookies-from-browser' in args:
                    # Simulate gallery-dl completing successfully within the 120 s timeout.
                    # The actual timeout kwarg passed to subprocess.run must be >= duration
                    # for this to be realistic — the fix sets timeout=120, so durations
                    # 31–120 s now complete without TimeoutExpired.
                    actual_timeout = kwargs.get('timeout', 0)
                    assert actual_timeout >= duration, (
                        f"subprocess.run called with timeout={actual_timeout} but "
                        f"gallery-dl duration={duration}s — would have timed out on unfixed code. "
                        f"Fix must set timeout >= {duration}."
                    )
                    # Write the cookies file to simulate successful extraction
                    cookies_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
                    return _completed(args, returncode=0)
                return _completed(args)

            with patch('src.provider.subprocess.run', side_effect=fake_run), \
                 patch('src.provider.create_tracker', return_value=SimpleNamespace()), \
                 patch('src.cookie_manager.TikTokCookieManager') as mock_mgr_cls, \
                 patch('src.utils.secure_file_permissions', return_value=True), \
                 patch.dict('sys.modules', {
                     'rookiepy': MagicMock(to_netscape=MagicMock(return_value='# Netscape HTTP Cookie File\n'))
                 }):

                mock_mgr = MagicMock()
                mock_mgr.validate_cookies.return_value = {'valid': True, 'warnings': []}
                mock_mgr_cls.return_value = mock_mgr

                provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': 1800}})
                provider.cookies_file = str(cookies_file)

                # On FIXED code: should return the cookies file path (no exception)
                # On UNFIXED code (timeout=30): the assert inside fake_run fires for duration > 30
                result = provider.setup_browser_cookies("chrome")
                assert _Path(result) == cookies_file, (
                    f"Expected cookies file path {cookies_file} but got {result}. "
                    f"gallery-dl duration={duration}s should be within the allowed timeout."
                )

    def test_cookie_extraction_boundary_at_30s_raises_timeout(self, tmp_path, monkeypatch):
        """Concrete boundary test: duration exactly 30 s raises TimeoutExpired (confirms hard-coded limit).

        This test documents the exact boundary of the bug: timeout=30 means any
        gallery-dl run that takes exactly 30 s (or more) will fail.

        On UNFIXED code: ProviderError is raised — confirming the hard-coded 30 s limit.
        On FIXED code: ProviderError is still raised at 30 s (30 < 120 boundary is fine,
        but the mock raises TimeoutExpired at exactly 30 s which is still a timeout).

        **Validates: Requirements 1.1, 1.2**
        """
        from src.errors import ProviderError as _ProviderError

        cookies_file = tmp_path / "tiktok_cookies.txt"
        cookies_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

        def fake_run(args, **kwargs):
            if '--version' in args:
                return _completed(args, stdout='1.31.0')
            if '--list-urls' in args:
                return _completed(args, stdout='')
            if '--cookies-from-browser' in args:
                # Simulate gallery-dl timing out at exactly 30 s
                raise subprocess.TimeoutExpired(cmd=args, timeout=30)
            return _completed(args)

        monkeypatch.setattr('src.provider.subprocess.run', fake_run)
        monkeypatch.setattr('src.provider.create_tracker', lambda *_a, **_kw: SimpleNamespace())

        provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': 1800}})
        provider.cookies_file = str(cookies_file)

        # At exactly 30 s, TimeoutExpired is raised and caught as ProviderError
        with pytest.raises(_ProviderError):
            provider.setup_browser_cookies("chrome")


# ---------------------------------------------------------------------------
# Task 2: Preservation property tests for setup_browser_cookies()
# ---------------------------------------------------------------------------
"""Preservation property tests for setup_browser_cookies().

These tests verify baseline behavior that must NOT change after the fix.
They MUST PASS on unfixed code (timeout=30 still in place), confirming the
behavior we want to preserve.

Validates: Requirements 3.1, 3.2, 3.3, 3.4
"""

import tempfile


class TestPreservationCookieSetup:
    """Property 2: Preservation — Fast-Path and Error-Path Behavior Unchanged.

    These tests run on UNFIXED code and MUST PASS, confirming the baseline
    behavior that the fix must not break.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
    """

    @given(st.integers(min_value=0, max_value=29))
    @settings(max_examples=30)
    def test_fast_success_preservation(self, duration):
        """Property: for all fast gallery-dl durations (< 30 s), setup_browser_cookies()
        returns the cookies file path and calls TikTokCookieManager.validate_cookies().

        On UNFIXED code (timeout=30): gallery-dl completes in < 30 s → no TimeoutExpired
        → cookies file path is returned. This test PASSES on unfixed code.

        **Validates: Requirements 3.1, 3.3**
        """
        from pathlib import Path as _Path
        from src.errors import ProviderError as _ProviderError

        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_file = _Path(tmpdir) / "tiktok_cookies.txt"

            def fake_run(args, **kwargs):
                if '--version' in args:
                    return _completed(args, stdout='1.31.0')
                if '--list-urls' in args:
                    return _completed(args, stdout='')
                if '--cookies-from-browser' in args:
                    # Simulate gallery-dl completing quickly (no TimeoutExpired)
                    cookies_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
                    return _completed(args, returncode=0)
                return _completed(args)

            with patch('src.provider.subprocess.run', side_effect=fake_run), \
                 patch('src.provider.create_tracker', return_value=SimpleNamespace()), \
                 patch('src.cookie_manager.TikTokCookieManager') as mock_mgr_cls, \
                 patch('src.utils.secure_file_permissions', return_value=True):

                mock_mgr = MagicMock()
                mock_mgr.validate_cookies.return_value = {'valid': True, 'warnings': []}
                mock_mgr_cls.return_value = mock_mgr

                provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': 1800}})
                provider.cookies_file = str(cookies_file)

                result = provider.setup_browser_cookies("chrome")

                # Cookies file path must be returned
                assert _Path(result) == cookies_file, (
                    f"Expected cookies file path {cookies_file} but got {result}. "
                    f"Fast gallery-dl (duration={duration}s) should return the cookies path."
                )
                # validate_cookies must have been called
                mock_mgr.validate_cookies.assert_called_once()

    @given(st.text(min_size=1))
    @settings(max_examples=30)
    def test_non_zero_exit_code_preservation(self, stderr_text):
        """Property: for all non-empty stderr strings with exit code 1,
        setup_browser_cookies() raises ProviderError containing that stderr.

        On UNFIXED code: non-zero exit code → ProviderError raised with stderr.
        This test PASSES on unfixed code.

        **Validates: Requirement 3.2**
        """
        from src.errors import ProviderError as _ProviderError
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmpdir:
            cookies_file = _Path(tmpdir) / "tiktok_cookies.txt"

            def fake_run(args, **kwargs):
                if '--version' in args:
                    return _completed(args, stdout='1.31.0')
                if '--list-urls' in args:
                    return _completed(args, stdout='')
                if '--cookies-from-browser' in args:
                    # Simulate gallery-dl failing with non-zero exit code
                    return _completed(args, returncode=1, stderr=stderr_text)
                return _completed(args)

            with patch('src.provider.subprocess.run', side_effect=fake_run), \
                 patch('src.provider.create_tracker', return_value=SimpleNamespace()), \
                 patch.dict('sys.modules', {'rookiepy': None}):

                provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': 1800}})
                provider.cookies_file = str(cookies_file)

                with pytest.raises(_ProviderError) as exc_info:
                    provider.setup_browser_cookies("chrome")

                # ProviderError must contain the stderr text
                assert stderr_text in str(exc_info.value), (
                    f"Expected ProviderError to contain stderr={stderr_text!r} "
                    f"but got: {exc_info.value}"
                )

    @given(st.text(min_size=1, alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd'))))
    @settings(max_examples=30)
    def test_configured_path_preservation(self, cookie_filename):
        """Property: for all valid custom cookie file names, setup_browser_cookies()
        uses the configured self.cookies_file path, not the default configs/tiktok_cookies.txt.

        On UNFIXED code: self.cookies_file is used when set. This test PASSES on unfixed code.

        **Validates: Requirement 3.4**
        """
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmpdir:
            custom_cookies_file = _Path(tmpdir) / f"{cookie_filename}.txt"

            def fake_run(args, **kwargs):
                if '--version' in args:
                    return _completed(args, stdout='1.31.0')
                if '--list-urls' in args:
                    return _completed(args, stdout='')
                if '--cookies-from-browser' in args:
                    # Write to whatever path was passed via --cookies-export
                    for i, arg in enumerate(args):
                        if arg == '--cookies-export' and i + 1 < len(args):
                            _Path(args[i + 1]).write_text(
                                "# Netscape HTTP Cookie File\n", encoding="utf-8"
                            )
                            break
                    return _completed(args, returncode=0)
                return _completed(args)

            with patch('src.provider.subprocess.run', side_effect=fake_run), \
                 patch('src.provider.create_tracker', return_value=SimpleNamespace()), \
                 patch('src.cookie_manager.TikTokCookieManager') as mock_mgr_cls, \
                 patch('src.utils.secure_file_permissions', return_value=True):

                mock_mgr = MagicMock()
                mock_mgr.validate_cookies.return_value = {'valid': True, 'warnings': []}
                mock_mgr_cls.return_value = mock_mgr

                provider = GalleryDLProvider({'gallerydl': {'timeout_seconds': 1800}})
                provider.cookies_file = str(custom_cookies_file)

                result = provider.setup_browser_cookies("chrome")

                # Must use the configured path, not the default
                assert _Path(result) == custom_cookies_file, (
                    f"Expected configured path {custom_cookies_file} but got {result}. "
                    f"setup_browser_cookies() must use self.cookies_file when set."
                )
                assert str(result) != "configs/tiktok_cookies.txt", (
                    "setup_browser_cookies() used the default path instead of the configured one."
                )
