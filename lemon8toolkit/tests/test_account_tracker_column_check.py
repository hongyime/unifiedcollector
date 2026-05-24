"""
Integration test for _check_column_exists in AccountTracker class
"""
import os
import sqlite3
import tempfile
import pytest
from pathlib import Path

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from tracking import AccountTracker


class TestAccountTrackerColumnCheck:
    """Test _check_column_exists method in the actual AccountTracker class"""
    
    def setup_method(self):
        """Create a temporary database for testing"""
        self.temp_db = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.db')
        self.temp_db.close()
        self.db_path = self.temp_db.name

        import config
        self.original_db_path = config.LEMON8_DB_FILE
        self.original_visited = config.VISITED_USERS_FILE
        config.LEMON8_DB_FILE = self.db_path
        config.VISITED_USERS_FILE = self.db_path + "_users.json"  # empty; prevents real data merge
    
    def teardown_method(self):
        """Clean up temporary database"""
        import gc
        import config
        config.LEMON8_DB_FILE = self.original_db_path
        config.VISITED_USERS_FILE = self.original_visited
        gc.collect()  # flush SQLite connections before file deletion (Windows file lock)
        if os.path.exists(self.db_path):
            try:
                os.unlink(self.db_path)
            except OSError:
                pass  # Windows may still hold the lock; OS cleans up on process exit
    
    def test_check_column_exists_on_users_table(self):
        """Test that _check_column_exists works on the users table"""
        tracker = AccountTracker(auto_save=False)
        
        # These columns should always exist in the users table
        assert tracker._check_column_exists('users', 'username') is True
        assert tracker._check_column_exists('users', 'first_visited') is True
        assert tracker._check_column_exists('users', 'last_visited') is True
        assert tracker._check_column_exists('users', 'visit_count') is True
        assert tracker._check_column_exists('users', 'metadata') is True
        
        # These columns should not exist
        assert tracker._check_column_exists('users', 'nonexistent_column') is False
        assert tracker._check_column_exists('users', 'fake_field') is False
    
    def test_check_column_exists_on_user_snapshots_table(self):
        """Test that _check_column_exists works on the user_snapshots table"""
        tracker = AccountTracker(auto_save=False)
        
        # These columns should exist in user_snapshots
        assert tracker._check_column_exists('user_snapshots', 'id') is True
        assert tracker._check_column_exists('user_snapshots', 'username') is True
        assert tracker._check_column_exists('user_snapshots', 'followers_count') is True
        assert tracker._check_column_exists('user_snapshots', 'following_count') is True
        assert tracker._check_column_exists('user_snapshots', 'post_count') is True
        
        # These should not exist
        assert tracker._check_column_exists('user_snapshots', 'nonexistent') is False
    
    def test_check_column_exists_distinguishes_tables(self):
        """Test that _check_column_exists correctly distinguishes between tables"""
        tracker = AccountTracker(auto_save=False)
        
        # 'username' exists in both tables
        assert tracker._check_column_exists('users', 'username') is True
        assert tracker._check_column_exists('user_snapshots', 'username') is True
        
        # 'id' exists only in user_snapshots
        assert tracker._check_column_exists('user_snapshots', 'id') is True
        assert tracker._check_column_exists('users', 'id') is False
        
        # 'metadata' exists only in users
        assert tracker._check_column_exists('users', 'metadata') is True
        assert tracker._check_column_exists('user_snapshots', 'metadata') is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
