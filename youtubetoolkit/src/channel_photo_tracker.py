"""Channel profile photo change detection using pHash."""
import os
import sqlite3
import requests
from pathlib import Path
from typing import Optional
import imagehash
from PIL import Image
from io import BytesIO


class ChannelPhotoTracker:
    """Tracks channel profile photo changes using perceptual hashing."""
    
    def __init__(self, db_path: str, blob_max_size_mb: int = 5000):
        self.db_path = db_path
        self.blob_max_size_mb = blob_max_size_mb
    
    def check_and_update_photo(self, channel_id: str, new_photo_url: str) -> bool:
        """Check if profile photo changed and update if needed.
        
        Two-stage detection:
        1. Compare URL - if same, skip
        2. If URL changed, download and compare pHash
        
        Returns:
            True if photo changed (genuine change), False otherwise
        """
        with sqlite3.connect(self.db_path) as conn:
            # Get current photo URL and pHash
            row = conn.execute('''
                SELECT profile_pic_url, profile_pic_phash 
                FROM channels 
                WHERE channel_id = ?
            ''', (channel_id,)).fetchone()
            
            if not row:
                # Channel not in DB yet
                return False
            
            current_url, current_phash = row
            
            # Stage 1: URL comparison
            if current_url == new_photo_url:
                # URL unchanged, no need to download
                return False
            
            # Stage 2: Download and compare pHash
            try:
                response = requests.get(new_photo_url, timeout=30)
                response.raise_for_status()
                
                # Compute pHash
                img = Image.open(BytesIO(response.content))
                new_phash = str(imagehash.phash(img))
                
                # Compare pHash (Hamming distance)
                if current_phash:
                    old_hash = imagehash.hex_to_hash(current_phash)
                    new_hash = imagehash.hex_to_hash(new_phash)
                    distance = old_hash - new_hash
                    
                    if distance <= 10:
                        # CDN rotation, not a genuine change
                        # Update URL only, don't store blob
                        conn.execute('''
                            UPDATE channels 
                            SET profile_pic_url = ? 
                            WHERE channel_id = ?
                        ''', (new_photo_url, channel_id))
                        conn.commit()
                        print(f"[PHOTO] CDN rotation for {channel_id} (distance={distance})")
                        return False
                
                # Genuine change - store blob and history
                self._store_photo_change(
                    conn, channel_id, new_photo_url, 
                    new_phash, response.content
                )
                
                # Update channels table
                conn.execute('''
                    UPDATE channels 
                    SET profile_pic_url = ?, profile_pic_phash = ?
                    WHERE channel_id = ?
                ''', (new_photo_url, new_phash, channel_id))
                
                conn.commit()
                print(f"[PHOTO] Genuine change detected for {channel_id}")
                return True
                
            except Exception as e:
                print(f"[ERROR] Failed to check photo for {channel_id}: {e}")
                return False
    
    def _store_photo_change(self, conn, channel_id: str, photo_url: str, 
                           photo_phash: str, photo_data: bytes):
        """Store photo change in history table.
        
        Checks DB size before storing blob. If over threshold, stores file path only.
        """
        # Check DB size
        db_size_mb = Path(self.db_path).stat().st_size / (1024 * 1024)
        
        if db_size_mb < self.blob_max_size_mb:
            # Store blob in DB
            conn.execute('''
                INSERT INTO channel_photo_history 
                (channel_id, photo_url, photo_phash, photo_blob)
                VALUES (?, ?, ?, ?)
            ''', (channel_id, photo_url, photo_phash, photo_data))
            print(f"[PHOTO] Stored blob in DB (DB size: {db_size_mb:.1f}MB)")
        else:
            # DB too large, save to file instead
            photo_dir = Path('downloads') / 'profile_photos' / channel_id
            photo_dir.mkdir(parents=True, exist_ok=True)
            
            # Use pHash as filename
            file_path = photo_dir / f"{photo_phash}.jpg"
            
            # Atomic write
            tmp_path = file_path.with_suffix('.tmp')
            tmp_path.write_bytes(photo_data)
            tmp_path.replace(file_path)
            
            conn.execute('''
                INSERT INTO channel_photo_history 
                (channel_id, photo_url, photo_phash, file_path)
                VALUES (?, ?, ?, ?)
            ''', (channel_id, photo_url, photo_phash, str(file_path)))
            
            print(f"[PHOTO] DB over {self.blob_max_size_mb}MB, saved to file: {file_path}")
    
    def get_photo_history(self, channel_id: str) -> list:
        """Get profile photo change history for a channel."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT photo_url, photo_phash, detected_at, file_path
                FROM channel_photo_history
                WHERE channel_id = ?
                ORDER BY detected_at DESC
            ''', (channel_id,)).fetchall()
            
            return [dict(row) for row in rows]
