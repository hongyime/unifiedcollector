"""Tests for src/batch_processor.py — parallel batch processing."""
import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch
from batch_processor import (
    BatchProcessor,
    ProcessingResult,
    OperationType,
    parallel_collect_relationships,
    parallel_download_media,
)


class TestProcessingResult:
    """Test ProcessingResult data class."""
    
    def test_successful_result(self):
        result = ProcessingResult(username="test_user", success=True)
        assert result.username == "test_user"
        assert result.success is True
        assert result.error is None
        assert result.details is None
    
    def test_failed_result(self):
        result = ProcessingResult(username="test_user", success=False, error="Some error")
        assert result.username == "test_user"
        assert result.success is False
        assert result.error == "Some error"


class TestBatchProcessor:
    """Test BatchProcessor functionality."""
    
    @pytest.fixture
    def processor(self, tmp_path):
        """Create a BatchProcessor with temp data directory."""
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            proc = BatchProcessor(max_workers=2, operation_type=OperationType.COLLECT_RELATIONSHIPS)
            yield proc
    
    def test_init(self, processor):
        assert processor.max_workers == 2
        assert processor.operation_type == OperationType.COLLECT_RELATIONSHIPS
        assert processor.results == []
    
    def test_process_single_valid_username(self, processor):
        mock_func = MagicMock(return_value=True)
        result = processor._process_single("valid_user", mock_func)
        
        assert result.username == "valid_user"
        assert result.success is True
        assert result.error is None
        mock_func.assert_called_once_with("valid_user")
    
    def test_process_single_invalid_username(self, processor):
        mock_func = MagicMock()
        result = processor._process_single("invalid@user", mock_func)
        
        assert result.success is False
        assert "Invalid username" in result.error
        mock_func.assert_not_called()
    
    def test_process_single_with_retries(self, processor):
        call_count = [0]
        
        def flaky_func(username):
            call_count[0] += 1
            if call_count[0] < 3:
                return False
            return True
        
        result = processor._process_single("test_user", flaky_func, max_retries=3)
        
        assert result.success is True
        assert call_count[0] == 3
    
    def test_process_single_all_retries_fail(self, processor):
        mock_func = MagicMock(return_value=False)
        result = processor._process_single("test_user", mock_func, max_retries=3)
        
        assert result.success is False
        assert mock_func.call_count == 3
    
    def test_process_single_exception_handling(self, processor):
        def raising_func(username):
            raise Exception("Test error")
        
        result = processor._process_single("test_user", raising_func, max_retries=1)
        
        assert result.success is False
        assert "Test error" in result.error
    
    def test_process_batch_all_success(self, processor, tmp_path):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            usernames = ["user1", "user2", "user3"]
            mock_func = MagicMock(return_value=True)
            
            results = processor.process_batch(usernames, mock_func)
            
            assert len(results) == 3
            assert all(r.success for r in results)
            assert mock_func.call_count == 3
    
    def test_process_batch_mixed_results(self, processor, tmp_path):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            usernames = ["user1", "user2", "user3"]
            
            def mixed_func(username):
                return username != "user2"  # user2 fails
            
            results = processor.process_batch(usernames, mixed_func)
            
            assert len(results) == 3
            successful = [r for r in results if r.success]
            failed = [r for r in results if not r.success]
            
            assert len(successful) == 2
            assert len(failed) == 1
            assert failed[0].username == "user2"
    
    def test_process_batch_skips_completed_when_requested(self, processor, tmp_path):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            # First run
            usernames = ["user1", "user2"]
            mock_func = MagicMock(return_value=True)
            first_results = processor.process_batch(usernames, mock_func, skip_completed=True)
            
            # Verify first run processed both
            assert len(first_results) == 2
            assert mock_func.call_count == 2
            
            # Second run with same usernames
            mock_func.reset_mock()
            second_results = processor.process_batch(usernames, mock_func, skip_completed=True)
            
            # Should skip already completed - mock not called again
            assert mock_func.call_count == 0
            # Results contain cached completed usernames
            assert len(second_results) == 2
            assert all(r.success for r in second_results)
    
    def test_process_batch_does_not_skip_when_requested(self, processor, tmp_path):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            usernames = ["user1", "user2"]
            mock_func = MagicMock(return_value=True)
            
            # First run
            processor.process_batch(usernames, mock_func, skip_completed=True)
            
            # Second run with skip_completed=False
            mock_func.reset_mock()
            results = processor.process_batch(usernames, mock_func, skip_completed=False)
            
            # Should process all again
            assert len(results) == 2
            assert mock_func.call_count == 2
    
    def test_get_summary(self, processor):
        processor.results = [
            ProcessingResult("user1", True),
            ProcessingResult("user2", False, error="Error 2"),
            ProcessingResult("user3", True),
        ]
        
        summary = processor.get_summary()
        
        assert summary['total'] == 3
        assert summary['successful'] == 2
        assert summary['failed'] == 1
        assert summary['success_rate'] == pytest.approx(66.67, rel=0.01)
        assert len(summary['errors']) == 1
    
    def test_save_and_load_progress(self, processor, tmp_path):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            # Save some progress
            processor._save_progress(
                completed=["user1"],
                failed=["user2"],
                pending=["user3"]
            )
            
            # Load progress
            loaded = processor._load_progress()
            
            assert "user1" in loaded['completed']
            assert "user2" in loaded['failed']
            assert "user3" in loaded['pending']
    
    def test_print_summary(self, processor, capsys):
        processor.results = [
            ProcessingResult("user1", True),
            ProcessingResult("user2", False, error="Error 2"),
        ]
        
        processor.print_summary()
        captured = capsys.readouterr()
        
        assert "BATCH PROCESSING SUMMARY" in captured.out
        assert "Total processed: 2" in captured.out
        assert "Successful: 1" in captured.out
        assert "Failed: 1" in captured.out


class TestParallelConvenienceFunctions:
    """Test parallel_collect_relationships and parallel_download_media."""
    
    @pytest.fixture
    def mock_collector(self):
        """Create a mock RelationshipCollector."""
        collector = MagicMock()
        collector.collect_for_user.return_value = None
        collector.cleanup.return_value = None
        return collector
    
    @pytest.fixture
    def mock_downloader(self):
        """Create a mock MediaDownloader."""
        downloader = MagicMock()
        downloader.download_all.return_value = {"profile_photo": True, "posts": True}
        downloader.cleanup.return_value = None
        downloader._get_downloads_dir.return_value = "/tmp/downloads"
        return downloader
    
    def test_parallel_collect_relationships(self, tmp_path, mock_collector):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            
            usernames = ["user1", "user2"]
            mock_collector_class = MagicMock(return_value=mock_collector)
            results = parallel_collect_relationships(
                usernames,
                mock_collector_class,
                account_name="test_account",
                max_workers=2
            )
            
            assert len(results) == 2
            assert all(r.success for r in results)
            assert mock_collector.collect_for_user.call_count == 2
    
    def test_parallel_download_media(self, tmp_path, mock_downloader):
        with patch('batch_processor.DATA_DIR', str(tmp_path)):
            
            usernames = ["user1", "user2"]
            mock_downloader_class = MagicMock(return_value=mock_downloader)
            results = parallel_download_media(
                usernames,
                mock_downloader_class,
                account_name="test_account",
                max_workers=2
            )
            
            assert len(results) == 2
            assert all(r.success for r in results)
            assert mock_downloader.download_all.call_count == 2
