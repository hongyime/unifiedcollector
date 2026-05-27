"""Tests for src/archive_manager.py — archive retention and cleanup logic.

**Validates: Property 4 (Bug Condition - Logic Error Archive Cleanup)**

This test module demonstrates the archive cleanup path/naming inconsistency bug
BEFORE implementing fixes. The tests are expected to reveal that archive cleanup
fails to find files due to inconsistent naming patterns between ProgressManager
and ArchiveRetentionManager.
"""
import os
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from archive_manager import ArchiveRetentionManager


# ── Helpers ──────────────────────────────────────────────────

@pytest.fixture
def archive_dir(tmp_path):
    """Create a temporary archive directory."""
    arch_dir = tmp_path / "archived_logs"
    arch_dir.mkdir(exist_ok=True)
    return str(arch_dir)


@pytest.fixture
def manager(archive_dir, monkeypatch):
    """Create an ArchiveRetentionManager with temp directory."""
    # Patch DATA_DIR and ARCHIVED_LOGS_DIR to use temp directory
    monkeypatch.setattr("archive_manager.DATA_DIR", str(os.path.dirname(archive_dir)))
    monkeypatch.setattr("archive_manager.ARCHIVED_LOGS_DIR", "archived_logs")
    
    return ArchiveRetentionManager(max_archives=5, max_age_days=7)


# ══════════════════════════════════════════════════════════════
#  Task 9.1: Create archive files with ProgressManager naming convention
# ══════════════════════════════════════════════════════════════

class TestArchiveNamingConvention:
    """Test archive file creation with ProgressManager naming patterns."""
    
    def test_create_progress_archives_with_pm_naming(self, archive_dir):
        """Create archive files using ProgressManager naming convention.
        
        ProgressManager creates archives with pattern:
        - progress_{base}_{timestamp}.archive
        - batch_{base}_{timestamp}.archive
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create progress archives with ProgressManager naming
        progress_files = [
            f"progress_spider_progress_{timestamp}.archive",
            f"progress_download_progress_{timestamp}.archive",
            f"progress_general_progress_{timestamp}.archive",
        ]
        
        for filename in progress_files:
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write('{"test": "data"}')
        
        # Verify files were created
        created_files = os.listdir(archive_dir)
        assert len(created_files) == 3
        for filename in progress_files:
            assert filename in created_files
    
    def test_create_batch_archives_with_pm_naming(self, archive_dir):
        """Create batch archive files using ProgressManager naming convention."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create batch archives with ProgressManager naming
        batch_files = [
            f"batch_batch_state_{timestamp}.archive",
            f"batch_spider_batch_{timestamp}.archive",
        ]
        
        for filename in batch_files:
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write('{"test": "batch_data"}')
        
        # Verify files were created
        created_files = os.listdir(archive_dir)
        assert len(created_files) == 2
        for filename in batch_files:
            assert filename in created_files


# ══════════════════════════════════════════════════════════════
#  Task 9.2: Run ArchiveRetentionManager cleanup and verify files are found
#  (should fail on unfixed code due to pattern mismatch)
# ══════════════════════════════════════════════════════════════

class TestArchiveCleanupPatternMismatch:
    """Test that demonstrates archive cleanup behavior.
    
    **Validates: Property 4 (Bug Condition - Logic Error Archive Cleanup)**
    
    FINDING: Tests PASS, indicating archive cleanup works correctly.
    
    The pattern matching between ProgressManager and ArchiveRetentionManager
    is consistent and functional:
    - ProgressManager creates: progress_{base}_{timestamp}.archive
    - ArchiveRetentionManager searches for: progress_*.archive
    - Result: Pattern matches successfully
    """
    
    def test_cleanup_finds_progress_archives(self, archive_dir, manager):
        """Test that cleanup_by_count finds progress archives created by ProgressManager.
        
        FINDING: Pattern matching works correctly. Archives are found and cleaned up.
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create 7 progress archives (exceeds max_archives=5)
        for i in range(7):
            filename = f"progress_spider_progress_{timestamp}_{i}.archive"
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write(f'{{"index": {i}}}')
            # Sleep to ensure different modification times
            time.sleep(0.01)
        
        # Run cleanup by count for progress archives
        total_files, deleted_files = manager.cleanup_by_count("progress")
        
        # FINDING: Pattern matching works correctly
        # - Found 7 files
        # - Deleted 2 files (keeping most recent 5)
        assert total_files == 7, f"Expected to find 7 progress archives, found {total_files}"
        assert deleted_files == 2, f"Expected to delete 2 archives, deleted {deleted_files}"
        
        # Verify only 5 files remain
        remaining_files = [f for f in os.listdir(archive_dir) if f.startswith("progress_")]
        assert len(remaining_files) == 5, f"Expected 5 remaining files, found {len(remaining_files)}"
    
    def test_cleanup_finds_batch_archives(self, archive_dir, manager):
        """Test that cleanup_by_count finds batch archives created by ProgressManager.
        
        FINDING: Pattern matching works correctly for batch archives.
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create 6 batch archives (exceeds max_archives=5)
        for i in range(6):
            filename = f"batch_batch_state_{timestamp}_{i}.archive"
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write(f'{{"index": {i}}}')
            time.sleep(0.01)
        
        # Run cleanup by count for batch archives
        total_files, deleted_files = manager.cleanup_by_count("batch")
        
        # FINDING: Pattern matching works correctly
        # - Found 6 files
        # - Deleted 1 file (keeping most recent 5)
        assert total_files == 6, f"Expected to find 6 batch archives, found {total_files}"
        assert deleted_files == 1, f"Expected to delete 1 archive, deleted {deleted_files}"
        
        # Verify only 5 files remain
        remaining_files = [f for f in os.listdir(archive_dir) if f.startswith("batch_")]
        assert len(remaining_files) == 5, f"Expected 5 remaining files, found {len(remaining_files)}"
    
    def test_cleanup_all_processes_both_types(self, archive_dir, manager):
        """Test that cleanup_all processes both progress and batch archives.
        
        FINDING: cleanup_all correctly processes both archive types.
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create 7 progress archives
        for i in range(7):
            filename = f"progress_download_progress_{timestamp}_{i}.archive"
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write(f'{{"type": "progress", "index": {i}}}')
            time.sleep(0.01)
        
        # Create 6 batch archives
        for i in range(6):
            filename = f"batch_spider_batch_{timestamp}_{i}.archive"
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write(f'{{"type": "batch", "index": {i}}}')
            time.sleep(0.01)
        
        # Run cleanup_all
        stats = manager.cleanup_all()
        
        # FINDING: cleanup_all works correctly
        # - Found and processed progress archives
        # - Found and processed batch archives
        # - Deleted excess archives
        assert stats['progress_files_checked'] == 7, \
            f"Expected 7 progress files checked, got {stats['progress_files_checked']}"
        assert stats['batch_files_checked'] == 6, \
            f"Expected 6 batch files checked, got {stats['batch_files_checked']}"
        assert stats['progress_files_deleted'] == 2, \
            f"Expected 2 progress files deleted, got {stats['progress_files_deleted']}"
        assert stats['batch_files_deleted'] == 1, \
            f"Expected 1 batch file deleted, got {stats['batch_files_deleted']}"


# ══════════════════════════════════════════════════════════════
#  Task 9.3: Document counterexamples showing pattern mismatch
# ══════════════════════════════════════════════════════════════

class TestArchivePatternMismatchCounterexamples:
    """Document specific counterexamples demonstrating the pattern mismatch bug.
    
    **Validates: Property 4 (Bug Condition - Logic Error Archive Cleanup)**
    
    FINDING: The exploration tests PASSED, indicating NO BUG EXISTS in current code.
    
    Analysis:
    - ProgressManager creates: progress_{base}_{timestamp}.archive
      Example: progress_spider_progress_20250101_120000.archive
    - ArchiveRetentionManager searches: {file_type}_*.archive
      Example: progress_*.archive
    - Result: Pattern MATCHES correctly (progress_* matches progress_spider_progress_*)
    
    Conclusion: Either the bug was already fixed, or the root cause analysis is incorrect.
    The current implementation correctly finds and cleans up archives.
    """
    
    def test_counterexample_progress_pattern_works(self, archive_dir, manager):
        """FINDING: Pattern matching WORKS correctly for progress archives.
        
        ProgressManager creates: progress_spider_progress_20250101_120000.archive
        ArchiveRetentionManager searches: progress_*.archive
        Result: Pattern matches successfully
        """
        # Create archive with ProgressManager naming
        filename = "progress_spider_progress_20250101_120000.archive"
        filepath = os.path.join(archive_dir, filename)
        with open(filepath, 'w') as f:
            f.write('{"test": "data"}')
        
        # Try to find it with cleanup_by_count
        total_files, _ = manager.cleanup_by_count("progress")
        
        # FINDING: Pattern matching works correctly
        assert total_files == 1, \
            f"FINDING: File '{filename}' created by ProgressManager " \
            f"WAS FOUND by ArchiveRetentionManager.cleanup_by_count('progress'). " \
            f"Pattern matching works correctly. No bug detected."
    
    def test_counterexample_batch_pattern_works(self, archive_dir, manager):
        """FINDING: Pattern matching WORKS correctly for batch archives.
        
        ProgressManager creates: batch_batch_state_20250101_120000.archive
        ArchiveRetentionManager searches: batch_*.archive
        Result: Pattern matches successfully
        """
        # Create archive with ProgressManager naming
        filename = "batch_batch_state_20250101_120000.archive"
        filepath = os.path.join(archive_dir, filename)
        with open(filepath, 'w') as f:
            f.write('{"test": "batch_data"}')
        
        # Try to find it with cleanup_by_count
        total_files, _ = manager.cleanup_by_count("batch")
        
        # FINDING: Pattern matching works correctly
        assert total_files == 1, \
            f"FINDING: File '{filename}' created by ProgressManager " \
            f"WAS FOUND by ArchiveRetentionManager.cleanup_by_count('batch'). " \
            f"Pattern matching works correctly. No bug detected."
    
    def test_counterexample_retention_policy_enforced(self, archive_dir, manager):
        """FINDING: Retention policy IS ENFORCED correctly.
        
        When cleanup runs, it successfully finds and deletes old archives,
        correctly enforcing the retention policy (max_archives=5, max_age_days=7).
        """
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Create 10 progress archives (double the max_archives limit)
        for i in range(10):
            filename = f"progress_general_progress_{timestamp}_{i}.archive"
            filepath = os.path.join(archive_dir, filename)
            with open(filepath, 'w') as f:
                f.write(f'{{"index": {i}}}')
            time.sleep(0.01)
        
        # Run cleanup
        stats = manager.cleanup_all()
        
        # Count remaining files
        remaining_files = [f for f in os.listdir(archive_dir) if f.endswith('.archive')]
        
        # FINDING: Retention policy is enforced correctly
        assert len(remaining_files) == 5, \
            f"FINDING: Created 10 archives, cleanup correctly kept only 5 " \
            f"({len(remaining_files)} files remain). This demonstrates that " \
            f"cleanup successfully found and deleted old archives. " \
            f"Retention policy is working correctly. No bug detected."


# ══════════════════════════════════════════════════════════════
#  Additional tests for archive manager functionality
# ══════════════════════════════════════════════════════════════

class TestArchiveRetentionBasics:
    """Basic tests for ArchiveRetentionManager functionality."""
    
    def test_get_archive_files_finds_archives(self, archive_dir, manager):
        """Test that _get_archive_files can find archive files."""
        # Create some archive files
        for i in range(3):
            filepath = os.path.join(archive_dir, f"test_{i}.archive")
            with open(filepath, 'w') as f:
                f.write('{}')
        
        files = manager._get_archive_files()
        assert len(files) == 3
    
    def test_get_archive_summary_empty(self, manager):
        """Test archive summary with no archives."""
        summary = manager.get_archive_summary()
        assert summary['total_archives'] == 0
        assert summary['total_size_bytes'] == 0
    
    def test_cleanup_by_age(self, archive_dir, manager):
        """Test cleanup by age removes old files."""
        # Create an old file by modifying its timestamp
        old_file = os.path.join(archive_dir, "old.archive")
        with open(old_file, 'w') as f:
            f.write('{}')
        
        # Set modification time to 31 days ago (exceeds max_age_days=7)
        old_time = time.time() - (31 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))
        
        # Run cleanup by age
        checked, deleted = manager.cleanup_by_age()
        
        assert checked == 1
        assert deleted == 1
        assert not os.path.exists(old_file)
