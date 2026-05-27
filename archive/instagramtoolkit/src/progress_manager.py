"""
Progress Management System for Instagram Toolkit
Handles saving and resuming progress for all operations to prevent data loss on premature exit.

Persistence is delegated to OperationProgressRepository (SQLite/PostgreSQL).
"""

import os
import sys
import time
import signal
import sys as _sys
from datetime import datetime
from typing import Any, Dict, List, Optional

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import DATA_DIR, SPIDER_PROGRESS_FILE, DOWNLOAD_PROGRESS_FILE, BATCH_STATE_FILE, ARCHIVED_LOGS_DIR


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class ProgressManager:
    """Manages progress tracking and resumption for all Instagram operations."""

    def __init__(self, operation_type="general"):
        self.operation_type = operation_type
        self.progress_file = self._get_progress_file()
        self.batch_state_file = BATCH_STATE_FILE

        # Derive a stable operation_id from the operation type
        self._operation_id = operation_type

        from db.repositories.operation_progress_repository import OperationProgressRepository
        self._repo = OperationProgressRepository(_get_db())

        # In-memory statistics (not persisted to DB — kept for backward compat)
        self.progress_data = self._load_progress()
        self._migrate_progress_data()
        self.batch_state = self._load_batch_state()
        self._setup_signal_handlers()

        os.makedirs(DATA_DIR, exist_ok=True)

    # ── Legacy-data helpers ────────────────────────────────────────────────

    @staticmethod
    def _extract_username(entry) -> str:
        if isinstance(entry, dict):
            return entry.get('username', str(entry))
        return str(entry)

    def _migrate_progress_data(self):
        """Normalise legacy progress data so all lists contain plain username strings."""
        changed = False
        for key in ('completed', 'failed', 'pending'):
            raw = self.progress_data.get(key, [])
            if raw and isinstance(raw[0], dict):
                self.progress_data[key] = [self._extract_username(e) for e in raw]
                changed = True
        if changed:
            print("[MIGRATE] Converted legacy progress data to current format")

    def _get_progress_file(self):
        if self.operation_type == "spider":
            return SPIDER_PROGRESS_FILE
        elif self.operation_type == "download":
            return DOWNLOAD_PROGRESS_FILE
        elif self.operation_type == "following_media_download":
            return f"{DATA_DIR}/following_media_download_progress.json"
        else:
            return f"{DATA_DIR}/general_progress.json"

    def _setup_signal_handlers(self):
        """Setup signal handlers to save progress on exit."""
        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def signal_handler(signum, frame):
            print(f"\n[SAVE] Received signal {signum}, stopping gracefully...")
            # Wake all interruptible_sleep calls immediately
            try:
                from rate_limiter import _SHUTDOWN_EVENT
                _SHUTDOWN_EVENT.set()
            except Exception:
                pass
            self.save_progress()
            self.save_batch_state()
            # Flush WAL and close DB before exit
            try:
                _get_db().close()
            except Exception:
                pass
            print("[OK] Progress saved. Exiting.")
            prev = previous_sigint if signum == signal.SIGINT else previous_sigterm
            if callable(prev) and prev not in (signal.SIG_DFL, signal.SIG_IGN):
                prev(signum, frame)
            else:
                _sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, signal_handler)

    def _load_progress(self) -> dict:
        """Build in-memory progress dict from DB."""
        try:
            completed = self._repo.get_completed(self._operation_id)
            failed = self._repo.get_failed(self._operation_id)
            pending = self._repo.get_pending(self._operation_id)
            stats = self._repo.get_statistics(self._operation_id)
            if completed or failed or pending:
                print(f"[STATS] Loaded existing progress: {len(completed)} completed, {len(failed)} failed")
            return {
                'started_at': datetime.now().isoformat(),
                'operation_type': self.operation_type,
                'completed': completed,
                'failed': failed,
                'pending': pending,
                'current_batch': {},
                'statistics': {
                    'total_processed': stats.get('completed', 0) + stats.get('failed', 0),
                    'successful': stats.get('completed', 0),
                    'failed': stats.get('failed', 0),
                    'skipped': 0,
                },
            }
        except Exception as e:
            print(f"[WARNING] Could not load progress from DB: {e}")
            return {
                'started_at': datetime.now().isoformat(),
                'operation_type': self.operation_type,
                'completed': [],
                'failed': [],
                'pending': [],
                'current_batch': {},
                'statistics': {'total_processed': 0, 'successful': 0, 'failed': 0, 'skipped': 0},
            }

    def _load_batch_state(self) -> dict:
        """Load batch state from DB."""
        try:
            state = self._repo.get_batch_state(self._operation_id)
            if state:
                return state
        except Exception as e:
            print(f"[WARNING] Could not load batch state from DB: {e}")
        return {
            'current_operation': None,
            'current_user_index': 0,
            'total_users': 0,
            'current_account_index': 0,
            'operation_count': 0,
            'last_break_time': None,
            'downloads_directory': None,
        }

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def save_progress(self) -> bool:
        """Persist current in-memory progress to DB."""
        try:
            for username in self.progress_data.get('completed', []):
                self._repo.upsert_progress(self._operation_id, username, 'completed')
            for username in self.progress_data.get('failed', []):
                self._repo.upsert_progress(self._operation_id, username, 'failed')
            for username in self.progress_data.get('pending', []):
                self._repo.upsert_progress(self._operation_id, username, 'pending')
            return True
        except Exception as e:
            print(f"[ERROR] Error saving progress: {e}")
            return False

    def save_batch_state(self) -> bool:
        """Persist batch state to DB."""
        try:
            state = dict(self.batch_state)
            state['operation_type'] = self.operation_type
            state['last_updated'] = datetime.now().isoformat()
            self._repo.upsert_batch_state(self._operation_id, state)
            return True
        except Exception as e:
            print(f"[ERROR] Error saving batch state: {e}")
            return False

    def update_batch_state(self, **kwargs: Any) -> None:
        """Update batch state with new values."""
        self.batch_state.update(kwargs)
        self.save_batch_state()

    def is_completed(self, username: str) -> bool:
        """Check if a username has already been completed."""
        status = self._repo.get_status(self._operation_id, username)
        return status == 'completed'

    def mark_pending(self, username: str) -> None:
        """Mark a username as pending / in-progress."""
        self._repo.upsert_progress(self._operation_id, username, 'pending')
        pending = self.progress_data.setdefault('pending', [])
        if username not in pending:
            pending.append(username)

    def mark_completed(self, username: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Mark a username as successfully completed."""
        # Update in-memory state first so callers see it even if the DB call fails
        completed = self.progress_data.setdefault('completed', [])
        if username not in completed:
            completed.append(username)
            self.progress_data['statistics']['successful'] += 1

        for key in ('pending', 'failed'):
            lst = self.progress_data.get(key, [])
            if username in lst:
                lst.remove(username)

        if details:
            meta = self.progress_data.setdefault('details', {})
            meta[username] = details

        self.progress_data['statistics']['total_processed'] += 1

        try:
            self._repo.upsert_progress(self._operation_id, username, 'completed', details=details)
        except Exception as e:
            print(f"[WARNING] Could not persist completed state for {username}: {e}")

    def mark_failed(self, username: str, error_msg: str = "") -> None:
        """Mark a username as failed with an error message."""
        self._repo.upsert_progress(self._operation_id, username, 'failed', error=error_msg)

        failed = self.progress_data.setdefault('failed', [])
        if username not in failed:
            failed.append(username)
            self.progress_data['statistics']['failed'] += 1

        for key in ('pending', 'completed'):
            lst = self.progress_data.get(key, [])
            if username in lst:
                lst.remove(username)

        if error_msg:
            errors = self.progress_data.setdefault('errors', {})
            errors[username] = {'error': error_msg, 'timestamp': datetime.now().isoformat()}

        self.progress_data['statistics']['total_processed'] += 1

    def get_remaining_users(self, usernames: List[str]) -> List[str]:
        """Return usernames that have not been completed or failed."""
        return self._repo.get_remaining(self._operation_id, usernames)

    def get_failed_users(self) -> List[str]:
        """Get list of usernames that failed (for retry)."""
        return self._repo.get_failed(self._operation_id)

    def clear_failed_users(self):
        """Clear failed users list (for retry)."""
        for username in self._repo.get_failed(self._operation_id):
            self._repo.upsert_progress(self._operation_id, username, 'pending')
        self.progress_data['failed'] = []
        print("[RESUME] Cleared failed users list for retry")

    def get_progress_summary(self):
        """Get a summary of current progress."""
        stats = self._repo.get_statistics(self._operation_id)
        completed = stats.get('completed', 0)
        failed = stats.get('failed', 0)
        pending = stats.get('pending', 0)
        total = completed + failed
        return {
            'completed': completed,
            'failed': failed,
            'pending': pending,
            'total_processed': total,
            'success_rate': (completed / max(total, 1)) * 100,
        }

    def print_progress_summary(self):
        """Print a formatted progress summary."""
        summary = self.get_progress_summary()
        print("\n[STATS] Progress Summary:")
        print("=" * 30)
        print(f"[OK] Completed: {summary['completed']}")
        print(f"[ERROR] Failed: {summary['failed']}")
        print(f"⏳ Pending: {summary['pending']}")
        print(f"[PROGRESS] Success rate: {summary['success_rate']:.1f}%")
        print(f"[LIST] Total processed: {summary['total_processed']}")

    def can_resume(self):
        """Check if there's resumable progress."""
        stats = self._repo.get_statistics(self._operation_id)
        return any(stats.get(k, 0) > 0 for k in ('completed', 'failed', 'pending'))

    def cleanup_progress(self):
        """Archive progress (call when operation completes)."""
        try:
            from archive_manager import ArchiveRetentionManager
            archive_dir = os.path.join(DATA_DIR, ARCHIVED_LOGS_DIR)
            os.makedirs(archive_dir, exist_ok=True)
            self._repo.archive_operation(self._operation_id)
            print(f"[FOLDER] Progress archived for operation {self._operation_id}")
            manager = ArchiveRetentionManager(max_archives=5, max_age_days=7)
            cleaned = manager.cleanup_all()
            if cleaned['total_deleted'] > 0:
                print(f"[CLEAN] Removed {cleaned['total_deleted']} old progress archives")
        except Exception as e:
            print(f"[WARNING] Could not cleanup progress: {e}")

    def mark_media_download_completed(self, username, media_stats=None):
        """Mark a username as completed for media download with statistics."""
        details = {'media_stats': media_stats} if media_stats else None
        self.mark_completed(username, details=details)
        if media_stats:
            self.progress_data.setdefault('media_stats', {})[username] = media_stats

    def mark_media_download_failed(self, username, error=None):
        """Mark a username as failed for media download."""
        self.mark_failed(username, error_msg=str(error) if error else "")

    def get_remaining_accounts(self, all_accounts):
        """Get list of accounts that still need to be processed."""
        return self.get_remaining_users(all_accounts)

    def get_media_download_stats(self):
        """Get comprehensive media download statistics."""
        stats = self._repo.get_statistics(self._operation_id)
        return {
            'accounts_completed': stats.get('completed', 0),
            'accounts_failed': stats.get('failed', 0),
            'total_processed': stats.get('completed', 0) + stats.get('failed', 0),
            'media_downloaded': {},
            'started_at': self.progress_data.get('started_at'),
            'last_updated': datetime.now().isoformat(),
        }


def handle_graceful_exit():
    """Decorator to ensure progress is saved on function exit."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except KeyboardInterrupt:
                print("\n[SAVE] Keyboard interrupt detected, saving progress...")
                raise
            except Exception as e:
                print(f"\n[ERROR] Unexpected error: {e}")
                print("[SAVE] Saving progress before exit...")
                raise
        return wrapper
    return decorator


