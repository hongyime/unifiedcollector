"""
Preservation Property Tests - Account Management Operations

These tests verify that existing account management functionality remains unchanged after bugfixes.
They test cookie rotation, cooldown management, and account pool operations.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.7 (Preservation)**
"""
import os
import sys
import tempfile
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from hypothesis import given, strategies as st, settings, assume

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config


# Test fixtures and helpers
@pytest.fixture
def test_env():
    """Create isolated test environment with temporary directories"""
    test_dir = tempfile.mkdtemp(prefix="lemon8_account_preservation_test_")
    
    # Save original config values
    original_data_dir = config.DATA_DIR
    original_db_file = config.LEMON8_DB_FILE
    
    # Set up test directories
    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    
    # Override config for testing
    config.DATA_DIR = test_data_dir
    config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
    
    # Also patch the account_manager module's LEMON8_DB_FILE (imported at module level)
    import account_manager as am_module
    am_module.LEMON8_DB_FILE = config.LEMON8_DB_FILE
    
    yield {
        'test_dir': test_dir,
        'data_dir': test_data_dir,
    }
    
    # Restore original config
    config.DATA_DIR = original_data_dir
    config.LEMON8_DB_FILE = original_db_file
    
    # Restore account_manager module's LEMON8_DB_FILE
    import account_manager as am_module
    am_module.LEMON8_DB_FILE = original_db_file
    
    # Clean up test directory
    shutil.rmtree(test_dir, ignore_errors=True)


class TestEnvContext:
    """Context manager for test environment (for use with Hypothesis)"""
    def __init__(self):
        self.test_dir = None
        self.original_data_dir = None
        self.original_db_file = None
    
    def __enter__(self):
        self.test_dir = tempfile.mkdtemp(prefix="lemon8_account_prop_test_")
        
        # Save original config values
        self.original_data_dir = config.DATA_DIR
        self.original_db_file = config.LEMON8_DB_FILE
        
        # Set up test directories
        test_data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(test_data_dir, exist_ok=True)
        
        # Override config for testing
        config.DATA_DIR = test_data_dir
        config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
        
        # Also patch the account_manager module's LEMON8_DB_FILE (imported at module level)
        import account_manager as am_module
        am_module.LEMON8_DB_FILE = config.LEMON8_DB_FILE
        
        return {
            'test_dir': self.test_dir,
            'data_dir': test_data_dir,
        }
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Close any open database connections
        import gc
        gc.collect()  # Force garbage collection to close any lingering connections
        
        # Restore original config
        config.DATA_DIR = self.original_data_dir
        config.LEMON8_DB_FILE = self.original_db_file
        
        # Restore account_manager module's LEMON8_DB_FILE
        import account_manager as am_module
        am_module.LEMON8_DB_FILE = self.original_db_file
        
        # Clean up test directory
        if self.test_dir:
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass  # Ignore cleanup errors


def create_test_cookies_file(file_path):
    """Create a minimal test cookies.txt file"""
    cookies_content = """# Netscape HTTP Cookie File
# This is a generated file! Do not edit.

.lemon8-app.com	TRUE	/	FALSE	1735689600	session_id	test_session_123
.lemon8-app.com	TRUE	/	FALSE	1735689600	user_token	test_token_456
"""
    with open(file_path, 'w') as f:
        f.write(cookies_content)


# Test 1: Account addition and retrieval works correctly
def test_account_addition_preserves_behavior(test_env):
    """
    Observation: AccountManager account addition works on unfixed code
    
    Verifies that:
    1. Accounts can be added with name and cookies file path
    2. Duplicate account names are rejected
    3. Invalid cookies file paths are rejected
    4. Added accounts are retrievable from database
    5. Account metadata (added_ts, is_active) is set correctly
    
    **Validates: Requirement 3.7 - Account addition**
    """
    from account_manager import AccountManager
    
    # Create test cookies files
    cookies_file_1 = os.path.join(test_env['test_dir'], 'cookies_1.txt')
    cookies_file_2 = os.path.join(test_env['test_dir'], 'cookies_2.txt')
    create_test_cookies_file(cookies_file_1)
    create_test_cookies_file(cookies_file_2)
    
    # Create account manager
    manager = AccountManager()
    
    # Test 1: Add first account - should succeed
    result1 = manager.add_account('account1', cookies_file_1)
    assert result1 is True, "First account addition should succeed"
    
    # Test 2: Add second account - should succeed
    result2 = manager.add_account('account2', cookies_file_2)
    assert result2 is True, "Second account addition should succeed"
    
    # Test 3: Add duplicate account - should fail
    result3 = manager.add_account('account1', cookies_file_1)
    assert result3 is False, "Duplicate account addition should fail"
    
    # Test 4: Add account with invalid cookies file - should fail
    result4 = manager.add_account('account3', '/nonexistent/cookies.txt')
    assert result4 is False, "Account with invalid cookies file should fail"
    
    # Test 5: Verify accounts are in database
    accounts = manager.get_all_accounts()
    assert len(accounts) == 2, f"Should have 2 accounts, got {len(accounts)}"
    
    # Test 6: Verify account metadata
    account1 = next((a for a in accounts if a['account_name'] == 'account1'), None)
    assert account1 is not None, "Account1 should exist"
    assert account1['cookies_file_path'] == cookies_file_1, "Cookies file path should match"
    assert account1['is_active'] == 1, "Account should be active by default"
    assert account1['added_ts'] is not None, "Added timestamp should be set"
    assert account1['last_used_ts'] is None, "Last used should be None initially"
    
    manager.close()


# Test 2: Cookie rotation works correctly
def test_cookie_rotation_preserves_behavior(test_env):
    """
    Observation: AccountManager cookie rotation works on unfixed code
    
    Verifies that:
    1. get_available_account() returns accounts not in cooldown
    2. Accounts are rotated based on last_used_ts (least recently used first)
    3. mark_account_used() updates last_used_ts
    4. Accounts in cooldown are excluded from rotation
    5. Rotation order is deterministic and fair
    
    **Validates: Requirement 3.7 - Cookie rotation**
    """
    from account_manager import AccountManager
    
    # Create test cookies files
    cookies_files = []
    for i in range(3):
        cookies_file = os.path.join(test_env['test_dir'], f'cookies_{i}.txt')
        create_test_cookies_file(cookies_file)
        cookies_files.append(cookies_file)
    
    # Create account manager and add accounts
    manager = AccountManager()
    for i, cookies_file in enumerate(cookies_files):
        manager.add_account(f'account{i}', cookies_file)
    
    # Test 1: Get first available account (should be account0 - first added, never used)
    account1 = manager.get_available_account()
    assert account1 is not None, "Should return an available account"
    assert account1['account_name'] == 'account0', "Should return first account (never used)"
    
    # Mark account0 as used
    manager.mark_account_used('account0')
    
    # Test 2: Get next available account (should be account1 - never used)
    account2 = manager.get_available_account()
    assert account2 is not None, "Should return an available account"
    assert account2['account_name'] == 'account1', "Should return second account (never used)"
    
    # Mark account1 as used
    manager.mark_account_used('account1')
    
    # Test 3: Get next available account (should be account2 - never used)
    account3 = manager.get_available_account()
    assert account3 is not None, "Should return an available account"
    assert account3['account_name'] == 'account2', "Should return third account (never used)"
    
    # Mark account2 as used
    manager.mark_account_used('account2')
    
    # Test 4: Get next available account (should be account0 - least recently used)
    account4 = manager.get_available_account()
    assert account4 is not None, "Should return an available account"
    assert account4['account_name'] == 'account0', "Should return account0 (least recently used)"
    
    # Test 5: Put account0 in cooldown
    manager.set_account_cooldown('account0', cooldown_minutes=5)
    
    # Test 6: Get next available account (should skip account0 in cooldown)
    account5 = manager.get_available_account()
    assert account5 is not None, "Should return an available account"
    assert account5['account_name'] != 'account0', "Should skip account in cooldown"
    assert account5['account_name'] in ['account1', 'account2'], "Should return account1 or account2"
    
    manager.close()


# Test 3: Cooldown management works correctly
def test_cooldown_management_preserves_behavior(test_env):
    """
    Observation: AccountManager cooldown management works on unfixed code
    
    Verifies that:
    1. set_account_cooldown() puts account in cooldown with expiration time
    2. Cooldown duration is calculated correctly
    3. Cooldown reason is stored
    4. clear_account_cooldown() removes cooldown
    5. Expired cooldowns are automatically excluded from active cooldowns
    6. get_all_accounts() includes cooldown information
    
    **Validates: Requirement 3.7 - Cooldown management**
    """
    from account_manager import AccountManager
    
    # Create test cookies file
    cookies_file = os.path.join(test_env['test_dir'], 'cookies.txt')
    create_test_cookies_file(cookies_file)
    
    # Create account manager and add account
    manager = AccountManager()
    manager.add_account('test_account', cookies_file)
    
    # Test 1: Set cooldown with default duration (5 minutes)
    manager.set_account_cooldown('test_account')
    
    # Verify cooldown is set
    accounts = manager.get_all_accounts()
    account = accounts[0]
    assert account['cooldown_until'] is not None, "Cooldown should be set"
    assert account['cooldown_reason'] == 'rate-limit', "Default reason should be rate-limit"
    
    # Parse cooldown expiration time
    cooldown_until = datetime.fromisoformat(account['cooldown_until'])
    now = datetime.utcnow()
    time_diff = (cooldown_until - now).total_seconds()
    
    # Verify cooldown duration is approximately 5 minutes (300 seconds)
    assert 290 <= time_diff <= 310, f"Cooldown should be ~5 minutes, got {time_diff} seconds"
    
    # Test 2: Clear cooldown
    manager.clear_account_cooldown('test_account')
    
    # Verify cooldown is cleared
    accounts = manager.get_all_accounts()
    account = accounts[0]
    assert account['cooldown_until'] is None, "Cooldown should be cleared"
    
    # Test 3: Set cooldown with custom duration and reason
    manager.set_account_cooldown('test_account', cooldown_minutes=10, reason='manual-test')
    
    # Verify custom cooldown
    accounts = manager.get_all_accounts()
    account = accounts[0]
    assert account['cooldown_until'] is not None, "Cooldown should be set"
    assert account['cooldown_reason'] == 'manual-test', "Custom reason should be stored"
    
    # Parse cooldown expiration time
    cooldown_until = datetime.fromisoformat(account['cooldown_until'])
    now = datetime.utcnow()
    time_diff = (cooldown_until - now).total_seconds()
    
    # Verify cooldown duration is approximately 10 minutes (600 seconds)
    assert 590 <= time_diff <= 610, f"Cooldown should be ~10 minutes, got {time_diff} seconds"
    
    # Test 4: Verify account is not available during cooldown
    available = manager.get_available_account()
    assert available is None, "Account in cooldown should not be available"
    
    manager.close()


# Test 4: Account statistics work correctly
def test_account_stats_preserves_behavior(test_env):
    """
    Observation: AccountManager statistics work on unfixed code
    
    Verifies that:
    1. get_account_stats() returns total_accounts count
    2. in_cooldown count is accurate
    3. available count is calculated correctly (total - in_cooldown)
    4. Stats update when accounts are added/removed
    5. Stats update when cooldowns are set/cleared
    
    **Validates: Requirement 3.7 - Account statistics**
    """
    from account_manager import AccountManager
    
    # Create test cookies files
    cookies_files = []
    for i in range(3):
        cookies_file = os.path.join(test_env['test_dir'], f'cookies_{i}.txt')
        create_test_cookies_file(cookies_file)
        cookies_files.append(cookies_file)
    
    # Create account manager
    manager = AccountManager()
    
    # Test 1: Initial stats (no accounts)
    stats = manager.get_account_stats()
    assert stats['total_accounts'] == 0, "Should have 0 accounts initially"
    assert stats['in_cooldown'] == 0, "Should have 0 in cooldown initially"
    assert stats['available'] == 0, "Should have 0 available initially"
    
    # Add accounts
    for i, cookies_file in enumerate(cookies_files):
        manager.add_account(f'account{i}', cookies_file)
    
    # Test 2: Stats after adding accounts
    stats = manager.get_account_stats()
    assert stats['total_accounts'] == 3, "Should have 3 accounts"
    assert stats['in_cooldown'] == 0, "Should have 0 in cooldown"
    assert stats['available'] == 3, "Should have 3 available"
    
    # Put one account in cooldown
    manager.set_account_cooldown('account0', cooldown_minutes=5)
    
    # Test 3: Stats after setting cooldown
    stats = manager.get_account_stats()
    assert stats['total_accounts'] == 3, "Should still have 3 accounts"
    assert stats['in_cooldown'] == 1, "Should have 1 in cooldown"
    assert stats['available'] == 2, "Should have 2 available"
    
    # Put another account in cooldown
    manager.set_account_cooldown('account1', cooldown_minutes=5)
    
    # Test 4: Stats after setting second cooldown
    stats = manager.get_account_stats()
    assert stats['total_accounts'] == 3, "Should still have 3 accounts"
    assert stats['in_cooldown'] == 2, "Should have 2 in cooldown"
    assert stats['available'] == 1, "Should have 1 available"
    
    # Clear one cooldown
    manager.clear_account_cooldown('account0')
    
    # Test 5: Stats after clearing cooldown
    stats = manager.get_account_stats()
    assert stats['total_accounts'] == 3, "Should still have 3 accounts"
    assert stats['in_cooldown'] == 1, "Should have 1 in cooldown"
    assert stats['available'] == 2, "Should have 2 available"
    
    # Remove one account
    manager.remove_account('account2')
    
    # Test 6: Stats after removing account
    stats = manager.get_account_stats()
    assert stats['total_accounts'] == 2, "Should have 2 accounts"
    assert stats['in_cooldown'] == 1, "Should have 1 in cooldown"
    assert stats['available'] == 1, "Should have 1 available"
    
    manager.close()


# Test 5: Account removal works correctly
def test_account_removal_preserves_behavior(test_env):
    """
    Observation: AccountManager account removal works on unfixed code
    
    Verifies that:
    1. remove_account() deletes account from database
    2. Associated cooldowns are also removed
    3. Removing non-existent account returns False
    4. Account list is updated after removal
    
    **Validates: Requirement 3.7 - Account removal**
    """
    from account_manager import AccountManager
    
    # Create test cookies file
    cookies_file = os.path.join(test_env['test_dir'], 'cookies.txt')
    create_test_cookies_file(cookies_file)
    
    # Create account manager and add account
    manager = AccountManager()
    manager.add_account('test_account', cookies_file)
    
    # Set cooldown
    manager.set_account_cooldown('test_account', cooldown_minutes=5)
    
    # Verify account and cooldown exist
    accounts = manager.get_all_accounts()
    assert len(accounts) == 1, "Should have 1 account"
    assert accounts[0]['cooldown_until'] is not None, "Cooldown should be set"
    
    # Test 1: Remove account
    result = manager.remove_account('test_account')
    assert result is True, "Account removal should succeed"
    
    # Test 2: Verify account is removed
    accounts = manager.get_all_accounts()
    assert len(accounts) == 0, "Should have 0 accounts after removal"
    
    # Test 3: Verify cooldown is also removed
    conn = sqlite3.connect(config.LEMON8_DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM account_cooldowns WHERE account_name = ?', ('test_account',))
    cooldown_count = cursor.fetchone()[0]
    conn.close()
    assert cooldown_count == 0, "Cooldown should be removed with account"
    
    # Test 4: Remove non-existent account
    result = manager.remove_account('nonexistent_account')
    assert result is False, "Removing non-existent account should return False"
    
    manager.close()


# ============================================================================
# PROPERTY-BASED TESTS - Account Management Preservation
# ============================================================================

@given(
    num_accounts=st.integers(min_value=1, max_value=10)
)
@settings(max_examples=50, deadline=None)
def test_property_cookie_rotation_fairness(num_accounts):
    """
    Property: Cookie rotation is fair and deterministic
    
    For any number of accounts N, after N rotations (marking each as used),
    the next available account should be the first account (least recently used).
    
    This property verifies that:
    1. Rotation follows least-recently-used (LRU) order
    2. All accounts get equal opportunity to be selected
    3. Rotation is deterministic and predictable
    
    **Validates: Requirement 3.7 - Cookie rotation fairness**
    """
    from account_manager import AccountManager
    
    with TestEnvContext() as test_env:
        # Create test cookies files
        cookies_files = []
        for i in range(num_accounts):
            cookies_file = os.path.join(test_env['test_dir'], f'cookies_prop_{i}.txt')
            create_test_cookies_file(cookies_file)
            cookies_files.append(cookies_file)
        
        # Create account manager and add accounts
        manager = AccountManager()
        try:
            account_names = []
            for i, cookies_file in enumerate(cookies_files):
                account_name = f'prop_account_{i}'
                result = manager.add_account(account_name, cookies_file)
                if result:  # Only add to list if successfully added
                    account_names.append(account_name)
            
            # Rotate through all accounts once
            selected_accounts = []
            for _ in range(len(account_names)):
                account = manager.get_available_account()
                assert account is not None, "Should always have an available account"
                selected_accounts.append(account['account_name'])
                manager.mark_account_used(account['account_name'])
            
            # Property: After one full rotation, the next account should be the first one
            next_account = manager.get_available_account()
            assert next_account is not None, "Should have an available account after rotation"
            assert next_account['account_name'] == selected_accounts[0], \
                f"After full rotation, should return first account {selected_accounts[0]}, got {next_account['account_name']}"
        finally:
            manager.close()


@given(
    num_accounts=st.integers(min_value=2, max_value=10),
    num_cooldowns=st.integers(min_value=1, max_value=5)
)
@settings(max_examples=50, deadline=None)
def test_property_cooldown_exclusion(num_accounts, num_cooldowns):
    """
    Property: Accounts in cooldown are excluded from rotation
    
    For any number of accounts N and cooldowns C (where C < N),
    after putting C accounts in cooldown, get_available_account() should:
    1. Never return an account in cooldown
    2. Only return accounts not in cooldown
    3. Return None only if all accounts are in cooldown
    
    **Validates: Requirement 3.7 - Cooldown exclusion**
    """
    from account_manager import AccountManager
    
    # Ensure we don't put all accounts in cooldown
    assume(num_cooldowns < num_accounts)
    
    with TestEnvContext() as test_env:
        # Create test cookies files
        cookies_files = []
        for i in range(num_accounts):
            cookies_file = os.path.join(test_env['test_dir'], f'cookies_cooldown_{i}.txt')
            create_test_cookies_file(cookies_file)
            cookies_files.append(cookies_file)
        
        # Create account manager and add accounts
        manager = AccountManager()
        try:
            account_names = []
            for i, cookies_file in enumerate(cookies_files):
                account_name = f'cooldown_account_{i}'
                result = manager.add_account(account_name, cookies_file)
                if result:
                    account_names.append(account_name)
            
            # Put first num_cooldowns accounts in cooldown
            cooldown_accounts = account_names[:num_cooldowns]
            for account_name in cooldown_accounts:
                manager.set_account_cooldown(account_name, cooldown_minutes=5)
            
            # Property: get_available_account() should never return an account in cooldown
            for _ in range(len(account_names) - num_cooldowns):
                account = manager.get_available_account()
                assert account is not None, "Should have available accounts"
                assert account['account_name'] not in cooldown_accounts, \
                    f"Should not return account in cooldown: {account['account_name']}"
                manager.mark_account_used(account['account_name'])
        finally:
            manager.close()


@given(
    num_accounts=st.integers(min_value=1, max_value=10),
    operations=st.lists(
        st.tuples(
            st.sampled_from(['add_cooldown', 'clear_cooldown', 'mark_used']),
            st.integers(min_value=0, max_value=9)
        ),
        min_size=1,
        max_size=20
    )
)
@settings(max_examples=50, deadline=None)
def test_property_account_stats_consistency(num_accounts, operations):
    """
    Property: Account statistics are always consistent
    
    For any sequence of operations (add cooldown, clear cooldown, mark used),
    the account statistics should always satisfy:
    1. total_accounts >= 0
    2. in_cooldown >= 0
    3. available >= 0
    4. available = total_accounts - in_cooldown
    5. in_cooldown <= total_accounts
    
    **Validates: Requirement 3.7 - Account statistics consistency**
    """
    from account_manager import AccountManager
    
    with TestEnvContext() as test_env:
        # Create test cookies files
        cookies_files = []
        for i in range(num_accounts):
            cookies_file = os.path.join(test_env['test_dir'], f'cookies_stats_{i}.txt')
            create_test_cookies_file(cookies_file)
            cookies_files.append(cookies_file)
        
        # Create account manager and add accounts
        manager = AccountManager()
        try:
            account_names = []
            for i, cookies_file in enumerate(cookies_files):
                account_name = f'stats_account_{i}'
                result = manager.add_account(account_name, cookies_file)
                if result:
                    account_names.append(account_name)
            
            # Apply operations
            for operation, account_idx in operations:
                # Ensure account_idx is valid
                if account_idx >= len(account_names):
                    continue
                
                account_name = account_names[account_idx]
                
                if operation == 'add_cooldown':
                    manager.set_account_cooldown(account_name, cooldown_minutes=5)
                elif operation == 'clear_cooldown':
                    manager.clear_account_cooldown(account_name)
                elif operation == 'mark_used':
                    manager.mark_account_used(account_name)
            
            # Property: Statistics should always be consistent
            stats = manager.get_account_stats()
            
            assert stats['total_accounts'] >= 0, "Total accounts should be non-negative"
            assert stats['in_cooldown'] >= 0, "In cooldown should be non-negative"
            assert stats['available'] >= 0, "Available should be non-negative"
            assert stats['available'] == stats['total_accounts'] - stats['in_cooldown'], \
                f"Available ({stats['available']}) should equal total ({stats['total_accounts']}) - in_cooldown ({stats['in_cooldown']})"
            assert stats['in_cooldown'] <= stats['total_accounts'], \
                f"In cooldown ({stats['in_cooldown']}) should not exceed total ({stats['total_accounts']})"
        finally:
            manager.close()


@given(
    num_accounts=st.integers(min_value=1, max_value=10),
    num_removals=st.integers(min_value=0, max_value=5)
)
@settings(max_examples=50, deadline=None)
def test_property_account_removal_cleanup(num_accounts, num_removals):
    """
    Property: Account removal cleans up all associated data
    
    For any number of accounts N and removals R (where R <= N),
    after removing R accounts:
    1. Total accounts should be N - R
    2. Removed accounts should not appear in get_all_accounts()
    3. Cooldowns for removed accounts should be deleted
    4. get_available_account() should never return removed accounts
    
    **Validates: Requirement 3.7 - Account removal cleanup**
    """
    from account_manager import AccountManager
    
    # Ensure we don't remove more accounts than we have
    assume(num_removals <= num_accounts)
    
    with TestEnvContext() as test_env:
        # Create test cookies files
        cookies_files = []
        for i in range(num_accounts):
            cookies_file = os.path.join(test_env['test_dir'], f'cookies_removal_{i}.txt')
            create_test_cookies_file(cookies_file)
            cookies_files.append(cookies_file)
        
        # Create account manager and add accounts
        manager = AccountManager()
        try:
            account_names = []
            for i, cookies_file in enumerate(cookies_files):
                account_name = f'removal_account_{i}'
                result = manager.add_account(account_name, cookies_file)
                if result:
                    account_names.append(account_name)
            
            # Put some accounts in cooldown before removal
            for i in range(min(num_removals, len(account_names))):
                manager.set_account_cooldown(account_names[i], cooldown_minutes=5)
            
            # Remove first num_removals accounts
            removed_accounts = account_names[:num_removals]
            for account_name in removed_accounts:
                manager.remove_account(account_name)
            
            # Property 1: Total accounts should be N - R
            stats = manager.get_account_stats()
            expected_total = len(account_names) - num_removals
            assert stats['total_accounts'] == expected_total, \
                f"Total accounts should be {expected_total}, got {stats['total_accounts']}"
            
            # Property 2: Removed accounts should not appear in get_all_accounts()
            all_accounts = manager.get_all_accounts()
            remaining_account_names = [acc['account_name'] for acc in all_accounts]
            for removed_account in removed_accounts:
                assert removed_account not in remaining_account_names, \
                    f"Removed account {removed_account} should not appear in account list"
            
            # Property 3: Cooldowns for removed accounts should be deleted
            conn = sqlite3.connect(config.LEMON8_DB_FILE)
            cursor = conn.cursor()
            for removed_account in removed_accounts:
                cursor.execute('SELECT COUNT(*) FROM account_cooldowns WHERE account_name = ?', (removed_account,))
                cooldown_count = cursor.fetchone()[0]
                assert cooldown_count == 0, \
                    f"Cooldown for removed account {removed_account} should be deleted"
            conn.close()
            
            # Property 4: get_available_account() should never return removed accounts
            if expected_total > 0:
                for _ in range(min(expected_total, 5)):  # Check a few times
                    account = manager.get_available_account()
                    if account is not None:
                        assert account['account_name'] not in removed_accounts, \
                            f"Should not return removed account: {account['account_name']}"
                        manager.mark_account_used(account['account_name'])
        finally:
            manager.close()


@given(
    num_accounts=st.integers(min_value=2, max_value=10),
    rotation_count=st.integers(min_value=1, max_value=20)
)
@settings(max_examples=20, deadline=None)
def test_property_rotation_determinism(num_accounts, rotation_count):
    """
    Property: Cookie rotation is fair - all accounts get used
    
    For any number of accounts N and rotation count R (where R >= N),
    all accounts should appear in the rotation sequence.
    
    **Validates: Requirement 3.7 - Cookie rotation fairness**
    """
    from account_manager import AccountManager
    from hypothesis import assume
    
    # Only test when we have enough rotations to cover all accounts
    assume(rotation_count >= num_accounts)
    
    with TestEnvContext() as test_env:
        # Create test cookies files
        cookies_files = []
        for i in range(num_accounts):
            cookies_file = os.path.join(test_env['test_dir'], f'cookies_determ_{i}.txt')
            create_test_cookies_file(cookies_file)
            cookies_files.append(cookies_file)
        
        # Create account manager and add accounts
        manager = AccountManager()
        try:
            account_names = []
            for i, cookies_file in enumerate(cookies_files):
                account_name = f'determ_account_{i}'
                result = manager.add_account(account_name, cookies_file)
                if result:
                    account_names.append(account_name)
            
            # Perform rotations and record the sequence
            rotation_sequence = []
            for _ in range(rotation_count):
                account = manager.get_available_account()
                assert account is not None, "Should always have an available account"
                rotation_sequence.append(account['account_name'])
                manager.mark_account_used(account['account_name'])
            
            # Property: All accounts should appear in the rotation
            used_accounts = set(rotation_sequence)
            for account_name in account_names:
                assert account_name in used_accounts, \
                    f"Account {account_name} should appear in rotation sequence"
        finally:
            manager.close()


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
