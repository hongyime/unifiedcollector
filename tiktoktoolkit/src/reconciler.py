"""DB-to-disk reconciliation for TikTok Toolkit.

Two-tier reconciliation:
- Tier 1 (fast): Check if files exist on disk
- Tier 2 (deep): Re-hash files and verify integrity

Pattern copied from instagramtoolkit TASK 5.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional, List, Tuple
from dataclasses import dataclass

from . import resilience

logger = logging.getLogger("uttk.reconciler")


@dataclass
class ReconcileResult:
    """Result of a reconciliation operation."""
    total_checked: int = 0
    missing_files: int = 0
    hash_mismatches: int = 0
    fixed: int = 0
    errors: int = 0


def compute_file_hash(file_path: Path, algorithm: str = 'sha256') -> Optional[str]:
    """Compute hash of a file.
    
    Args:
        file_path: Path to file
        algorithm: Hash algorithm (sha256, md5, etc.)
        
    Returns:
        Hex digest string or None on error
    """
    try:
        hasher = hashlib.new(algorithm)
        with open(file_path, 'rb') as f:
            while chunk := f.read(8192):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        logger.debug(f"Failed to hash {file_path}: {e}")
        return None


class Reconciler:
    """Reconcile database records with filesystem state."""
    
    def __init__(self, db_path: Path, chunk_size: int = 500):
        """Initialize reconciler.
        
        Args:
            db_path: Path to SQLite database
            chunk_size: Number of records to process per batch
        """
        self.db_path = Path(db_path)
        self.chunk_size = min(chunk_size, 500)  # Cap at 500 for memory safety
        
    def reconcile_tier1(self, table: str = 'videos') -> ReconcileResult:
        """Tier 1: Fast file existence check.

        Checks all records with a filepath and verifies the file exists on disk.
        Reports missing files; does not modify any rows (videos table has no status column).

        Args:
            table: Database table to check (default: 'videos')

        Returns:
            ReconcileResult with statistics
        """
        result = ReconcileResult()

        logger.info(f"[RECONCILE T1] Starting tier 1 reconciliation on table '{table}'")

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE filepath IS NOT NULL"
            )
            total = cursor.fetchone()[0]

            if total == 0:
                logger.info("[RECONCILE T1] No records with filepaths to check")
                return result

            logger.info(f"[RECONCILE T1] Checking {total} records...")

            offset = 0
            while offset < total:
                if resilience.is_shutdown():
                    logger.info("[RECONCILE T1] Shutdown requested, stopping")
                    break

                cursor = conn.execute(
                    f"SELECT id, filepath FROM {table} WHERE filepath IS NOT NULL LIMIT ? OFFSET ?",
                    (self.chunk_size, offset)
                )
                rows = cursor.fetchall()

                if not rows:
                    break

                for row_id, filepath in rows:
                    result.total_checked += 1
                    if not filepath or not Path(filepath).exists():
                        result.missing_files += 1

                    if result.total_checked % 100 == 0:
                        print(f"[RECONCILE T1] Checked {result.total_checked}/{total}...", end='\r')
                        sys.stdout.flush()

                offset += self.chunk_size

        print()
        logger.info(
            f"[RECONCILE T1] Complete: {result.total_checked} checked, "
            f"{result.missing_files} missing on disk"
        )

        return result
    
    def reconcile_tier2(self, table: str = 'videos') -> ReconcileResult:
        """Tier 2: Deep hash verification.

        Re-computes file hashes and compares against the stored 'hash' column.
        Only checks records that have both filepath and hash stored.
        Reports mismatches; does not modify rows.

        Args:
            table: Database table to check (default: 'videos')

        Returns:
            ReconcileResult with statistics
        """
        result = ReconcileResult()

        logger.info(f"[RECONCILE T2] Starting tier 2 (deep) reconciliation on table '{table}'")

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE filepath IS NOT NULL AND hash IS NOT NULL"
            )
            total = cursor.fetchone()[0]

            if total == 0:
                logger.info("[RECONCILE T2] No records with stored hashes to verify")
                return result

            logger.info(f"[RECONCILE T2] Verifying {total} file hashes...")

            offset = 0
            while offset < total:
                if resilience.is_shutdown():
                    logger.info("[RECONCILE T2] Shutdown requested, stopping")
                    break

                cursor = conn.execute(
                    f"""
                    SELECT id, filepath, hash FROM {table}
                    WHERE filepath IS NOT NULL AND hash IS NOT NULL
                    LIMIT ? OFFSET ?
                    """,
                    (self.chunk_size, offset)
                )
                rows = cursor.fetchall()

                if not rows:
                    break

                for row_id, filepath, stored_hash in rows:
                    result.total_checked += 1

                    file_path = Path(filepath)
                    if not file_path.exists():
                        result.missing_files += 1
                        continue

                    current_hash = compute_file_hash(file_path)
                    if current_hash is None:
                        result.errors += 1
                        continue

                    if current_hash != stored_hash:
                        result.hash_mismatches += 1
                        logger.warning(
                            f"[RECONCILE T2] Hash mismatch: {filepath} "
                            f"(expected: {stored_hash[:8]}..., got: {current_hash[:8]}...)"
                        )

                    if result.total_checked % 50 == 0:
                        print(f"[RECONCILE T2] Verified {result.total_checked}/{total}...", end='\r')
                        sys.stdout.flush()

                offset += self.chunk_size

        print()
        logger.info(
            f"[RECONCILE T2] Complete: {result.total_checked} verified, "
            f"{result.hash_mismatches} mismatches, {result.errors} errors"
        )

        return result
    
    def reconcile_profile_photos(self, output_dir: Path) -> ReconcileResult:
        """Reconcile profile photo blobs.
        
        If photo_blob exists but file_path is missing, re-export blob to disk.
        Uses atomic writes (temp file + os.replace).
        
        Args:
            output_dir: Directory to export photos to
            
        Returns:
            ReconcileResult with statistics
        """
        result = ReconcileResult()
        
        logger.info("[RECONCILE PHOTOS] Starting profile photo reconciliation")
        
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(str(self.db_path)) as conn:
            # Get blobs without file paths
            cursor = conn.execute("""
                SELECT id, username, photo_url, photo_blob
                FROM profile_photo_history
                WHERE photo_blob IS NOT NULL
                  AND (file_path IS NULL OR file_path = '')
            """)
            rows = cursor.fetchall()
            
            if not rows:
                logger.info("[RECONCILE PHOTOS] No orphaned photo blobs found")
                return result
            
            logger.info(f"[RECONCILE PHOTOS] Found {len(rows)} orphaned photo blobs")
            
            for row_id, username, photo_url, photo_blob in rows:
                if resilience.is_shutdown():
                    logger.info("[RECONCILE PHOTOS] Shutdown requested, stopping")
                    break
                
                result.total_checked += 1
                
                try:
                    # Generate filename from URL or use username
                    if photo_url:
                        filename = f"{username}_{hashlib.md5(photo_url.encode()).hexdigest()[:8]}.jpg"
                    else:
                        filename = f"{username}_photo_{row_id}.jpg"
                    
                    file_path = output_dir / filename
                    temp_path = output_dir / f".{filename}.tmp"
                    
                    # Atomic write: temp file + os.replace
                    temp_path.write_bytes(photo_blob)
                    os.replace(temp_path, file_path)
                    
                    # Update database
                    conn.execute(
                        "UPDATE profile_photo_history SET file_path=? WHERE id=?",
                        (str(file_path), row_id)
                    )
                    conn.commit()
                    
                    result.fixed += 1
                    logger.debug(f"[RECONCILE PHOTOS] Exported: {file_path}")
                    
                except Exception as e:
                    result.errors += 1
                    logger.error(f"[RECONCILE PHOTOS] Failed to export blob {row_id}: {e}")
                
                # Progress indicator
                if result.total_checked % 10 == 0:
                    print(f"[RECONCILE PHOTOS] Processed {result.total_checked}/{len(rows)}...", end='\r')
                    sys.stdout.flush()
        
        print()  # Clear progress line
        logger.info(
            f"[RECONCILE PHOTOS] Complete: {result.total_checked} checked, "
            f"{result.fixed} exported, {result.errors} errors"
        )
        
        return result
    
    def run_full_reconciliation(self, deep: bool = False, export_photos: bool = True) -> ReconcileResult:
        """Run full reconciliation (all tiers).
        
        Args:
            deep: If True, run tier 2 (hash verification)
            export_photos: If True, export orphaned photo blobs
            
        Returns:
            Combined ReconcileResult
        """
        logger.info("[RECONCILE] Starting full reconciliation")
        
        # Tier 1: Fast file existence check
        result = self.reconcile_tier1()

        # Tier 2: Deep hash verification (optional)
        if deep and not resilience.is_shutdown():
            tier2_result = self.reconcile_tier2()
            result.total_checked += tier2_result.total_checked
            result.hash_mismatches += tier2_result.hash_mismatches
            result.fixed += tier2_result.fixed
            result.errors += tier2_result.errors
        
        # Profile photos (optional)
        if export_photos and not resilience.is_shutdown():
            photo_dir = self.db_path.parent / 'profile_photos'
            photo_result = self.reconcile_profile_photos(photo_dir)
            result.total_checked += photo_result.total_checked
            result.fixed += photo_result.fixed
            result.errors += photo_result.errors
        
        logger.info(
            f"[RECONCILE] Full reconciliation complete: "
            f"{result.total_checked} checked, {result.missing_files} missing, "
            f"{result.hash_mismatches} corrupted, {result.fixed} fixed, {result.errors} errors"
        )
        
        return result
