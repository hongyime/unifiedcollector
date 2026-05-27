"""Profile photo tracker with pHash and blob storage."""
import os
import hashlib
import io
from pathlib import Path
from typing import Optional
import aiosqlite
import aiohttp

try:
    import imagehash
    from PIL import Image
    PHASH_AVAILABLE = True
except ImportError:
    PHASH_AVAILABLE = False
    print("⚠️  imagehash not available. Install with: pip install imagehash Pillow")

from src.config import Config


class ProfilePhotoTracker:
    """Tracks profile photo changes with pHash and blob storage."""
    
    def __init__(self, db_path: Path = Config.DB_PATH):
        """Initialize photo tracker.
        
        Args:
            db_path: Path to database
        """
        self.db_path = db_path
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        """Async context manager entry."""
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={'User-Agent': 'GitHub-Toolkit/2.0'}
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()
        return False
    
    def _get_md5(self, data: bytes) -> str:
        """Get MD5 hash of data."""
        return hashlib.md5(data).hexdigest()
    
    def _get_phash(self, data: bytes) -> Optional[str]:
        """Get perceptual hash of image.
        
        Args:
            data: Image bytes
            
        Returns:
            pHash string or None if unavailable
        """
        if not PHASH_AVAILABLE:
            return None
        
        try:
            img = Image.open(io.BytesIO(data))
            return str(imagehash.phash(img))
        except Exception:
            return None
    
    async def track_photo_change(self, username: str, user_id: int, avatar_url: str) -> bool:
        """Track profile photo change for a user.
        
        Args:
            username: GitHub username
            user_id: GitHub user ID
            avatar_url: Current avatar URL
            
        Returns:
            True if photo changed (new entry created)
        """
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            # Stage 1: URL check (fast, zero download cost)
            cursor = await db.execute("""
                SELECT avatar_url, avatar_phash FROM profile_photo_history
                WHERE username=?
                ORDER BY detected_at DESC
                LIMIT 1
            """, (username,))
            latest = await cursor.fetchone()
            
            if latest and latest[0] == avatar_url:
                # No change
                return False
            
            # Stage 2: Download and pHash comparison
            try:
                async with self.session.get(avatar_url) as response:
                    if response.status != 200:
                        return False
                    
                    photo_data = await response.read()
                    md5_hash = self._get_md5(photo_data)
                    phash = self._get_phash(photo_data)
                    
                    # Compare pHash if available
                    if phash and latest and latest[1]:
                        if PHASH_AVAILABLE:
                            distance = imagehash.hex_to_hash(phash) - imagehash.hex_to_hash(latest[1])
                            if distance <= 10:
                                # CDN rotation only - update URL
                                await db.execute("""
                                    UPDATE profile_photo_history
                                    SET avatar_url=?
                                    WHERE username=? AND detected_at=(
                                        SELECT MAX(detected_at) FROM profile_photo_history WHERE username=?
                                    )
                                """, (avatar_url, username, username))
                                await db.commit()
                                return False
                    
                    # Genuine change - store blob
                    db_size_mb = os.path.getsize(self.db_path) / (1024 * 1024)
                    max_mb = Config.PROFILE_PHOTO_BLOB_MAX_SIZE_MB
                    
                    if db_size_mb > max_mb:
                        # Store without blob
                        await db.execute("""
                            INSERT INTO profile_photo_history
                            (username, user_id, avatar_url, avatar_md5, avatar_phash)
                            VALUES (?, ?, ?, ?, ?)
                        """, (username, user_id, avatar_url, md5_hash, phash))
                    else:
                        # Store with blob
                        await db.execute("""
                            INSERT INTO profile_photo_history
                            (username, user_id, avatar_url, avatar_md5, avatar_phash, avatar_blob)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (username, user_id, avatar_url, md5_hash, phash, photo_data))
                    
                    await db.commit()
                    print(f"📸 Photo change detected: {username} (pHash: {phash})")
                    return True
            
            except Exception as e:
                print(f"❌ Failed to track photo for {username}: {e}")
                return False
    
    async def get_photo_history(self, username: str) -> list:
        """Get photo change history for a user.
        
        Args:
            username: GitHub username
            
        Returns:
            List of photo history records
        """
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            cursor = await db.execute("""
                SELECT id, avatar_url, avatar_md5, avatar_phash, detected_at
                FROM profile_photo_history
                WHERE username=?
                ORDER BY detected_at DESC
            """, (username,))
            
            rows = await cursor.fetchall()
            return [
                {
                    'id': row[0],
                    'url': row[1],
                    'md5': row[2],
                    'phash': row[3],
                    'detected_at': row[4]
                }
                for row in rows
            ]
