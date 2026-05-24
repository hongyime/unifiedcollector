"""Two-tier disk-sync verification for media items and profile photo blobs."""
import os
from pathlib import Path
from src.core.state_manager import get_state_manager
from src.core.resilience import chunked_file_hash


class Reconciler:
    CHUNK_SIZE = 500

    def __init__(self, state_manager=None):
        self.state = state_manager or get_state_manager()

    def tier1_fast_check(self, shutdown=None):
        """Tier 1: existence check. Runs on startup. Marks missing files for re-download."""
        offset = 0
        marked_missing = 0
        while True:
            if shutdown and shutdown.requested:
                break
            rows = self.state.conn.execute(
                "SELECT id, file_path FROM media_items"
                " WHERE download_status='downloaded' AND file_path IS NOT NULL"
                " LIMIT ? OFFSET ?",
                (self.CHUNK_SIZE, offset)
            ).fetchall()
            if not rows:
                break
            for row in rows:
                if not Path(row['file_path']).exists():
                    self.state.conn.execute(
                        "UPDATE media_items SET download_status='missing' WHERE id=?",
                        (row['id'],)
                    )
                    marked_missing += 1
            self.state.conn.commit()
            del rows
            offset += self.CHUNK_SIZE
        if marked_missing:
            print(f"[RECONCILE] Tier 1: {marked_missing} missing files marked for re-download.")

    def tier2_deep_check(self, shutdown=None):
        """Tier 2: SHA-256 re-hash. Opt-in via menu or --deep-verify. Detects corruption."""
        offset = 0
        corrupted = 0
        while True:
            if shutdown and shutdown.requested:
                break
            rows = self.state.conn.execute(
                "SELECT id, file_path, file_hash FROM media_items"
                " WHERE download_status='downloaded'"
                " AND file_path IS NOT NULL AND file_hash IS NOT NULL"
                " LIMIT ? OFFSET ?",
                (self.CHUNK_SIZE, offset)
            ).fetchall()
            if not rows:
                break
            for row in rows:
                if shutdown and shutdown.requested:
                    break
                actual_hash = chunked_file_hash(row['file_path'])
                if actual_hash and actual_hash != row['file_hash']:
                    self.state.conn.execute(
                        "UPDATE media_items SET download_status='corrupted' WHERE id=?",
                        (row['id'],)
                    )
                    corrupted += 1
            self.state.conn.commit()
            del rows
            offset += self.CHUNK_SIZE
        if corrupted:
            print(f"[RECONCILE] Tier 2: {corrupted} corrupted files marked for re-download.")

    def reexport_blobs(self, export_dir: str, shutdown=None):
        """Re-export profile photo blobs to disk when file_path is missing."""
        rows = self.state.conn.execute(
            "SELECT id, entity_id, entity_type, photo_blob FROM profile_photo_history"
            " WHERE photo_blob IS NOT NULL AND file_path IS NULL"
        ).fetchall()
        reexported = 0
        for row in rows:
            if shutdown and shutdown.requested:
                break
            out_path = Path(export_dir) / f"{row['entity_type']}_{row['entity_id']}.jpg"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(out_path) + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(row['photo_blob'])
            os.replace(tmp, out_path)
            self.state.conn.execute(
                "UPDATE profile_photo_history SET file_path=? WHERE id=?",
                (str(out_path), row['id'])
            )
            reexported += 1
        self.state.conn.commit()
        if reexported:
            print(f"[RECONCILE] Re-exported {reexported} profile photo blobs to {export_dir}")
