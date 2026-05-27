"""
Parallel batch processor for Instagram operations.

Provides concurrent processing capabilities for relationship collection and media
downloads while respecting rate limits and account quotas.
"""
from __future__ import annotations

import os
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass
from enum import Enum, auto

from src.config import DATA_DIR, INSTAGRAM_ACCOUNTS
from src.io_utils import safe_json_write
from src.validation import validate_username


class OperationType(Enum):
    """Types of operations that can be parallelized."""
    COLLECT_RELATIONSHIPS = auto()
    DOWNLOAD_MEDIA = auto()


@dataclass
class ProcessingResult:
    """Result of a single processing operation."""
    username: str
    success: bool
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class BatchProcessor:
    """
    Parallel batch processor for Instagram operations.
    
    Features:
    - Concurrent processing with controlled thread pool
    - Progress tracking and resumption
    - Rate limit awareness
    - Error handling and retry logic
    """
    
    def __init__(self, max_workers: int = 3, operation_type: OperationType = OperationType.COLLECT_RELATIONSHIPS):
        """
        Initialize batch processor.
        
        Args:
            max_workers: Maximum number of concurrent threads
            operation_type: Type of operation being performed
        """
        self.max_workers = max_workers
        self.operation_type = operation_type
        self.results: List[ProcessingResult] = []
        self.progress_file = os.path.join(DATA_DIR, f"batch_progress_{operation_type.name.lower()}.json")
        
        # Ensure data directory exists
        os.makedirs(DATA_DIR, exist_ok=True)
    
    def _save_progress(self, completed: List[str], failed: List[str], pending: List[str]):
        """Save batch processing progress."""
        progress_data = {
            'operation_type': self.operation_type.name,
            'completed': completed,
            'failed': failed,
            'pending': pending,
            'results': [
                {
                    'username': r.username,
                    'success': r.success,
                    'error': r.error,
                    'details': r.details
                }
                for r in self.results
            ]
        }
        safe_json_write(self.progress_file, progress_data)
    
    def _load_progress(self) -> Dict[str, Any]:
        """Load batch processing progress."""
        if not os.path.exists(self.progress_file):
            return {'completed': [], 'failed': [], 'pending': [], 'results': []}
        
        try:
            with open(self.progress_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {'completed': [], 'failed': [], 'pending': [], 'results': []}
    
    def _process_single(
        self,
        username: str,
        operation_func: Callable[[str], bool],
        max_retries: int = 3
    ) -> ProcessingResult:
        """
        Process a single username with retries.
        
        Args:
            username: Instagram username to process
            operation_func: Function to execute (collect_relationships or download_media)
            max_retries: Maximum retry attempts
            
        Returns:
            ProcessingResult with success/failure status
        """
        # Validate username
        is_valid, error = validate_username(username)
        if not is_valid:
            return ProcessingResult(
                username=username,
                success=False,
                error=f"Invalid username: {error}"
            )
        
        last_error = None
        for attempt in range(max_retries):
            try:
                # Add jitter delay to avoid thundering herd
                if attempt > 0:
                    delay = random.uniform(5, 15) * (2 ** (attempt - 1))
                    print(f"[RETRY] Waiting {delay:.0f}s before retry {attempt + 1}/{max_retries}")
                    time.sleep(delay)
                
                success = operation_func(username)
                
                if success:
                    return ProcessingResult(
                        username=username,
                        success=True,
                        details={'attempts': attempt + 1}
                    )
                else:
                    last_error = "Operation returned False"
                    
            except Exception as e:
                last_error = str(e)
                print(f"[ERROR] Processing {username} (attempt {attempt + 1}): {e}")
        
        return ProcessingResult(
            username=username,
            success=False,
            error=last_error
        )
    
    def process_batch(
        self,
        usernames: List[str],
        operation_func: Callable[[str], bool],
        skip_completed: bool = True
    ) -> List[ProcessingResult]:
        """
        Process a batch of usernames concurrently.
        
        Args:
            usernames: List of Instagram usernames to process
            operation_func: Function to execute for each username
            skip_completed: Whether to skip already completed usernames
            
        Returns:
            List of ProcessingResult objects
        """
        # Load progress if resuming
        progress = self._load_progress()
        completed_usernames = set(progress.get('completed', []))
        failed_usernames = set(progress.get('failed', []))
        
        # Filter out already processed if requested
        if skip_completed:
            pending_usernames = [
                u for u in usernames
                if u not in completed_usernames and u not in failed_usernames
            ]
        else:
            pending_usernames = list(usernames)
        
        if not pending_usernames:
            print("[INFO] All usernames have already been processed")
            return [ProcessingResult(u, True) for u in completed_usernames] + \
                   [ProcessingResult(u, False, error="Previously failed") for u in failed_usernames]
        
        print(f"[INFO] Processing {len(pending_usernames)} usernames with {self.max_workers} workers")
        
        # Process concurrently
        self.results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_username = {
                executor.submit(self._process_single, username, operation_func): username
                for username in pending_usernames
            }
            
            completed = list(completed_usernames)
            failed = list(failed_usernames)
            pending = list(pending_usernames)
            
            for i, future in enumerate(as_completed(future_to_username), 1):
                username = future_to_username[future]
                try:
                    result = future.result()
                    self.results.append(result)
                    
                    if result.success:
                        completed.append(username)
                        print(f"[{i}/{len(pending_usernames)}] ✅ {username}")
                    else:
                        failed.append(username)
                        print(f"[{i}/{len(pending_usernames)}] ❌ {username}: {result.error}")
                    
                    # Update progress
                    pending.remove(username)
                    self._save_progress(completed, failed, pending)
                    
                except Exception as e:
                    print(f"[{i}/{len(pending_usernames)}] ❌ {username}: Unexpected error - {e}")
                    failed.append(username)
                    self.results.append(ProcessingResult(username, False, str(e)))
                    pending.remove(username)
                    self._save_progress(completed, failed, pending)
        
        return self.results
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary of batch processing results."""
        total = len(self.results)
        successful = sum(1 for r in self.results if r.success)
        failed = total - successful
        
        return {
            'total': total,
            'successful': successful,
            'failed': failed,
            'success_rate': (successful / total * 100) if total > 0 else 0,
            'errors': [
                {'username': r.username, 'error': r.error}
                for r in self.results if not r.success
            ]
        }
    
    def print_summary(self):
        """Print formatted summary of batch processing."""
        summary = self.get_summary()
        
        print("\n" + "=" * 50)
        print("BATCH PROCESSING SUMMARY")
        print("=" * 50)
        print(f"Total processed: {summary['total']}")
        print(f"Successful: {summary['successful']}")
        print(f"Failed: {summary['failed']}")
        print(f"Success rate: {summary['success_rate']:.1f}%")
        
        if summary['errors']:
            print("\nFailed usernames:")
            for error in summary['errors'][:10]:  # Show first 10 failures
                print(f"  - {error['username']}: {error['error']}")
            if len(summary['errors']) > 10:
                print(f"  ... and {len(summary['errors']) - 10} more")


def parallel_collect_relationships(
    usernames: List[str],
    collector_class,
    account_name: Optional[str] = None,
    max_workers: int = 3,
    **collector_kwargs
) -> List[ProcessingResult]:
    """
    Convenience function for parallel relationship collection.
    
    Args:
        usernames: List of usernames to collect relationships for
        collector_class: RelationshipCollector class
        account_name: Account to use for collection
        max_workers: Number of concurrent workers
        **collector_kwargs: Arguments to pass to collector
        
    Returns:
        List of ProcessingResult objects
    """
    def collect_op(username: str) -> bool:
        collector = collector_class(account_name)
        try:
            collector.collect_for_user(username, **collector_kwargs)
            return True
        finally:
            collector.cleanup()
    
    processor = BatchProcessor(max_workers=max_workers, operation_type=OperationType.COLLECT_RELATIONSHIPS)
    return processor.process_batch(usernames, collect_op)


def parallel_download_media(
    usernames: List[str],
    downloader_class,
    account_name: Optional[str] = None,
    max_workers: int = 3,
    **downloader_kwargs
) -> List[ProcessingResult]:
    """
    Convenience function for parallel media download.
    
    Args:
        usernames: List of usernames to download media for
        downloader_class: MediaDownloader class
        account_name: Account to use for download
        max_workers: Number of concurrent workers
        **downloader_kwargs: Arguments to pass to downloader
        
    Returns:
        List of ProcessingResult objects
    """
    def download_op(username: str) -> bool:
        downloader = downloader_class(account_name)
        try:
            downloader.downloads_dir = downloader._get_downloads_dir()
            result = downloader.download_all(username, **downloader_kwargs)
            if isinstance(result, dict) and 'results' in result:
                return result.get('success') or result.get('partial_success', False)
            return all(result.values()) if isinstance(result, dict) else bool(result)
        finally:
            downloader.cleanup()
    
    processor = BatchProcessor(max_workers=max_workers, operation_type=OperationType.DOWNLOAD_MEDIA)
    return processor.process_batch(usernames, download_op)


__all__ = [
    "BatchProcessor",
    "ProcessingResult",
    "OperationType",
    "parallel_collect_relationships",
    "parallel_download_media",
]


# ---------------------------------------------------------------------------
# Smart routing integration (Task 11.1)
# Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 10.4
# ---------------------------------------------------------------------------

class SmartBatchProcessor:
    """
    Batch processor that uses SmartAccountSelector and ConservativeRateLimiter
    for intelligent account assignment and rate limiting.

    Replaces direct account selection with operation-aware routing via
    process_operation_with_smart_routing().

    Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 10.4
    """

    def __init__(
        self,
        operation_name: str,
        username_db=None,
        rate_limiter=None,
        account_selector=None,
    ):
        """
        Args:
            operation_name: Registered operation name (e.g. "download_stories")
            username_db: Optional UsernameDatabase instance
            rate_limiter: Optional ConservativeRateLimiter instance
            account_selector: Optional SmartAccountSelector instance
        """
        self.operation_name = operation_name
        self._username_db = username_db
        self._rate_limiter = rate_limiter
        self._account_selector = account_selector
        self._checkpoint: dict = {}
        self._checkpoint_file = os.path.join(
            DATA_DIR, f"smart_batch_{operation_name}.json"
        )
        os.makedirs(DATA_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    def _load_checkpoint(self) -> dict:
        """Load progress checkpoint for resuming interrupted batches."""
        if os.path.exists(self._checkpoint_file):
            try:
                with open(self._checkpoint_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"completed": [], "failed": [], "account_assignments": {}}

    def _save_checkpoint(self, completed: list, failed: list, account_assignments: dict):
        """Persist progress checkpoint."""
        data = {
            "operation_name": self.operation_name,
            "completed": completed,
            "failed": failed,
            "account_assignments": account_assignments,
            "timestamp": time.time(),
        }
        safe_json_write(self._checkpoint_file, data)

    def clear_checkpoint(self):
        """Remove checkpoint file to start fresh."""
        if os.path.exists(self._checkpoint_file):
            os.remove(self._checkpoint_file)

    # ------------------------------------------------------------------
    # Main processing
    # ------------------------------------------------------------------

    def process(
        self,
        usernames: list[str],
        execute_fn: Callable[[str, str], bool],
        available_accounts: Optional[list[str]] = None,
        resume: bool = True,
    ) -> dict:
        """
        Process a batch of usernames using smart account routing.

        Args:
            usernames: List of usernames to process
            execute_fn: Callable(account_name, username) -> bool
            available_accounts: Optional list of available account names
            resume: If True, skip already-completed usernames from checkpoint

        Returns:
            Dict with total, success_count, failed_count, results
        """
        from src.operation_router import process_operation_with_smart_routing

        # Load checkpoint for resumption (Requirement 10.4)
        checkpoint = self._load_checkpoint() if resume else {}
        already_completed = set(checkpoint.get("completed", []))
        already_failed = set(checkpoint.get("failed", []))

        # Filter pending usernames
        pending = [
            u for u in usernames
            if u not in already_completed and u not in already_failed
        ]

        if not pending:
            return {
                "total": len(usernames),
                "success_count": len(already_completed),
                "failed_count": len(already_failed),
                "results": {
                    "success": list(already_completed),
                    "failed": list(already_failed),
                },
            }

        # Delegate to smart routing
        result = process_operation_with_smart_routing(
            operation_name=self.operation_name,
            target_usernames=pending,
            execute_fn=execute_fn,
            username_db=self._username_db,
            rate_limiter=self._rate_limiter,
            account_selector=self._account_selector,
            available_accounts=available_accounts,
        )

        # Merge with checkpoint results
        all_success = list(already_completed) + result["results"]["success"]
        all_failed = list(already_failed) + result["results"]["failed"]

        # Save updated checkpoint
        self._save_checkpoint(
            completed=all_success,
            failed=all_failed,
            account_assignments={},
        )

        return {
            "total": len(usernames),
            "success_count": len(all_success),
            "failed_count": len(all_failed),
            "results": {"success": all_success, "failed": all_failed},
        }


