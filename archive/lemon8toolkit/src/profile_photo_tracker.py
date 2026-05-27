"""
Unified Lemon8 Toolkit - Profile Photo Change Tracking
Two-stage detection: URL check first, pHash comparison only if URL changed
"""
import sqlite3
import hashlib
import requests
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from io import BytesIO

try:
    import imagehash
    from PIL import Image
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False
    print("⚠️ imagehash not available - profile photo change detection disabled")

from config import LEMON8_DB_FILE, ensure_data_directory


class ProfilePhotoTracker:
    """Track profile photo changes using two-stage detection (URL → pHash)"""
    
    def __init__(self):
        ensure_data_directory()
        import config as _config
        self.conn = sqlite3.connect(LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _config.configure_db_connection(self.conn)
        self._init_table()
    
    def _init_table(self):
        """Initialize profile_photo_history table"""
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS profile_photo_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                user_id TEXT,
                photo_url TEXT NOT NULL,
                photo_phash TEXT NOT NULL,
                photo_blob BLOB,
                file_path TEXT,
                detected_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (username) REFERENCES users(username) ON DELETE RESTRICT
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_photo_history_username 
            ON profile_photo_history(username, detected_at DESC)
        ''')
        self.conn.commit()
    
    def _compute_phash(self, image_data: bytes) -> Optional[str]:
        """Compute perceptual hash of image data"""
        if not IMAGEHASH_AVAILABLE:
            return None
        
        try:
            image = Image.open(BytesIO(image_data))
            # Convert to RGB if needed
            if image.mode != 'RGB':
                image = image.convert('RGB')
            # Compute pHash
            phash = imagehash.phash(image)
            return str(phash)
        except Exception as e:
            print(f"⚠️ Error computing pHash: {e}")
            return None
    
    def _download_image(self, url: str, session: Optional[requests.Session] = None) -> Optional[bytes]:
        """Download image from URL"""
        try:
            if session:
                response = session.get(url, timeout=30)
            else:
                response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            print(f"⚠️ Error downloading image from {url}: {e}")
            return None
    
    def _get_latest_photo(self, username: str) -> Optional[Dict[str, Any]]:
        """Get the most recent profile photo record for a user"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM profile_photo_history 
            WHERE username = ? 
            ORDER BY detected_at DESC 
            LIMIT 1
        ''', (username,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    
    def check_and_track_photo(
        self,
        username: str,
        photo_url: str,
        user_id: Optional[str] = None,
        session: Optional[requests.Session] = None,
        store_blob: bool = True,
        file_path: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if profile photo has changed and track it.
        
        Two-stage detection:
        1. URL check: If URL is same as last known, no change
        2. pHash check: If URL changed, download and compare pHash
        
        Args:
            username: User's username
            photo_url: Current profile photo URL
            user_id: Optional user_id
            session: Optional requests session for downloading
            store_blob: Whether to store photo blob in DB (default: True)
            file_path: Optional file path if photo was downloaded to disk
        
        Returns:
            Tuple of (changed: bool, phash: Optional[str])
        """
        if not IMAGEHASH_AVAILABLE:
            return (False, None)
        
        username = username.lstrip('@').lower()
        
        # Stage 1: URL check
        latest_photo = self._get_latest_photo(username)
        if latest_photo and latest_photo['photo_url'] == photo_url:
            # URL hasn't changed, no need to check pHash
            return (False, latest_photo['photo_phash'])
        
        # Stage 2: pHash check (URL changed or first time)
        # Download image
        image_data = self._download_image(photo_url, session)
        if not image_data:
            return (False, None)
        
        # Compute pHash
        phash = self._compute_phash(image_data)
        if not phash:
            return (False, None)
        
        # Check if pHash is different from latest
        changed = True
        if latest_photo:
            latest_phash = latest_photo['photo_phash']
            if latest_phash == phash:
                # pHash is same, URL just rotated (CDN rotation)
                changed = False
            else:
                # pHash is different, genuine photo change
                changed = True
        
        # Store new record if changed or first time
        if changed or not latest_photo:
            cursor = self.conn.cursor()
            blob_data = image_data if store_blob else None
            cursor.execute('''
                INSERT INTO profile_photo_history 
                (username, user_id, photo_url, photo_phash, photo_blob, file_path)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (username, user_id, photo_url, phash, blob_data, file_path))
            self.conn.commit()
            
            if changed and latest_photo:
                print(f"📸 Profile photo changed for @{username} (pHash: {phash})")
            elif not latest_photo:
                print(f"📸 First profile photo tracked for @{username} (pHash: {phash})")
        
        return (changed, phash)
    
    def get_photo_history(self, username: str, limit: int = 10) -> list:
        """Get profile photo change history for a user"""
        username = username.lstrip('@').lower()
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, username, user_id, photo_url, photo_phash, file_path, detected_at
            FROM profile_photo_history 
            WHERE username = ? 
            ORDER BY detected_at DESC 
            LIMIT ?
        ''', (username, limit))
        return [dict(row) for row in cursor.fetchall()]
    
    def export_photo_blob(self, photo_id: int, output_path: str) -> bool:
        """Export a stored photo blob to disk"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT photo_blob, photo_url FROM profile_photo_history WHERE id = ?
        ''', (photo_id,))
        row = cursor.fetchone()
        
        if not row or not row['photo_blob']:
            return False
        
        try:
            output_file = Path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(row['photo_blob'])
            print(f"✅ Exported photo blob to {output_path}")
            return True
        except Exception as e:
            print(f"❌ Error exporting photo blob: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get profile photo tracking statistics"""
        cursor = self.conn.cursor()
        
        # Total photos tracked
        cursor.execute('SELECT COUNT(*) as count FROM profile_photo_history')
        total_photos = cursor.fetchone()['count']
        
        # Unique users tracked
        cursor.execute('SELECT COUNT(DISTINCT username) as count FROM profile_photo_history')
        unique_users = cursor.fetchone()['count']
        
        # Photos with blobs
        cursor.execute('SELECT COUNT(*) as count FROM profile_photo_history WHERE photo_blob IS NOT NULL')
        photos_with_blobs = cursor.fetchone()['count']
        
        # Total blob size
        cursor.execute('SELECT SUM(LENGTH(photo_blob)) as size FROM profile_photo_history WHERE photo_blob IS NOT NULL')
        total_blob_size = cursor.fetchone()['size'] or 0
        
        return {
            'total_photos_tracked': total_photos,
            'unique_users_tracked': unique_users,
            'photos_with_blobs': photos_with_blobs,
            'total_blob_size_bytes': total_blob_size,
            'total_blob_size_mb': round(total_blob_size / (1024 * 1024), 2),
        }
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
