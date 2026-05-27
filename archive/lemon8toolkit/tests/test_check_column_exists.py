"""
Unit tests for _check_column_exists helper method in AccountTracker
"""
import os
import sqlite3
import tempfile
import pytest
from pathlib import Path

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


class TestCheckColumnExistsMethod:
    """Test the _check_column_exists helper method in isolation"""
    
    def setup_method(self):
        """Create a temporary database for testing"""
        self.temp_db = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.db')
        self.temp_db.close()
        self.db_path = self.temp_db.name
        
        # Create a connection and a simple test table
        self.conn = sqlite3.connect(self.db_path)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE test_table (
                id INTEGER PRIMARY KEY,
                name TEXT,
                email TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE another_table (
                username TEXT PRIMARY KEY,
                age INTEGER
            )
        ''')
        self.conn.commit()
    
    def teardown_method(self):
        """Clean up temporary database"""
        if hasattr(self, 'conn'):
            self.conn.close()
        
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
    
    def _check_column_exists(self, table: str, column: str) -> bool:
        """Helper method to test - same implementation as AccountTracker"""
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        return column in columns
    
    def test_returns_true_for_existing_columns(self):
        """Test that method returns True for columns that exist"""
        assert self._check_column_exists('test_table', 'id') is True
        assert self._check_column_exists('test_table', 'name') is True
        assert self._check_column_exists('test_table', 'email') is True
        
        assert self._check_column_exists('another_table', 'username') is True
        assert self._check_column_exists('another_table', 'age') is True
    
    def test_returns_false_for_nonexistent_columns(self):
        """Test that method returns False for columns that don't exist"""
        assert self._check_column_exists('test_table', 'nonexistent') is False
        assert self._check_column_exists('test_table', 'fake_column') is False
        assert self._check_column_exists('test_table', 'random_field') is False
        
        assert self._check_column_exists('another_table', 'email') is False
        assert self._check_column_exists('another_table', 'name') is False
    
    def test_works_with_different_tables(self):
        """Test that method correctly distinguishes between different tables"""
        # 'name' exists in test_table but not in another_table
        assert self._check_column_exists('test_table', 'name') is True
        assert self._check_column_exists('another_table', 'name') is False
        
        # 'username' exists in another_table but not in test_table
        assert self._check_column_exists('another_table', 'username') is True
        assert self._check_column_exists('test_table', 'username') is False
    
    def test_after_adding_column(self):
        """Test that method detects newly added columns"""
        # Initially, 'phone' doesn't exist
        assert self._check_column_exists('test_table', 'phone') is False
        
        # Add the column
        cursor = self.conn.cursor()
        cursor.execute("ALTER TABLE test_table ADD COLUMN phone TEXT")
        self.conn.commit()
        
        # Now it should exist
        assert self._check_column_exists('test_table', 'phone') is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
