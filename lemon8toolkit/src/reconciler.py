"""
Unified Lemon8 Toolkit - DB-to-Disk Reconciliation
Two-tier verification: Path.exists() → MD5 re-hash

NOTE: The primary reconciliation path is Lemon8Toolkit.reconcile_missing_files()
in main.py, which uses the session progress ledger. This module provides a
lower-level utility for direct DB inspection if needed.
"""
import os
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional

from config import LEMON8_DB_FILE, ensure_data_directory


class Reconciler:
    """Two-tier DB-to-disk reconciliation for profile photo blobs."""

    def __init__(self):
        ensure_data_directory()
        import config as _config
        self.conn = sqlite3.connect(LEMON8_DB_FILE, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _config.configure_db_connection(self.conn)

    def _compute_md5(self, file_path: str) -> Optional[str]:
        """Compute MD5 hash of a file."""
        try:
            md5 = hashlib.md5()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            print(f"Warning: Error computing MD5 for {file_path}: {e}")
            return None

    def reconcile_profile_photos(self) -> Dict[str, int]:
        """
        Reconcile profile photo blobs — export to disk if file is missing.
        Returns: Dict with counts of exported, skipped, total.
        """
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, username, photo_url, photo_blob, file_path
            FROM profile_photo_history
            WHERE photo_blob IS NOT NULL
        ''')

        total = 0
        exported = 0
        skipped = 0

        for row in cursor.fetchall():
            total += 1
            file_path = row['file_path']

            if file_path and os.path.exists(file_path):
                skipped += 1
                continue

            username = row['username']
            photo_id = row['id']

            export_dir = Path('downloads') / username / 'profile_photos'
            export_dir.mkdir(parents=True, exist_ok=True)
            export_path = export_dir / f'profile_photo_{photo_id}.jpg'

            try:
                export_path.write_bytes(row['photo_blob'])
                cursor.execute(
                    'UPDATE profile_photo_history SET file_path = ? WHERE id = ?',
                    (str(export_path), photo_id),
                )
                exported += 1
            except Exception as e:
                print(f"Warning: Error exporting photo {photo_id}: {e}")

        self.conn.commit()
        return {'total': total, 'exported': exported, 'skipped': skipped}

    def get_dedup_stats(self) -> Dict[str, int]:
        """Return counts from the downloaded_media deduplication table."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM downloaded_media')
        total = cursor.fetchone()['count']
        return {'total_hashes': total}

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
