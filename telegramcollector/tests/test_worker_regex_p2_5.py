"""
Tests for P2.5 - worker.py regex pattern fix (F-014).

Validates: Requirements 2.14 (Fix Checking - F-014) and 3.11 (Preservation - F-014)
"""
import re
import pytest
import unittest.mock as mock


# ---------------------------------------------------------------------------
# Import the module-level compiled pattern directly
# ---------------------------------------------------------------------------

import worker as worker_module


class TestSessionPatternCompiles:
    """Fix Checking: regex pattern compiles successfully (Req 2.14)."""

    def test_session_pattern_is_not_none(self):
        """_SESSION_PATTERN must compile without error."""
        assert worker_module._SESSION_PATTERN is not None

    def test_session_pattern_is_compiled_regex(self):
        """_SESSION_PATTERN must be a compiled re.Pattern object."""
        assert isinstance(worker_module._SESSION_PATTERN, re.Pattern)

    def test_pattern_string_is_correct(self):
        """Pattern must match the expected regex string."""
        assert worker_module._SESSION_PATTERN.pattern == r'account_(\d+)_\d+\.session$'


class TestSessionPatternMatching:
    """Preservation Checking: valid session filenames still discovered (Req 3.11)."""

    def test_matches_valid_session_filename(self):
        pattern = worker_module._SESSION_PATTERN
        assert pattern.match('account_123_1700000000.session') is not None

    def test_captures_phone_number(self):
        pattern = worker_module._SESSION_PATTERN
        m = pattern.match('account_447911234567_1700000000.session')
        assert m is not None
        assert m.group(1) == '447911234567'

    def test_does_not_match_wrong_prefix(self):
        pattern = worker_module._SESSION_PATTERN
        assert pattern.match('user_123_1700000000.session') is None

    def test_does_not_match_missing_timestamp(self):
        pattern = worker_module._SESSION_PATTERN
        assert pattern.match('account_123.session') is None

    def test_does_not_match_non_session_extension(self):
        pattern = worker_module._SESSION_PATTERN
        assert pattern.match('account_123_1700000000.db') is None

    def test_does_not_match_empty_string(self):
        pattern = worker_module._SESSION_PATTERN
        assert pattern.match('') is None

    def test_matches_multiple_digit_timestamps(self):
        pattern = worker_module._SESSION_PATTERN
        assert pattern.match('account_1_9999999999.session') is not None


class TestNonePatternGracefulHandling:
    """Fix Checking: graceful failure when pattern is None (Req 2.14)."""

    @pytest.mark.asyncio
    async def test_auto_discover_returns_early_when_pattern_none(self, tmp_path, monkeypatch):
        """When _SESSION_PATTERN is None, _auto_discover_sessions returns without crashing."""
        # Patch the module-level pattern to None
        monkeypatch.setattr(worker_module, '_SESSION_PATTERN', None)

        # Create a minimal sessions dir with a session file
        sessions_dir = tmp_path / 'sessions'
        sessions_dir.mkdir()
        (sessions_dir / 'account_123_1700000000.session').write_text('')

        # Patch settings.SESSIONS_DIR
        monkeypatch.setattr(worker_module.settings, 'SESSIONS_DIR', str(sessions_dir))

        # Build a minimal MainWorker without full init
        worker = object.__new__(worker_module.MainWorker)

        # Should not raise; returns early due to None pattern
        await worker._auto_discover_sessions()

    @pytest.mark.asyncio
    async def test_auto_discover_returns_early_when_no_sessions(self, tmp_path, monkeypatch):
        """When sessions dir is empty, returns early before pattern is used."""
        sessions_dir = tmp_path / 'sessions'
        sessions_dir.mkdir()

        monkeypatch.setattr(worker_module.settings, 'SESSIONS_DIR', str(sessions_dir))

        worker = object.__new__(worker_module.MainWorker)
        # Should not raise
        await worker._auto_discover_sessions()


class TestRegexErrorHandling:
    """Verify try/except around re.compile handles malformed patterns gracefully."""

    def test_malformed_pattern_raises_re_error(self):
        """Confirm re.error is raised for a broken pattern (sanity check)."""
        with pytest.raises(re.error):
            re.compile(r'account_(\d+_\d+\.session')  # unmatched parenthesis

    def test_try_except_pattern_produces_none_on_error(self):
        """Simulate the module-level try/except block with a bad pattern."""
        bad_pattern_str = r'account_(\d+_\d+\.session'  # broken
        result = None
        try:
            result = re.compile(bad_pattern_str)
        except re.error:
            result = None
        assert result is None

    def test_try_except_pattern_succeeds_with_valid_pattern(self):
        """Simulate the module-level try/except block with the correct pattern."""
        good_pattern_str = r'account_(\d+)_\d+\.session$'
        result = None
        try:
            result = re.compile(good_pattern_str)
        except re.error:
            result = None
        assert result is not None
        assert isinstance(result, re.Pattern)
