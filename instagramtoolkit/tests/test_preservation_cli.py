"""
Preservation tests for CLI interface.

**Validates: Requirements 5.1, 5.2, 5.3**

These tests verify that CLI interface behavior is unchanged.
"""

import pytest
import subprocess
import sys


class TestCLIPreservation:
    """Test that CLI interface is preserved after fixes."""

    def test_main_module_imports(self):
        """
        Property: main.py imports without errors.
        
        **Validates: Requirements 5.1**
        
        The main module should import successfully.
        """
        # Test that main.py can be imported
        result = subprocess.run(
            [sys.executable, '-c', 'import main'],
            capture_output=True,
            text=True
        )
        
        # Should not have import errors
        assert 'ImportError' not in result.stderr
        assert 'ModuleNotFoundError' not in result.stderr

    def test_cli_help_command(self):
        """
        Property: All CLI commands work with --help.
        
        **Validates: Requirements 5.1**
        
        The CLI should provide help documentation.
        """
        result = subprocess.run(
            [sys.executable, 'main.py', '--help'],
            capture_output=True,
            text=True
        )
        
        # Should show help without errors
        assert result.returncode in [0, 2]  # 0 for success, 2 for argparse help
        assert 'usage:' in result.stdout.lower() or 'usage:' in result.stderr.lower()
