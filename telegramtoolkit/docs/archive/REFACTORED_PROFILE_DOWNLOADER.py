#!/usr/bin/env python3
"""
Proposed refactored ProfilePhotoDownloader using DB instead of CSV + JSON

This shows how to:
1. Query users directly from DB
2. Store download results back in DB
3. Remove CSV and JSON tracking dependencies
4. Get true durability from SQLite WAL
"""

import asyncio
from pathlib import Path
from toolkit.core.state_manager import get_state_manager

class RefactoredProfilePhotoDownloader:
    """DB-first design, no CSV, no JSON tracking"""
    
    def __init__(self, save_path, parallel_processor=None):
        """Initialize without CSV file path"""
        self.save_path = save_path
        self.parallel_processor = parallel_processor
        self.state = get_state_manager()
        
        # Everything else comes from DB
        Path(self.save_path).mkdir(parents=True, exist_ok=True)
        
        # First time setup: ensure tracking columns exist
        self._ensure_profile_tracking_columns()
    
    def _ensure_profile_tracking_columns(self):
        """Add profile photo tracking columns if they don't exist"""
        try:
            conn = self.state.conn
            
            # Get current columns
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = {row['name'] for row in cursor}
            
            # Add missing columns
            if 'profile_photo_downloaded' not in columns:
                conn.execute('''
                    ALTER TABLE users ADD COLUMN profile_photo_downloaded INTEGER DEFAULT 0
                ''')
                print("✅ Added profile_photo_downloaded column")
            
            if 'profile_photo_last_checked' not in columns:
                conn.execute('''
                    ALTER TABLE users ADD COLUMN profile_photo_last_checked TIMESTAMP
                ''')
                print("✅ Added profile_photo_last_checked column")
            
            if 'profile_photo_count' not in columns:
                conn.execute('''
                    ALTER TABLE users ADD COLUMN profile_photo_count INTEGER DEFAULT 0
                ''')
                print("✅ Added profile_photo_count column")
            
            conn.commit()
        except Exception as e:
            print(f"⚠️ Column check error: {e}")
    
    def load_users_from_db(self, filter_already_downloaded=True):
        """
        Stream users from DB (memory efficient, always in sync)
        
        Instead of:
            with open("data/Users.csv") as f:
                users = csv.DictReader(f)
        
        Now:
            for user in self.load_users_from_db():
                process_user(user)
        """
        query = "SELECT user_id, username, first_name, last_name FROM users"
        
        if filter_already_downloaded:
            query += " WHERE profile_photo_downloaded = 0"
        
        return self.state.conn.execute(query)
    
    def on_profile_photo_download_success(self, user_id: int, photo_count: int, photo_path: str = None):
        """
        Save download results to DB (ATOMIC, DURABLE)
        
        This replaces:
            - Writing to JSON files
            - Maintaining separate tracking sets
            - Manual save() calls
        
        With a single, safe DB update.
        """
        try:
            cursor = self.state.conn.execute(
                '''UPDATE users 
                   SET 
                       profile_photo_downloaded = 1,
                       profile_photo_last_checked = CURRENT_TIMESTAMP,
                       profile_photo_count = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?''',
                (photo_count, user_id)
            )
            self.state.conn.commit()
            print(f"✅ Saved: User {user_id} - {photo_count} photos downloaded")
        except Exception as e:
            print(f"❌ Error saving download for user {user_id}: {e}")
    
    def on_profile_photo_download_failed(self, user_id: int, reason: str = "unknown"):
        """Track failed attempts in DB (for resume capability)"""
        try:
            # Record the attempt
            self.state.conn.execute(
                '''UPDATE users 
                   SET profile_photo_last_checked = CURRENT_TIMESTAMP
                   WHERE user_id = ?''',
                (user_id,)
            )
            
            # Optionally save to user_history for audit trail
            self.state.save_user_history_event(
                user_id=user_id,
                event_type="profile_download_failed",
                event_data={"reason": reason}
            )
            
            self.state.conn.commit()
            print(f"⚠️ Marked: User {user_id} download failed ({reason})")
        except Exception as e:
            print(f"❌ Error marking failure for user {user_id}: {e}")
    
    def get_download_stats(self) -> dict:
        """Query DB for resume/status info (not possible with JSON!)"""
        stats = {}
        
        try:
            # Total users
            cursor = self.state.conn.execute("SELECT COUNT(*) as count FROM users")
            stats['total_users'] = cursor.fetchone()['count']
            
            # Already downloaded
            cursor = self.state.conn.execute(
                "SELECT COUNT(*) as count FROM users WHERE profile_photo_downloaded = 1"
            )
            stats['downloaded'] = cursor.fetchone()['count']
            
            # Pending
            stats['pending'] = stats['total_users'] - stats['downloaded']
            
            # Average photos per user
            cursor = self.state.conn.execute(
                "SELECT AVG(profile_photo_count) as avg FROM users WHERE profile_photo_downloaded = 1"
            )
            stats['avg_photos_per_user'] = cursor.fetchone()['avg'] or 0
            
            return stats
        except Exception as e:
            print(f"⚠️ Error getting stats: {e}")
            return {}
    
    def resume_download(self):
        """
        Resume capability: Get users that failed/weren't checked recently
        
        With JSON tracking, this is hard because you must parse the entire file.
        With DB, it's a simple query.
        """
        # Users never checked (is_null)
        query1 = "SELECT * FROM users WHERE profile_photo_last_checked IS NULL LIMIT 100"
        
        # Users checked > 48 hours ago
        query2 = '''
            SELECT * FROM users 
            WHERE profile_photo_last_checked < datetime('now', '-48 hours')
            LIMIT 100
        '''
        
        # Combine to get resume list
        cursor = self.state.conn.execute(
            '''SELECT * FROM users 
               WHERE (profile_photo_last_checked IS NULL 
                      OR profile_photo_last_checked < datetime('now', '-48 hours'))
               ORDER BY profile_photo_last_checked ASC
               LIMIT 100'''
        )
        return cursor
    
    async def download_all_profiles(self, accounts_list, max_concurrent=3):
        """
        Refactored main download loop using DB
        
        Key changes:
        1. Stream users from DB (not load CSV)
        2. Save results to DB (not JSON)
        3. Get resume capability for free
        """
        stats = self.get_download_stats()
        print(f"📊 Status: {stats['downloaded']}/{stats['total_users']} users processed")
        print(f"   Pending: {stats['pending']} users")
        
        # Loop through users (streamed from DB, not loaded into memory)
        for user_row in self.load_users_from_db(filter_already_downloaded=True):
            user_id = user_row['user_id']
            username = user_row['username']
            
            print(f"Processing user {user_id} ({username})")
            
            try:
                # Download photos (pseudocode)
                photo_count = await self._download_user_photos(user_id, accounts_list)
                
                # SAVE RESULTS TO DB (atomic, durable)
                self.on_profile_photo_download_success(
                    user_id=user_id,
                    photo_count=photo_count,
                    photo_path=f"downloads/profiles/{user_id}"
                )
                
            except Exception as e:
                self.on_profile_photo_download_failed(user_id, reason=str(e))
        
        print("✅ Download complete (all results safely in database)")
    
    async def _download_user_photos(self, user_id: int, accounts_list):
        """Pseudocode for actual download logic"""
        # ... download implementation ...
        return 5  # example: downloaded 5 photos


# ============================================================================
# USAGE COMPARISON
# ============================================================================

if __name__ == "__main__":
    """
    OLD WAY (CSV + JSON):
    =====================
    
    1. Add to __init__:
        self.csv_file_path = csv_file_path  # Need this param
        
    2. Load users:
        with open(self.csv_file_path) as f:
            for row in csv.DictReader(f):
                # Load entire CSV into memory
                
    3. Track downloads:
        self.downloaded_photos = set()
        
    4. Save results:
        self.downloaded_photos.add(photo_id)
        # Must call save_downloaded_photos() manually
        atomic_json_write(self.profile_photos_file, list(self.downloaded_photos))
        
    5. Resume?
        # Must load JSON, parse it, compare with current run
        # Very error-prone
    
    6. Query "Which users have photos?"
        # Impossible without loading entire JSON
    
    
    NEW WAY (DB-ONLY):
    ==================
    
    1. No CSV parameter needed
        downloader = RefactoredProfilePhotoDownloader(save_path)
        
    2. Load users (streaming):
        for user in downloader.load_users_from_db():
            # Efficient, always in sync, can filter
            
    3. Track downloads (automatic):
        # UPDATE statement atomically saves to DB
        downloader.on_profile_photo_download_success(user_id=123, photo_count=5)
        
    4. Resume?
        cursor = downloader.resume_download()
        # Just query DB, no file parsing
        
    5. Query "Which users have photos?"
        SELECT * FROM users WHERE profile_photo_downloaded=1
        # Done!
    """
    print(__doc__)
