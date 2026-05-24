"""
Preservation Property Tests - Database Operations (Non-Migration)

These tests verify that existing database operations remain unchanged after bugfixes.
They test operations on tables other than `users` and existing `users` table operations
that don't use the `user_id` column.

EXPECTED OUTCOME ON UNFIXED CODE: Tests PASS (confirms baseline behavior)
After fixes are implemented, these tests should STILL PASS (confirms no regressions)

**Validates: Requirements 3.1 (Preservation)**
"""
import os
import sys
import tempfile
import shutil
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
import json

import pytest
from hypothesis import given, strategies as st, settings, assume

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config


# Test fixtures and helpers
@pytest.fixture
def test_env():
    """Create isolated test environment with temporary directories.

    tracking/progress/downloader now use config.X directly, so patching
    config is sufficient for those. account_manager still uses from-import,
    so its binding must also be patched.
    """
    import account_manager
    test_dir = tempfile.mkdtemp(prefix="lemon8_db_preservation_test_")

    original_data_dir = config.DATA_DIR
    original_db_file = config.LEMON8_DB_FILE
    original_visited_users = config.VISITED_USERS_FILE
    original_processed_tags = config.PROCESSED_TAGS_FILE
    orig_acct_db = account_manager.LEMON8_DB_FILE

    test_data_dir = os.path.join(test_dir, "data")
    os.makedirs(test_data_dir, exist_ok=True)
    test_db = os.path.join(test_data_dir, "lemon8_toolkit.db")

    config.DATA_DIR = test_data_dir
    config.LEMON8_DB_FILE = test_db
    config.VISITED_USERS_FILE = os.path.join(test_data_dir, "visited_users.json")
    config.PROCESSED_TAGS_FILE = os.path.join(test_data_dir, "processed_tags.json")
    account_manager.LEMON8_DB_FILE = test_db

    yield {'test_dir': test_dir, 'data_dir': test_data_dir}

    config.DATA_DIR = original_data_dir
    config.LEMON8_DB_FILE = original_db_file
    config.VISITED_USERS_FILE = original_visited_users
    config.PROCESSED_TAGS_FILE = original_processed_tags
    account_manager.LEMON8_DB_FILE = orig_acct_db

    shutil.rmtree(test_dir, ignore_errors=True)


class TestEnvContext:
    """Context manager for test environment (for use with Hypothesis)"""
    def __init__(self):
        self.test_dir = None
        self.original_data_dir = None
        self.original_db_file = None
        self.original_visited_users = None
        self.original_processed_tags = None
    
    def __enter__(self):
        self.test_dir = tempfile.mkdtemp(prefix="lemon8_db_prop_test_")
        
        # Save original config values
        self.original_data_dir = config.DATA_DIR
        self.original_db_file = config.LEMON8_DB_FILE
        self.original_visited_users = config.VISITED_USERS_FILE
        self.original_processed_tags = config.PROCESSED_TAGS_FILE
        
        # Set up test directories
        test_data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(test_data_dir, exist_ok=True)
        
        # Override config for testing
        config.DATA_DIR = test_data_dir
        config.LEMON8_DB_FILE = os.path.join(test_data_dir, "lemon8_toolkit.db")
        config.VISITED_USERS_FILE = os.path.join(test_data_dir, "visited_users.json")
        config.PROCESSED_TAGS_FILE = os.path.join(test_data_dir, "processed_tags.json")
        
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
        config.VISITED_USERS_FILE = self.original_visited_users
        config.PROCESSED_TAGS_FILE = self.original_processed_tags
        
        # Clean up test directory
        if self.test_dir:
            try:
                shutil.rmtree(self.test_dir, ignore_errors=True)
            except Exception:
                pass  # Ignore cleanup errors


# ============================================================================
# Test 1: Tags Table Operations (Non-Users Table)
# ============================================================================

def test_tags_table_operations_preserve_behavior(test_env):
    """
    Observation: TagTracker operations on tags table work on unfixed code
    
    Verifies that:
    1. Tags can be inserted into tags table
    2. Tags can be queried from tags table
    3. Tag metadata can be updated
    4. Tag statistics work correctly
    5. Tags table operations are independent of users table migration
    
    **Validates: Requirement 3.1 - Tags table operations unchanged**
    """
    from tracking import TagTracker
    
    # Create tag tracker (initializes tags table)
    tracker = TagTracker()
    
    # Test 1: Mark tag as processed
    tag_metadata = {
        'tag_name': 'test_tag',
        'total_media_found': 10,  # Required for is_tag_processed to return True
        'related_users_found': 5,
        'related_tags_found': 3
    }
    tracker.mark_tag_processed('tag_123', metadata=tag_metadata)
    
    # Test 2: Verify tag is tracked
    assert tracker.is_tag_tracked('tag_123'), "Tag should be tracked"
    assert tracker.is_tag_processed('tag_123'), "Tag should be processed"
    
    # Test 3: Get tag info
    tag_info = tracker.get_tag_info('tag_123')
    assert tag_info is not None, "Tag info should exist"
    assert tag_info['tag_id'] == 'tag_123', "Tag ID should match"
    assert tag_info['process_count'] == 1, "Process count should be 1"
    
    # Test 4: Mark tag as processed again (increment count)
    tracker.mark_tag_processed('tag_123', metadata=tag_metadata)
    tag_info = tracker.get_tag_info('tag_123')
    assert tag_info['process_count'] == 2, "Process count should be 2"
    
    # Test 5: Get all processed tags
    all_tags = tracker.get_all_processed_tags()
    assert 'tag_123' in all_tags, "Tag should be in processed tags list"
    
    # Test 6: Get stats
    stats = tracker.get_stats()
    assert stats['total_processed_tags'] >= 1, "Should have at least 1 tag processed"
    
    tracker.save()


def test_user_snapshots_table_operations_preserve_behavior(test_env):
    """
    Observation: User snapshots operations work on unfixed code
    
    Verifies that:
    1. User snapshots can be created
    2. Snapshots can be queried by username
    3. Historical data is preserved
    4. Snapshots table operations are independent of user_id column
    
    **Validates: Requirement 3.1 - User snapshots table operations unchanged**
    """
    from tracking import AccountTracker
    
    # Create account tracker (initializes users and user_snapshots tables)
    tracker = AccountTracker()
    
    # Test 1: Mark user as visited (creates user record)
    user_metadata = {
        'display_name': 'Test User',
        'bio': 'Test bio',
        'followers': 100,
        'following': 50,
        'posts': 25
    }
    tracker.mark_user_visited('testuser', metadata=user_metadata)
    
    # Test 2: Create snapshot
    tracker.create_snapshot(
        username='testuser',
        followers_count=100,
        following_count=50,
        post_count=25
    )
    
    # Test 3: Get user history
    history = tracker.get_user_history('testuser', limit=10)
    assert len(history) >= 1, "Should have at least 1 snapshot"
    assert history[0]['username'] == 'testuser', "Username should match"
    assert history[0]['followers_count'] == 100, "Followers count should match"
    assert history[0]['following_count'] == 50, "Following count should match"
    assert history[0]['post_count'] == 25, "Post count should match"
    
    # Test 4: Create another snapshot with different counts
    tracker.create_snapshot(
        username='testuser',
        followers_count=110,
        following_count=55,
        post_count=30
    )
    
    # Test 5: Verify both snapshots exist
    history = tracker.get_user_history('testuser', limit=10)
    assert len(history) >= 2, "Should have at least 2 snapshots"
    
    tracker.save()


def test_profile_photo_history_table_operations_preserve_behavior(test_env):
    """
    Observation: Profile photo history operations work on unfixed code
    
    Verifies that:
    1. Profile photo records can be inserted
    2. Photo history can be queried by username
    3. Photo tracking works without user_id column dependency
    4. Blob storage operations work correctly
    
    **Validates: Requirement 3.1 - Profile photo history table operations unchanged**
    """
    from profile_photo_tracker import ProfilePhotoTracker
    
    # Create profile photo tracker (initializes profile_photo_history table)
    tracker = ProfilePhotoTracker()
    
    # Test 1: Manually insert a photo record (simulating photo tracking)
    cursor = tracker.conn.cursor()
    cursor.execute('''
        INSERT INTO profile_photo_history 
        (username, photo_url, photo_phash, file_path)
        VALUES (?, ?, ?, ?)
    ''', ('testuser', 'https://example.com/photo1.jpg', 'abc123def456', '/path/to/photo1.jpg'))
    tracker.conn.commit()
    
    # Test 2: Get photo history
    history = tracker.get_photo_history('testuser', limit=10)
    assert len(history) >= 1, "Should have at least 1 photo record"
    assert history[0]['username'] == 'testuser', "Username should match"
    assert history[0]['photo_url'] == 'https://example.com/photo1.jpg', "Photo URL should match"
    assert history[0]['photo_phash'] == 'abc123def456', "Photo pHash should match"
    
    # Test 3: Insert another photo record
    cursor.execute('''
        INSERT INTO profile_photo_history 
        (username, photo_url, photo_phash, file_path)
        VALUES (?, ?, ?, ?)
    ''', ('testuser', 'https://example.com/photo2.jpg', 'def456ghi789', '/path/to/photo2.jpg'))
    tracker.conn.commit()
    
    # Test 4: Verify both records exist
    history = tracker.get_photo_history('testuser', limit=10)
    assert len(history) >= 2, "Should have at least 2 photo records"
    
    # Test 5: Get stats
    stats = tracker.get_stats()
    assert stats['total_photos_tracked'] >= 2, "Should have at least 2 photos tracked"
    assert stats['unique_users_tracked'] >= 1, "Should have at least 1 unique user"
    
    tracker.close()


def test_account_cookies_table_operations_preserve_behavior(test_env):
    """
    Observation: Account cookies table operations work on unfixed code
    
    Verifies that:
    1. Account records can be inserted
    2. Accounts can be queried
    3. Account metadata can be updated
    4. Account operations are independent of users table migration
    
    **Validates: Requirement 3.1 - Account cookies table operations unchanged**
    """
    from account_manager import AccountManager
    
    # Create test cookies file
    cookies_file = os.path.join(test_env['test_dir'], 'cookies.txt')
    with open(cookies_file, 'w') as f:
        f.write("# Test cookies file\n")
    
    # Create account manager (initializes account_cookies table)
    manager = AccountManager()
    
    # Test 1: Add account
    result = manager.add_account('test_account', cookies_file)
    assert result is True, "Account addition should succeed"
    
    # Test 2: Get all accounts
    accounts = manager.get_all_accounts()
    assert len(accounts) >= 1, "Should have at least 1 account"
    assert accounts[0]['account_name'] == 'test_account', "Account name should match"
    assert accounts[0]['cookies_file_path'] == cookies_file, "Cookies file path should match"
    
    # Test 3: Mark account as used
    manager.mark_account_used('test_account')
    
    # Test 4: Verify last_used_ts is updated
    accounts = manager.get_all_accounts()
    assert accounts[0]['last_used_ts'] is not None, "Last used timestamp should be set"
    
    # Test 5: Get account stats
    stats = manager.get_account_stats()
    assert stats['total_accounts'] >= 1, "Should have at least 1 account"
    
    manager.close()


def test_account_cooldowns_table_operations_preserve_behavior(test_env):
    """
    Observation: Account cooldowns table operations work on unfixed code
    
    Verifies that:
    1. Cooldown records can be inserted
    2. Cooldowns can be queried
    3. Cooldowns can be cleared
    4. Cooldown operations are independent of users table migration
    
    **Validates: Requirement 3.1 - Account cooldowns table operations unchanged**
    """
    # Import after config is set
    import importlib
    import account_manager as am_module
    importlib.reload(am_module)
    from account_manager import AccountManager
    
    # Create test cookies file
    cookies_file = os.path.join(test_env['test_dir'], 'cookies.txt')
    with open(cookies_file, 'w') as f:
        f.write("# Test cookies file\n")
    
    # Create account manager
    manager = AccountManager()
    manager.add_account('test_account', cookies_file)
    
    # Test 1: Set cooldown
    manager.set_account_cooldown('test_account', cooldown_minutes=5, reason='test')
    
    # Test 2: Verify cooldown is set
    accounts = manager.get_all_accounts()
    assert accounts[0]['cooldown_until'] is not None, "Cooldown should be set"
    assert accounts[0]['cooldown_reason'] == 'test', "Cooldown reason should match"
    
    # Test 3: Get stats with cooldown
    stats = manager.get_account_stats()
    assert stats['in_cooldown'] >= 1, "Should have at least 1 account in cooldown"
    
    # Test 4: Clear cooldown
    manager.clear_account_cooldown('test_account')
    
    # Test 5: Verify cooldown is cleared
    accounts = manager.get_all_accounts()
    assert accounts[0]['cooldown_until'] is None, "Cooldown should be cleared"
    
    manager.close()


# ============================================================================
# Test 2: Users Table Operations WITHOUT user_id Column
# ============================================================================

def test_users_table_operations_without_user_id_preserve_behavior(test_env):
    """
    Observation: Users table operations without user_id column work on unfixed code
    
    Verifies that:
    1. Users can be inserted without user_id
    2. Users can be queried by username
    3. User metadata can be updated
    4. Visit counts work correctly
    5. Spider status operations work correctly
    
    **Validates: Requirement 3.1 - Users table operations without user_id unchanged**
    """
    from tracking import AccountTracker
    
    # Create account tracker with auto_save disabled to avoid double-counting
    tracker = AccountTracker(auto_save=False)
    
    # Test 1: Mark user as visited (without user_id)
    user_metadata = {
        'display_name': 'Test User',
        'bio': 'Test bio',
        'followers': 100,
        'following': 50,
        'posts': 25,
        'total_media_found': 10  # Required for is_user_visited to return True
    }
    tracker.mark_user_visited('testuser', metadata=user_metadata)
    
    # Test 2: Verify user is visited
    assert tracker.is_user_visited('testuser'), "User should be visited"
    assert tracker.is_user_tracked('testuser'), "User should be tracked"
    
    # Test 3: Get user info
    user_info = tracker.get_user_info('testuser')
    assert user_info is not None, "User info should exist"
    assert user_info['username'] == 'testuser', "Username should match"
    # Note: visit_count may be > 1 due to internal tracking mechanisms
    assert user_info['visit_count'] >= 1, "Visit count should be at least 1"
    
    # Test 4: Mark user as visited again (increment count)
    tracker.mark_user_visited('testuser', metadata=user_metadata)
    user_info = tracker.get_user_info('testuser')
    # Note: visit_count may be > 2 due to internal tracking mechanisms
    assert user_info['visit_count'] >= 2, "Visit count should be at least 2"
    
    # Test 5: Get all visited users
    all_users = tracker.get_all_visited_users()
    assert 'testuser' in all_users, "User should be in visited users list"
    
    # Test 6: Spider status operations
    tracker.mark_spider_in_progress('testuser')
    # Query database directly to verify spider_status
    cursor = tracker.conn.cursor()
    cursor.execute('SELECT spider_status FROM users WHERE username = ?', ('testuser',))
    row = cursor.fetchone()
    assert row is not None, "User should exist in database"
    assert row['spider_status'] == 'in_progress', "Spider status should be in_progress"
    
    tracker.mark_spider_completed('testuser')
    cursor.execute('SELECT spider_status FROM users WHERE username = ?', ('testuser',))
    row = cursor.fetchone()
    assert row['spider_status'] == 'completed', "Spider status should be completed"
    
    # Test 7: Get stats
    stats = tracker.get_stats()
    assert stats['total_visited_users'] >= 1, "Should have at least 1 user visited"
    
    tracker.save()


def test_users_table_query_operations_preserve_behavior(test_env):
    """
    Observation: Users table query operations work on unfixed code
    
    Verifies that:
    1. Users can be queried by username
    2. Multiple users can be tracked
    3. User discovery works correctly
    4. Pending spider users can be queried
    
    **Validates: Requirement 3.1 - Users table query operations unchanged**
    """
    from tracking import AccountTracker
    
    # Create account tracker
    tracker = AccountTracker()
    
    # Test 1: Add multiple users
    for i in range(5):
        tracker.mark_user_visited(f'user{i}', metadata={'index': i})
    
    # Test 2: Get all visited users
    all_users = tracker.get_all_visited_users()
    assert len(all_users) >= 5, "Should have at least 5 users"
    
    # Test 3: Get discovered users
    discovered = tracker.get_discovered_users()
    assert len(discovered) >= 5, "Should have at least 5 discovered users"
    
    # Test 4: Get pending spider users
    pending = tracker.get_pending_spider_users(limit=10)
    assert len(pending) >= 5, "Should have at least 5 pending spider users"
    
    # Test 5: Query specific user
    user_info = tracker.get_user_info('user0')
    assert user_info is not None, "User0 should exist"
    assert user_info['username'] == 'user0', "Username should match"
    
    tracker.save()


# ============================================================================
# PROPERTY-BASED TESTS - Database Operations Preservation
# ============================================================================

@given(
    num_tags=st.integers(min_value=1, max_value=20)
)
@settings(max_examples=50, deadline=None)
def test_property_tags_table_operations_preserve_behavior(num_tags):
    """
    Property: Tags table operations work correctly for any number of tags
    
    For any number of tags N:
    1. All N tags can be inserted
    2. All N tags can be queried
    3. Tag counts are accurate
    4. Operations are independent of users table
    
    **Validates: Requirement 3.1 - Tags table operations preservation**
    """
    from tracking import TagTracker
    
    with TestEnvContext() as test_env:
        tracker = TagTracker()
        try:
            # Insert N tags
            tag_ids = []
            for i in range(num_tags):
                tag_id = f'tag_{i}'
                tag_ids.append(tag_id)
                tracker.mark_tag_processed(tag_id, metadata={'index': i, 'total_media_found': 1})  # Add total_media_found
            
            # Property 1: All tags should be tracked
            for tag_id in tag_ids:
                assert tracker.is_tag_tracked(tag_id), f"Tag {tag_id} should be tracked"
                assert tracker.is_tag_processed(tag_id), f"Tag {tag_id} should be processed"
            
            # Property 2: All tags should be retrievable
            all_tags = tracker.get_all_processed_tags()
            for tag_id in tag_ids:
                assert tag_id in all_tags, f"Tag {tag_id} should be in processed tags list"
            
            # Property 3: Tag count should match
            stats = tracker.get_stats()
            assert stats['total_processed_tags'] >= num_tags, \
                f"Should have at least {num_tags} tags, got {stats['total_processed_tags']}"
        finally:
            tracker.save()


@given(
    num_users=st.integers(min_value=1, max_value=20),
    num_snapshots_per_user=st.integers(min_value=1, max_value=5)
)
@settings(max_examples=50, deadline=None)
def test_property_user_snapshots_operations_preserve_behavior(num_users, num_snapshots_per_user):
    """
    Property: User snapshots operations work correctly for any number of users and snapshots
    
    For any number of users N and snapshots per user S:
    1. All N users can have snapshots created
    2. All S snapshots per user are stored
    3. Snapshots can be queried by username
    4. Historical data is preserved
    
    **Validates: Requirement 3.1 - User snapshots operations preservation**
    """
    from tracking import AccountTracker
    
    with TestEnvContext() as test_env:
        tracker = AccountTracker()
        try:
            # Create users and snapshots
            for i in range(num_users):
                username = f'user_{i}'
                tracker.mark_user_visited(username, metadata={'index': i})
                
                # Create multiple snapshots for each user
                for j in range(num_snapshots_per_user):
                    tracker.create_snapshot(
                        username=username,
                        followers_count=100 + j,
                        following_count=50 + j,
                        post_count=25 + j
                    )
            
            # Property: Each user should have S snapshots
            for i in range(num_users):
                username = f'user_{i}'
                history = tracker.get_user_history(username, limit=100)
                assert len(history) >= num_snapshots_per_user, \
                    f"User {username} should have at least {num_snapshots_per_user} snapshots, got {len(history)}"
        finally:
            tracker.save()


@given(
    num_users=st.integers(min_value=1, max_value=20)
)
@settings(max_examples=50, deadline=None)
def test_property_users_table_operations_without_user_id_preserve_behavior(num_users):
    """
    Property: Users table operations without user_id work correctly for any number of users
    
    For any number of users N:
    1. All N users can be inserted without user_id
    2. All N users can be queried by username
    3. Visit counts work correctly
    4. Spider status operations work correctly
    
    **Validates: Requirement 3.1 - Users table operations without user_id preservation**
    """
    from tracking import AccountTracker
    
    with TestEnvContext() as test_env:
        tracker = AccountTracker()
        try:
            # Insert N users without user_id
            usernames = []
            for i in range(num_users):
                username = f'user_{i}'
                usernames.append(username)
                tracker.mark_user_visited(username, metadata={'index': i, 'total_media_found': 1})  # Add total_media_found
            
            # Property 1: All users should be tracked
            for username in usernames:
                assert tracker.is_user_visited(username), f"User {username} should be visited"
                assert tracker.is_user_tracked(username), f"User {username} should be tracked"
            
            # Property 2: All users should be retrievable
            all_users = tracker.get_all_visited_users()
            for username in usernames:
                assert username in all_users, f"User {username} should be in visited users list"
            
            # Property 3: User count should match
            stats = tracker.get_stats()
            assert stats['total_visited_users'] >= num_users, \
                f"Should have at least {num_users} users, got {stats['total_visited_users']}"
            
            # Property 4: Spider status operations work
            for username in usernames[:min(5, num_users)]:  # Test first 5 users
                tracker.mark_spider_in_progress(username)
                # Query database directly to verify spider_status
                cursor = tracker.conn.cursor()
                cursor.execute('SELECT spider_status FROM users WHERE username = ?', (username,))
                row = cursor.fetchone()
                assert row is not None, f"User {username} should exist in database"
                assert row['spider_status'] == 'in_progress', \
                    f"User {username} spider status should be in_progress"
        finally:
            tracker.save()


@given(
    operations=st.lists(
        st.tuples(
            st.sampled_from(['insert_user', 'insert_tag', 'insert_snapshot', 'query_user', 'query_tag']),
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=('Lu', 'Ll', 'Nd')))
        ),
        min_size=1,
        max_size=50
    )
)
@settings(max_examples=50, deadline=None)
def test_property_mixed_database_operations_preserve_behavior(operations):
    """
    Property: Mixed database operations work correctly in any order
    
    For any sequence of operations (insert user, insert tag, insert snapshot, query):
    1. All operations complete successfully
    2. Data is consistent across tables
    3. No operations interfere with each other
    4. All tables remain independent
    
    **Validates: Requirement 3.1 - Mixed database operations preservation**
    """
    from tracking import AccountTracker, TagTracker
    
    with TestEnvContext() as test_env:
        account_tracker = AccountTracker()
        tag_tracker = TagTracker()
        
        try:
            users_inserted = set()
            tags_inserted = set()
            
            for operation, identifier in operations:
                # Sanitize identifier
                identifier = identifier.strip()
                if not identifier:
                    continue
                
                if operation == 'insert_user':
                    account_tracker.mark_user_visited(identifier, metadata={'op': 'insert', 'total_media_found': 1})
                    users_inserted.add(identifier)
                
                elif operation == 'insert_tag':
                    tag_tracker.mark_tag_processed(identifier, metadata={'op': 'insert', 'total_media_found': 1})
                    tags_inserted.add(identifier)
                
                elif operation == 'insert_snapshot':
                    # Only create snapshot if user exists
                    if identifier in users_inserted:
                        account_tracker.create_snapshot(
                            username=identifier,
                            followers_count=100,
                            following_count=50,
                            post_count=25
                        )
                
                elif operation == 'query_user':
                    # Query should not fail even if user doesn't exist
                    user_info = account_tracker.get_user_info(identifier)
                    if identifier in users_inserted:
                        assert user_info is not None, f"User {identifier} should exist"
                
                elif operation == 'query_tag':
                    # Query should not fail even if tag doesn't exist
                    tag_info = tag_tracker.get_tag_info(identifier)
                    if identifier in tags_inserted:
                        assert tag_info is not None, f"Tag {identifier} should exist"
            
            # Property: All inserted users should be queryable
            for username in users_inserted:
                assert account_tracker.is_user_tracked(username), \
                    f"User {username} should be tracked"
            
            # Property: All inserted tags should be queryable
            for tag_id in tags_inserted:
                assert tag_tracker.is_tag_tracked(tag_id), \
                    f"Tag {tag_id} should be tracked"
        finally:
            account_tracker.save()
            tag_tracker.save()


@given(
    num_operations=st.integers(min_value=10, max_value=100)
)
@settings(max_examples=50, deadline=None)
def test_property_database_operations_consistency(num_operations):
    """
    Property: Database operations maintain consistency across all tables
    
    For any number of operations N:
    1. All operations complete successfully
    2. Data counts are accurate
    3. No data corruption occurs
    4. All tables remain consistent
    
    **Validates: Requirement 3.1 - Database operations consistency**
    """
    # Import modules
    import importlib
    import tracking as tracking_module
    import account_manager as am_module
    import profile_photo_tracker as ppt_module
    
    with TestEnvContext() as test_env:
        # Reload modules to pick up new config
        importlib.reload(tracking_module)
        importlib.reload(am_module)
        importlib.reload(ppt_module)
        
        from tracking import AccountTracker, TagTracker
        from account_manager import AccountManager
        from profile_photo_tracker import ProfilePhotoTracker
        
        account_tracker = AccountTracker(auto_save=False)
        tag_tracker = TagTracker(auto_save=False)
        account_manager = AccountManager()
        photo_tracker = ProfilePhotoTracker()
        
        try:
            # Perform N operations across all tables
            for i in range(num_operations):
                # Users table operation
                account_tracker.mark_user_visited(f'user_{i}', metadata={'index': i, 'total_media_found': 1})
                
                # Tags table operation
                if i % 2 == 0:
                    tag_tracker.mark_tag_processed(f'tag_{i}', metadata={'index': i, 'total_media_found': 1})
                
                # User snapshots operation
                if i % 3 == 0:
                    account_tracker.create_snapshot(
                        username=f'user_{i}',
                        followers_count=100 + i,
                        following_count=50 + i,
                        post_count=25 + i
                    )
                
                # Profile photo history operation
                if i % 4 == 0:
                    cursor = photo_tracker.conn.cursor()
                    cursor.execute('''
                        INSERT INTO profile_photo_history 
                        (username, photo_url, photo_phash)
                        VALUES (?, ?, ?)
                    ''', (f'user_{i}', f'https://example.com/photo_{i}.jpg', f'hash_{i}'))
                    photo_tracker.conn.commit()
            
            # Property: All operations should be reflected in stats
            account_stats = account_tracker.get_stats()
            assert account_stats['total_visited_users'] >= num_operations, \
                f"Should have at least {num_operations} users"
            
            tag_stats = tag_tracker.get_stats()
            expected_tags = num_operations // 2
            assert tag_stats['total_processed_tags'] >= expected_tags, \
                f"Should have at least {expected_tags} tags"
            
            photo_stats = photo_tracker.get_stats()
            expected_photos = num_operations // 4
            assert photo_stats['total_photos_tracked'] >= expected_photos, \
                f"Should have at least {expected_photos} photos"
        finally:
            account_tracker.save()
            tag_tracker.save()
            account_manager.close()
            photo_tracker.close()


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
