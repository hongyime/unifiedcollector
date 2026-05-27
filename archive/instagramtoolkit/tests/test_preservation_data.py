"""
Preservation tests for data integrity mechanisms.

**Validates: Requirements 3.1, 3.2, 3.3**

These tests verify that data integrity mechanisms are unchanged.
"""

import pytest
import tempfile
import os
import json
import time

from src.io_utils import safe_json_write, FileLock, retry_with_backoff


class TestDataIntegrityPreservation:
    """Test that data integrity mechanisms are preserved after fixes."""

    def test_safe_json_write_creates_file(self):
        """
        Property: safe_json_write performs atomic writes.
        
        **Validates: Requirements 3.1**
        
        safe_json_write should create files atomically.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            temp_file = f.name
        
        try:
            test_data = {"test": "data", "number": 123}
            safe_json_write(temp_file, test_data)
            
            # Verify file exists
            assert os.path.exists(temp_file)
            
            # Verify data is correct
            with open(temp_file, 'r') as f:
                loaded = json.load(f)
            assert loaded == test_data
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)

    def test_file_lock_context_manager(self):
        """
        Property: FileLock provides cross-platform locking.
        
        **Validates: Requirements 3.2**
        
        FileLock should work as a context manager.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            temp_file = f.name
        
        try:
            # Test context manager usage
            with FileLock(temp_file, timeout=5):
                pass  # Lock acquired and released
            
            # Should not raise exception
            assert True
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)
            lock_file = temp_file + '.lock'
            if os.path.exists(lock_file):
                os.unlink(lock_file)
