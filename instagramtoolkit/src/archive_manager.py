"""
Archive retention policy for progress files.

Provides cleanup of old progress archives to prevent disk space issues.
"""
from __future__ import annotations

import os
import glob
import time
from datetime import datetime
from typing import List, Tuple
from src.config import DATA_DIR, ARCHIVED_LOGS_DIR


class ArchiveRetentionManager:
    """Manages retention and cleanup of archived progress files."""
    
    def __init__(self, max_archives: int = 10, max_age_days: int = 30):
        """
        Initialize archive retention manager.
        
        Args:
            max_archives: Maximum number of archives to keep per type
            max_age_days: Maximum age in days before archive is deleted
        """
        self.max_archives = max_archives
        self.max_age_days = max_age_days
        self.archives_dir = os.path.join(DATA_DIR, ARCHIVED_LOGS_DIR)
        
        # Ensure archives directory exists
        os.makedirs(self.archives_dir, exist_ok=True)
    
    def _get_archive_files(self, pattern: str = "*.archive") -> List[str]:
        """Get list of archive files matching pattern."""
        search_pattern = os.path.join(self.archives_dir, pattern)
        return glob.glob(search_pattern)
    
    def _get_file_age_days(self, filepath: str) -> float:
        """Get age of file in days."""
        mtime = os.path.getmtime(filepath)
        age_seconds = time.time() - mtime
        return age_seconds / (24 * 3600)
    
    def _get_file_creation_time(self, filepath: str) -> datetime:
        """Get file creation/modification time as datetime."""
        mtime = os.path.getmtime(filepath)
        return datetime.fromtimestamp(mtime)
    
    def cleanup_by_age(self) -> Tuple[int, int]:
        """
        Remove archives older than max_age_days.
        
        Returns:
            Tuple of (files_checked, files_deleted)
        """
        files = self._get_archive_files()
        deleted = 0
        
        for filepath in files:
            age_days = self._get_file_age_days(filepath)
            if age_days > self.max_age_days:
                try:
                    os.remove(filepath)
                    deleted += 1
                    print(f"[ARCHIVE] Deleted old archive: {os.path.basename(filepath)} ({age_days:.0f} days old)")
                except Exception as e:
                    print(f"[ARCHIVE] Failed to delete {filepath}: {e}")
        
        return len(files), deleted
    
    def cleanup_by_count(self, file_type: str = "progress") -> Tuple[int, int]:
        """
        Keep only the most recent max_archives files of a given type.
        
        Args:
            file_type: Type of archive files to clean (e.g., "progress", "batch")
            
        Returns:
            Tuple of (total_files, files_deleted)
        """
        # Glob pattern must match ProgressManager archive naming convention
        # Get files of this type, sorted by modification time (newest first)
        pattern = f"{file_type}_*.archive"
        files = self._get_archive_files(pattern)
        files.sort(key=os.path.getmtime, reverse=True)
        
        deleted = 0
        if len(files) > self.max_archives:
            for filepath in files[self.max_archives:]:
                try:
                    os.remove(filepath)
                    deleted += 1
                    print(f"[ARCHIVE] Deleted excess archive: {os.path.basename(filepath)}")
                except Exception as e:
                    print(f"[ARCHIVE] Failed to delete {filepath}: {e}")
        
        return len(files), deleted
    
    def cleanup_all(self) -> dict:
        """
        Run all cleanup policies.
        
        Returns:
            Dictionary with cleanup statistics
        """
        stats = {
            'progress_files_checked': 0,
            'progress_files_deleted': 0,
            'batch_files_checked': 0,
            'batch_files_deleted': 0,
            'all_files_checked': 0,
            'all_files_deleted': 0,
            'total_deleted': 0,
        }
        
        # Cleanup by age (all files)
        checked, deleted = self.cleanup_by_age()
        stats['all_files_checked'] = checked
        stats['all_files_deleted'] = deleted
        stats['total_deleted'] += deleted
        
        # Cleanup by count for progress archives
        checked, deleted = self.cleanup_by_count("progress")
        stats['progress_files_checked'] = checked
        stats['progress_files_deleted'] = deleted
        stats['total_deleted'] += deleted
        
        # Cleanup by count for batch archives
        checked, deleted = self.cleanup_by_count("batch")
        stats['batch_files_checked'] = checked
        stats['batch_files_deleted'] = deleted
        stats['total_deleted'] += deleted
        
        return stats
    
    def get_archive_summary(self) -> dict:
        """
        Get summary of current archives.
        
        Returns:
            Dictionary with archive statistics
        """
        files = self._get_archive_files()
        
        if not files:
            return {
                'total_archives': 0,
                'total_size_bytes': 0,
                'oldest_archive': None,
                'newest_archive': None,
            }
        
        total_size = sum(os.path.getsize(f) for f in files)
        ages = [self._get_file_age_days(f) for f in files]
        
        return {
            'total_archives': len(files),
            'total_size_bytes': total_size,
            'total_size_mb': total_size / (1024 * 1024),
            'oldest_archive_days': max(ages),
            'newest_archive_days': min(ages),
            'average_age_days': sum(ages) / len(files),
        }
    
    def print_summary(self):
        """Print formatted archive summary."""
        summary = self.get_archive_summary()
        
        print("\n" + "=" * 50)
        print("ARCHIVE SUMMARY")
        print("=" * 50)
        print(f"Total archives: {summary['total_archives']}")
        print(f"Total size: {summary['total_size_mb']:.2f} MB")
        
        if summary['total_archives'] > 0:
            print(f"Oldest archive: {summary['oldest_archive_days']:.0f} days")
            print(f"Newest archive: {summary['newest_archive_days']:.0f} days")
            print(f"Average age: {summary['average_age_days']:.1f} days")
        else:
            print("No archives found")


def cleanup_archives(max_archives: int = 10, max_age_days: int = 30) -> dict:
    """
    Convenience function to clean up old archives.
    
    Args:
        max_archives: Maximum number of archives to keep per type
        max_age_days: Maximum age in days before deletion
        
    Returns:
        Dictionary with cleanup statistics
    """
    manager = ArchiveRetentionManager(max_archives=max_archives, max_age_days=max_age_days)
    return manager.cleanup_all()


def print_archive_summary():
    """Print current archive summary."""
    manager = ArchiveRetentionManager()
    manager.print_summary()


__all__ = [
    "ArchiveRetentionManager",
    "cleanup_archives",
    "print_archive_summary",
]


