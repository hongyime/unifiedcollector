"""
Unified Lemon8 Toolkit - Multi-Account Cookie Management
"""
import os
import sqlite3
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime

from config import LEMON8_DB_FILE, ensure_data_directory


class AccountManager:
    """Manage multiple account cookies for rotation"""
    
    def __init__(self):
        ensure_data_directory()
        import config as _config
        self.conn = sqlite3.connect(LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _config.configure_db_connection(self.conn)
        self._init_tables()
    
    def _init_tables(self):
        """Initialize account management tables"""
        cursor = self.conn.cursor()
        
        # Account cookies pool
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS account_cookies (
                account_name TEXT PRIMARY KEY,
                cookies_file_path TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                last_used_ts TEXT,
                added_ts TEXT DEFAULT (datetime('now'))
            )
        ''')
        
        # Account cooldowns
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS account_cooldowns (
                account_name TEXT PRIMARY KEY,
                until_ts TEXT NOT NULL,
                reason TEXT DEFAULT 'rate-limit',
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        
        self.conn.commit()
    
    def add_account(self, account_name: str, cookies_file_path: str) -> bool:
        """
        Add a new account to the pool
        
        Args:
            account_name: Unique account identifier
            cookies_file_path: Path to cookies.txt file
            
        Returns:
            True if added successfully, False otherwise
        """
        # Verify file exists
        if not os.path.exists(cookies_file_path):
            print(f"❌ Cookies file not found: {cookies_file_path}")
            return False
        
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO account_cookies (account_name, cookies_file_path)
                VALUES (?, ?)
            ''', (account_name, cookies_file_path))
            self.conn.commit()
            print(f"✅ Added account: {account_name}")
            return True
        except sqlite3.IntegrityError:
            print(f"⚠️ Account {account_name} already exists")
            return False
    
    def remove_account(self, account_name: str) -> bool:
        """Remove an account from the pool"""
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM account_cookies WHERE account_name = ?', (account_name,))
        cursor.execute('DELETE FROM account_cooldowns WHERE account_name = ?', (account_name,))
        self.conn.commit()
        
        if cursor.rowcount > 0:
            print(f"✅ Removed account: {account_name}")
            return True
        else:
            print(f"⚠️ Account not found: {account_name}")
            return False
    
    def get_available_account(self) -> Optional[Dict[str, Any]]:
        """
        Get next available account (not in cooldown)
        
        Returns:
            Dict with account info, or None if no accounts available
        """
        cursor = self.conn.cursor()
        
        # Get active accounts not in cooldown
        cursor.execute('''
            SELECT ac.* FROM account_cookies ac
            LEFT JOIN account_cooldowns cd ON ac.account_name = cd.account_name
            WHERE ac.is_active = 1
            AND (cd.until_ts IS NULL OR datetime(cd.until_ts) < datetime('now'))
            ORDER BY ac.last_used_ts ASC NULLS FIRST
            LIMIT 1
        ''')
        
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    
    def mark_account_used(self, account_name: str):
        """Mark account as recently used"""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE account_cookies 
            SET last_used_ts = datetime('now')
            WHERE account_name = ?
        ''', (account_name,))
        self.conn.commit()
    
    def set_account_cooldown(
        self,
        account_name: str,
        cooldown_minutes: int = 5,
        reason: str = 'rate-limit'
    ):
        """
        Put account in cooldown
        
        Args:
            account_name: Account to cooldown
            cooldown_minutes: Duration in minutes
            reason: Reason for cooldown
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO account_cooldowns (account_name, until_ts, reason)
            VALUES (?, datetime('now', '+' || ? || ' minutes'), ?)
        ''', (account_name, cooldown_minutes, reason))
        self.conn.commit()
        print(f"⏳ Account {account_name} in cooldown for {cooldown_minutes} minutes ({reason})")
    
    def clear_account_cooldown(self, account_name: str):
        """Clear cooldown for an account"""
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM account_cooldowns WHERE account_name = ?', (account_name,))
        self.conn.commit()
        print(f"✅ Cleared cooldown for {account_name}")
    
    def get_all_accounts(self) -> List[Dict[str, Any]]:
        """Get all accounts with their status"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                ac.*,
                cd.until_ts as cooldown_until,
                cd.reason as cooldown_reason
            FROM account_cookies ac
            LEFT JOIN account_cooldowns cd ON ac.account_name = cd.account_name
            ORDER BY ac.added_ts
        ''')
        return [dict(row) for row in cursor.fetchall()]
    
    def get_account_stats(self) -> Dict[str, int]:
        """Get account pool statistics"""
        cursor = self.conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM account_cookies WHERE is_active = 1')
        active_count = cursor.fetchone()['count']
        
        cursor.execute('''
            SELECT COUNT(*) as count FROM account_cooldowns 
            WHERE datetime(until_ts) > datetime('now')
        ''')
        cooldown_count = cursor.fetchone()['count']
        
        return {
            'total_accounts': active_count,
            'in_cooldown': cooldown_count,
            'available': active_count - cooldown_count
        }
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
