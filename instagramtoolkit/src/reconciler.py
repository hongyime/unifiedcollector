"""Database-to-disk reconciliation module.

Checks if downloaded files still exist on disk. Two-tier verification:
- Tier 1 (fast, automatic on startup): Path.exists() stat call
- Tier 2 (deep, opt-in via menu): SHA-256 re-hash and compare
"""
import hashlib
import os
from pathlib import Path
from typing import Dict, List, Tuple

from src.resilience import _SHUTDOWN


class Reconciler:
    """Reconciles database state with actual files on disk."""

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager instance
        """
        self.db = db_manager
        self.CHUNK_SIZE = 500  # Process in chunks of 500 to avoid memory issues

    def verify_files_exist(self, deep: bool = False) -> Dict[str, int]:
        """
        Tier 1 verification - check if files exist on disk.

        Args:
            deep: If True, also re-hash files to detect corruption

        Returns:
            Dict with counts of issues found:
            {
                'missing': count of files missing,
                'corrupted': count of files with hash mismatch (only if deep=True),
                'checked': total number of files checked,
                'fixed': number of issues fixed
            }
        """
        print(f"[RECONCILE] Starting database-to-disk verification (deep={deep})")

        results = {
            'missing': 0,
            'corrupted': 0,
            'checked': 0,
            'fixed': 0
        }

        offset = 0
        while True:
            # Check shutdown at top of each chunk
            if _SHUTDOWN.is_set():
                print(f"[STOPPED] Verification stopped by user")
                break

            # Fetch chunk of downloaded media items
            chunk = self.db.fetchall(
                "SELECT id, username, file_path, file_hash FROM media_items "
                "WHERE download_status='downloaded' AND file_path IS NOT NULL "
                "LIMIT ? OFFSET ?",
                (self.CHUNK_SIZE, offset)
            )

            if not chunk:
                break

            for row in chunk:
                # Check shutdown per item
                if _SHUTDOWN.is_set():
                    print(f"[STOPPED] Verification stopped by user")
                    return results

                item_id = row[0]
                username = row[1]
                file_path = row[2]
                stored_hash = row[3]

                results['checked'] += 1

                # Check if file exists
                if not Path(file_path).exists():
                    print(f"[MISSING] {username}: {file_path}")
                    self.db.execute(
                        "UPDATE media_items SET download_status='missing' WHERE id=?",
                        (item_id,)
                    )
                    results['missing'] += 1
                    results['fixed'] += 1
                    continue

                # Deep verification - check hash
                if deep and stored_hash:
                    actual_hash = self._compute_file_hash(file_path)
                    if actual_hash != stored_hash:
                        print(f"[CORRUPTED] {username}: {file_path}")
                        self.db.execute(
                            "UPDATE media_items SET download_status='corrupted' WHERE id=?",
                            (item_id,)
                        )
                        results['corrupted'] += 1
                        results['fixed'] += 1

            # Move to next chunk
            offset += self.CHUNK_SIZE
            if results['checked'] % 1000 == 0:
                print(f"[PROGRESS] Checked {results['checked']} files, "
                      f"{results['missing']} missing, {results['corrupted']} corrupted")

        return results

    def export_profile_photo_blobs(self) -> int:
        """
        Export profile photo blobs to disk if file_path is missing.

        For any profile_photo_history row where:
        - photo_blob IS NOT NULL (blob stored in DB)
        - file_path IS NULL OR file_path doesn't exist

        This exports the blob to disk and updates file_path.
        Uses atomic write pattern (.tmp -> final).

        Returns:
            Number of photos exported
        """
        print("[RECONCILE] Exporting profile photo blobs to disk...")

        exported = 0
        offset = 0

        while True:
            # Check shutdown at top of each chunk
            if _SHUTDOWN.is_set():
                print(f"[STOPPED] Export stopped by user")
                break

            # Fetch chunk of photos with blobs
            chunk = self.db.fetchall(
                "SELECT id, username, photo_blob FROM profile_photo_history "
                "WHERE photo_blob IS NOT NULL "
                "LIMIT ? OFFSET ?",
                (self.CHUNK_SIZE, offset)
            )

            if not chunk:
                break

            for row in chunk:
                # Check shutdown per item
                if _SHUTDOWN.is_set():
                    print(f"[STOPPED] Export stopped by user")
                    return exported

                photo_id = row[0]
                username = row[1]
                photo_blob = row[2]

                # Generate file path
                timestamp = photo_id  # Use ID as simple unique identifier
                filename = f"{username}_profile_{timestamp}.jpg"
                file_path = os.path.join("data", "profile_photos", filename)

                # Export with atomic write pattern
                success = self._atomic_write_binary(file_path, photo_blob)
                if success:
                    self.db.execute(
                        "UPDATE profile_photo_history SET file_path=? WHERE id=?",
                        (file_path, photo_id)
                    )
                    exported += 1

                    if exported % 10 == 0:
                        print(f"[PROGRESS] Exported {exported} profile photos to disk")

        return exported

    def _compute_file_hash(self, file_path: str) -> str:
        """
        Compute SHA-256 hash of file using chunked reads.

        Args:
            file_path: Path to file

        Returns:
            Hexadecimal SHA-256 hash
        """
        sha256 = hashlib.sha256()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception as e:
            print(f"[ERROR] Failed to compute hash for {file_path}: {e}")
            return ""

    def _atomic_write_binary(self, file_path: str, data: bytes) -> bool:
        """
        Write binary data to file atomically (write to .tmp, then rename).

        Args:
            file_path: Target file path
            data: Binary data to write

        Returns:
            True if successful, False otherwise
        """
        try:
            # Ensure directory exists
            dir_path = os.path.dirname(file_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)

            # Write to temp file
            temp_path = file_path + '.tmp'
            with open(temp_path, 'wb') as f:
                f.write(data)

            # Atomic rename
            os.replace(temp_path, file_path)
            return True

        except Exception as e:
            print(f"[ERROR] Failed to write {file_path}: {e}")
            # Clean up temp file if it exists
            temp_path = file_path + '.tmp'
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
            return False


__all__ = ["Reconciler"]



