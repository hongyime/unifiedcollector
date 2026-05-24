"""Media items repository for tracking downloaded media."""
import hashlib
from typing import Dict, List, Optional


class MediaItemRepository:
    """Repository for media_items table operations."""

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager instance
        """
        self.db = db_manager

    def add_media_item(
        self,
        username: str,
        user_id: str,
        shortcode: str,
        media_type: str,
        media_url: str,
        file_path: str,
        file_hash: str,
        file_size: int,
        taken_at: Optional[int],
        downloaded_at: int,
        download_status: str = 'downloaded'
    ) -> int:
        """
        Insert or ignore a media item.

        Args:
            username: Instagram username
            user_id: Instagram user ID (profile.userid)
            shortcode: Media shortcode/post ID
            media_type: 'post', 'story', 'highlight', 'profile_photo'
            media_url: Source URL
            file_path: Local file path
            file_hash: SHA-256 hash
            file_size: File size in bytes
            taken_at: Unix timestamp when media was taken
            downloaded_at: Unix timestamp when downloaded
            download_status: 'downloaded', 'missing', 'corrupted'

        Returns:
            Row ID of inserted or existing item
        """
        try:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO media_items
                (username, user_id, shortcode, media_type, media_url, file_path,
                 file_hash, file_size, taken_at, downloaded_at, download_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username,
                    str(user_id),  # Store as TEXT
                    shortcode,
                    media_type,
                    media_url,
                    file_path,
                    file_hash,
                    file_size,
                    taken_at,
                    downloaded_at,
                    download_status,
                )
            )
            return cursor.lastrowid
        except Exception as e:
            print(f"[ERROR] Failed to insert media item for {username}: {e}")
            return -1

    def get_media_by_shortcode(self, shortcode: str) -> Optional[Dict]:
        """Get media item by shortcode."""
        rows = self.db.fetchall(
            "SELECT * FROM media_items WHERE shortcode=?",
            (shortcode,)
        )
        return rows[0] if rows else None

    def get_media_by_user(self, username: str, media_type: Optional[str] = None) -> List[Dict]:
        """
        Get all media items for a user.

        Args:
            username: Instagram username
            media_type: Optional filter by type
        """
        if media_type:
            rows = self.db.fetchall(
                "SELECT * FROM media_items WHERE username=? AND media_type=? ORDER BY downloaded_at DESC",
                (username, media_type)
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM media_items WHERE username=? ORDER BY downloaded_at DESC",
                (username,)
            )
        return rows

    def mark_missing(self, media_id: int):
        """Mark a media item as missing."""
        self.db.execute(
            "UPDATE media_items SET download_status='missing' WHERE id=?",
            (media_id,)
        )

    def mark_corrupted(self, media_id: int):
        """Mark a media item as corrupted (hash mismatch)."""
        self.db.execute(
            "UPDATE media_items SET download_status='corrupted' WHERE id=?",
            (media_id,)
        )

    def update_file_hash(self, media_id: int, new_hash: str):
        """Update file hash for a media item."""
        self.db.execute(
            "UPDATE media_items SET file_hash=? WHERE id=?",
            (new_hash, media_id)
        )

    def get_stats(self, username: Optional[str] = None) -> Dict[str, int]:
        """
        Get media item statistics.

        Args:
            username: Optional filter by username

        Returns:
            Dict with counts by status
        """
        if username:
            rows = self.db.fetchall(
                """SELECT download_status, COUNT(*) as count
                FROM media_items
                WHERE username=?
                GROUP BY download_status""",
                (username,)
            )
        else:
            rows = self.db.fetchall(
                """SELECT download_status, COUNT(*) as count
                FROM media_items
                GROUP BY download_status"""
            )

        stats = {
            'downloaded': 0,
            'missing': 0,
            'corrupted': 0
        }
        for row in rows:
            status = row['download_status']
            count = row['count']
            if status in stats:
                stats[status] = count

        return stats

    @staticmethod
    def compute_sha256_hash(file_path: str, chunk_size: int = 8192) -> str:
        """
        Compute SHA-256 hash of file using chunked reads.

        Args:
            file_path: Path to file
            chunk_size: Read chunk size in bytes

        Returns:
            Hexadecimal SHA-256 hash
        """
        sha256 = hashlib.sha256()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(chunk_size), b''):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception as e:
            print(f"[ERROR] Failed to compute hash for {file_path}: {e}")
            return ""

    @staticmethod
    def get_file_size(file_path: str) -> int:
        """Get file size in bytes."""
        try:
            return os.path.getsize(file_path)
        except Exception as e:
            print(f"[ERROR] Failed to get file size for {file_path}: {e}")
            return 0


__all__ = ["MediaItemRepository"]


