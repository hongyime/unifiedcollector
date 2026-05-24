"""
Preservation tests for testing infrastructure.

**Validates: Requirements 6.1, 6.2, 6.3**

These tests verify that testing infrastructure is unchanged.
"""

import pytest
import subprocess
import sys


class TestTestingInfrastructurePreservation:
    """Test that testing infrastructure is preserved after fixes."""

    def test_pytest_configuration_exists(self):
        """
        Property: pytest configuration exists.
        
        **Validates: Requirements 6.1**
        
        pytest.ini should exist and be valid.
        """
        import os
        assert os.path.exists('pytest.ini')

    def test_conftest_imports(self):
        """
        Property: conftest.py imports without errors.
        
        **Validates: Requirements 6.1**
        
        The conftest module should import successfully.
        """
        result = subprocess.run(
            [sys.executable, '-c', 'import sys; sys.path.insert(0, "tests"); import conftest'],
            capture_output=True,
            text=True
        )
        
        # Should not have import errors
        assert 'ImportError' not in result.stderr
        assert 'ModuleNotFoundError' not in result.stderr

    def test_test_files_exist(self):
        """
        Property: Test suite has multiple test files.
        
        **Validates: Requirements 6.3**
        
        The test suite should contain multiple test modules.
        """
        import os
        import glob
        
        test_files = glob.glob('tests/test_*.py')
        
        # Should have multiple test files
        assert len(test_files) > 10  # We know there are 22+ test modules
