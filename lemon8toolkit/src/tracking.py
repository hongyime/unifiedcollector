"""
Unified Lemon8 Toolkit - Account and Tag Tracking
"""
import json
import os
import sqlite3
from datetime import datetime
from typing import Set, Dict, List, Optional, Any

import config


def _atomic_json_write(path: str, data: Any) -> None:
    """Write JSON to a .tmp file then atomically rename to path."""
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class AccountTracker:
    def __init__(self, auto_save: bool = True):
        config.ensure_data_directory()
        self.visited_users: Dict[str, Dict[str, Any]] = {}
        self.auto_save = auto_save
        self._init_sqlite()
        self._sync_sqlite_and_json()
    
    def _check_column_exists(self, table: str, column: str) -> bool:
        """Check if a column exists in a table using PRAGMA table_info"""
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        return column in columns
    
    def _init_sqlite(self):
        """Initialize SQLite database and table"""
        self.conn = sqlite3.connect(config.LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        config.configure_db_connection(self.conn)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                user_id TEXT,
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
        # Add user_id column if it doesn't exist (migration for existing databases)
        if not self._check_column_exists('users', 'user_id'):
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN user_id TEXT")
                self.conn.commit()
                if self._check_column_exists('users', 'user_id'):
                    print("✅ Migration: user_id column added to users table")
                else:
                    print("❌ Migration failed: user_id column was not added — check DB permissions")
            except sqlite3.OperationalError as e:
                print(f"❌ Migration error (user_id): {e}")

        # Add spider_status column if it doesn't exist (migration for existing databases)
        if not self._check_column_exists('users', 'spider_status'):
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN spider_status TEXT DEFAULT 'pending'")
                self.conn.commit()
                if self._check_column_exists('users', 'spider_status'):
                    print("✅ Migration: spider_status column added to users table")
                else:
                    print("❌ Migration failed: spider_status column was not added — check DB permissions")
            except sqlite3.OperationalError as e:
                print(f"❌ Migration error (spider_status): {e}")
        
        # Create index on spider_status
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_users_spider_status ON users(spider_status)
        ''')
        
        # Create user_snapshots table for historical tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                user_id TEXT,
                followers_count INTEGER DEFAULT 0,
                following_count INTEGER DEFAULT 0,
                post_count INTEGER DEFAULT 0,
                snapshot_ts TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE RESTRICT
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_snapshots_username 
            ON user_snapshots(username, snapshot_ts DESC)
        ''')
        self.conn.commit()

    def _sync_sqlite_and_json(self):
        """Sync SQLite and JSON on startup"""
        # Load from JSON
        json_users = {}
        if os.path.exists(config.VISITED_USERS_FILE):
            try:
                with open(config.VISITED_USERS_FILE, 'r', encoding='utf-8') as f:
                    json_users = json.load(f)
            except Exception as e:
                print(f"⚠️ Error loading visited users file: {e}")

        # Load from SQLite
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users")
        sqlite_users = {row['username']: dict(row) for row in cursor.fetchall()}
        
        # Merge: If something is in JSON but not in SQLite, add it to SQLite
        merged_count = 0
        for username, data in json_users.items():
            if username not in sqlite_users:
                # Add to SQLite
                metadata = data.copy()
                first_visited = metadata.pop('first_visited', datetime.now().isoformat())
                last_visited = metadata.pop('last_visited', datetime.now().isoformat())
                visit_count = metadata.pop('visit_count', 1)
                total_media = metadata.pop('total_media_found', 0)
                related_users = metadata.pop('related_users_found', 0)
                tags = metadata.pop('tags_found', 0)
                
                cursor.execute('''
                    INSERT INTO users (username, first_visited, last_visited, visit_count, 
                                     total_media_found, related_users_found, tags_found, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (username, first_visited, last_visited, visit_count, 
                      total_media, related_users, tags, json.dumps(metadata)))
                merged_count += 1
        
        if merged_count > 0:
            self.conn.commit()
            print(f"💾 Merged {merged_count} users from JSON to SQLite")

        # Now update JSON to match SQLite
        cursor.execute("SELECT * FROM users")
        rows = cursor.fetchall()
        for row in rows:
            username = row['username']
            data = dict(row)
            # Unpack metadata JSON
            metadata_str = data.pop('metadata', '{}')
            try:
                metadata = json.loads(metadata_str)
                data.update(metadata)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"⚠️ Malformed metadata for user {username}: {e}")
            self.visited_users[username] = data
            
        self._save_visited_users() # Ensure JSON is up to date

    def _save_visited_users(self):
        """Save visited users to file (JSON backup)"""
        try:
            _atomic_json_write(config.VISITED_USERS_FILE, self.visited_users)
        except (OSError, TypeError) as e:
            print(f"⚠️ Error saving visited users file: {e}")
    
    def is_user_visited(self, username: str) -> bool:
        """Check if user has already been fully visited/scraped"""
        username = username.lstrip('@').lower()
        if username in self.visited_users:
            # Check if it was actually visited (has media count)
            return self.visited_users[username].get('total_media_found', 0) > 0
        return False

    def is_user_tracked(self, username: str) -> bool:
        """Check if user exists in tracking database (either visited or discovered)"""
        username = username.lstrip('@').lower()
        return username in self.visited_users

    def mark_user_visited(self, username: str, metadata: Optional[Dict[str, Any]] = None):
        """Mark user as visited with optional metadata in both SQLite and JSON"""
        username = username.lstrip('@').lower()
        
        now = datetime.now().isoformat()
        visit_count = 1
        first_visited = now
        user_id = None
        
        if username in self.visited_users:
            existing = self.visited_users[username]
            first_visited = existing.get('first_visited', now)
            visit_count = existing.get('visit_count', 0) + 1
            user_id = existing.get('user_id')
        
        # Prepare data for SQLite
        total_media = 0
        related_users = 0
        tags = 0
        extra_metadata = {}
        
        if metadata:
            extra_metadata = metadata.copy()
            total_media = extra_metadata.pop('total_media_found', 0)
            related_users = extra_metadata.pop('related_users_found', 0)
            tags = extra_metadata.pop('tags_found', 0)
            # Extract user_id if provided in metadata
            if 'user_id' in extra_metadata:
                user_id = extra_metadata.pop('user_id')
            
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (username, user_id, first_visited, last_visited, visit_count, 
                                        total_media_found, related_users_found, tags_found, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (username, user_id, first_visited, now, visit_count, 
              total_media, related_users, tags, json.dumps(extra_metadata)))
        self.conn.commit()
        
        # Update local cache for JSON backup
        visit_data = {
            'username': username,
            'user_id': user_id,
            'first_visited': first_visited,
            'last_visited': now,
            'visit_count': visit_count,
            'total_media_found': total_media,
            'related_users_found': related_users,
            'tags_found': tags
        }
        visit_data.update(extra_metadata)
        self.visited_users[username] = visit_data
        
        if self.auto_save:
            self.save()
            
    def save(self):
        """Force save visited users to JSON backup"""
        self._save_visited_users()

    def get_discovered_users(self) -> List[str]:
        """Get users discovered from feed but not yet visited"""
        discovered = []
        for username, data in self.visited_users.items():
            # If visit count is low or total media found is 0, it was probably just discovered
            if data.get('total_media_found', 0) == 0:
                discovered.append(username)
        return discovered
    
    def get_user_info(self, username: str) -> Optional[Dict[str, Any]]:
        """Get stored info about a user"""
        username = username.lstrip('@').lower()
        return self.visited_users.get(username)
    
    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get stored info about a user by user_id"""
        for user_data in self.visited_users.values():
            if user_data.get('user_id') == user_id:
                return user_data
        return None
    
    def resolve_username_from_id(self, user_id: str) -> Optional[str]:
        """Resolve username from user_id"""
        user_data = self.get_user_by_id(user_id)
        if user_data:
            return user_data.get('username')
        return None
    
    def create_snapshot(
        self,
        username: str,
        user_id: Optional[str] = None,
        followers_count: int = 0,
        following_count: int = 0,
        post_count: int = 0
    ):
        """Create a historical snapshot of user's follower/following/post counts"""
        username = username.lstrip('@').lower()
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO user_snapshots (username, user_id, followers_count, following_count, post_count)
            VALUES (?, ?, ?, ?, ?)
        ''', (username, user_id, followers_count, following_count, post_count))
        self.conn.commit()
    
    def get_user_history(self, username: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get historical snapshots for a user"""
        username = username.lstrip('@').lower()
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM user_snapshots 
            WHERE username = ? 
            ORDER BY snapshot_ts DESC 
            LIMIT ?
        ''', (username, limit))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_pending_spider_users(self, limit: int = 100) -> List[str]:
        """Get users pending spider (spider_status='pending')"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT username FROM users 
            WHERE spider_status = 'pending' 
            ORDER BY first_visited ASC 
            LIMIT ?
        ''', (limit,))
        return [row['username'] for row in cursor.fetchall()]
    
    def mark_spider_in_progress(self, username: str):
        """Mark user spider as in progress"""
        username = username.lstrip('@').lower()
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users SET spider_status = 'in_progress' 
            WHERE username = ?
        ''', (username,))
        self.conn.commit()
    
    def mark_spider_completed(self, username: str):
        """Mark user spider as completed"""
        username = username.lstrip('@').lower()
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users SET spider_status = 'completed' 
            WHERE username = ?
        ''', (username,))
        self.conn.commit()
    
    def reset_stuck_spiders(self) -> int:
        """Reset any stuck 'in_progress' spiders back to 'pending' (for crash recovery)"""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE users SET spider_status = 'pending' 
            WHERE spider_status = 'in_progress'
        ''')
        self.conn.commit()
        count = cursor.rowcount
        if count > 0:
            print(f"🔄 Reset {count} stuck spider(s) from 'in_progress' to 'pending'")
        return count
    
    def get_all_visited_users(self) -> List[str]:
        """Get list of all visited usernames"""
        return list(self.visited_users.keys())
    
    def get_stats(self) -> Dict:
        """Get account tracking statistics"""
        return {
            'total_visited_users': len(self.visited_users),
            'tracking_file': config.VISITED_USERS_FILE,
            'tracking_file_exists': os.path.exists(config.VISITED_USERS_FILE)
        }
    
    def clear_visited_users(self):
        """Clear visited users history"""
        self.visited_users.clear()
        self._save_visited_users()
        print("🗑️ Visited users history cleared")


class TagTracker:
    def __init__(self, auto_save: bool = True):
        config.ensure_data_directory()
        self.processed_tags: Dict[str, Dict[str, Any]] = {}
        self.auto_save = auto_save
        self._init_sqlite()
        self._sync_sqlite_and_json()
    
    def _init_sqlite(self):
        """Initialize SQLite database and table"""
        self.conn = sqlite3.connect(config.LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        config.configure_db_connection(self.conn)
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                tag_id TEXT PRIMARY KEY,
                first_processed TEXT,
                last_processed TEXT,
                process_count INTEGER,
                total_media_found INTEGER,
                related_users_found INTEGER,
                related_tags_found INTEGER,
                metadata TEXT
            )
        ''')
        self.conn.commit()

    def _sync_sqlite_and_json(self):
        """Sync SQLite and JSON on startup"""
        # Load from JSON
        json_tags = {}
        if os.path.exists(config.PROCESSED_TAGS_FILE):
            try:
                with open(config.PROCESSED_TAGS_FILE, 'r', encoding='utf-8') as f:
                    json_tags = json.load(f)
            except Exception as e:
                print(f"⚠️ Error loading processed tags file: {e}")

        # Load from SQLite
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM tags")
        sqlite_tags = {row['tag_id']: dict(row) for row in cursor.fetchall()}
        
        # Merge: If something is in JSON but not in SQLite, add it to SQLite
        merged_count = 0
        for tag_id, data in json_tags.items():
            if tag_id not in sqlite_tags:
                # Add to SQLite
                metadata = data.copy()
                first_processed = metadata.pop('first_processed', datetime.now().isoformat())
                last_processed = metadata.pop('last_processed', datetime.now().isoformat())
                process_count = metadata.pop('process_count', 1)
                total_media = metadata.pop('total_media_found', 0)
                related_users = metadata.pop('related_users_found', 0)
                related_tags = metadata.pop('related_tags_found', 0)
                
                cursor.execute('''
                    INSERT INTO tags (tag_id, first_processed, last_processed, process_count, 
                                    total_media_found, related_users_found, related_tags_found, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (tag_id, first_processed, last_processed, process_count, 
                      total_media, related_users, related_tags, json.dumps(metadata)))
                merged_count += 1
        
        if merged_count > 0:
            self.conn.commit()
            print(f"💾 Merged {merged_count} tags from JSON to SQLite")

        # Now update JSON to match SQLite
        cursor.execute("SELECT * FROM tags")
        rows = cursor.fetchall()
        for row in rows:
            tag_id = row['tag_id']
            data = dict(row)
            # Unpack metadata JSON
            metadata_str = data.pop('metadata', '{}')
            try:
                metadata = json.loads(metadata_str)
                data.update(metadata)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"⚠️ Malformed metadata for tag {tag_id}: {e}")
            self.processed_tags[tag_id] = data
            
        self._save_processed_tags()

    def _save_processed_tags(self):
        """Save processed tags to file (JSON backup)"""
        try:
            _atomic_json_write(config.PROCESSED_TAGS_FILE, self.processed_tags)
        except (OSError, TypeError) as e:
            print(f"⚠️ Error saving processed tags file: {e}")
    
    def is_tag_processed(self, tag_id: str) -> bool:
        """Check if tag has already been fully processed/scraped"""
        tag_id = str(tag_id)
        if tag_id in self.processed_tags:
            return self.processed_tags[tag_id].get('total_media_found', 0) > 0
        return False

    def is_tag_tracked(self, tag_id: str) -> bool:
        """Check if tag exists in tracking database (either processed or discovered)"""
        tag_id = str(tag_id)
        return tag_id in self.processed_tags
    
    def mark_tag_processed(self, tag_id: str, metadata: Optional[Dict[str, Any]] = None):
        """Mark tag as processed with optional metadata in both SQLite and JSON"""
        tag_id = str(tag_id)
        
        now = datetime.now().isoformat()
        process_count = 1
        first_processed = now
        
        if tag_id in self.processed_tags:
            existing = self.processed_tags[tag_id]
            first_processed = existing.get('first_processed', now)
            process_count = existing.get('process_count', 0) + 1
        
        # Prepare data for SQLite
        total_media = 0
        related_users = 0
        related_tags = 0
        extra_metadata = {}
        
        if metadata:
            extra_metadata = metadata.copy()
            total_media = extra_metadata.pop('total_media_found', 0)
            related_users = extra_metadata.pop('related_users_found', 0)
            related_tags = extra_metadata.pop('related_tags_found', 0)
            
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tags (tag_id, first_processed, last_processed, process_count, 
                                       total_media_found, related_users_found, related_tags_found, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (tag_id, first_processed, now, process_count, 
              total_media, related_users, related_tags, json.dumps(extra_metadata)))
        self.conn.commit()
        
        # Update local cache for JSON backup
        process_data = {
            'tag_id': tag_id,
            'first_processed': first_processed,
            'last_processed': now,
            'process_count': process_count,
            'total_media_found': total_media,
            'related_users_found': related_users,
            'related_tags_found': related_tags
        }
        process_data.update(extra_metadata)
        self.processed_tags[tag_id] = process_data
        
        if self.auto_save:
            self.save()
            
    def save(self):
        """Force save processed tags to JSON backup"""
        self._save_processed_tags()
    
    def get_tag_info(self, tag_id: str) -> Optional[Dict[str, Any]]:
        """Get stored info about a tag"""
        return self.processed_tags.get(str(tag_id))
    
    def get_all_processed_tags(self) -> List[str]:
        """Get list of all processed tag IDs"""
        return list(self.processed_tags.keys())
    
    def get_stats(self) -> Dict:
        """Get tag tracking statistics"""
        return {
            'total_processed_tags': len(self.processed_tags),
            'tracking_file': config.PROCESSED_TAGS_FILE,
            'tracking_file_exists': os.path.exists(config.PROCESSED_TAGS_FILE)
        }
    
    def clear_processed_tags(self):
        """Clear processed tags history"""
        self.processed_tags.clear()
        self._save_processed_tags()
        print("🗑️ Processed tags history cleared")


class UnifiedTracker:
    """Combined tracker for both accounts and tags"""
    
    def __init__(self, auto_save: bool = True):
        self.account_tracker = AccountTracker(auto_save=auto_save)
        self.tag_tracker = TagTracker(auto_save=auto_save)
    
    def save(self):
        """Save both trackers"""
        self.account_tracker.save()
        self.tag_tracker.save()
    
    def get_combined_stats(self) -> Dict:
        """Get combined statistics from both trackers"""
        account_stats = self.account_tracker.get_stats()
        tag_stats = self.tag_tracker.get_stats()
        
        return {
            'accounts': account_stats,
            'tags': tag_stats,
            'total_tracked_items': account_stats['total_visited_users'] + tag_stats['total_processed_tags']
        }
    
    def clear_all_tracking(self):
        """Clear all tracking data"""
        self.account_tracker.clear_visited_users()
        self.tag_tracker.clear_processed_tags()
        print("🗑️ All tracking data cleared")