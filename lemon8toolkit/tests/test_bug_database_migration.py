"""
Bug Condition Exploration Test - Database Migration Failure

This test demonstrates the bug where the user_id column migration fails silently
or crashes when existing databases lack the column.

EXPECTED OUTCOME ON UNFIXED CODE: Test FAILS with sqlite3.OperationalError
This failure confirms the bug exists.

After the fix is implemented, this test should PASS, confirming the bug is fixed.
"""
import os
import sqlite3
import tempfile
import shutil
from pathlib import Path
import sys

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tracking import AccountTracker


def test_database_migration_failure():
    """
    Test that demonstrates the database migration bug.
    
    Bug Condition: When an existing database without the user_id column is used
    AND a user record with user_id value is inserted using AccountTracker
    THEN the system crashes with sqlite3.OperationalError
    
    Expected Behavior (after fix):
    - Migration detects missing column
    - Column is added successfully
    - Column existence is verified
    - Operation completes without errors
    
    **Validates: Requirements 1.1, 1.2, 1.3 (Bug Condition)**
    **Validates: Requirements 2.1, 2.2, 2.3 (Expected Behavior)**
    """
    # Create a temporary directory for test database
    test_dir = tempfile.mkdtemp(prefix="lemon8_test_")
    
    try:
        # Set up test database path
        test_db_path = os.path.join(test_dir, "lemon8_toolkit.db")
        
        # Create a database WITHOUT the user_id column (simulating old database)
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        # Create users table WITHOUT user_id column (old schema)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                first_visited TEXT,
                last_visited TEXT,
                visit_count INTEGER,
                total_media_found INTEGER,
                related_users_found INTEGER,
                tags_found INTEGER,
                spider_status TEXT DEFAULT 'pending',
                metadata TEXT
            )
        ''')
        conn.commit()
        
        # Verify user_id column does NOT exist
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        assert 'user_id' not in columns, "Test setup failed: user_id column should not exist yet"
        
        conn.close()
        
        # Now temporarily override the config to use our test database
        import config
        original_db_file = config.LEMON8_DB_FILE
        original_data_dir = config.DATA_DIR
        
        config.LEMON8_DB_FILE = test_db_path
        config.DATA_DIR = test_dir
        config.VISITED_USERS_FILE = os.path.join(test_dir, "visited_users.json")
        
        # Create AccountTracker - this should trigger migration
        tracker = AccountTracker(auto_save=False)
        
        # Try to mark a user visited with user_id metadata
        # This is where the bug manifests - it should crash on unfixed code
        tracker.mark_user_visited(
            username="testuser",
            metadata={
                'user_id': '123456789',
                'total_media_found': 10,
                'related_users_found': 5,
                'tags_found': 3
            }
        )
        
        # Verify the operation succeeded
        user_info = tracker.get_user_info("testuser")
        assert user_info is not None, "User should be tracked after marking visited"
        assert user_info.get('user_id') == '123456789', "User ID should be stored correctly"
        
        # Verify the column exists in the database
        cursor = tracker.conn.cursor()
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        assert 'user_id' in columns, "Migration should have added user_id column"
        
        # Verify we can query by user_id
        cursor.execute("SELECT username FROM users WHERE user_id = ?", ('123456789',))
        result = cursor.fetchone()
        assert result is not None, "Should be able to query by user_id"
        assert result[0] == 'testuser', "Query by user_id should return correct username"
        
        # Test that we can resolve username from user_id
        resolved_username = tracker.resolve_username_from_id('123456789')
        assert resolved_username == 'testuser', "Should be able to resolve username from user_id"
        
        tracker.conn.close()
        
        # Restore original config
        config.LEMON8_DB_FILE = original_db_file
        config.DATA_DIR = original_data_dir
        config.VISITED_USERS_FILE = f"{original_data_dir}/visited_users.json"
        
        print("✅ Test PASSED: Database migration succeeded, column exists, operations complete")
        
    finally:
        # Clean up test directory
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    test_database_migration_failure()
