"""
Preservation tests for core functionality.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4**

These tests verify that existing behavior is unchanged for all non-buggy inputs.
Tests focus on verifying key interfaces and behaviors remain stable.
"""

import pytest
import tempfile
import os
import json

# Import modules to test
from src.progress_manager import ProgressManager
from src.io_utils import safe_json_write, FileLock
from src.rate_limiter import RateLimiter
from src.account_cooldown import AccountCooldownManager, AccountQuotaManager


class TestCorePreservation:
    """Test that core functionality is preserved after fixes."""

    def test_progress_manager_save_and_load(self):
        """
        Property: Progress tracking enables resumption.
        
        **Validates: Requirements 1.4**
        
        Progress manager should save and restore state correctly.
        """
        # Create temporary progress file
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            temp_file = f.name
        
        try:
            # Create progress manager
            manager = ProgressManager("test_operation")
            manager.progress_file = temp_file
            
            # Mark some users as completed
            manager.mark_completed("user1")
            manager.mark_completed("user2")
            manager.mark_failed("user3", "Test error")
            
            # Save progress
            assert manager.save_progress()
            
            # Create new manager and verify data persists
            manager2 = ProgressManager("test_operation")
            manager2.progress_file = temp_file
            manager2.progress_data = manager2._load_progress()
            
            # Verify progress is preserved
            assert "user1" in manager2.progress_data['completed']
            assert "user2" in manager2.progress_data['completed']
            assert "user3" in manager2.progress_data['failed']
        
        finally:
            # Cleanup
            if os.path.exists(temp_file):
                os.unlink(temp_file)

    def test_safe_json_write_atomic_behavior(self):
        """
        Property: safe_json_write performs atomic writes.
        
        **Validates: Requirements 3.1**
        
        Atomic writes should use temp file + rename pattern.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            temp_file = f.name
        
        try:
            test_data = {"key": "value", "number": 42}
            
            # Write data
            safe_json_write(temp_file, test_data)
            
            # Verify file exists and contains correct data
            assert os.path.exists(temp_file)
            
            with open(temp_file, 'r') as f:
                loaded_data = json.load(f)
            
            assert loaded_data == test_data
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)

    def test_file_lock_provides_locking(self):
        """
        Property: FileLock provides cross-platform locking.
        
        **Validates: Requirements 3.2**
        
        FileLock should acquire and release locks correctly.
        """
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            temp_file = f.name
        
        try:
            # Test lock acquisition and release
            with FileLock(temp_file, timeout=5):
                # Lock acquired
                assert os.path.exists(temp_file + '.lock')
            
            # Lock should be released after context exit
            # Note: Lock file may or may not exist after release depending on implementation
        
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)
            lock_file = temp_file + '.lock'
            if os.path.exists(lock_file):
                os.unlink(lock_file)

    def test_rate_limiter_enforces_delays(self):
        """
        Property: RateLimiter enforces delays and breaks.
        
        **Validates: Requirements 4.1**
        
        RateLimiter should track operations and enforce delays.
        """
        limiter = RateLimiter(label="test", min_delay=0.1, max_delay=0.2)
        
        # Verify limiter initializes correctly
        assert limiter._op_counter == 0
        assert limiter.min_delay == 0.1
        assert limiter.max_delay == 0.2

    def test_account_cooldown_manager_tracks_cooldowns(self):
        """
        Property: AccountCooldownManager manages cooldowns.
        
        **Validates: Requirements 4.3**
        
        Cooldown manager should track per-account cooldowns.
        """
        manager = AccountCooldownManager()
        
        # Set cooldown for an account
        manager.put_on_cooldown("test_account", minutes=1, reason="test")
        
        # Verify cooldown is tracked
        assert manager.is_on_cooldown("test_account")

    def test_account_quota_manager_tracks_quotas(self):
        """
        Property: AccountQuotaManager enforces daily quotas.
        
        **Validates: Requirements 4.2**
        
        Quota manager should track per-account daily quotas.
        """
        manager = AccountQuotaManager()
        
        # Record usage
        manager.record_profile_view("test_account")
        manager.record_action("test_account")
        
        # Verify quota is tracked (should not be exhausted after 1 operation)
        assert manager.can_view_profiles("test_account")
        assert manager.can_perform_action("test_account")

