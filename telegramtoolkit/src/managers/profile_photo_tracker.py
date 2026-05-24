"""Profile photo change detection for Telegram using perceptual hashing."""
import os
import io
import imagehash
from PIL import Image
from pathlib import Path
from src.core.resilience import wait_for_internet


class ProfilePhotoTracker:
    def __init__(self, state_manager):
        self.state = state_manager
        self.max_blob_size_mb = int(os.environ.get('PROFILE_PHOTO_BLOB_MAX_SIZE_MB', 5000))

    async def check_for_change(self, client, entity_id, entity_type, current_file_id, shutdown=None):
        """
        Detect if profile photo has genuinely changed.
        Returns: (changed: bool, phash: str or None)

        Stage 1: file_id check (fast, no download needed)
        Stage 2: pHash comparison (only when file_id differs)
        """
        # Stage 1: file_id check (Telegram recycles file_ids on CDN rotation)
        cursor = self.state.conn.execute(
            "SELECT file_id, photo_phash FROM profile_photo_history"
            " WHERE entity_id=? AND entity_type=? ORDER BY detected_at DESC LIMIT 1",
            (entity_id, entity_type)
        )
        latest = cursor.fetchone()

        if latest and latest[0] == current_file_id:
            return False, None  # No change, skip entirely

        # Stage 2: pHash comparison (only when file_id changed)
        if shutdown and shutdown.requested:
            return False, None

        if not wait_for_internet(shutdown):
            return False, None

        try:
            photo_bytes = await client.download_media(current_file_id, file=bytes)
            if photo_bytes is None:
                return False, None

            img = Image.open(io.BytesIO(photo_bytes))
            new_phash = str(imagehash.phash(img))

            if latest and latest[1]:
                stored_phash = latest[1]
                distance = imagehash.hex_to_hash(new_phash) - imagehash.hex_to_hash(stored_phash)

                if distance <= 10:
                    # CDN file_id rotation only — update file_id without storing new blob
                    self.state.conn.execute(
                        "UPDATE profile_photo_history SET file_id=?"
                        " WHERE entity_id=? AND entity_type=?"
                        " AND detected_at=(SELECT MAX(detected_at) FROM profile_photo_history"
                        " WHERE entity_id=? AND entity_type=?)",
                        (current_file_id, entity_id, entity_type, entity_id, entity_type)
                    )
                    self.state.conn.commit()
                    return False, new_phash

            # Genuine change or first-time seen — store blob
            self._store_photo(entity_id, entity_type, current_file_id, new_phash, photo_bytes)
            return True, new_phash

        except Exception as e:
            print(f"[ERROR] Failed to download photo for {entity_type} {entity_id}: {e}")
            return False, None

    def _store_photo(self, entity_id, entity_type, file_id, phash, photo_bytes):
        """Store photo in DB with blob if under the configured size limit."""
        db_path = self.state.conn.execute("PRAGMA database_list").fetchone()[2]
        db_size_mb = os.path.getsize(db_path) / (1024 * 1024)

        if db_size_mb > self.max_blob_size_mb:
            print(
                f"[WARNING] DB size {db_size_mb:.1f}MB exceeds limit {self.max_blob_size_mb}MB."
                " Skipping blob storage."
            )
            self.state.conn.execute(
                "INSERT INTO profile_photo_history (entity_id, entity_type, file_id, photo_phash)"
                " VALUES (?, ?, ?, ?)",
                (entity_id, entity_type, file_id, phash)
            )
        else:
            self.state.conn.execute(
                "INSERT INTO profile_photo_history"
                " (entity_id, entity_type, file_id, photo_phash, photo_blob)"
                " VALUES (?, ?, ?, ?, ?)",
                (entity_id, entity_type, file_id, phash, photo_bytes)
            )

        self.state.conn.commit()
        print(f"[PHOTO CHANGE] {entity_type} {entity_id}: new photo stored (pHash: {phash})")
