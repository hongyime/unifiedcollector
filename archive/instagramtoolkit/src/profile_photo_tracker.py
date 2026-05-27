"""Profile photo change detection using perceptual hashing."""
import os
import io
import pathlib
from typing import Optional, Tuple

import requests
from src.PIL import Image
import imagehash

from src.resilience import _SHUTDOWN, wait_for_internet


class ProfilePhotoTracker:
    """Detects genuine profile photo changes using two-stage detection.

    Stage 1 - URL check (fast, zero download cost):
      If URL hasn't changed, skip entirely (no change)

    Stage 2 - pHash comparison (only when URL changed):
      Download new photo, compute pHash, compare with stored pHash.
      If Hamming distance > 10: genuine change detected.
      If Hamming distance <= 10: CDN rotation only (update URL, skip blob).

    The database (profile_photo_history table) is the source of truth.
    """
    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager instance with execute/fetchone methods
        """
        self.db = db_manager
        # Default 5GB limit for storing photo blobs in DB
        self.max_blob_size_mb = int(os.environ.get('PROFILE_PHOTO_BLOB_MAX_SIZE_MB', 5000))

    def check_for_change(self, username: str, new_photo_url: str) -> Tuple[bool, Optional[str]]:
        """
        Detect if profile photo has genuinely changed.

        Args:
            username: Instagram username
            new_photo_url: New profile photo URL from Instagram

        Returns:
            Tuple of (changed: bool, phash: str or None)
            - changed=True: genuine photo change detected
            - changed=False: no change or CDN rotation only
            - phash: latest pHash (if computed), None otherwise
        """
        # Stage 1: URL check (fast, zero download cost)
        stored_url = self._get_latest_photo_url(username)
        if stored_url == new_photo_url:
            return False, None  # No change at all, skip entirely

        # Stage 2: pHash comparison (only when URL changed)
        if _SHUTDOWN.is_set():
            return False, None

        # Download new photo with internet retry
        photo_bytes = self._download_photo(new_photo_url)
        if photo_bytes is None:
            return False, None

        # Compute pHash
        try:
            img = Image.open(io.BytesIO(photo_bytes))
            new_phash = str(imagehash.phash(img))
        except Exception as e:
            print(f"[ERROR] Failed to compute pHash for {username}: {e}")
            return False, None

        # Get stored phash
        stored_phash = self._get_latest_phash(username)
        if stored_phash is None:
            # First time seeing this user's photo
            self._store_photo(username, new_photo_url, new_phash, photo_bytes)
            return True, new_phash

        # Compute Hamming distance
        try:
            distance = imagehash.hex_to_hash(new_phash) - imagehash.hex_to_hash(stored_phash)
        except Exception as e:
            print(f"[ERROR] Failed to compare hashes for {username}: {e}")
            return False, None

        if distance > 10:
            # Genuine change detected
            self._store_photo(username, new_photo_url, new_phash, photo_bytes)
            print(f"[PHOTO CHANGE] {username}: profile photo changed (distance={distance})")
            return True, new_phash
        else:
            # CDN rotation only - update URL but don't store blob
            self._update_url_only(username, new_photo_url)
            print(f"[CDN ROTATION] {username}: cache refresh only (distance={distance})")
            return False, new_phash

    def _get_latest_photo_url(self, username: str) -> Optional[str]:
        """Get the most recent photo URL for this username."""
        try:
            row = self.db.fetchone(
                "SELECT photo_url FROM profile_photo_history WHERE username=? ORDER BY detected_at DESC LIMIT 1",
                (username,)
            )
            return row[0] if row else None
        except Exception as e:
            print(f"[ERROR] Failed to get photo URL for {username}: {e}")
            return None

    def _get_latest_phash(self, username: str) -> Optional[str]:
        """Get the most recent pHash for this username."""
        try:
            row = self.db.fetchone(
                "SELECT photo_phash FROM profile_photo_history WHERE username=? ORDER BY detected_at DESC LIMIT 1",
                (username,)
            )
            return row[0] if row else None
        except Exception as e:
            print(f"[ERROR] Failed to get pHash for {username}: {e}")
            return None

    def _download_photo(self, url: str) -> Optional[bytes]:
        """
        Download photo bytes with internet retry.

        Args:
            url: Photo URL to download

        Returns:
            Photo bytes or None on failure
        """
        try:
            # Wait for internet if needed
            if not wait_for_internet():
                return None

            # Check shutdown before download
            if _SHUTDOWN.is_set():
                return None

            response = requests.get(url, timeout=10, stream=True)
            response.raise_for_status()

            # Download with streaming to avoid loading full file into memory
            buffer = io.BytesIO()
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    buffer.write(chunk)
            return buffer.getvalue()

        except Exception as e:
            print(f"[ERROR] Failed to download photo from {url}: {e}")
            return None

    def _store_photo(self, username: str, photo_url: str, phash: str, photo_bytes: bytes) -> None:
        """
        Store photo in DB with blob (if under size limit).

        Args:
            username: Instagram username
            photo_url: Photo URL
            phash: Perceptual hash
            photo_bytes: Photo bytes
        """
        try:
            # Check DB size before storing blob
            db_file = self.db.db_path if hasattr(self.db, 'db_path') else None
            if db_file:
                try:
                    db_size_mb = os.path.getsize(db_file) / (1024 * 1024)
                    if db_size_mb > self.max_blob_size_mb:
                        print(f"[WARNING] DB size {db_size_mb:.1f}MB exceeds limit {self.max_blob_size_mb}MB.")
                        print(f"[WARNING] Skipping blob storage for {username} photo.")
                        # Store without blob
                        self.db.execute(
                            "INSERT INTO profile_photo_history (username, photo_url, photo_phash, detected_at) "
                            "VALUES (?, ?, ?, unixepoch())",
                            (username, photo_url, phash)
                        )
                        return
                except Exception as e:
                    print(f"[WARNING] Could not check DB size: {e}. Proceeding with blob storage.")

            # Store with blob
            self.db.execute(
                "INSERT INTO profile_photo_history (username, photo_url, photo_phash, photo_blob, detected_at) "
                "VALUES (?, ?, ?, ?, unixepoch())",
                (username, photo_url, phash, photo_bytes)
            )

            print(f"[PHOTO STORED] {username}: new photo tracked (pHash: {phash[:8]}...)")

        except Exception as e:
            print(f"[ERROR] Failed to store photo for {username}: {e}")

    def _update_url_only(self, username: str, new_url: str) -> None:
        """
        Update URL without storing blob (CDN rotation only).

        Args:
            username: Instagram username
            new_url: New photo URL (same photo, different CDN URL)
        """
        try:
            self.db.execute(
                "UPDATE profile_photo_history SET photo_url=? "
                "WHERE username=? AND detected_at=(SELECT MAX(detected_at) FROM profile_photo_history WHERE username=?)",
                (new_url, username, username)
            )
        except Exception as e:
            print(f"[ERROR] Failed to update URL for {username}: {e}")


__all__ = ["ProfilePhotoTracker"]


