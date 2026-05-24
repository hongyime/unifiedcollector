"""
Tests for P1.5 fix: update_checker.py signal file path consistency.

Validates Requirements 2.7 (bugfix.md):
  WHEN update_checker.py writes update signal file THEN the system SHALL
  write to `/app/signals/update_available` matching the path monitored
  by update_handler.py.

Bug condition F-007:
  RETURN X.checker.signal_path != X.handler.signal_path
"""
import os
import pytest
import tempfile
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock


# ============================================================
# Signal path consistency: fix checking (F-007)
# ============================================================

class TestSignalPathConsistency:
    """
    Fix Checking (F-007): checker and handler must use the same signal path.

    Validates Requirements 2.7 (bugfix.md)
    """

    def test_update_checker_default_signal_path(self):
        """
        update_checker.py SIGNAL_FILE default must be /app/signals/update_available.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker
        assert update_checker.SIGNAL_FILE == '/app/signals/update_available'

    def test_update_handler_default_signal_path(self):
        """
        update_handler.py SIGNAL_FILE default must be /app/signals/update_available.

        Validates: Requirements 2.7
        """
        import services.collector.update_handler as update_handler
        assert update_handler.SIGNAL_FILE == '/app/signals/update_available'

    def test_checker_and_handler_paths_are_identical(self):
        """
        Both modules must use the same default signal path.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker
        import services.collector.update_handler as update_handler
        assert update_checker.SIGNAL_FILE == update_handler.SIGNAL_FILE

    def test_checker_signal_path_not_old_buggy_path(self):
        """
        update_checker.py must NOT use the old buggy path /app/data/.update_available.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker
        assert update_checker.SIGNAL_FILE != '/app/data/.update_available'

    def test_signal_path_uses_signals_directory(self):
        """
        Signal file must be under /app/signals/ directory.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker
        assert update_checker.SIGNAL_FILE.startswith('/app/signals/')


# ============================================================
# _trigger_update(): writes to correct path
# ============================================================

class TestTriggerUpdateWritesCorrectPath:
    """
    _trigger_update() must write the signal file to SIGNAL_FILE.

    Validates Requirements 2.7 (bugfix.md)
    """

    @pytest.mark.asyncio
    async def test_trigger_update_writes_to_signal_file(self):
        """
        _trigger_update() must write to the SIGNAL_FILE path.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker

        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, 'update_available')

            with patch.object(update_checker, 'SIGNAL_FILE', signal_path):
                checker = update_checker.UpdateChecker()
                await checker._trigger_update()

            assert os.path.exists(signal_path), (
                f"Signal file was not written to {signal_path}"
            )

    @pytest.mark.asyncio
    async def test_trigger_update_creates_parent_directory(self):
        """
        _trigger_update() must create the signals directory if it doesn't exist.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker

        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, 'signals', 'update_available')

            with patch.object(update_checker, 'SIGNAL_FILE', signal_path):
                checker = update_checker.UpdateChecker()
                await checker._trigger_update()

            assert os.path.exists(signal_path), (
                "Signal file must be created even when parent directory doesn't exist"
            )

    @pytest.mark.asyncio
    async def test_trigger_update_writes_non_empty_content(self):
        """
        _trigger_update() must write non-empty content to the signal file.

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker

        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, 'update_available')

            with patch.object(update_checker, 'SIGNAL_FILE', signal_path):
                checker = update_checker.UpdateChecker()
                await checker._trigger_update()

            with open(signal_path, 'r') as f:
                content = f.read()
            assert content.strip() != '', "Signal file must contain non-empty content"


# ============================================================
# update_handler: detects signal written by update_checker
# ============================================================

class TestUpdateHandlerDetectsSignal:
    """
    update_handler.py must detect the signal file written by update_checker.py.

    Validates Requirements 2.7 (bugfix.md) - end-to-end detection.
    """

    @pytest.mark.asyncio
    async def test_handler_detects_signal_written_by_checker(self):
        """
        Signal written by _trigger_update() must be detected by _check_update_signal().

        Validates: Requirements 2.7
        """
        import services.collector.update_checker as update_checker
        import services.collector.update_handler as update_handler

        with tempfile.TemporaryDirectory() as tmpdir:
            signal_path = os.path.join(tmpdir, 'update_available')

            # Checker writes the signal
            with patch.object(update_checker, 'SIGNAL_FILE', signal_path):
                checker = update_checker.UpdateChecker()
                await checker._trigger_update()

            # Handler detects the signal
            with patch.object(update_handler, 'SIGNAL_FILE', signal_path):
                handler = update_handler.UpdateHandler()
                detected = handler._check_update_signal()

            assert detected is True, (
                "update_handler must detect the signal file written by update_checker"
            )

    def test_handler_returns_false_when_no_signal(self):
        """
        _check_update_signal() returns False when no signal file exists.

        Validates: Requirements 3.7 (preservation - detection still works)
        """
        import services.collector.update_handler as update_handler

        with patch.object(update_handler, 'SIGNAL_FILE', '/nonexistent/path/update_available'):
            handler = update_handler.UpdateHandler()
            assert handler._check_update_signal() is False


# ============================================================
# Environment variable override: preservation checking (F-007)
# ============================================================

class TestSignalPathEnvOverride:
    """
    Preservation Checking (F-007): custom UPDATE_SIGNAL_FILE env var must still work.

    Validates Requirements 3.7 (bugfix.md)
    """

    def test_checker_respects_env_override(self):
        """
        update_checker.py must respect UPDATE_SIGNAL_FILE env var override.

        Validates: Requirements 3.7
        """
        custom_path = '/custom/path/signal'
        with patch.dict(os.environ, {'UPDATE_SIGNAL_FILE': custom_path}):
            # Re-import to pick up env var (module-level constant)
            import importlib
            import services.collector.update_checker as uc_module
            # The module-level SIGNAL_FILE is set at import time;
            # verify the pattern uses os.getenv correctly
            import inspect
            source = inspect.getsource(uc_module)
            assert "os.getenv('UPDATE_SIGNAL_FILE'" in source or \
                   'os.getenv("UPDATE_SIGNAL_FILE"' in source, (
                "update_checker.py must use os.getenv('UPDATE_SIGNAL_FILE', ...) "
                "to allow environment override"
            )

    def test_handler_respects_env_override(self):
        """
        update_handler.py must respect UPDATE_SIGNAL_FILE env var override.

        Validates: Requirements 3.7
        """
        import inspect
        import services.collector.update_handler as uh_module
        source = inspect.getsource(uh_module)
        assert "os.getenv('UPDATE_SIGNAL_FILE'" in source or \
               'os.getenv("UPDATE_SIGNAL_FILE"' in source, (
            "update_handler.py must use os.getenv('UPDATE_SIGNAL_FILE', ...) "
            "to allow environment override"
        )

    def test_both_modules_use_same_env_var_name(self):
        """
        Both modules must use the same environment variable name for override.

        Validates: Requirements 2.7
        """
        import inspect
        import services.collector.update_checker as update_checker
        import services.collector.update_handler as update_handler

        checker_src = inspect.getsource(update_checker)
        handler_src = inspect.getsource(update_handler)

        assert 'UPDATE_SIGNAL_FILE' in checker_src, \
            "update_checker.py must reference UPDATE_SIGNAL_FILE env var"
        assert 'UPDATE_SIGNAL_FILE' in handler_src, \
            "update_handler.py must reference UPDATE_SIGNAL_FILE env var"
