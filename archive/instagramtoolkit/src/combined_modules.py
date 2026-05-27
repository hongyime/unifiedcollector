"""Per-account cooldown and daily quota tracking.

When an account hits a rate-limit or block, it is placed on cooldown and
skipped during rotation until the cooldown expires.

Daily quotas prevent exceeding Instagram's known thresholds proactively,
rather than waiting to get blocked.

Persistence is delegated to AccountCooldownRepository / AccountQuotaRepository.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
Any

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import (
    DATA_DIR,
    ACCOUNT_COOLDOWN_MINUTES,
    DAILY_QUOTA_PROFILE_VIEWS,
    DAILY_QUOTA_ACTIONS,
    QUOTA_RESET_HOUR,
)


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class AccountCooldownManager:
    """Tracks per-account cooldowns so blocked accounts are not reused immediately."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.account_cooldown_repository import AccountCooldownRepository
        self._repo = AccountCooldownRepository(_get_db())

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def put_on_cooldown(self, account_name: str, minutes: int = ACCOUNT_COOLDOWN_MINUTES, reason: str = "rate-limit"):
        """Place *account_name* on cooldown for *minutes* minutes."""
        until = time.time() + minutes * 60
        self._repo.put_on_cooldown(account_name, until, reason)
        print(f"[COOLDOWN] {account_name} on cooldown for {minutes}m (reason: {reason})")

    def is_on_cooldown(self, account_name: str) -> bool:
        """Return True if *account_name* is still cooling down."""
        return self._repo.is_on_cooldown(account_name)

    def get_cooldown_remaining(self, account_name: str) -> float:
        """Return seconds remaining on cooldown, or 0."""
        return self._repo.get_remaining(account_name)

    def get_available_accounts(self, account_names: list[str]) -> list[str]:
        """Filter to accounts NOT on cooldown."""
        return self._repo.get_available(account_names)

    def clear_cooldown(self, account_name: str):
        """Manually clear cooldown (e.g. after login success)."""
        self._repo.clear_cooldown(account_name)


class AccountQuotaManager:
    """Tracks daily API usage per account to stay under Instagram thresholds."""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.account_quota_repository import AccountQuotaRepository
        self._repo = AccountQuotaRepository(_get_db())

    def _ensure_account(self, account_name: str):
        """Ensure quota row exists and reset if new day."""
        self._repo.reset_if_new_day(account_name)

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def record_profile_view(self, account_name: str, count: int = 1):
        self._repo.reset_if_new_day(account_name)
        self._repo.record_profile_view(account_name, count)

    def record_action(self, account_name: str, count: int = 1):
        self._repo.reset_if_new_day(account_name)
        self._repo.record_action(account_name, count)

    def can_view_profiles(self, account_name: str) -> bool:
        """Return True if account hasn't exceeded daily profile-view budget."""
        if DAILY_QUOTA_PROFILE_VIEWS <= 0:
            return True
        self._repo.reset_if_new_day(account_name)
        usage = self._repo.get_usage(account_name)
        return usage.get('profile_views', 0) < DAILY_QUOTA_PROFILE_VIEWS

    def can_perform_action(self, account_name: str) -> bool:
        """Return True if account hasn't exceeded daily action budget."""
        if DAILY_QUOTA_ACTIONS <= 0:
            return True
        self._repo.reset_if_new_day(account_name)
        usage = self._repo.get_usage(account_name)
        return usage.get('actions', 0) < DAILY_QUOTA_ACTIONS

    def get_usage_summary(self, account_name: str) -> dict[str, str]:
        self._repo.reset_if_new_day(account_name)
        usage = self._repo.get_usage(account_name)
        return {
            "profile_views": f"{usage.get('profile_views', 0)}/{DAILY_QUOTA_PROFILE_VIEWS}",
            "actions": f"{usage.get('actions', 0)}/{DAILY_QUOTA_ACTIONS}",
            "date": usage.get('quota_date', ''),
        }

    def print_all_usage(self):
        """Print usage for all tracked accounts."""
        print("\n[QUOTA] Daily usage summary:")
        print("=" * 50)
        rows = _get_db().fetchall("SELECT * FROM account_quotas")
        for row in rows:
            print(
                f"  {row['account_name']}: "
                f"views={row['profile_views']}/{DAILY_QUOTA_PROFILE_VIEWS}  "
                f"actions={row['actions']}/{DAILY_QUOTA_ACTIONS}  "
                f"({row['quota_date']})"
            )
        if not rows:
            print("  No usage data yet.")


__all__ = ["AccountCooldownManager", "AccountQuotaManager"]

# Instagram account management using Instaloader
import os
import instaloader
from src.config import INSTAGRAM_ACCOUNTS, SESSIONS_DIR, PROXY_CONFIG

class InstagramAccountManager:
    def __init__(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        self.loader = None
        self.current_account = None

    def get_session_file(self, username):
        """Get session file path for a username"""
        return os.path.join(SESSIONS_DIR, f"{username}")

    def login(self, account):
        """Login to Instagram using instaloader"""
        try:
            self.loader = instaloader.Instaloader(
                download_pictures=True,
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=True,
                compress_json=False,
                max_connection_attempts=1,        # disable internal retry; our retry_with_backoff handles it
                dirname_pattern=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads", "{target}"),
                filename_pattern="{date_utc:%Y-%m-%d_%H-%M-%S_UTC}"
            )
            
            # Set network timeout to prevent indefinite hangs
            self.loader.context.request_timeout = 30  # seconds
            
            # Apply proxy if configured (per-account or global)
            proxy_url = PROXY_CONFIG.get(account['name']) or PROXY_CONFIG.get('__global__')
            if proxy_url:
                try:
                    self.loader.context._session.proxies = {
                        'http': proxy_url,
                        'https': proxy_url,
                    }
                    print(f"[PROXY] Using proxy for {account['username']}")
                except Exception as e:
                    print(f"[WARNING] Failed to set proxy: {e}")
            
            # Try to load existing session
            session_file = self.get_session_file(account['username'])
            global_session_file = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Instaloader', f"session-{account['username']}")
            
            loaded_session_path = None
            if os.path.exists(session_file):
                loaded_session_path = session_file
            elif os.path.exists(global_session_file):
                loaded_session_path = global_session_file
                
            if loaded_session_path:
                print(f"📁 Loading existing session from {loaded_session_path} for {account['username']}")
                try:
                    self.loader.load_session_from_file(account['username'], loaded_session_path)
                    
                    # Test if session is still valid by checking the loader's context.
                    # If this check itself fails, treat the session as invalid and re-authenticate.
                    try:
                        logged_in = self.loader.context.is_logged_in
                    except Exception as login_check_err:
                        print(f"⚠️  Session check failed ({login_check_err}), re-authenticating...")
                        logged_in = False
                    
                    if logged_in:
                        print(f"✅ Session restored for {account['username']}")
                        # is_logged_in=True is sufficient — no extra API call needed.
                        
                        # Copy global session to local project if it was loaded from global
                        if loaded_session_path and loaded_session_path == global_session_file and not os.path.exists(session_file):
                            try:
                                self.loader.save_session_to_file(session_file)
                                print(f"💾 Copied CLI session to project sessions/{account['username']}")
                            except Exception as save_err:
                                print(f"⚠️  Could not copy session locally: {save_err}")
                                
                        self.current_account = account
                        return True
                    else:
                        print(f"❌ Session exists but not logged in, re-authenticating...")
                        # Remove invalid session file
                        try:
                            os.remove(loaded_session_path)
                            loaded_session_path = None  # Clear path after removing file
                        except OSError:
                            pass
                except Exception as e:
                    print(f"❌ Session restore failed: {e}")
                    # Remove invalid session file
                    try:
                        os.remove(loaded_session_path)
                        loaded_session_path = None  # Clear path after removing file
                    except OSError:
                        pass
            
            if 'browser' in account:
                browser_name = account['browser'].lower()
                print(f"🌐 Attempting to load session from {browser_name} browser for {account['username']}...")
                try:
                    self.loader.load_session_from_browser(browser_name)
                    
                    # Check if login is successful. If validation fails here,
                    # do not trust the browser session and fall back to credentials.
                    try:
                        logged_in = self.loader.context.is_logged_in
                    except Exception as login_check_err:
                        print(f"⚠️  Browser session check failed ({login_check_err}), falling back to credentials...")
                        logged_in = False
                    
                    if logged_in:
                        print(f"✅ Login successful using {browser_name} cookies for {account['username']}")
                        
                        # Save session for future use
                        self.loader.save_session_to_file(session_file)
                        self.current_account = account
                        return True
                    else:
                        print(f"❌ {browser_name} cookies are invalid or not logged in.")
                except Exception as e:
                    print(f"❌ Failed to load session from {browser_name} browser: {e}")
            
            # Fallback to login with credentials
            print(f"🔐 Logging in to {account['username']} with credentials...")
            self.loader.login(account['username'], account['password'])
            
            # Save session
            self.loader.save_session_to_file(session_file)
            print(f"✅ Login successful for {account['username']}")
            self.current_account = account
            return True
            
        except instaloader.exceptions.BadCredentialsException:
            print(f"❌ Invalid credentials for {account['username']}")
            return False
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            print(f"🔐 2FA required for {account['username']}")
            print(f"💡 Enter 'skip' to skip this account and try next one")
            
            # Multi-attempt 2FA flow
            for attempt in range(3):
                try:
                    if attempt > 0:
                        print(f"[RETRY] 2FA attempt {attempt+1}/3")
                    
                    two_factor_code = input("Enter 2FA code (or 'skip'): ").strip()
                    
                    if two_factor_code.lower() == 'skip':
                        print(f"[2FA] Skipping {account['username']}")
                        return False
                    
                    # Attempt 2FA login with code
                    self.loader.two_factor_login(two_factor_code)
                    
                    # Save session after successful 2FA
                    session_file = self.get_session_file(account['username'])
                    self.loader.save_session_to_file(session_file)
                    self.current_account = account
                    
                    print(f"✅ 2FA successful for {account['username']}")
                    self.current_account = account
                    return True
                    
                except Exception as e:
                    print(f"[ERROR] 2FA attempt {attempt+1}/3 failed: {e}")
                    if attempt < 2:
                        print(f"[RETRY] Please try again... (2 attempts remaining)")
                    else:
                        print(f"[ERROR] All 2FA attempts exhausted for {account['username']}")
                        print(f"[INFO] Try again later or skip this account")
                        return False
        except Exception as e:
            print(f"❌ Login failed for {account['username']}: {e}")
            return False

    def get_authenticated_loader(self, account_name=None, force_fresh_login=False):
        """Get an authenticated instaloader instance"""
        if account_name:
            account = next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name), None)
            if not account:
                print(f"❌ Account '{account_name}' not found in config")
                return None
        else:
            # Use first available account
            account = INSTAGRAM_ACCOUNTS[0] if INSTAGRAM_ACCOUNTS else None
            if not account:
                print("❌ No accounts configured")
                return None
        
        # Check if we need a fresh login
        if force_fresh_login or not (self.current_account and self.current_account['name'] == account['name'] and self.loader):
            # Force a fresh login by removing session
            if force_fresh_login:
                session_file = self.get_session_file(account['username'])
                if os.path.exists(session_file):
                    print(f"🔄 Forcing fresh login, removing old session...")
                    try:
                        os.remove(session_file)
                    except:
                        pass
            
            # Login with the account
            if self.login(account):
                return self.loader
            else:
                return None
        else:
            # Already logged in with this account
            return self.loader

    def logout(self):
        """Logout and cleanup"""
        if self.loader:
            self.loader = None
        self.current_account = None
        print("🔓 Logged out")

    def is_logged_in(self):
        """Check if currently logged in"""
        return self.loader is not None and self.current_account is not None

    def get_available_accounts(self, rate_limiter=None):
        """
        Return account names that are not currently in cooldown.

        Integrates with ConservativeRateLimiter for cooldown checks.
        If no rate_limiter is provided, all configured accounts are returned.

        Args:
            rate_limiter: Optional ConservativeRateLimiter instance for cooldown checks.

        Returns:
            List of account name strings that are available (not in cooldown).

        Requirements: 3.1, 4.7, 8.1
        """
        all_names = [account['name'] for account in INSTAGRAM_ACCOUNTS]
        if rate_limiter is None:
            return all_names
        return rate_limiter.get_available_accounts(all_names)

# User data analysis: compute follower/following counts and summary reports
import os
import csv
from src.config import DATA_DIR
from src.io_utils import safe_json_write


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class UserAnalyzer:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.usernames = self._load_usernames()
        self.relationships = self._load_relationships()

    def _load_usernames(self):
        try:
            from db.repositories.username_repository import UsernameRepository
            rows = UsernameRepository(_get_db()).get_all()
            usernames = [r["username"] for r in rows]
            print(f"Loaded {len(usernames)} usernames for analysis")
            return usernames
        except Exception as e:
            print(f"Error loading usernames: {e}")
            return []

    def _load_relationships(self):
        try:
            from db.repositories.relationship_repository import RelationshipRepository
            rows = RelationshipRepository(_get_db()).get_relationships()
            print(f"Loaded {len(rows)} relationships for analysis")
            return rows
        except Exception as e:
            print(f"Error loading relationships: {e}")
            return []

    def analyze(self):
        # Initialize stats for known usernames
        default_entry = lambda: {'followers_count': 0, 'following_count': 0}
        stats = {u: default_entry() for u in self.usernames}
        # Count relationships
        for rel in self.relationships:
            src = rel.get('source')
            tgt = rel.get('target')
            typ = rel.get('type')
            if not src:
                continue
            # Auto-create entry for sources not in usernames table
            if src not in stats:
                stats[src] = default_entry()
            if typ == 'followers':
                stats[src]['followers_count'] += 1
            elif typ == 'following':
                stats[src]['following_count'] += 1
        return stats

    def save_json(self, path):
        """Write analysis to a JSON file (optional export)."""
        try:
            summary = self.analyze()
            safe_json_write(path, summary)
            print(f"Saved JSON report to {path}")
        except Exception as e:
            print(f"Error saving JSON report: {e}")

    def save_csv(self, path):
        """Write analysis to a CSV file (optional export)."""
        try:
            summary = self.analyze()
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['username', 'followers_count', 'following_count'])
                for u, s in summary.items():
                    writer.writerow([u, s['followers_count'], s['following_count']])
            print(f"Saved CSV report to {path}")
        except Exception as e:
            print(f"Error saving CSV report: {e}")

    def print_summary(self):
        """Print a quick summary directly from the DB — no file I/O needed."""
        try:
            db = _get_db()
            total_users = db.fetchone("SELECT COUNT(*) as cnt FROM usernames")
            total_rels = db.fetchone("SELECT COUNT(*) as cnt FROM relationships")
            followers = db.fetchone("SELECT COUNT(*) as cnt FROM relationships WHERE type='followers'")
            following = db.fetchone("SELECT COUNT(*) as cnt FROM relationships WHERE type='following'")
            top = db.fetchall(
                "SELECT source, COUNT(*) as cnt FROM relationships GROUP BY source ORDER BY cnt DESC LIMIT 10"
            )

            print()
            print("=" * 50)
            print("  Network Analysis Summary")
            print("=" * 50)
            print(f"  Tracked usernames : {total_users['cnt'] if total_users else 0:,}")
            print(f"  Total relationships: {total_rels['cnt'] if total_rels else 0:,}")
            print(f"    Follower links   : {followers['cnt'] if followers else 0:,}")
            print(f"    Following links  : {following['cnt'] if following else 0:,}")
            if top:
                print()
                print("  Top 10 most-connected users:")
                for i, r in enumerate(top, 1):
                    print(f"    {i:2d}. {r['source']} ({r['cnt']} relationships)")
            print("=" * 50)
            print()
        except Exception as e:
            print(f"[ERROR] Could not read analysis from DB: {e}")

"""
Archive retention policy for progress files.

Provides cleanup of old progress archives to prevent disk space issues.
"""
from __future__ import annotations

import os
import glob
import time
from datetime import datetime
Tuple
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
Callable
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

"""CLI Helper utilities for Unified Instagram Toolkit.

Centralizes common helper logic used by the main CLI entrypoint to reduce
duplication and keep `main.py` focused on argument parsing and command routing.
"""
from __future__ import annotations

Optional
import os

from src.config import get_account_by_name, get_default_account


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


def load_usernames() -> List[str]:
    """Load usernames from the database.

    Returns an empty list if no usernames are tracked yet.
    """
    try:
        from db.repositories.username_repository import UsernameRepository
        rows = UsernameRepository(_get_db()).get_all()
        usernames = [r["username"] for r in rows]
        if not usernames:
            print("[ERROR] No usernames found in database. Add usernames with: python main.py add-username <name>")
        return usernames
    except Exception as e:
        print(f"[ERROR] Failed to load usernames from database: {e}")
        return []


def resolve_account_config(account_name: Optional[str]):
    """Resolve an account configuration by name (or default if None).

    Prints user-friendly error messages on failure and returns None.
    """
    account_config = get_account_by_name(account_name) if account_name else get_default_account()
    if not account_config:
        print("[ERROR] No valid account found (check config.py)")
        return None
    return account_config


def get_account_username(account_name: Optional[str]) -> Optional[str]:
    """Convenience wrapper returning the Instagram username for a configured account."""
    account = resolve_account_config(account_name)
    if account:
        return account.get("username")
    return None


__all__ = [
    "load_usernames",
    "resolve_account_config",
    "get_account_username",
]

# Collect followers and following relationships using Instaloader
import os
import sys
import time
import random
import instaloader

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.account_manager import InstagramAccountManager
from src.config import (
    INSTAGRAM_ACCOUNTS, DATA_DIR, MIN_DELAY, MAX_DELAY,
    USERNAMES_FILE, RELATIONSHIPS_FILE,
    ENUM_PAUSE_EVERY, ENUM_PAUSE_SECONDS,
    MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY,
)
from src.profile_access_tracker import ProfileAccessTracker
from src.user_metadata_manager import UserMetadataManager
from src.io_utils import retry_with_backoff
from src.rate_limiter import RateLimiter
from src.resilience import _SHUTDOWN, _interruptible_sleep, with_internet_retry


# Read scraper filters from .env
FILTER_MAX_FOLLOWERS = int(os.environ.get('FILTER_MAX_FOLLOWERS', '0'))
FILTER_MIN_FOLLOWERS = int(os.environ.get('FILTER_MIN_FOLLOWERS', '0'))
FILTER_MAX_FOLLOWING = int(os.environ.get('FILTER_MAX_FOLLOWING', '0'))
FILTER_MIN_FOLLOWING = int(os.environ.get('FILTER_MIN_FOLLOWING', '0'))
FILTER_PUBLIC_ONLY = os.environ.get('FILTER_PUBLIC_ONLY', 'false').lower() == 'true'


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class RelationshipCollector:
    def __init__(self, account_name=None):
        os.makedirs(DATA_DIR, exist_ok=True)

        self.manager = InstagramAccountManager()
        self.loader = self.manager.get_authenticated_loader(account_name)

        if not self.loader:
            raise RuntimeError(f"Failed to authenticate account")

        # Initialize access tracker and metadata manager
        self.access_tracker = ProfileAccessTracker()
        self.metadata_manager = UserMetadataManager()
        self.rate = RateLimiter(label="spider")

        # Repository-backed storage
        from db.repositories.relationship_repository import RelationshipRepository
        from db.repositories.username_repository import UsernameRepository
        db = _get_db()
        self._rel_repo = RelationshipRepository(db)
        self._usr_repo = UsernameRepository(db)

        # In-memory caches (populated lazily for backward compat)
        self._usernames_cache: list[str] | None = None
        self._relationships_cache: list[dict] | None = None

    # ── Backward-compat properties ────────────────────────────────────────

    @property
    def usernames(self) -> list[str]:
        if self._usernames_cache is None:
            self._usernames_cache = self._load_usernames()
        return self._usernames_cache

    @usernames.setter
    def usernames(self, value):
        self._usernames_cache = value

    @property
    def relationships(self) -> list[dict]:
        if self._relationships_cache is None:
            self._relationships_cache = self._load_relationships()
        return self._relationships_cache

    @relationships.setter
    def relationships(self, value):
        self._relationships_cache = value

    def cleanup(self):
        """Cleanup resources."""
        if self.manager:
            self.manager.logout()

    # ── Private helpers (now delegate to repositories) ────────────────────

    def _load_usernames(self) -> list[str]:
        """Load usernames from the DB (replaces flat-file read)."""
        try:
            rows = self._usr_repo.get_all()
            usernames = [r["username"] for r in rows]
            print(f"[LIST] Loaded {len(usernames)} usernames")
            return usernames
        except Exception as e:
            print(f"[ERROR] Error loading usernames: {e}")
            return []

    def _save_usernames(self):
        """Persist in-memory username cache to the DB."""
        if self._usernames_cache is None:
            return
        for username in self._usernames_cache:
            try:
                self._usr_repo.add_username(username, source_account="collected")
            except Exception:
                pass

    def _load_relationships(self) -> list[dict]:
        """Load relationships from the DB (replaces JSON read)."""
        try:
            rows = self._rel_repo.get_relationships()
            print(f"[STATS] Loaded {len(rows)} relationships")
            return rows
        except Exception as e:
            print(f"[ERROR] Error loading relationships: {e}")
            return []

    def _save_relationships(self):
        """Persist in-memory relationship cache to the DB."""
        if self._relationships_cache is None:
            return
        try:
            self._rel_repo.bulk_upsert(self._relationships_cache)
        except Exception as e:
            print(f"[ERROR] Error saving relationships: {e}")

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def collect_for_user(self, username, max_followers=1000, max_following=1000):
        """Collect followers and following for a specific user."""
        if not username or username.strip() == '':
            print("[ERROR] Invalid username provided")
            return

        print(f"[SPIDER] Collecting relationships for: {username}")

        try:
            if not self.loader or not hasattr(self.loader, 'context'):
                raise RuntimeError("Invalid loader or context")

            profile = retry_with_backoff(
                instaloader.Profile.from_username,
                self.loader.context,
                username,
                max_retries=MAX_RETRIES,
                base_delay=RETRY_BASE_DELAY,
                max_delay=RETRY_MAX_DELAY,
                label=f"profile:{username}",
            )
            if profile is None:
                print(f"[ERROR] Could not load profile {username} after retries")
                return

            current_account_name = self.manager.current_account['name'] if self.manager.current_account else 'unknown'
            self.access_tracker.record_profile_access(username, current_account_name, {
                'can_access': True,
                'is_public': not profile.is_private,
                'is_followed': profile.followed_by_viewer if hasattr(profile, 'followed_by_viewer') else False,
            })

            self.metadata_manager.update_profile(username, profile, current_account_name)

            is_followed = profile.followed_by_viewer if hasattr(profile, 'followed_by_viewer') else False
            if profile.is_private and not is_followed:
                try:
                    profile = instaloader.Profile.from_username(self.loader.context, username)
                    is_followed = profile.followed_by_viewer if hasattr(profile, 'followed_by_viewer') else False
                except Exception:
                    pass

            if profile.is_private and not is_followed:
                print(f"[PRIVATE] Profile {username} is private and not followed by authenticated user")
                self.access_tracker.record_profile_access(username, current_account_name, {
                    'can_access': False,
                    'is_public': False,
                    'is_followed': False,
                    'error': 'Private profile not followed',
                })
                return

            # Add username to DB
            self._usr_repo.add_username(username, source_account=current_account_name)

            collected_count = 0
            batch: list[dict] = []

            # Collect followers
            if max_followers > 0:
                print(f"[SPIDER] Collecting followers for {username} (max: {max_followers})")
                try:
                    followers_count = 0
                    for follower in profile.get_followers():
                        if followers_count >= max_followers:
                            break
                        follower_username = follower.username
                        self._usr_repo.add_username(follower_username, source_account=current_account_name)
                        batch.append({
                            'source': username,
                            'target': follower_username,
                            'type': 'followers',
                            'collected_by': current_account_name,
                            'source_is_public': not profile.is_private,
                        })
                        followers_count += 1
                        self.rate.periodic(followers_count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                    print(f"[OK] Collected {followers_count} followers for {username}")
                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"[PRIVATE] Cannot access followers of private profile {username}")
                except Exception as e:
                    print(f"[ERROR] Error collecting followers for {username}: {e}")

            # Collect following
            if max_following > 0:
                print(f"[SPIDER] Collecting following for {username} (max: {max_following})")
                try:
                    following_count = 0
                    for followee in profile.get_followees():
                        if following_count >= max_following:
                            break
                        followee_username = followee.username
                        self._usr_repo.add_username(followee_username, source_account=current_account_name)
                        batch.append({
                            'source': username,
                            'target': followee_username,
                            'type': 'following',
                            'collected_by': current_account_name,
                            'source_is_public': not profile.is_private,
                        })
                        following_count += 1
                        self.rate.periodic(following_count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                    print(f"[OK] Collected {following_count} following for {username}")
                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"[PRIVATE] Cannot access following of private profile {username}")
                except Exception as e:
                    print(f"[ERROR] Error collecting following for {username}: {e}")

            # Bulk upsert to DB
            if batch:
                collected_count = self._rel_repo.bulk_upsert(batch)

            print(f"[STATS] Total new relationships collected: {collected_count}")

        except instaloader.exceptions.ProfileNotExistsException:
            print(f"[ERROR] Profile {username} does not exist")
            current_account_name = self.manager.current_account['name'] if self.manager.current_account else 'unknown'
            self.access_tracker.record_profile_access(username, current_account_name, {
                'can_access': False,
                'error': 'Profile does not exist',
            })
        except Exception as e:
            print(f"[ERROR] Error collecting relationships for {username}: {e}")
            current_account_name = self.manager.current_account['name'] if self.manager.current_account else 'unknown'
            self.access_tracker.record_profile_access(username, current_account_name, {
                'can_access': False,
                'error': str(e),
            })

    def run_batch(self, max_users=None):
        """Process all usernames in batch mode."""
        processed = set(r['source'] for r in self._rel_repo.get_relationships())
        to_process = [u for u in self.usernames if u not in processed]

        if not to_process:
            print("[NOTE] All usernames have been processed")
            return

        if max_users:
            to_process = to_process[:max_users]

        print(f"[LIST] Processing {len(to_process)} unprocessed usernames")

        for i, username in enumerate(to_process, 1):
            try:
                print(f"\n[{i}/{len(to_process)}] Processing {username}...")
                self.collect_for_user(username)
                if i < len(to_process):
                    self.rate.user_delay(multiplier=2)
            except Exception as e:
                print(f"[ERROR] Failed to process {username}: {e}")
                continue

        print("[OK] Batch processing complete")

"""
Unified Instagram Toolkit - Configuration

Credentials are loaded from a .env file in the toolkit root directory.
See .env.example for the expected format.
"""
import os
import sys
from pathlib import Path

from src.download_path_manager import prompt_for_download_path, get_session_path

def get_downloads_directory() -> str:
    """
    Get downloads directory using unified path manager.
    Returns the validated directory path.
    
    Default: ./downloads (relative to toolkit root)
    """
    # Check if path already set in current session
    session_path = get_session_path()
    if session_path:
        return session_path
    
    # Set default path relative to toolkit root
    toolkit_root = Path(__file__).resolve().parents[1]
    default_path = str(toolkit_root / "downloads")
    
    # Prompt for new path with default
    downloads_dir = prompt_for_download_path(
        context="Instagram media",
        out_path=None,
        default_path=default_path
    )
    
    return downloads_dir


# --------------- Credential Loading ---------------

def _load_accounts_from_env() -> list:
    """Load Instagram accounts from .env file.

    Expected keys per account (N = 1, 2, 3, ...):
        INSTA_ACCOUNT_{N}_NAME
        INSTA_ACCOUNT_{N}_USER
        INSTA_ACCOUNT_{N}_PASS
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        print("[WARNING] python-dotenv not installed. Run: pip install python-dotenv")
        print("[WARNING] Falling back to empty account list.")
        return []

    # .env lives in the project root (one level up from src/)
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        print(f"[ERROR] .env file not found at {env_path}")
        print("[ERROR] Create a .env file with your Instagram credentials. See .gitignore for format.")
        return []

    env = dotenv_values(env_path)
    accounts = []
    n = 1
    while True:
        name = env.get(f"INSTA_ACCOUNT_{n}_NAME")
        user = env.get(f"INSTA_ACCOUNT_{n}_USER")
        pw = env.get(f"INSTA_ACCOUNT_{n}_PASS")
        browser = env.get(f"INSTA_ACCOUNT_{n}_BROWSER")
        
        if not name and not user:
            break
        if name and user and pw:
            account_data = {"name": name, "username": user, "password": pw}
            if browser:
                account_data["browser"] = browser.strip()
            accounts.append(account_data)
        else:
            print(f"[WARNING] Incomplete credentials for account {n} — skipping")
        n += 1

    if not accounts:
        print("[WARNING] No valid accounts found in .env file")
    return accounts


# Instagram accounts configuration (loaded from .env)
# The first account in the list is used as the default for batch processing.
INSTAGRAM_ACCOUNTS = _load_accounts_from_env()

# Directory configuration
DATA_DIR = "data"
SESSIONS_DIR = "sessions"
ARCHIVED_LOGS_DIR = "archived_logs"

# Progress tracking files (legacy JSON files - database is now primary)
SPIDER_PROGRESS_FILE = f"{DATA_DIR}/spider_progress.json"
DOWNLOAD_PROGRESS_FILE = f"{DATA_DIR}/download_progress.json"
BATCH_STATE_FILE = f"{DATA_DIR}/batch_state.json"

# --------------- Rate Limiting & Anti-Ban ---------------
MIN_DELAY = 20                 # Increased to ensure <180 req/hr (was 3)
MAX_DELAY = 40                 # Wider range for more human-like patterns (was 8)

# ADD: Random micro-delays for human-like behavior
MIN_RANDOM_DELAY = 0.3        # Small random delay (0.3-1.0s)
MAX_RANDOM_DELAY = 1.0

# ADD: Human rest periods (occasional longer breaks)
HUMAN_REST_INTERVAL = 40       # Every N ops, chance for rest
HUMAN_REST_CHANCE = 0.3        # 30% chance to rest when interval reached
HUMAN_REST_MIN = 30           # Minimum rest (30-60s)
HUMAN_REST_MAX = 60

# Periodic pause during follower/following enumeration
ENUM_PAUSE_EVERY = 12          # Reduced from 15 to trigger more frequent pauses
ENUM_PAUSE_SECONDS = 30        # Increased to maintain <180 req/hr (was 10)

# Automatic long break thresholds (randomised within range)
OPS_BEFORE_BREAK_MIN = 30      # Increased to mimic sustained human activity (was 5)
OPS_BEFORE_BREAK_MAX = 50      # Increased to mimic sustained human activity (was 15)
BREAK_DURATION_MIN = 5         # minutes - Increased for longer rest periods (was 3)
BREAK_DURATION_MAX = 10        # minutes - Increased for longer rest periods (was 8)

# Emergency break on severe rate-limit (minutes, randomised within range)
EMERGENCY_BREAK_MIN = 5
EMERGENCY_BREAK_MAX = 10

# Account switch delay (seconds, randomised within range)
ACCOUNT_SWITCH_DELAY_MIN = 60  # Increased to mimic human account switching (was 5)
ACCOUNT_SWITCH_DELAY_MAX = 120 # Increased to mimic human account switching (was 10)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 30.0        # seconds
RETRY_MAX_DELAY = 600.0        # seconds
RATE_LIMIT_FLOOR = 300.0       # minimum delay on rate-limit (seconds)

# Download pacing
DOWNLOAD_PAUSE_EVERY = 10      # pause every N posts downloaded
DOWNLOAD_PAUSE_SECONDS = 10    # seconds to pause

# Per-account cooldown after rate-limit hit (minutes)
ACCOUNT_COOLDOWN_MINUTES = 15

# Daily quota budget per account (0 = unlimited)
DAILY_QUOTA_PROFILE_VIEWS = 180    # Instagram ~200/hr, stay under
DAILY_QUOTA_ACTIONS = 6000         # Instagram ~7500/day, stay under
QUOTA_RESET_HOUR = 0               # hour of day (0-23) to reset quotas

# --------------- Proxy (optional) ---------------
# Set in .env as PROXY_URL=socks5://user:pass@host:port
# Or per-account: INSTA_ACCOUNT_1_PROXY=socks5://...
def _load_proxy_config() -> dict:
    """Load proxy configuration from .env."""
    try:
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if not os.path.exists(env_path):
            return {}
        env = dotenv_values(env_path)
        proxies = {}
        # Global proxy
        global_proxy = env.get('PROXY_URL', '').strip()
        if global_proxy:
            proxies['__global__'] = global_proxy
        # Per-account proxy
        n = 1
        while True:
            name = env.get(f'INSTA_ACCOUNT_{n}_NAME')
            if not name:
                break
            proxy = env.get(f'INSTA_ACCOUNT_{n}_PROXY', '').strip()
            if proxy:
                proxies[name] = proxy
            n += 1
        return proxies
    except Exception:
        return {}

PROXY_CONFIG = _load_proxy_config()

# --------------- Download Filters ---------------
def _load_filter_config() -> dict:
    """Load download filter settings from .env."""
    try:
        from dotenv import dotenv_values
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        env = dotenv_values(env_path) if os.path.exists(env_path) else {}
        raw = env.get('FILTER_MAX_FOLLOWERS', '0').strip()
        return {'max_followers': int(raw) if raw.isdigit() else 0}
    except Exception:
        return {'max_followers': 0}

_filter_cfg = _load_filter_config()
# Max followers allowed for downloads. 0 = no filter (download everyone).
FILTER_MAX_FOLLOWERS: int = _filter_cfg['max_followers']

# --------------- Error Detection Phrases ---------------
# Phrases that indicate rate limiting / temporary blocks
RATE_LIMIT_PHRASES = (
    "please wait a few minutes",
    "rate limit",
    "too many requests",
    "temporarily blocked",
    "401 unauthorized",
    "try again later",
)

# Phrases that indicate the account needs manual intervention
CHALLENGE_PHRASES = (
    "checkpoint_required",
    "challenge_required",
    "consent_required",
    "feedback_required",
    "login_required",
    "suspicious activity",
    "account has been disabled",
    "your account has been temporarily locked",
)

# Phrases that trigger account switching (not just backoff)
ACCOUNT_SWITCH_PHRASES = (
    "bad credentials",
    "2fa",
    "two factor",
    "verification code",
    "authentication",
    "login_required",
)

# --------------- Operation Registry ---------------
# Maps operation names to their metadata for the OperationClassifier.
# Each entry must have: operation_type, rate_limit_weight (1-10), description.
OPERATION_REGISTRY = {
    "download_profile_pic": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 2,
        "description": "Download a user's profile picture (public access)",
    },
    "get_basic_info": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 1,
        "description": "Retrieve basic profile information (public access)",
    },
    "download_stories": {
        "operation_type": "FOLLOWING_REQUIRED",
        "rate_limit_weight": 7,
        "description": "Download a user's stories (requires following)",
    },
    "download_highlights": {
        "operation_type": "FOLLOWING_REQUIRED",
        "rate_limit_weight": 6,
        "description": "Download a user's story highlights (requires following)",
    },
    "download_media": {
        "operation_type": "FOLLOWING_REQUIRED",
        "rate_limit_weight": 5,
        "description": "Download a user's media posts (requires following)",
    },
    "get_followers": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 8,
        "description": "Retrieve a user's followers list (public access)",
    },
    "get_following": {
        "operation_type": "PUBLIC",
        "rate_limit_weight": 8,
        "description": "Retrieve a user's following list (public access)",
    },
}


def get_default_account() -> dict | None:
    """
    Get the default account for batch processing.
    Returns the first account in INSTAGRAM_ACCOUNTS list.
    """
    if INSTAGRAM_ACCOUNTS:
        return INSTAGRAM_ACCOUNTS[0]
    return None

def get_account_by_name(account_name: str | None) -> dict | None:
    """
    Get account configuration by name.
    Returns None if account not found.
    """
    if not account_name:
        return get_default_account()
    
    return next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name), None)

"""
Conservative Rate Limiter - Enhanced rate limiting with operation-specific delays
to avoid Instagram bans without proxy infrastructure.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""
from __future__ import annotations

import random
import time
import logging
Optional

from src.operation_classifier import OperationType
from src.account_cooldown import AccountCooldownManager
from src.config import (
    MIN_DELAY,
    MAX_DELAY,
    ACCOUNT_SWITCH_DELAY_MIN,
    ACCOUNT_SWITCH_DELAY_MAX,
    ENUM_PAUSE_EVERY,
    ENUM_PAUSE_SECONDS,
    ACCOUNT_COOLDOWN_MINUTES,
)

logger = logging.getLogger(__name__)

# Delay multipliers per operation type (Requirement 4.2, 4.3, 4.4)
_DELAY_MULTIPLIERS = {
    OperationType.PUBLIC: 1.0,
    OperationType.FOLLOWING_REQUIRED: 1.5,
    OperationType.MUTUAL_FOLLOWING: 2.0,
}


class ConservativeRateLimiter:
    """
    Enhanced rate limiter with operation-specific delays and account cooldown enforcement.

    Delay scaling:
      - PUBLIC:             1.0x base delay
      - FOLLOWING_REQUIRED: 1.5x base delay
      - MUTUAL_FOLLOWING:   2.0x base delay

    Additional features:
      - Random jitter for human-like behaviour
      - Mandatory account-switch delays
      - Progressive delays every N operations (following enumeration)
      - Emergency cooldown on rate-limit hits (≥15 minutes)
      - Account availability checking via AccountCooldownManager

    Requirements: 4.1–4.8
    """

    def __init__(
        self,
        min_delay: float = MIN_DELAY,
        max_delay: float = MAX_DELAY,
        cooldown_manager: Optional[AccountCooldownManager] = None,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._cooldown_manager = cooldown_manager or AccountCooldownManager()

    # ------------------------------------------------------------------
    # Core delay helpers
    # ------------------------------------------------------------------

    def _base_delay(self) -> float:
        """Return a random base delay in [min_delay, max_delay]."""
        return random.uniform(self.min_delay, self.max_delay)

    def _jitter(self, base: float) -> float:
        """Add ±20% gaussian jitter for human-like behaviour."""
        jitter = random.gauss(0, base * 0.2)
        return max(self.min_delay * 0.5, base + jitter)

    def _sleep(self, seconds: float, reason: str = ""):
        if seconds <= 0:
            return
        logger.debug("Rate limit sleep %.1fs%s", seconds, f" ({reason})" if reason else "")
        end = time.time() + seconds
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def operation_delay(self, operation_type: OperationType) -> None:
        """
        Apply operation-specific rate limiting delay.

        Delay = base_delay × multiplier + jitter

        Requirements: 4.1, 4.2, 4.3, 4.4
        """
        multiplier = _DELAY_MULTIPLIERS.get(operation_type, 1.0)
        base = self._base_delay() * multiplier
        delay = self._jitter(base)
        self._sleep(delay, reason=f"operation_delay({operation_type.value})")

    def account_switch_delay(self) -> None:
        """
        Enforce a mandatory delay between account switches.

        Requirement: 4.5
        """
        delay = random.uniform(ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX)
        delay = self._jitter(delay)
        self._sleep(delay, reason="account_switch_delay")

    def following_enumeration_delay(self, count: int) -> None:
        """
        Apply progressive delays during follower/following enumeration.

        Triggers every ENUM_PAUSE_EVERY operations.

        Requirement: 4.8
        """
        if count > 0 and count % ENUM_PAUSE_EVERY == 0:
            # Progressive: longer delay as count grows
            multiplier = 1.0 + (count // ENUM_PAUSE_EVERY) * 0.1
            delay = self._jitter(ENUM_PAUSE_SECONDS * multiplier)
            self._sleep(delay, reason=f"enumeration_pause at count={count}")

    def emergency_cooldown(self, account: str, duration_minutes: int = ACCOUNT_COOLDOWN_MINUTES) -> None:
        """
        Apply emergency cooldown to an account after a rate-limit hit.

        Enforces a minimum of 15 minutes.

        Requirement: 4.6
        """
        effective_minutes = max(15, duration_minutes)
        logger.warning(
            "Emergency cooldown: account '%s' for %d minutes", account, effective_minutes
        )
        self._cooldown_manager.put_on_cooldown(
            account, minutes=effective_minutes, reason="rate-limit-emergency"
        )

    def check_account_available(self, account: str) -> bool:
        """
        Return True if account is NOT in cooldown.

        Requirement: 4.7
        """
        return not self._cooldown_manager.is_on_cooldown(account)

    def get_cooldown_remaining(self, account: str) -> float:
        """Return seconds remaining on cooldown for account (0 if not in cooldown)."""
        return self._cooldown_manager.get_cooldown_remaining(account)

    def get_available_accounts(self, account_names: list[str]) -> list[str]:
        """Filter list to accounts not currently in cooldown."""
        return self._cooldown_manager.get_available_accounts(account_names)

# Media downloader using Instaloader - posts, stories, highlights, and profile photos
import glob
import os
import instaloader
from src.account_manager import InstagramAccountManager
from src.config import (
    get_downloads_directory,
    DOWNLOAD_PAUSE_EVERY, DOWNLOAD_PAUSE_SECONDS,
    MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY,
)
from src.media_utils import get_profile, summarize_profile, profile_access_blocked
from src.io_utils import retry_with_backoff
from src.rate_limiter import RateLimiter
from src.resilience import _SHUTDOWN, _interruptible_sleep, with_internet_retry

# Media file extensions we consider a successful download
_MEDIA_EXTENSIONS = ("*.jpg", "*.jpeg", "*.mp4", "*.png", "*.webp")

class MediaDownloader:
    def __init__(self, account_name=None):
        self.manager = InstagramAccountManager()
        self.loader = self.manager.get_authenticated_loader(account_name)
        
        if not self.loader:
            raise RuntimeError("Failed to authenticate account")

        self.downloads_dir = None
        self.rate = RateLimiter(label="media")

    def cleanup(self):
        """Cleanup resources"""
        if self.manager:
            self.manager.logout()

    def _get_downloads_dir(self):
        """Get downloads directory, prompting user if not set"""
        if self.downloads_dir is None:
            self.downloads_dir = get_downloads_directory()
        return self.downloads_dir

    def _setup_target_directory(self, username):
        """Setup target directory for downloads"""
        downloads_dir = self._get_downloads_dir()
        target_dir = os.path.join(downloads_dir, f"user_{username}")
        os.makedirs(target_dir, exist_ok=True)
        
        # Configure instaloader to use this directory
        self.loader.dirname_pattern = target_dir
        return target_dir

    def verify_download(self, username: str, category: str, target_dir: str) -> bool:
        """Check that at least one media file exists in target_dir after a download.

        Scans for *.jpg, *.jpeg, *.mp4, *.png, *.webp recursively under target_dir.
        Returns True if any media file is found, False otherwise.
        """
        for pattern in _MEDIA_EXTENSIONS:
            matches = glob.glob(os.path.join(target_dir, "**", pattern), recursive=True)
            if matches:
                return True
        print(f"[VERIFY] No media files found for {username}/{category} in {target_dir}")
        return False

    def _download_with_verify(self, username: str, category: str, download_fn, max_retries: int = 2):
        """Run download_fn(), then verify files exist. Retry up to max_retries times if empty.

        Returns (success: bool, downloaded_count: int).
        download_fn must return (success: bool, count: int).
        """
        target_dir = self._setup_target_directory(username)
        for attempt in range(max_retries + 1):
            success, count = download_fn(target_dir)
            if not success:
                return False, 0
            # Stories/highlights may legitimately have 0 items — treat as success
            if count == 0:
                return True, 0
            if self.verify_download(username, category, target_dir):
                return True, count
            if attempt < max_retries:
                print(f"[VERIFY] Retry {attempt + 1}/{max_retries} — re-downloading {category} for {username}")
            else:
                print(f"[VERIFY] ❌ Files missing after {max_retries} retries for {username}/{category}")
                return False, count
        return False, 0

    def download_profile_photo(self, username):
        """Download profile photo for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"📸 Downloading profile photo for {username}")

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"pfp:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                result = retry_with_backoff(
                    self.loader.download_pic,
                    filename=os.path.join(target_dir, f"{username}_profile"),
                    url=profile.profile_pic_url,
                    mtime=None,
                    max_retries=2,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"pfp_dl:{username}",
                )
                if result is None:
                    return False, 0
                return True, 1
            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading profile photo for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "profile_photo", _do_download)
        if success:
            print(f"✅ Profile photo downloaded for {username}")
        return success

    def download_posts(self, username, limit=None):
        """Download posts for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"📱 Downloading posts for {username}" + (f" (limit: {limit})" if limit else ""))

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"profile:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                print(f"🔍 Profile Debug: {summarize_profile(profile)}")
                if profile_access_blocked(profile):
                    print(f"🔒 Profile {username} not accessible (private & not followed)")
                    return False, 0

                downloaded = 0
                failed_posts = 0
                try:
                    for post in profile.get_posts():
                        if limit and downloaded >= limit:
                            break
                        result = retry_with_backoff(
                            self.loader.download_post, post, username,
                            max_retries=2,
                            base_delay=RETRY_BASE_DELAY,
                            max_delay=RETRY_MAX_DELAY,
                            label=f"post:{username}",
                        )
                        if result is None:
                            failed_posts += 1
                            print(f"❌ Failed to download post after retries ({failed_posts} failures)")
                            if failed_posts >= 3:
                                print(f"[ERROR] Too many post download failures, aborting")
                                return False, downloaded
                            continue
                        downloaded += 1
                        failed_posts = 0
                        print(f"📥 Downloaded post {downloaded}" + (f"/{limit}" if limit else ""))
                        self.rate.periodic(downloaded, every=DOWNLOAD_PAUSE_EVERY, seconds=DOWNLOAD_PAUSE_SECONDS)

                    if failed_posts > 0:
                        print(f"[WARNING] Downloaded {downloaded} posts with {failed_posts} failures for {username}")
                    else:
                        print(f"✅ Downloaded {downloaded} posts for {username}")
                    return True, downloaded

                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"🔒 Cannot access posts of private profile {username}")
                    return False, 0

            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading posts for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "posts", _do_download)
        return success

    def download_stories(self, username):
        """Download active stories for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"📚 Downloading stories for {username}")

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"stories:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                print(f"🔍 Stories Debug: {summarize_profile(profile)}")
                if profile_access_blocked(profile):
                    print(f"🔒 Profile {username} not accessible (private & not followed)")
                    return False, 0

                downloaded = 0
                try:
                    for story in self.loader.get_stories(userids=[profile.userid]):
                        for item in story.get_items():
                            result = retry_with_backoff(
                                self.loader.download_storyitem, item, username,
                                max_retries=2,
                                base_delay=RETRY_BASE_DELAY,
                                max_delay=RETRY_MAX_DELAY,
                                label=f"story:{username}",
                            )
                            if result is None:
                                print(f"❌ Skipping story item after retries")
                                continue
                            downloaded += 1
                            print(f"📥 Downloaded story item {downloaded}")

                    if downloaded == 0:
                        print(f"📝 No active stories found for {username}")
                    else:
                        print(f"✅ Downloaded {downloaded} story items for {username}")
                    return True, downloaded

                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"🔒 Cannot access stories of private profile {username}")
                    return False, 0

            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading stories for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "stories", _do_download)
        return success

    def download_highlights(self, username):
        """Download highlight reels for a user, with post-download file verification."""
        if not username or username.strip() == '':
            print("❌ Invalid username provided")
            return False

        print(f"⭐ Downloading highlights for {username}")

        def _do_download(target_dir):
            try:
                profile = retry_with_backoff(
                    get_profile, self.loader, username,
                    max_retries=MAX_RETRIES,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"highlights:{username}",
                )
                if not profile:
                    print(f"❌ Could not load profile {username}")
                    return False, 0
                print(f"🔍 Highlights Debug: {summarize_profile(profile)}")
                if profile_access_blocked(profile):
                    print(f"🔒 Profile {username} not accessible (private & not followed)")
                    return False, 0

                downloaded = 0
                try:
                    for highlight in self.loader.get_highlights(profile):
                        highlight_name = highlight.title or f"highlight_{highlight.unique_id}"
                        print(f"📥 Downloading highlight: {highlight_name}")
                        for item in highlight.get_items():
                            result = retry_with_backoff(
                                self.loader.download_storyitem, item, f"{username}_highlights_{highlight_name}",
                                max_retries=2,
                                base_delay=RETRY_BASE_DELAY,
                                max_delay=RETRY_MAX_DELAY,
                                label=f"highlight:{username}",
                            )
                            if result is None:
                                print(f"❌ Skipping highlight item after retries")
                                continue
                            downloaded += 1

                    if downloaded == 0:
                        print(f"📝 No highlights found for {username}")
                    else:
                        print(f"✅ Downloaded {downloaded} highlight items for {username}")
                    return True, downloaded

                except instaloader.exceptions.PrivateProfileNotFollowedException:
                    print(f"🔒 Cannot access highlights of private profile {username}")
                    return False, 0

            except instaloader.exceptions.ProfileNotExistsException:
                print(f"❌ Profile {username} does not exist")
                return False, 0
            except Exception as e:
                print(f"❌ Error downloading highlights for {username}: {e}")
                return False, 0

        success, _ = self._download_with_verify(username, "highlights", _do_download)
        return success

    def download_all(self, username, post_limit=None):
        """Download all media categories sequentially for the given username.

        Returns dict with success/partial_success keys - callers must check dict structure, not truthiness.
        
        Return structure:
        {
            'success': bool,           # True if all categories succeeded
            'partial_success': bool,   # True if some (but not all) categories succeeded
            'success_count': int,      # Number of successful categories
            'total_count': int,        # Total number of categories attempted
            'results': dict            # Per-category success/failure results
        }
        """
        print(f"🎯 Starting complete download for {username}")

        category_results = {
            'profile_photo': self.download_profile_photo(username),
            'posts': self.download_posts(username, post_limit),
            'stories': self.download_stories(username),
            'highlights': self.download_highlights(username),
        }
        success_count = sum(1 for v in category_results.values() if v)
        total_count = len(category_results)
        success = success_count == total_count
        partial_success = success_count > 0 and not success

        print(f"📊 Download summary for {username}: {success_count}/{total_count} categories successful")
        return {
            'success': success,
            'partial_success': partial_success,
            'success_count': success_count,
            'total_count': total_count,
            'results': category_results,
        }






#!/usr/bin/env python3
"""
Unified Download Path Manager
==============================
Handles download path configuration across all toolkits.

Key Features:
- Always prompts for download path (no defaults, no env vars)
- Session-based caching with automatic cleanup
- Batch mode support via --out flag
- Path validation and auto-creation with confirmation
- Automatic cache clearing on any termination

IMPORTANT FOR FUTURE LLM AGENTS:
- NEVER persist paths between script runs
- NEVER use environment variables for paths
- ALWAYS prompt for paths (except with --out flag)
- ALWAYS use this module for download path handling
"""

import os
import sys
import atexit
import signal
from pathlib import Path
Optional

# Session cache - memory only, cleared on ANY termination
_SESSION_DOWNLOAD_PATH: Optional[str] = None
_SESSION_ACTIVE: bool = False


def _cleanup_session():
    """
    Clear session cache on exit.
    Called automatically by atexit, signal handlers, or manual termination.
    """
    global _SESSION_DOWNLOAD_PATH, _SESSION_ACTIVE
    _SESSION_DOWNLOAD_PATH = None
    _SESSION_ACTIVE = False


# Register cleanup handlers for all termination scenarios
atexit.register(_cleanup_session)
try:
    signal.signal(signal.SIGINT, lambda s, f: (_cleanup_session(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (_cleanup_session(), sys.exit(0)))
except (ValueError, OSError):
    # Signal handling not available on some platforms
    pass


def prompt_for_download_path(
    context: str = "files",
    allow_session_reuse: bool = True,
    out_path: Optional[str] = None,
    default_path: Optional[str] = None
) -> str:
    """
    Prompt user for download location with session caching support.
    
    Args:
        context: Description of what's being downloaded (e.g., "Instagram photos", "TikTok videos")
        allow_session_reuse: If True and path exists in session, ask to reuse
        out_path: Pre-specified output path from --out flag (skips prompting)
        default_path: Default path to use if user leaves prompt empty
    """
    global _SESSION_DOWNLOAD_PATH, _SESSION_ACTIVE
    
    # Handle --out flag (batch mode)
    if out_path:
        validated = _validate_and_create_path(out_path, context)
        # Update session cache even for --out paths
        _SESSION_DOWNLOAD_PATH = validated
        _SESSION_ACTIVE = True
        return validated
    
    # Check session cache for reuse
    if allow_session_reuse and _SESSION_ACTIVE and _SESSION_DOWNLOAD_PATH:
        print(f"\n{'='*70}")
        print(f"♻️  CURRENT SESSION PATH: {_SESSION_DOWNLOAD_PATH}")
        print(f"{'='*70}")
        reuse = input("Use same path for this download? (y/n): ").strip().lower()
        if reuse == 'y':
            print(f"✅ Reusing session path: {_SESSION_DOWNLOAD_PATH}\n")
            return _SESSION_DOWNLOAD_PATH
        else:
            print("Prompting for new path...\n")
    
    # Prompt for new path
    print(f"\n{'='*70}")
    print(f"📁 DOWNLOAD LOCATION REQUIRED FOR: {context.upper()}")
    print(f"{'='*70}")
    if default_path:
        print(f"💡 DEFAULT: {default_path}")
    print(f"⚠️  WARNING: Path is NOT saved between script runs!")
    print(f"    Each new session requires path configuration.")
    print(f"    This is for your safety and transparency.")
    print(f"{'='*70}")
    print(f"\n💡 Examples:")
    print(f"   • Windows:   C:\\Users\\YourName\\Downloads\\{context.replace(' ', '_').lower()}")
    print(f"   • Mac/Linux: /Users/yourname/Downloads/{context.replace(' ', '_').lower()}")
    print(f"   • Relative:  ./{context.replace(' ', '_').lower()}_downloads")
    print(f"\n💡 Tips:")
    print(f"   • Use absolute paths for clarity")
    print(f"   • Directory will be created if it doesn't exist")
    print(f"   • Type 'exit' or 'q' to cancel")
    print(f"{'='*70}\n")
    
    while True:
        prompt_msg = f"📂 Enter download directory path: "
        if default_path:
            prompt_msg = f"📂 Enter download directory path (Press Enter for default): "
            
        download_path = input(prompt_msg).strip()
        
        # Handle exit request
        if download_path.lower() in ['exit', 'q', 'quit']:
            print("\n❌ Operation cancelled by user.")
            _cleanup_session()
            sys.exit(0)
        
        # Handle empty input with default
        if not download_path:
            if default_path:
                download_path = default_path
                print(f"ℹ️  Using default path: {download_path}")
            else:
                print("❌ Path cannot be empty. Please enter a valid path.\n")
                continue
        
        try:
            validated_path = _validate_and_create_path(download_path, context)
            
            # Update session cache
            _SESSION_DOWNLOAD_PATH = validated_path
            _SESSION_ACTIVE = True
            
            print(f"\n{'='*70}")
            print(f"✅ SESSION PATH SET: {validated_path}")
            print(f"⚠️  Valid for this session only - will clear on exit!")
            print(f"{'='*70}\n")
            
            return validated_path
            
        except PermissionError:
            print(f"❌ Permission denied: {download_path}")
            print(f"   Please choose a location with write permissions.\n")
        except FileExistsError as e:
            print(f"❌ {e}")
            print(f"   Please choose a different path.\n")
        except Exception as e:
            print(f"❌ Invalid path: {download_path}")
            print(f"   Error: {e}\n")


def _validate_and_create_path(path_str: str, context: str) -> str:
    """
    Validate path and create directory if needed.
    
    Args:
        path_str: Path string to validate
        context: Context for error messages
        
    Returns:
        str: Absolute validated path
        
    Raises:
        PermissionError: If no write access
        FileExistsError: If path exists and is a file (not directory)
        Exception: For other validation errors
    """
    # Expand user path (~) and make absolute
    path = Path(path_str).expanduser().resolve()
    
    # Check if path exists and is a file (not directory)
    if path.exists() and not path.is_dir():
        raise FileExistsError(f"Path exists but is a file, not a directory: {path}")
    
    # Create directory if it doesn't exist
    if not path.exists():
        print(f"\n📁 Directory does not exist: {path}")
        confirm = input("   Create this directory? (y/n): ").strip().lower()
        
        if confirm != 'y':
            raise ValueError("Directory creation declined by user")
        
        try:
            path.mkdir(parents=True, exist_ok=True)
            print(f"✅ Created directory: {path}")
        except PermissionError:
            raise PermissionError(f"Cannot create directory (permission denied): {path}")
        except Exception as e:
            raise Exception(f"Failed to create directory: {e}")
    
    # Test write access
    try:
        test_file = path / '.write_test_temp'
        test_file.write_text('test')
        test_file.unlink()
    except PermissionError:
        raise PermissionError(f"No write permission for directory: {path}")
    except Exception as e:
        raise Exception(f"Cannot write to directory: {e}")
    
    abs_path = str(path)
    return abs_path


def get_session_path() -> Optional[str]:
    """
    Get current session download path without prompting.
    
    Returns:
        str or None: Current session path if set, None otherwise
    """
    return _SESSION_DOWNLOAD_PATH if _SESSION_ACTIVE else None


def clear_session_cache():
    """
    Manually clear session cache.
    Useful for testing or forcing fresh prompts.
    """
    _cleanup_session()
    print("🧹 Session download path cache cleared")


# Example usage and testing
if __name__ == "__main__":
    import argparse
    
    print("Testing Download Path Manager")
    print("="*70)
    
    parser = argparse.ArgumentParser(description="Test download path manager")
    parser.add_argument('--out', type=str, help='Output directory (batch mode)')
    args = parser.parse_args()
    
    # Test 1: First download
    print("\n[TEST 1] First download prompt:")
    path1 = prompt_for_download_path(
        context="test_files",
        out_path=args.out
    )
    print(f"Result: {path1}")
    
    # Test 2: Second download with session reuse option
    print("\n[TEST 2] Second download (should offer reuse):")
    path2 = prompt_for_download_path(
        context="more_files",
        allow_session_reuse=True,
        out_path=args.out
    )
    print(f"Result: {path2}")
    
    # Test 3: Check session path
    print("\n[TEST 3] Current session path:")
    session_path = get_session_path()
    print(f"Result: {session_path}")
    
    print("\n✅ All tests completed. Cache will clear on exit.")


"""
Centralized exception handling for Instagram operations.

This module provides a structured approach to handling Instaloader exceptions
by categorizing them into recovery strategies rather than relying on string
matching against error messages.
"""
from __future__ import annotations

import instaloader.exceptions
Optional
from dataclasses import dataclass
from enum import Enum, auto


class RecoveryStrategy(Enum):
    """Recovery strategies for different exception types."""
    RETRY = auto()              # Retry with exponential backoff
    SWITCH_ACCOUNT = auto()     # Switch to another account and retry
    COOLDOWN = auto()           # Put account on cooldown, then retry
    LONG_COOLDOWN = auto()      # Extended cooldown (4x normal)
    SKIP = auto()               # Skip this operation entirely
    ABORT = auto()              # Abort the entire batch


@dataclass
class ExceptionPolicy:
    """Policy for handling a specific exception type."""
    strategy: RecoveryStrategy
    message: str                # User-friendly error message
    cooldown_minutes: Optional[int] = None  # Cooldown duration if applicable
    is_rate_limit: bool = False  # Whether this is a rate-limiting issue


# Comprehensive exception mapping
EXCEPTION_POLICY_MAP: Dict[Type[Exception], ExceptionPolicy] = {
    # --- Non-retryable client errors ---
    instaloader.exceptions.ProfileNotExistsException: ExceptionPolicy(
        strategy=RecoveryStrategy.SKIP,
        message="Profile does not exist",
    ),
    instaloader.exceptions.PrivateProfileNotFollowedException: ExceptionPolicy(
        strategy=RecoveryStrategy.SKIP,
        message="Cannot access private profile (not following)",
    ),
    instaloader.exceptions.LoginRequiredException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="Login required - trying different account",
    ),
    instaloader.exceptions.BadCredentialsException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="Invalid credentials - switching account",
    ),
    instaloader.exceptions.TwoFactorAuthRequiredException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="2FA required - switching account",
    ),
    instaloader.exceptions.LoginException: ExceptionPolicy(
        strategy=RecoveryStrategy.SWITCH_ACCOUNT,
        message="Login failed - trying different account",
    ),
    
    # --- Rate limiting and temporary blocks ---
    instaloader.exceptions.ConnectionException: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Connection error - will retry",
        is_rate_limit=True,
    ),
    instaloader.exceptions.QueryReturnedBadRequestException: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Bad request - will retry",
        is_rate_limit=True,
    ),
    instaloader.exceptions.QueryReturnedNotFoundException: ExceptionPolicy(
        strategy=RecoveryStrategy.SKIP,
        message="Query returned not found",
    ),
    instaloader.exceptions.QueryReturnedForbiddenException: ExceptionPolicy(
        strategy=RecoveryStrategy.LONG_COOLDOWN,
        message="Forbidden - extended cooldown required",
        cooldown_minutes=60,
        is_rate_limit=True,
    ),
    instaloader.exceptions.TooManyRequestsException: ExceptionPolicy(
        strategy=RecoveryStrategy.COOLDOWN,
        message="Rate limited - cooling down",
        cooldown_minutes=15,
        is_rate_limit=True,
    ),
    instaloader.exceptions.BadResponseException: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Bad response - will retry",
        is_rate_limit=True,
    ),
    
    # --- Network and system errors ---
    ConnectionError: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Network connection error - will retry",
        is_rate_limit=True,
    ),
    TimeoutError: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="Request timeout - will retry",
        is_rate_limit=True,
    ),
    OSError: ExceptionPolicy(
        strategy=RecoveryStrategy.RETRY,
        message="System error - will retry",
        is_rate_limit=True,
    ),
}


def get_exception_policy(exception: Exception) -> Optional[ExceptionPolicy]:
    """
    Get the recovery policy for an exception.
    
    Searches the exception hierarchy to find the most specific policy.
    Returns None if no policy is found (treat as non-recoverable).
    """
    # Check exact type first
    exc_type = type(exception)
    if exc_type in EXCEPTION_POLICY_MAP:
        return EXCEPTION_POLICY_MAP[exc_type]
    
    # Check base classes (MRO)
    for base_class in exc_type.__mro__[1:]:  # Skip the exact type
        if base_class in EXCEPTION_POLICY_MAP:
            return EXCEPTION_POLICY_MAP[base_class]
    
    # Check for rate limit phrases in error message as fallback
    error_msg = str(exception).lower()
    rate_limit_phrases = (
        "please wait a few minutes",
        "rate limit",
        "too many requests",
        "temporarily blocked",
        "401 unauthorized",
        "try again later",
    )
    if any(phrase in error_msg for phrase in rate_limit_phrases):
        return ExceptionPolicy(
            strategy=RecoveryStrategy.COOLDOWN,
            message="Rate limit detected (from message)",
            cooldown_minutes=15,
            is_rate_limit=True,
        )
    
    challenge_phrases = (
        "checkpoint_required",
        "challenge_required",
        "consent_required",
        "feedback_required",
        "login_required",
        "suspicious activity",
        "account has been disabled",
        "your account has been temporarily locked",
    )
    if any(phrase in error_msg for phrase in challenge_phrases):
        return ExceptionPolicy(
            strategy=RecoveryStrategy.LONG_COOLDOWN,
            message="Challenge required (from message)",
            cooldown_minutes=60,
        )
    
    return None


def is_retryable_exception(exception: Exception) -> bool:
    """Check if an exception should be retried."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.strategy in (
        RecoveryStrategy.RETRY,
        RecoveryStrategy.COOLDOWN,
        RecoveryStrategy.LONG_COOLDOWN,
    )


def should_switch_account(exception: Exception) -> bool:
    """Check if account switching is the appropriate recovery."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.strategy == RecoveryStrategy.SWITCH_ACCOUNT


def get_cooldown_minutes(exception: Exception) -> Optional[int]:
    """Get cooldown duration for an exception, if applicable."""
    policy = get_exception_policy(exception)
    if policy is None:
        return None
    return policy.cooldown_minutes


def is_rate_limit_exception(exception: Exception) -> bool:
    """Check if exception is rate-limit related."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.is_rate_limit


def format_exception_message(exception: Exception) -> str:
    """
    Format an exception with its recovery strategy for logging.
    
    Returns a user-friendly message including the recovery action.
    """
    policy = get_exception_policy(exception)
    if policy is None:
        return f"Non-recoverable error: {exception}"
    
    strategy_names = {
        RecoveryStrategy.RETRY: "will retry",
        RecoveryStrategy.SWITCH_ACCOUNT: "switching account",
        RecoveryStrategy.COOLDOWN: f"cooldown {policy.cooldown_minutes}m",
        RecoveryStrategy.LONG_COOLDOWN: f"extended cooldown {policy.cooldown_minutes}m",
        RecoveryStrategy.SKIP: "skipping",
        RecoveryStrategy.ABORT: "aborting batch",
    }
    
    action = strategy_names.get(policy.strategy, "unknown action")
    return f"{policy.message} [{action}]"


# Legacy compatibility functions
def is_challenge_exception(exception: Exception) -> bool:
    """Check if exception requires manual intervention (legacy)."""
    policy = get_exception_policy(exception)
    if policy is None:
        return False
    return policy.strategy == RecoveryStrategy.LONG_COOLDOWN


def is_account_switch_exception(exception: Exception) -> bool:
    """Check if exception should trigger account switching (legacy)."""
    return should_switch_account(exception)


__all__ = [
    "RecoveryStrategy",
    "ExceptionPolicy",
    "EXCEPTION_POLICY_MAP",
    "get_exception_policy",
    "is_retryable_exception",
    "should_switch_account",
    "get_cooldown_minutes",
    "is_rate_limit_exception",
    "format_exception_message",
    "is_challenge_exception",  # Legacy
    "is_account_switch_exception",  # Legacy
]

# Following-Based Media Downloader
# Downloads media only from accounts you are following with account selection and resume capability

import os
import time
import instaloader
from datetime import datetime
from src.account_manager import InstagramAccountManager
from src.profile_access_tracker import ProfileAccessTracker
from src.media_utils import get_profile, summarize_profile, profile_access_blocked
from src.rate_limiter import RateLimiter
from src.io_utils import retry_with_backoff
from src.config import (
    INSTAGRAM_ACCOUNTS, DATA_DIR, get_downloads_directory,
    ENUM_PAUSE_EVERY, ENUM_PAUSE_SECONDS,
    DOWNLOAD_PAUSE_EVERY, DOWNLOAD_PAUSE_SECONDS,
    MAX_RETRIES, RETRY_BASE_DELAY, RETRY_MAX_DELAY,
    RATE_LIMIT_PHRASES, ACCOUNT_SWITCH_PHRASES, CHALLENGE_PHRASES,
)
from src.account_cooldown import AccountCooldownManager

class FollowingMediaDownloader:
    """
    Download media (photos, videos, stories, highlights) only from accounts you are following.
    Features:
    - Interactive account selection
    - Following-only filtering  
    - Progress tracking with resume capability
    - Batch processing of all followed accounts
    """
    
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.manager = InstagramAccountManager()
        self.access_tracker = ProfileAccessTracker()

        self.loader = None
        self.current_account = None
        self.downloads_dir = None

        # State containers
        self.following_list = []
        # Central rate limiter (replaces scattered time.sleep calls)
        self.rate = RateLimiter(label="following")

        # ADD: Account rotation support (BUG-003 fix)
        self.available_accounts = INSTAGRAM_ACCOUNTS.copy()
        self.current_account_index = 0
        self.cooldown_manager = AccountCooldownManager()

        # Progress tracking — stored in DB operation_progress table
        self._op_id = "following_media_download"
        from db.repositories.operation_progress_repository import OperationProgressRepository
        import os as _os
        from db.manager import DatabaseManager
        _db = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
        self._progress_repo = OperationProgressRepository(_db)
        self.download_state = self._load_download_state()
    
    def _load_download_state(self):
        """Load download progress state from DB."""
        try:
            state = self._progress_repo.get_batch_state(self._op_id)
            if state:
                print(f"[RESUME] Loaded download state for {len(state.get('completed_accounts', []))} completed accounts")
                return state
        except Exception as e:
            print(f"[WARNING] Error loading download state: {e}")

        return {
            'account_used': None,
            'started_at': None,
            'last_updated': None,
            'completed_accounts': [],
            'failed_accounts': [],
            'current_account_progress': {},
            'total_stats': {
                'photos': 0,
                'videos': 0,
                'stories': 0,
                'highlights': 0,
                'profile_photos': 0
            }
        }

    def _save_download_state(self):
        """Save download progress state to DB."""
        try:
            self.download_state['last_updated'] = datetime.now().isoformat()
            self._progress_repo.upsert_batch_state(self._op_id, self.download_state)
        except Exception as e:
            print(f"[ERROR] Failed to save download state: {e}")

    def cleanup(self):
        """Cleanup resources"""
        if self.manager:
            self.manager.logout()
    
    def _switch_account(self):
        """Switch to the next available (non-cooldown) account"""
        if len(self.available_accounts) <= 1:
            print("[WARNING] No other accounts available to switch to")
            return False
        
        # Store previous account for logging
        previous_account = self.available_accounts[self.current_account_index]
        
        # Find next account that is NOT on cooldown
        account_names = [a['name'] for a in self.available_accounts]
        available = self.cooldown_manager.get_available_accounts(account_names)
        
        if not available:
            # All accounts on cooldown — pick the one with shortest remaining cooldown
            print("[WARNING] All accounts on cooldown — picking least-cooled account")
            next_index = (self.current_account_index + 1) % len(self.available_accounts)
        else:
            # Pick the next available account (round-robin)
            next_index = None
            for offset in range(1, len(self.available_accounts) + 1):
                candidate = (self.current_account_index + offset) % len(self.available_accounts)
                if self.available_accounts[candidate]['name'] in available:
                    next_index = candidate
                    break
            if next_index is None:
                next_index = (self.current_account_index + 1) % len(self.available_accounts)
        
        self.current_account_index = next_index
        current_account = self.available_accounts[self.current_account_index]
        
        print(f"[RESUME] Switching from {previous_account['name']} to {current_account['name']} ({current_account['username']})")
        
        # Logout current session
        if self.manager:
            try:
                self.manager.logout()
                print("[UPLOAD] Logged out from previous account")
            except:
                pass
        
        # Login to new account
        print(f"🔐 Logging in as {current_account['username']}...")
        if self.manager.login(current_account):
            self.loader = self.manager.loader
            self.current_account = current_account
            
            # Update download state
            self.download_state['account_used'] = current_account['name']
            self._save_download_state()
            
            print(f"✅ Successfully switched to {current_account['name']}")
            return True
        else:
            print(f"❌ Failed to switch to {current_account['name']}")
            return False
    
    def select_account(self):
        """Interactive account selection"""
        print("\n🔐 Account Selection")
        print("=" * 50)
        
        if not INSTAGRAM_ACCOUNTS:
            print("❌ No accounts configured in config.py")
            return False
        
        print("Available Instagram accounts:")
        for i, account in enumerate(INSTAGRAM_ACCOUNTS):
            print(f"{i+1}. {account['name']} ({account['username']})")
        
        while True:
            try:
                choice = input(f"\nSelect account (1-{len(INSTAGRAM_ACCOUNTS)}): ").strip()
                if not choice:
                    print("❌ Please select an account")
                    continue
                
                account_index = int(choice) - 1
                if 0 <= account_index < len(INSTAGRAM_ACCOUNTS):
                    selected_account = INSTAGRAM_ACCOUNTS[account_index]
                    self.current_account_index = account_index  # ADD: Track account index (BUG-003 fix)
                    break
                else:
                    print(f"❌ Invalid choice. Please enter 1-{len(INSTAGRAM_ACCOUNTS)}")
            except ValueError:
                print("❌ Please enter a valid number")
        
        print(f"\n🔑 Logging in as {selected_account['username']}...")
        
        # Login to the selected account
        if self.manager.login(selected_account):
            self.loader = self.manager.loader
            self.current_account = selected_account
            self.current_account_index = INSTAGRAM_ACCOUNTS.index(selected_account)  # ADD: Track index (BUG-003 fix)
            print(f"✅ Successfully logged in as {selected_account['username']}")
            
            # Update download state
            self.download_state['account_used'] = selected_account['name']
            if not self.download_state['started_at']:
                self.download_state['started_at'] = datetime.now().isoformat()
            self._save_download_state()
            
            return True
        else:
            print(f"❌ Failed to login as {selected_account['username']}")
            return False
    
    def get_following_list(self):
        """Get list of accounts the logged-in user is following."""
        if not self.loader or not self.current_account:
            print("❌ No authenticated account available")
            return []
        
        print(f"\n👥 Collecting following list for {self.current_account['username']}...")
        
        def _fetch_followees():
            profile = instaloader.Profile.from_username(self.loader.context, self.current_account['username'])
            usernames = []
            print("📋 Collecting following list...")
            for i, followee in enumerate(profile.get_followees()):
                usernames.append(followee.username)
                # Configurable enumeration pacing
                self.rate.periodic(i + 1, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
            return usernames
        
        result = retry_with_backoff(
            _fetch_followees,
            max_retries=MAX_RETRIES,
            base_delay=RETRY_BASE_DELAY,
            max_delay=RETRY_MAX_DELAY,
            label="following_list",
        )
        
        if result is None:
            print("❌ Failed to collect following list after retries")
            print("💡 Try again later when Instagram rate limits have reset")
            return []
        
        self.following_list = result
        print(f"✅ Found {len(result)} accounts you are following")
        return result
    
    def setup_downloads_directory(self):
        """Setup downloads directory"""
        if not self.downloads_dir:
            self.downloads_dir = get_downloads_directory()
        
        # Create account-specific subdirectory
        account_downloads_dir = os.path.join(
            self.downloads_dir, 
            f"following_media_{self.current_account['name']}"
        )
        os.makedirs(account_downloads_dir, exist_ok=True)
        
        self.downloads_dir = account_downloads_dir
        print(f"📁 Downloads will be saved to: {self.downloads_dir}")
        
        return self.downloads_dir
    
    def download_account_media(self, username, max_account_switches=3):
        """Download all media types for a specific account with account rotation"""
        if username not in self.following_list:
            print(f"⚠️  Skipping {username} - not in following list")
            return False
        
        print(f"\n📥 Downloading media for {username}")
        
        # Retry with account switching (BUG-003 fix)
        for attempt in range(max_account_switches + 1):
            try:
                return self._download_account_media_internal(username)
            except Exception as e:
                error_msg = str(e).lower()
                print(f"[WARNING] Download attempt {attempt+1} failed: {e}")
                
                # Check if error requires account switch
                if any(phrase in error_msg for phrase in RATE_LIMIT_PHRASES + ACCOUNT_SWITCH_PHRASES):
                    print(f"[RESUME] Rate limit or auth issue - will retry with different account")
                    if attempt < max_account_switches:
                        # Put current account on cooldown
                        self.cooldown_manager.put_on_cooldown(
                            self.current_account['name'],
                            cooldown_minutes=15,
                            reason="rate-limit"
                        )
                        # Switch to next account
                        if not self._switch_account():
                            print(f"[ERROR] No more accounts available to switch")
                            break
                    else:
                        print(f"[ERROR] All accounts exhausted after {max_account_switches} switches")
                        break
                else:
                    # Non-retryable error
                    print(f"[ERROR] Non-retryable error, marking as failed")
                    if username not in self.download_state['failed_accounts']:
                        self.download_state['failed_accounts'].append(username)
                    self._save_download_state()
                    return False
        
        # All attempts failed
        print(f"[ERROR] Download failed for {username} after all retry attempts")
        if username not in self.download_state['failed_accounts']:
            self.download_state['failed_accounts'].append(username)
        self._save_download_state()
        return False
    
    def _download_account_media_internal(self, username):
        """Internal download method."""
        user_dir = os.path.join(self.downloads_dir, f"user_{username}")
        os.makedirs(user_dir, exist_ok=True)
        self.loader.dirname_pattern = user_dir

        results = {'profile_photo': False, 'posts': False, 'stories': False, 'highlights': False}

        profile = retry_with_backoff(
            get_profile, self.loader, username,
            max_retries=MAX_RETRIES,
            base_delay=RETRY_BASE_DELAY,
            max_delay=RETRY_MAX_DELAY,
            label=f"following_profile:{username}",
        )

        if profile is None:
            print(f"❌ Could not access profile for {username} after retries")
            raise RuntimeError(f"Profile {username} not accessible")

        self.access_tracker.record_profile_access(username, self.current_account['name'], {
            'can_access': True,
            'is_public': not profile.is_private,
            'is_followed': True,
        })

        if profile_access_blocked(profile):
            print(f"🔒 Profile {username} not accessible (private & not followed)")
            return False

        # 1. Profile photo
        try:
            print(f"  📸 Downloading profile photo...")
            result = retry_with_backoff(
                self.loader.download_pic,
                filename=os.path.join(user_dir, f"{username}_profile"),
                url=profile.profile_pic_url,
                mtime=None,
                max_retries=2,
                base_delay=RETRY_BASE_DELAY,
                max_delay=RETRY_MAX_DELAY,
                label=f"following_pfp:{username}",
            )
            if result is not None:
                results['profile_photo'] = True
                self.download_state['total_stats']['profile_photos'] += 1
                print(f"  ✅ Profile photo downloaded")
            else:
                print(f"  ❌ Profile photo failed after retries")
        except Exception as e:
            print(f"  ❌ Profile photo failed: {e}")

        # 2. Posts
        try:
            print(f"  📱 Downloading posts...")
            post_count = 0
            for post in profile.get_posts():
                result = retry_with_backoff(
                    self.loader.download_post, post, username,
                    max_retries=2,
                    base_delay=RETRY_BASE_DELAY,
                    max_delay=RETRY_MAX_DELAY,
                    label=f"following_post:{username}",
                )
                if result is None:
                    print(f"    ❌ Skipping post after retries")
                    continue
                post_count += 1
                if post.is_video:
                    self.download_state['total_stats']['videos'] += 1
                else:
                    self.download_state['total_stats']['photos'] += 1
                self.rate.periodic(post_count, every=DOWNLOAD_PAUSE_EVERY, seconds=DOWNLOAD_PAUSE_SECONDS)
            if post_count > 0:
                results['posts'] = True
                print(f"  ✅ Downloaded {post_count} posts")
            else:
                print(f"  📝 No posts found")
        except Exception as e:
            print(f"  ❌ Posts download failed: {e}")

        # 3. Stories
        try:
            print(f"  📚 Downloading stories...")
            story_count = 0
            for story in self.loader.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    result = retry_with_backoff(
                        self.loader.download_storyitem, item, username,
                        max_retries=2,
                        base_delay=RETRY_BASE_DELAY,
                        max_delay=RETRY_MAX_DELAY,
                        label=f"following_story:{username}",
                    )
                    if result is None:
                        continue
                    story_count += 1
                    self.download_state['total_stats']['stories'] += 1
            if story_count > 0:
                results['stories'] = True
                print(f"  ✅ Downloaded {story_count} story items")
            else:
                print(f"  📝 No active stories found")
        except Exception as e:
            print(f"  ❌ Stories download failed: {e}")

        # 4. Highlights
        try:
            print(f"  ⭐ Downloading highlights...")
            highlight_count = 0
            for highlight in self.loader.get_highlights(profile):
                highlight_name = highlight.title or f"highlight_{highlight.unique_id}"
                print(f"    📥 Downloading highlight: {highlight_name}")
                for item in highlight.get_items():
                    result = retry_with_backoff(
                        self.loader.download_storyitem, item, f"{username}_highlights_{highlight_name}",
                        max_retries=2,
                        base_delay=RETRY_BASE_DELAY,
                        max_delay=RETRY_MAX_DELAY,
                        label=f"following_hl:{username}",
                    )
                    if result is None:
                        continue
                    highlight_count += 1
                    self.download_state['total_stats']['highlights'] += 1
            if highlight_count > 0:
                results['highlights'] = True
                print(f"  ✅ Downloaded {highlight_count} highlight items")
            else:
                print(f"  📝 No highlights found")
        except Exception as e:
            print(f"  ❌ Highlights download failed: {e}")

        success_count = sum(1 for v in results.values() if v)
        print(f"📊 Download summary for {username}: {success_count}/4 categories successful")

        if username not in self.download_state['completed_accounts']:
            self.download_state['completed_accounts'].append(username)
        if username in self.download_state['failed_accounts']:
            self.download_state['failed_accounts'].remove(username)

        self._save_download_state()
        return True

    def download_single_account(self, username):
        """Download media from a specific account (must be in following list)"""
        if not self.current_account:
            if not self.select_account():
                return False
        
        if not self.following_list:
            self.get_following_list()
        
        if not self.downloads_dir:
            self.setup_downloads_directory()
        
        if username not in self.following_list:
            print(f"❌ {username} is not in your following list")
            print(f"💡 You can only download from accounts you are following")
            return False
        
        return self.download_account_media(username)
    
    def download_all_following(self):
        """Download media from all accounts in following list"""
        print("\n🎯 Starting batch download from all followed accounts")
        
        # Setup
        if not self.current_account:
            if not self.select_account():
                return False
        
        if not self.following_list:
            self.get_following_list()
        
        if not self.downloads_dir:
            self.setup_downloads_directory()
        
        if not self.following_list:
            print("❌ No following list available")
            return False
        
        # Show resume info if applicable
        completed = self.download_state.get('completed_accounts', [])
        failed = self.download_state.get('failed_accounts', [])
        remaining = [u for u in self.following_list if u not in completed and u not in failed]
        
        print(f"\n📊 Batch Download Status:")
        print(f"   ✅ Completed: {len(completed)} accounts")
        print(f"   ❌ Failed: {len(failed)} accounts") 
        print(f"   ⏳ Remaining: {len(remaining)} accounts")
        print(f"   📊 Total following: {len(self.following_list)} accounts")
        
        if completed:
            print(f"\n💡 Resuming from where you left off...")
        
        # Confirm start
        if remaining:
            confirm = input(f"\nStart downloading from {len(remaining)} remaining accounts? (y/n): ").strip().lower()
            if confirm != 'y':
                print("❌ Download cancelled")
                return False
        else:
            print("✅ All accounts already processed!")
            return True
        
        # Process remaining accounts
        successful = 0
        failed_count = 0
        
        for i, username in enumerate(remaining):
            print(f"\n📥 Processing {i+1}/{len(remaining)}: {username}")
            
            try:
                if self.download_account_media(username):
                    successful += 1
                else:
                    failed_count += 1
                
                # Progress update every 10 accounts
                if (i + 1) % 10 == 0:
                    print(f"\n📊 Progress Update: {i+1}/{len(remaining)} processed")
                    print(f"   ✅ Successful: {successful}")
                    print(f"   ❌ Failed: {failed_count}")
                    self.rate.periodic(i + 1, every=10, seconds=30)
                else:
                    self.rate.user_delay(multiplier=2)
                    
            except KeyboardInterrupt:
                print(f"\n⚠️  Download interrupted by user")
                print(f"📊 Progress saved. You can resume later.")
                break
            except Exception as e:
                print(f"❌ Unexpected error processing {username}: {e}")
                failed_count += 1
                continue
        
        # Final summary
        total_completed = len(self.download_state.get('completed_accounts', []))
        total_failed = len(self.download_state.get('failed_accounts', []))
        
        print(f"\n🎉 Batch Download Complete!")
        print(f"=" * 50)
        print(f"📊 Final Statistics:")
        print(f"   🎯 Total following: {len(self.following_list)}")
        print(f"   ✅ Successfully processed: {total_completed}")
        print(f"   ❌ Failed: {total_failed}")
        print(f"   📁 Downloads saved to: {self.downloads_dir}")
        
        # Media statistics
        stats = self.download_state.get('total_stats', {})
        print(f"\n📱 Media Downloaded:")
        print(f"   📸 Profile photos: {stats.get('profile_photos', 0)}")
        print(f"   🖼️  Photos: {stats.get('photos', 0)}")
        print(f"   🎥 Videos: {stats.get('videos', 0)}")
        print(f"   📚 Stories: {stats.get('stories', 0)}")
        print(f"   ⭐ Highlights: {stats.get('highlights', 0)}")
        
        return True
    
    def show_progress(self):
        """Show current download progress"""
        print(f"\n📊 Download Progress Report")
        print("=" * 50)
        
        if not self.download_state.get('account_used'):
            print("❌ No download session found")
            return
        
        account_name = self.download_state['account_used']
        started_at = self.download_state.get('started_at', 'Unknown')
        last_updated = self.download_state.get('last_updated', 'Unknown')
        
        completed = self.download_state.get('completed_accounts', [])
        failed = self.download_state.get('failed_accounts', [])
        
        print(f"🔐 Account: {account_name}")
        print(f"⏰ Started: {started_at}")
        print(f"🔄 Last updated: {last_updated}")
        print(f"✅ Completed: {len(completed)} accounts")
        print(f"❌ Failed: {len(failed)} accounts")
        
        # Media statistics
        stats = self.download_state.get('total_stats', {})
        print(f"\n📱 Media Downloaded:")
        print(f"   📸 Profile photos: {stats.get('profile_photos', 0)}")
        print(f"   🖼️  Photos: {stats.get('photos', 0)}")
        print(f"   🎥 Videos: {stats.get('videos', 0)}")
        print(f"   📚 Stories: {stats.get('stories', 0)}")
        print(f"   ⭐ Highlights: {stats.get('highlights', 0)}")
        
        if failed:
            print(f"\n❌ Failed accounts ({len(failed)}):")
            for username in failed[-10:]:  # Show last 10 failed
                print(f"   • {username}")
            if len(failed) > 10:
                print(f"   ... and {len(failed) - 10} more")
    
    def reset_progress(self):
        """Reset download progress"""
        confirm = input("⚠️  Are you sure you want to reset all download progress? (y/n): ").strip().lower()
        if confirm == 'y':
            self.download_state = {
                'account_used': None,
                'started_at': None,
                'last_updated': None,
                'completed_accounts': [],
                'failed_accounts': [],
                'current_account_progress': {},
                'total_stats': {
                    'photos': 0,
                    'videos': 0,
                    'stories': 0,
                    'highlights': 0,
                    'profile_photos': 0
                }
            }
            self._save_download_state()
            print("✅ Download progress reset")
        else:
            print("❌ Reset cancelled")
    
        return True

def interactive_menu():
    """Interactive menu for following media downloader"""
    downloader = FollowingMediaDownloader()
    
    while True:
        print(f"\n📱 Following Media Downloader")
        print("=" * 50)
        print("1. Download from specific account (following only)")
        print("2. Download from ALL followed accounts (batch)")
        print("3. Show download progress")
        print("4. Reset download progress")
        print("5. Exit")
        
        choice = input("\nSelect option (1-5): ").strip()
        
        try:
            if choice == "1":
                username = input("Enter Instagram username: ").strip()
                if username:
                    downloader.download_single_account(username)
                else:
                    print("❌ Please enter a valid username")
                    
            elif choice == "2":
                downloader.download_all_following()
                
            elif choice == "3":
                downloader.show_progress()
                
            elif choice == "4":
                downloader.reset_progress()
                
            elif choice == "5":
                print("👋 Goodbye!")
                downloader.cleanup()
                break
                
            else:
                print("❌ Invalid choice. Please try again.")
                
        except KeyboardInterrupt:
            print(f"\n⚠️  Operation interrupted")
        except Exception as e:
            print(f"❌ Error: {e}")
        
        if choice != "5":
            import sys, termios, tty
            try:
                # Flush any buffered/stray input before waiting
                import msvcrt
                while msvcrt.kbhit():
                    msvcrt.getwch()
                print("\nPress Enter to continue...")
                while True:
                    ch = msvcrt.getwch()
                    if ch in ('\r', '\n'):
                        break
            except ImportError:
                # Unix fallback
                try:
                    fd = sys.stdin.fileno()
                    old = termios.tcgetattr(fd)
                    try:
                        tty.setraw(fd)
                        termios.tcflush(fd, termios.TCIFLUSH)
                    finally:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                except Exception:
                    pass
                input("\nPress Enter to continue...")




"""Shared I/O utilities for safe file operations and network retry logic.

Provides:
- safe_json_write: Atomic JSON writes (temp file + rename) to prevent corruption
- retry_with_backoff: Retry wrapper with exponential backoff for Instaloader API calls
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import time
TypeVar

import instaloader.exceptions
from src.exception_handler import (
    is_retryable_exception,
    is_rate_limit_exception,
    format_exception_message,
)

T = TypeVar("T")

# --------------- Atomic JSON Writes ---------------

def safe_json_write(path: str, data: Any, indent: int = 2) -> None:
    """Write JSON data atomically: write to temp file, then rename over target.

    This prevents data corruption if the process is killed mid-write.
    The temp file is created in the same directory so os.replace is always
    an atomic same-filesystem rename.
    """
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp", prefix=".safe_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
        os.replace(tmp_path, path)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# --------------- Retry with Exponential Backoff ---------------


def retry_with_backoff(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 30.0,
    max_delay: float = 600.0,
    label: str = "",
    **kwargs: Any,
) -> Optional[T]:
    """Call *func* with retries and exponential backoff on transient failures.

    Uses the centralized exception handling system to determine which exceptions
    are retryable. Non-retryable exceptions (e.g. ProfileNotExistsException) 
    propagate immediately.
    
    Returns the function result, or None if all retries are exhausted.
    """
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            # Use centralized exception handling
            if not is_retryable_exception(exc):
                # Non-retryable exception - propagate immediately
                raise
            
            # Determine if this is a rate limit exception
            rate_limit = is_rate_limit_exception(exc)
            
            if attempt >= max_retries:
                tag = f"[RETRY:{label}]" if label else "[RETRY]"
                print(f"{tag} All {max_retries} retries exhausted: {exc}")
                return None

            # Exponential backoff with jitter
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 10), max_delay)
            if rate_limit:
                delay = max(delay, 300)  # At least 5 min on rate-limit

            tag = f"[RETRY:{label}]" if label else "[RETRY]"
            print(f"{tag} Attempt {attempt + 1}/{max_retries} failed: {format_exception_message(exc)}")
            print(f"{tag} Retrying in {delay:.0f}s...")
            time.sleep(delay)

    return None  # Shouldn't reach here, but for safety


# --------------- File Locking (Cross-Platform) ---------------

class FileLock:
    """Cross-platform file locking to prevent concurrent writes.
    
    Uses msvcrt on Windows and fcntl on Unix-like systems.
    
    Usage:
        with FileLock("/path/to/file"):
            safe_json_write("/path/to/file", data)
    """
    
    def __init__(self, filepath: str, timeout: float = 10.0):
        """
        Initialize file lock.
        
        Args:
            filepath: Path to the file to lock
            timeout: Maximum time to wait for lock (seconds)
        """
        self.filepath = filepath
        self.lockfile = f"{filepath}.lock"
        self.timeout = timeout
        self._lockfile_obj = None
        self._locked = False
        self._platform = os.name  # 'nt' for Windows, 'posix' for Unix
    
    def acquire(self) -> bool:
        """Acquire file lock with timeout.
        
        Returns:
            True if lock acquired, False if timeout
        """
        start_time = time.time()
        
        if self._platform == 'nt':
            return self._acquire_windows(start_time)
        else:
            return self._acquire_unix(start_time)
    
    def _acquire_unix(self, start_time: float) -> bool:
        """Unix file locking using fcntl."""
        import fcntl
        
        while time.time() - start_time < self.timeout:
            try:
                self._lockfile_obj = open(self.lockfile, 'w')
                fcntl.flock(self._lockfile_obj.fileno(), 
                          fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return True
            except (IOError, OSError) as e:
                if self._lockfile_obj:
                    self._lockfile_obj.close()
                    self._lockfile_obj = None
                # Check if timeout reached
                if time.time() - start_time >= self.timeout:
                    return False
                # Wait and retry
                time.sleep(0.1)
        
        return False
    
    def _acquire_windows(self, start_time: float) -> bool:
        """Windows file locking using msvcrt."""
        import msvcrt
        
        while time.time() - start_time < self.timeout:
            try:
                self._lockfile_obj = os.open(
                    self.lockfile,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR  # O_EXCL makes creation atomic
                )
                # Try to lock first byte
                msvcrt.locking(self._lockfile_obj, msvcrt.LK_NBLCK, 1)
                self._locked = True
                return True
            except FileExistsError:  # Lock held by another process
                if self._lockfile_obj:
                    try:
                        os.close(self._lockfile_obj)
                    except:
                        pass
                    self._lockfile_obj = None
                # Check if timeout reached
                if time.time() - start_time >= self.timeout:
                    return False
                # Wait and retry
                time.sleep(0.1)
            except (OSError, IOError):
                if self._lockfile_obj:
                    try:
                        os.close(self._lockfile_obj)
                    except:
                        pass
                    self._lockfile_obj = None
                # Check if timeout reached
                if time.time() - start_time >= self.timeout:
                    return False
                # Wait and retry
                time.sleep(0.1)
        
        return False
    
    def release(self):
        """Release file lock."""
        if not self._locked:
            return
        
        try:
            if self._platform == 'nt':
                import msvcrt
                if self._lockfile_obj:
                    # Unlock
                    msvcrt.locking(self._lockfile_obj, msvcrt.LK_UNLCK, 1)
                    os.close(self._lockfile_obj)
            else:
                import fcntl
                if self._lockfile_obj:
                    fcntl.flock(self._lockfile_obj.fileno(), fcntl.LOCK_UN)
                    self._lockfile_obj.close()
            
            # Remove lock file
            try:
                os.unlink(self.lockfile)
            except (OSError, FileNotFoundError):
                pass
                
        except Exception:
            # Ignore cleanup errors
            pass
        finally:
            self._locked = False
            self._lockfile_obj = None
    
    def __enter__(self):
        """Context manager entry."""
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock for {self.filepath} within {self.timeout}s")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release()
        return False  # Don't suppress exceptions


__all__ = [
    "safe_json_write",
    "retry_with_backoff", 
    "FileLock",
]

"""Shared media download utilities.

Provides helper functions to reduce duplication between MediaDownloader and
FollowingMediaDownloader for profile retrieval and common safety checks.
"""
from __future__ import annotations

Dict
import instaloader


def get_profile(loader: instaloader.Instaloader, username: str) -> Optional[instaloader.Profile]:
    """Fetch an Instaloader Profile object for a username with basic validation.

    Raises retryable exceptions (ConnectionException, etc.) so that callers
    using retry_with_backoff() can retry them.  Only returns None for
    non-retryable errors like ProfileNotExistsException.
    """
    if not loader or not hasattr(loader, "context"):
        raise RuntimeError("Invalid loader context")
    try:
        return instaloader.Profile.from_username(loader.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        return None
    # All other exceptions (ConnectionException, QueryReturnedBadRequestException,
    # LoginRequiredException, etc.) propagate up to retry_with_backoff.


def is_accessible_private(profile: instaloader.Profile) -> bool:
    """Return True if profile is private but followed (accessible)."""
    if profile is None:
        return False
    return profile.is_private and getattr(profile, "followed_by_viewer", False)


def profile_access_blocked(profile: instaloader.Profile) -> bool:
    """Return True if profile is private and not followed (blocked content)."""
    if profile is None:
        return True
    return profile.is_private and not getattr(profile, "followed_by_viewer", False)


def summarize_profile(profile: instaloader.Profile) -> Dict[str, object]:
    """Produce a lightweight summary dict for debug logs or analytics."""
    if profile is None:
        return {"exists": False}
    return {
        "exists": True,
        "username": profile.username,
        "is_private": profile.is_private,
        "followed_by_viewer": getattr(profile, "followed_by_viewer", None),
        "has_blocked_viewer": getattr(profile, "has_blocked_viewer", None),
    }

__all__ = [
    "get_profile",
    "is_accessible_private",
    "profile_access_blocked",
    "summarize_profile",
]

"""
Operation classification system for Instagram operations.

This module defines the data models for classifying operations by their access
requirements and rate limit sensitivity, and provides the OperationClassifier
class for querying operation metadata.
"""

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class OperationType(Enum):
    """Classification of operations by access requirements."""
    
    PUBLIC = "public"  # Any account can perform
    FOLLOWING_REQUIRED = "following_required"  # Must follow target
    MUTUAL_FOLLOWING = "mutual_following"  # Must be mutually following


@dataclass
class OperationMetadata:
    """Metadata for an Instagram operation.
    
    Attributes:
        name: Operation name (e.g., "download_stories")
        operation_type: Access requirement classification
        rate_limit_weight: Rate limit sensitivity (1-10 scale)
        description: Human-readable description
    """
    
    name: str
    operation_type: OperationType
    rate_limit_weight: int
    description: str
    
    def __post_init__(self):
        """Validate rate_limit_weight is in valid range."""
        if not isinstance(self.rate_limit_weight, int):
            raise ValueError(
                f"rate_limit_weight must be an integer, got {type(self.rate_limit_weight).__name__}"
            )
        
        if not (1 <= self.rate_limit_weight <= 10):
            raise ValueError(
                f"rate_limit_weight must be between 1 and 10, got {self.rate_limit_weight}"
            )
        
        if not isinstance(self.operation_type, OperationType):
            raise ValueError(
                f"operation_type must be an OperationType enum, got {type(self.operation_type).__name__}"
            )


class OperationClassifier:
    """Classifies Instagram operations by their access requirements.

    Loads the operation registry from config.py on initialization and
    validates all entries.  Unknown operations default to PUBLIC for safety.

    Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 12.1, 12.2, 12.4, 12.5
    """

    def __init__(self):
        """Initialize the classifier and validate the operation registry."""
        from config import OPERATION_REGISTRY  # imported here to avoid circular imports

        self._registry: dict[str, OperationMetadata] = {}
        self._load_and_validate_registry(OPERATION_REGISTRY)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_and_validate_registry(self, raw_registry: dict) -> None:
        """Parse and validate the raw registry dict from config.

        Raises ValueError for any entry that fails validation so that
        misconfiguration is caught at startup (Requirement 12.2).
        """
        valid_type_names = {t.name for t in OperationType}

        for op_name, entry in raw_registry.items():
            # Validate required fields (Requirement 12.5)
            for field in ("operation_type", "rate_limit_weight", "description"):
                if field not in entry:
                    raise ValueError(
                        f"Operation '{op_name}' is missing required field '{field}'"
                    )

            # Validate operation_type string (Requirement 12.4)
            op_type_str = entry["operation_type"]
            if op_type_str not in valid_type_names:
                raise ValueError(
                    f"Operation '{op_name}' has invalid operation_type '{op_type_str}'. "
                    f"Must be one of: {sorted(valid_type_names)}"
                )

            op_type = OperationType[op_type_str]

            # OperationMetadata.__post_init__ validates rate_limit_weight (Requirement 12.3)
            metadata = OperationMetadata(
                name=op_name,
                operation_type=op_type,
                rate_limit_weight=entry["rate_limit_weight"],
                description=entry["description"],
            )

            self._registry[op_name] = metadata

        logger.debug(
            "OperationClassifier loaded %d operations from registry",
            len(self._registry),
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def classify(self, operation_name: str) -> OperationType:
        """Return the OperationType for *operation_name*.

        Returns OperationType.PUBLIC as a safe default for unknown operations
        (Requirement 2.3).  The result is deterministic for the same input.
        """
        metadata = self._registry.get(operation_name)
        if metadata is None:
            logger.debug(
                "classify(): unknown operation '%s', defaulting to PUBLIC",
                operation_name,
            )
            return OperationType.PUBLIC
        return metadata.operation_type

    def requires_following(self, operation_name: str) -> bool:
        """Return True if *operation_name* requires a following relationship."""
        op_type = self.classify(operation_name)
        return op_type in (OperationType.FOLLOWING_REQUIRED, OperationType.MUTUAL_FOLLOWING)

    def is_public_operation(self, operation_name: str) -> bool:
        """Return True if *operation_name* can be performed by any account."""
        return self.classify(operation_name) == OperationType.PUBLIC

    def get_operation_metadata(self, operation_name: str) -> OperationMetadata:
        """Return full metadata for *operation_name*.

        For unknown operations a synthetic PUBLIC metadata entry is returned
        so callers always receive a valid OperationMetadata object.
        """
        metadata = self._registry.get(operation_name)
        if metadata is None:
            logger.debug(
                "get_operation_metadata(): unknown operation '%s', returning synthetic PUBLIC metadata",
                operation_name,
            )
            return OperationMetadata(
                name=operation_name,
                operation_type=OperationType.PUBLIC,
                rate_limit_weight=1,
                description="Unknown operation (defaulting to PUBLIC)",
            )
        return metadata

    def get_all_operations(self) -> dict[str, OperationMetadata]:
        """Return a copy of the full operation registry."""
        return dict(self._registry)

"""
Operation Router - Main entry point for processing Instagram operations with
smart account selection, conservative rate limiting, and error recovery.

Requirements: 7.1–7.8, 8.1–8.7
"""
from __future__ import annotations

import logging
import time
Optional

from src.operation_classifier import OperationClassifier, OperationType
from src.smart_account_selector import SmartAccountSelector
from src.conservative_rate_limiter import ConservativeRateLimiter
from src.username_database import UsernameDatabase

logger = logging.getLogger(__name__)


class RateLimitException(Exception):
    """Raised when Instagram rate-limits an account."""


def process_operation_with_smart_routing(
    operation_name: str,
    target_usernames: list[str],
    execute_fn: Callable[[str, str], bool],
    *,
    username_db: Optional[UsernameDatabase] = None,
    rate_limiter: Optional[ConservativeRateLimiter] = None,
    account_selector: Optional[SmartAccountSelector] = None,
    available_accounts: Optional[list[str]] = None,
) -> dict:
    """
    Process an Instagram operation with smart account selection and rate limiting.

    Preconditions:
    - operation_name is a registered operation name
    - target_usernames is a non-empty list of valid usernames
    - execute_fn(account_name, username) -> bool performs the actual operation
    - At least one account is available and not in cooldown

    Postconditions:
    - All usernames are processed or marked as failed
    - Username database is updated with access metadata for successes
    - Rate limits are respected throughout execution
    - Returns summary with total, success_count, failed_count

    Requirements: 7.1–7.8, 8.2, 8.6
    """
    if not target_usernames:
        return {"total": 0, "success_count": 0, "failed_count": 0,
                "results": {"success": [], "failed": []}}

    # Step 1: Classify operation (Requirement 7.1)
    classifier = OperationClassifier()
    operation_type = classifier.classify(operation_name)

    # Step 2: Initialise components
    if rate_limiter is None:
        rate_limiter = ConservativeRateLimiter()
    if username_db is None:
        username_db = UsernameDatabase()
    if account_selector is None:
        account_selector = SmartAccountSelector(username_db=username_db)

    # Step 3: Determine available accounts (Requirement 8.1)
    if available_accounts is None:
        try:
            from account_manager import InstagramAccountManager
            mgr = InstagramAccountManager()
            available_accounts = mgr.get_available_accounts(rate_limiter=rate_limiter)
        except Exception:
            available_accounts = []

    if not available_accounts:
        # All accounts in cooldown — wait for shortest cooldown to expire (Requirement 8.1)
        logger.warning("All accounts in cooldown; waiting for shortest cooldown to expire")
        _wait_for_shortest_cooldown(rate_limiter, available_accounts or [])
        # Re-fetch after waiting
        try:
            from account_manager import InstagramAccountManager
            mgr = InstagramAccountManager()
            available_accounts = mgr.get_available_accounts(rate_limiter=rate_limiter)
        except Exception:
            available_accounts = []

    if not available_accounts:
        logger.error("No available accounts after waiting — all usernames marked as failed")
        return {
            "total": len(target_usernames),
            "success_count": 0,
            "failed_count": len(target_usernames),
            "results": {"success": [], "failed": list(target_usernames)},
        }

    # Step 4: Assign usernames to accounts (Requirements 7.2, 7.3)
    account_assignment = account_selector.select_for_batch(
        operation_type, target_usernames, available_accounts
    )

    # Step 5: Process each account's batch
    results: dict[str, list[str]] = {"success": [], "failed": []}
    account_keys = list(account_assignment.keys())

    for idx, (account_name, usernames) in enumerate(account_assignment.items()):
        # Re-check availability (Requirement 7.4)
        if not rate_limiter.check_account_available(account_name):
            logger.warning("Account '%s' entered cooldown; marking %d usernames failed",
                           account_name, len(usernames))
            results["failed"].extend(usernames)
            continue

        # Process usernames for this account
        for i, username in enumerate(usernames):
            # Operation-specific delay (Requirement 7.5)
            rate_limiter.operation_delay(operation_type)

            # Progressive enumeration delay every 10 ops (Requirement 4.8)
            if i > 0 and i % 10 == 0:
                rate_limiter.following_enumeration_delay(i)

            try:
                success = execute_fn(account_name, username)

                if success:
                    results["success"].append(username)
                    # Update metadata (Requirement 7.8)
                    username_db.update_metadata(username, {
                        "last_accessed": time.time(),
                        "last_operation": operation_name,
                        "last_account": account_name,
                    })
                else:
                    results["failed"].append(username)

            except RateLimitException:
                # Emergency cooldown + mark remaining as failed (Requirement 8.2)
                logger.error("Rate limit hit on account '%s'; applying emergency cooldown", account_name)
                rate_limiter.emergency_cooldown(account_name, duration_minutes=15)
                results["failed"].extend(usernames[i:])
                break

            except Exception as exc:
                logger.error("Error processing '%s' with account '%s': %s", username, account_name, exc)
                results["failed"].append(username)

        # Account switch delay before next account (Requirement 4.5)
        if idx < len(account_keys) - 1:
            rate_limiter.account_switch_delay()

    # Step 6: Periodic database save with retry (Requirements 10.4, 8.7)
    _save_with_retry(username_db)

    return {
        "total": len(target_usernames),
        "success_count": len(results["success"]),
        "failed_count": len(results["failed"]),
        "results": results,
    }


def _wait_for_shortest_cooldown(
    rate_limiter: ConservativeRateLimiter,
    account_names: list[str],
) -> None:
    """Wait for the shortest cooldown among all accounts to expire (Requirement 8.1)."""
    if not account_names:
        return
    remaining_times = [
        rate_limiter.get_cooldown_remaining(name) for name in account_names
    ]
    min_wait = min((t for t in remaining_times if t > 0), default=0)
    if min_wait > 0:
        logger.info("Waiting %.0f seconds for shortest cooldown to expire", min_wait)
        time.sleep(min_wait + 1)  # +1s buffer


def _save_with_retry(db: UsernameDatabase, max_retries: int = 3) -> bool:
    """Save database with exponential backoff retry (Requirement 8.7)."""
    for attempt in range(max_retries):
        try:
            if db.save():
                return True
        except Exception as exc:
            logger.warning("DB save attempt %d/%d failed: %s", attempt + 1, max_retries, exc)
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    logger.error("Database save failed after %d attempts", max_retries)
    return False

# Simplified processor for Instagram operations using Instaloader
import threading
import time
import random
import os
from src.account_manager import InstagramAccountManager
from src.collect_relationships import RelationshipCollector
from src.download_media import MediaDownloader
from src.config import (
    MIN_DELAY, MAX_DELAY, INSTAGRAM_ACCOUNTS, get_downloads_directory,
    OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX,
    BREAK_DURATION_MIN, BREAK_DURATION_MAX,
    EMERGENCY_BREAK_MIN, EMERGENCY_BREAK_MAX,
    ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX,
    ACCOUNT_COOLDOWN_MINUTES,
    MAX_RETRIES, RETRY_BASE_DELAY,
    RATE_LIMIT_PHRASES, CHALLENGE_PHRASES, ACCOUNT_SWITCH_PHRASES,
    FILTER_MAX_FOLLOWERS,
)
from src.progress_manager import ProgressManager, handle_graceful_exit
from src.profile_access_tracker import ProfileAccessTracker
from src.user_metadata_manager import UserMetadataManager
from src.priority_manager import PriorityManager
from src.rate_limiter import RateLimiter, _SHUTDOWN_EVENT
from src.account_cooldown import AccountCooldownManager, AccountQuotaManager
from src.resilience import _SHUTDOWN, _interruptible_sleep

class InstagramProcessor:
    """Improved processor that handles account management, account switching, advanced rate limiting, and progress saving"""
    
    def __init__(self, account_name=None, operation_type="general"):
        self.account_name = account_name
        self.manager = InstagramAccountManager()
        self.available_accounts = INSTAGRAM_ACCOUNTS.copy()
        self.current_account_index = 0
        self.operation_count = 0
        self.downloads_dir = None  # Store downloads directory to avoid multiple prompts
        
        # Initialize progress manager
        self.progress_manager = ProgressManager(operation_type)
        
        # Initialize profile access tracker for intelligent account routing
        self.access_tracker = ProfileAccessTracker()
        
        # Initialize metadata manager for tracking profile info
        self.metadata_manager = UserMetadataManager()
        
        # Initialize priority manager for account-based prioritization
        self.priority_manager = PriorityManager()
        
        # Centralized rate limiter (replaces duplicated sleep logic)
        self.rate = RateLimiter(label="batch")
        
        # Per-account cooldown & quota managers
        self.cooldown_manager = AccountCooldownManager()
        self.quota_manager = AccountQuotaManager()

        # Graceful shutdown flag — set by signal handler or _SHUTDOWN_EVENT
        self._shutdown_requested = _SHUTDOWN_EVENT
        
        # Restore batch state if available
        if self.progress_manager.batch_state.get('current_account_index') is not None:
            self.current_account_index = self.progress_manager.batch_state['current_account_index']
            self.operation_count = self.progress_manager.batch_state.get('operation_count', 0)
            
            # Restore downloads directory if available
            if self.progress_manager.batch_state.get('downloads_directory'):
                self.downloads_dir = self.progress_manager.batch_state['downloads_directory']
        
        # Set initial account if specified
        if account_name:
            account = next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name), None)
            if account:
                self.current_account_index = INSTAGRAM_ACCOUNTS.index(account)
                print(f"[TARGET] Using specified account: {account['name']} ({account['username']})")
            else:
                print(f"[WARNING]  Account '{account_name}' not found, using default account")
        else:
            # Use default account (first in list)
            default_account = INSTAGRAM_ACCOUNTS[0] if INSTAGRAM_ACCOUNTS else None
            if default_account:
                print(f"[TARGET] Using default account: {default_account['name']} ({default_account['username']})")
        
        print(f"[RESUME] Initialized processor with {len(self.available_accounts)} available accounts")
        
        # Show resume information if available
        if self.progress_manager.can_resume():
            print("[RESUME] Found existing progress - will resume from where left off")
            self.progress_manager.print_progress_summary()

    def _get_current_account_username(self):
        """Get the username of the current account"""
        current_account = self.available_accounts[self.current_account_index]
        return current_account['username']
    
    def _get_downloads_dir(self):
        """Get downloads directory, prompting user only once"""
        if self.downloads_dir is None:
            self.downloads_dir = get_downloads_directory()
            # Save to batch state for resumption
            self.progress_manager.update_batch_state(downloads_directory=self.downloads_dir)
        return self.downloads_dir
    
    def _switch_account(self):
        """Switch to the next available (non-cooldown) account"""
        if len(self.available_accounts) <= 1:
            print("[WARNING]  No other accounts available to switch to")
            return False
        
        # Store previous account for logging
        previous_account = self.available_accounts[self.current_account_index]
        
        # Find next account that is NOT on cooldown
        account_names = [a['name'] for a in self.available_accounts]
        available = self.cooldown_manager.get_available_accounts(account_names)
        
        if not available:
            # All accounts on cooldown — pick the one with shortest remaining cooldown
            print("[WARNING]  All accounts on cooldown — picking least-cooled account")
            next_index = (self.current_account_index + 1) % len(self.available_accounts)
        else:
            # Pick the next available account (round-robin)
            next_index = None
            for offset in range(1, len(self.available_accounts) + 1):
                candidate = (self.current_account_index + offset) % len(self.available_accounts)
                if self.available_accounts[candidate]['name'] in available:
                    next_index = candidate
                    break
            if next_index is None:
                next_index = (self.current_account_index + 1) % len(self.available_accounts)
        
        self.current_account_index = next_index
        current_account = self.available_accounts[self.current_account_index]
        
        print(f"[RESUME] Switching from {previous_account['name']} to {current_account['name']} ({current_account['username']})")
        
        # Save account state
        self.progress_manager.update_batch_state(current_account_index=self.current_account_index)
        
        # Logout current session
        if self.manager:
            try:
                self.manager.logout()
                print("[UPLOAD] Logged out from previous account")
            except:
                pass
        
        # Config-driven account switch delay
        switch_delay = random.uniform(ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX)
        print(f"[WAIT] Account switch delay: {switch_delay:.1f}s")
        _interruptible_sleep(switch_delay)
        
        return True
    
    def _get_best_account_for_user(self, username):
        """Get the best account to use for accessing a specific user's profile"""
        available_account_names = [acc['name'] for acc in self.available_accounts]
        
        # Check if we have access data for this profile
        best_account = self.access_tracker.get_best_account_for_profile(username, available_account_names)
        
        if best_account:
            # Find the index of the best account
            for i, acc in enumerate(self.available_accounts):
                if acc['name'] == best_account:
                    return i
        
        # No specific preference, use current account
        return self.current_account_index
    
    def _record_access_attempt(self, username, success, error=None, is_public=None, is_followed=None):
        """Record the result of an access attempt for future intelligent routing"""
        current_account = self.available_accounts[self.current_account_index]
        
        access_result = {
            'can_access': success,
            'is_public': is_public,
            'is_followed': is_followed,
            'error': str(error) if error else None
        }
        
        self.access_tracker.record_profile_access(username, current_account['name'], access_result)
    
    def _handle_rate_limiting(self):
        """Handle advanced rate limiting with automatic long breaks via RateLimiter."""
        self.operation_count += 1
        self.progress_manager.update_batch_state(operation_count=self.operation_count)

        # Record quota usage for current account
        current_account = self.available_accounts[self.current_account_index]
        self.quota_manager.record_action(current_account['name'])

        # Regular short delay + automatic long-break when threshold is hit
        self.rate.short_delay()
        self.rate.track_operation()

        # Save progress before potential long break next time
        self.progress_manager.save_progress()
    
    def _execute_with_retry(self, operation_func, *args, **kwargs):
        """Execute an operation with account switching on failure.
        
        Uses centralized config phrases for error categorization and applies
        per-account cooldowns when rate-limits are hit.
        """
        max_retries = len(self.available_accounts)
        
        for attempt in range(max_retries):
            # Check quota before attempting
            current_account = self.available_accounts[self.current_account_index]
            if not self.quota_manager.can_perform_action(current_account['name']):
                print(f"[QUOTA] Daily quota exhausted for {current_account['name']}")
                if attempt < max_retries - 1 and self._switch_account():
                    continue
                else:
                    break
            
            try:
                result = operation_func(*args, **kwargs)
                if result:
                    return True
                else:
                    # Operation returned False — try next account
                    if attempt < max_retries - 1:
                        print(f"[RESUME] Operation failed, trying next account (attempt {attempt + 1}/{max_retries})")
                        if not self._switch_account():
                            break
                        _interruptible_sleep(random.uniform(10, 20))

            except Exception as e:
                error_msg = str(e).lower()
                
                # --- Challenge / manual-intervention errors ---
                if any(phrase in error_msg for phrase in CHALLENGE_PHRASES):
                    print(f"[CHALLENGE] Account requires manual intervention: {e}")
                    self.cooldown_manager.put_on_cooldown(
                        current_account['name'],
                        cooldown_minutes=ACCOUNT_COOLDOWN_MINUTES * 4,  # long cooldown
                    )
                    if attempt < max_retries - 1 and self._switch_account():
                        continue
                    break
                
                # --- Rate-limit / temporary block ---
                if any(phrase in error_msg for phrase in RATE_LIMIT_PHRASES):
                    print(f"[WARNING]  Rate-limit detected: {e}")
                    self.cooldown_manager.put_on_cooldown(
                        current_account['name'],
                        cooldown_minutes=ACCOUNT_COOLDOWN_MINUTES,
                    )
                    # Emergency break
                    break_time = random.randint(EMERGENCY_BREAK_MIN, EMERGENCY_BREAK_MAX)
                    print(f"[WAIT] Emergency break: {break_time} minutes")
                    _interruptible_sleep(break_time * 60)
                    if attempt < max_retries - 1 and self._switch_account():
                        continue
                    break
                
                # --- Auth / credential errors that need account switching ---
                if any(phrase in error_msg for phrase in ACCOUNT_SWITCH_PHRASES):
                    print(f"[WARNING]  Auth issue — switching account: {e}")
                    if attempt < max_retries - 1 and self._switch_account():
                        _interruptible_sleep(random.uniform(10, 20))
                        continue
                    break
                
                # --- Generic known Instagram issues ---
                if any(phrase in error_msg for phrase in ('private', 'not followed', 'fail')):
                    print(f"[WARNING]  Instagram issue: {e}")
                    if attempt < max_retries - 1 and self._switch_account():
                        _interruptible_sleep(random.uniform(10, 20))
                        continue
                    break
                
                # --- Non-recoverable error ---
                print(f"[ERROR] Non-recoverable error: {e}")
                return False
        
        print(f"[ERROR] All accounts exhausted for this operation")
        return False

    def collect_relationships(self, username, max_followers=1000, max_following=1000):
        """Collect relationships for a user with automatic retry, account switching, and progress tracking"""
        # Check if already completed
        if self.progress_manager.is_completed(username):
            print(f"[SKIP] Skipping {username} - already completed")
            return True
        
        # Get the best account for this user based on access history
        best_account_index = self._get_best_account_for_user(username)
        if best_account_index != self.current_account_index:
            print(f"[TARGET] Switching to optimal account for {username}")
            self.current_account_index = best_account_index
            self.progress_manager.update_batch_state(current_account_index=self.current_account_index)
        
        # Mark as pending
        self.progress_manager.mark_pending(username)
        
        def _collect_operation():
            try:
                current_account = self.available_accounts[self.current_account_index]
                
                print(f"[CONNECT] Using account: {current_account['name']} to collect relationships for {username}")
                
                # ADD: Check quota before profile view (BUG-004 fix)
                if not self.quota_manager.can_view_profiles(current_account['name']):
                    print(f"[QUOTA] Profile view quota exhausted for {current_account['name']}")
                    print(f"[QUOTA] Switching to next account...")
                    return False  # Will trigger account switch in retry loop
                
                # Record profile view against quota
                self.quota_manager.record_profile_view(current_account['name'])
                
                collector = RelationshipCollector(current_account['name'])
                collector.collect_for_user(username, max_followers, max_following)
                
                # Record successful access
                self._record_access_attempt(username, success=True)
                
                collector.cleanup()
                return True
            except Exception as e:
                error_msg = str(e).lower()
                
                print(f"[WARNING]  Collection error for {username}: {e}")
                
                is_private_error = 'private' in error_msg and 'not followed' in error_msg
                
                self._record_access_attempt(
                    username, 
                    success=False, 
                    error=e,
                    is_public=not is_private_error if is_private_error else None
                )
                
                # Use centralized phrase tuples for error detection
                if any(phrase in error_msg for phrase in RATE_LIMIT_PHRASES + ACCOUNT_SWITCH_PHRASES + CHALLENGE_PHRASES):
                    print(f"[RESUME] Instagram API/Auth issue detected - will retry with different account")
                    return False
                elif 'private and not followed' in error_msg:
                    print(f"[RESUME] Private profile - will retry with different account")
                    return False
                else:
                    print(f"[ERROR] Non-API error for {username}: {e}")
                    return False
        
        success = self._execute_with_retry(_collect_operation)
        
        if success:
            self.progress_manager.mark_completed(username, {
                'max_followers': max_followers,
                'max_following': max_following,
                'account_used': self.available_accounts[self.current_account_index]['name']
            })
            self._handle_rate_limiting()
        else:
            self.progress_manager.mark_failed(username, "All retry attempts failed")
        
        return success

    def download_media(self, username, post_limit=None):
        """Download media for a user with automatic retry, account switching, and progress tracking"""
        # Check if already completed
        if self.progress_manager.is_completed(username):
            print(f"[SKIP] Skipping {username} - already completed")
            return True

        # Apply follower count filter if enabled
        if FILTER_MAX_FOLLOWERS > 0:
            if not self.metadata_manager.is_within_follower_limit(username, FILTER_MAX_FOLLOWERS):
                followers = self.metadata_manager.get_profile(username).get('followers_count', '?')
                print(f"[FILTER] Skipping {username} - {followers} followers exceeds limit of {FILTER_MAX_FOLLOWERS}")
                return True  # treat as skipped-success so it doesn't show as failed
        
        # Mark as pending
        self.progress_manager.mark_pending(username)
        
        def _download_operation():
            try:
                current_account = self.available_accounts[self.current_account_index]
                downloader = MediaDownloader(current_account['name'])
                
                print(f"[DOWNLOAD] Using account: {current_account['name']} to download media for {username}")
                
                # ADD: Check quota before profile view (BUG-004 fix)
                if not self.quota_manager.can_view_profiles(current_account['name']):
                    print(f"[QUOTA] Profile view quota exhausted for {current_account['name']}")
                    print(f"[QUOTA] Switching to next account...")
                    return False  # Will trigger account switch in retry loop
                
                # Record profile view against quota
                self.quota_manager.record_profile_view(current_account['name'])
                
                # Set the downloads directory to avoid multiple prompts
                downloader.downloads_dir = self._get_downloads_dir()
                # download_all() returns dict - check success/partial_success keys explicitly
                result = downloader.download_all(username, post_limit)
                downloader.cleanup()
                return result.get('success') or result.get('partial_success', False)
            except Exception as e:
                error_msg = str(e).lower()
                
                print(f"[WARNING]  Download error for {username}: {e}")
                
                # Use centralized phrase tuples
                if any(phrase in error_msg for phrase in RATE_LIMIT_PHRASES + ACCOUNT_SWITCH_PHRASES + CHALLENGE_PHRASES):
                    print(f"[RESUME] Instagram API issue detected - will retry with different account")
                    return False
                else:
                    print(f"[ERROR] Non-API error for {username}: {e}")
                    return False
        
        success = self._execute_with_retry(_download_operation)
        
        if success:
            self.progress_manager.mark_completed(username, {
                'post_limit': post_limit,
                'downloads_directory': self.downloads_dir,
                'account_used': self.available_accounts[self.current_account_index]['name']
            })
            self._handle_rate_limiting()
        else:
            self.progress_manager.mark_failed(username, "All retry attempts failed")
        
        return success

    @handle_graceful_exit()
    def process_batch_relationships(self, usernames, max_followers=1000, max_following=1000):
        """Process multiple users for relationship collection with improved rate limiting and progress tracking"""
        if not usernames:
            print("[ERROR] No usernames provided")
            return
        
        # Get current account username for prioritization
        current_account_username = self._get_current_account_username()
        
        # Prioritize usernames based on relationship to current account
        print(f"[PRIORITY] Prioritizing usernames based on relationships to {current_account_username}")
        prioritized_usernames = self.priority_manager.get_prioritized_list(usernames, current_account_username)
        
        # Filter out already processed usernames
        remaining_usernames = self.progress_manager.get_remaining_users(prioritized_usernames)
        
        if not remaining_usernames:
            print("[OK] All usernames have already been processed!")
            self.progress_manager.print_progress_summary()
            return
        
        print(f"[START] Starting batch relationship collection for {len(remaining_usernames)} remaining users")
        print(f"[LIST] Using accounts: {', '.join([acc['name'] for acc in self.available_accounts])}")
        print(f"[PRIORITY] Users prioritized by: mutual connections > followers > following > public > unknown")
        
        if len(remaining_usernames) < len(prioritized_usernames):
            print(f"[SKIP] Skipping {len(prioritized_usernames) - len(remaining_usernames)} already processed users")
        
        # Update batch state
        self.progress_manager.update_batch_state(
            current_operation='spider',
            total_users=len(remaining_usernames)
        )
        
        successful = 0
        failed = 0
        
        for i, username in enumerate(remaining_usernames, 1):
            # Check for graceful shutdown request (Ctrl+C)
            if self._shutdown_requested.is_set():
                print("\n[STOP] Shutdown requested — stopping batch spider gracefully.")
                break

            print(f"\n[{i}/{len(remaining_usernames)}] Processing {username}...")
            
            # Update current position
            self.progress_manager.update_batch_state(current_user_index=i)
            
            try:
                success = self.collect_relationships(username, max_followers, max_following)
                if success:
                    successful += 1
                    print(f"[OK] [{i}/{len(remaining_usernames)}] Successfully processed {username}")
                else:
                    failed += 1
                    print(f"[ERROR] [{i}/{len(remaining_usernames)}] Failed to process {username}")
                
                # Regular delay between users (via centralized rate limiter)
                if i < len(remaining_usernames):
                    self.rate.user_delay()
                
                # Save progress periodically
                if i % 5 == 0:  # Save every 5 users
                    self.progress_manager.save_progress()
                    
            except Exception as e:
                failed += 1
                print(f"[ERROR] [{i}/{len(remaining_usernames)}] Error processing {username}: {e}")
                self.progress_manager.mark_failed(username, str(e))
                continue
        
        # Final progress save
        self.progress_manager.save_progress()
        
        print(f"\n[STATS] Batch processing complete:")
        print(f"[OK] Successful: {successful}")
        print(f"[ERROR] Failed: {failed}")
        print(f"[PROGRESS] Success rate: {successful/(successful+failed)*100:.1f}%" if (successful+failed) > 0 else "N/A")
        
        # Show overall progress
        self.progress_manager.print_progress_summary()
        
        # Clean up if all users processed
        total_summary = self.progress_manager.get_progress_summary()
        if total_summary['completed'] + total_summary['failed'] >= len(usernames):
            print("[SUCCESS] All users in the original list have been processed!")
            self.progress_manager.cleanup_progress()

    @handle_graceful_exit()
    def process_batch_downloads(self, usernames, post_limit=None):
        """Process multiple users for media downloads with improved rate limiting and progress tracking"""
        if not usernames:
            print("[ERROR] No usernames provided")
            return
        
        # Get current account username for prioritization
        current_account_username = self._get_current_account_username()
        
        # Prioritize usernames based on relationship to current account
        print(f"[PRIORITY] Prioritizing usernames based on relationships to {current_account_username}")
        prioritized_usernames = self.priority_manager.get_prioritized_list(usernames, current_account_username)
        
        # Filter out already processed usernames
        remaining_usernames = self.progress_manager.get_remaining_users(prioritized_usernames)
        
        if not remaining_usernames:
            print("[OK] All usernames have already been processed!")
            self.progress_manager.print_progress_summary()
            return
        
        # Set downloads directory once at the start
        downloads_dir = self._get_downloads_dir()
        print(f"[FOLDER] Downloads will be saved to: {downloads_dir}")
        
        print(f"[START] Starting batch media download for {len(remaining_usernames)} remaining users")
        print(f"[LIST] Using accounts: {', '.join([acc['name'] for acc in self.available_accounts])}")
        print(f"[PRIORITY] Users prioritized by: mutual connections > followers > following > public > unknown")
        if FILTER_MAX_FOLLOWERS > 0:
            print(f"[FILTER] Follower limit active: only downloading users with <= {FILTER_MAX_FOLLOWERS} followers")
        
        if len(remaining_usernames) < len(prioritized_usernames):
            print(f"[SKIP] Skipping {len(prioritized_usernames) - len(remaining_usernames)} already processed users")
        
        # Update batch state
        self.progress_manager.update_batch_state(
            current_operation='download',
            total_users=len(remaining_usernames)
        )
        
        successful = 0
        failed = 0
        
        for i, username in enumerate(remaining_usernames, 1):
            # Check for graceful shutdown request (Ctrl+C)
            if self._shutdown_requested.is_set():
                print("\n[STOP] Shutdown requested — stopping batch download gracefully.")
                break

            print(f"\n[{i}/{len(remaining_usernames)}] Downloading media for {username}...")
            
            # Update current position
            self.progress_manager.update_batch_state(current_user_index=i)
            
            try:
                result = self.download_media(username, post_limit)
                if result:
                    successful += 1
                    print(f"[OK] [{i}/{len(remaining_usernames)}] Successfully downloaded media for {username}")
                else:
                    failed += 1
                    print(f"[ERROR] [{i}/{len(remaining_usernames)}] Failed to download media for {username}")
                
                # Regular delay between users (longer for downloads)
                if i < len(remaining_usernames):
                    self.rate.user_delay(multiplier=2)
                
                # Save progress periodically
                if i % 3 == 0:  # Save every 3 users for downloads (more frequent due to larger operations)
                    self.progress_manager.save_progress()
                    
            except Exception as e:
                failed += 1
                print(f"[ERROR] [{i}/{len(remaining_usernames)}] Error downloading media for {username}: {e}")
                self.progress_manager.mark_failed(username, str(e))
                continue
        
        # Final progress save
        self.progress_manager.save_progress()
        
        print(f"\n[STATS] Batch download complete:")
        print(f"[OK] Successful: {successful}")
        print(f"[ERROR] Failed: {failed}")
        print(f"[PROGRESS] Success rate: {successful/(successful+failed)*100:.1f}%" if (successful+failed) > 0 else "N/A")
        
        # Show overall progress
        self.progress_manager.print_progress_summary()
        
        # Clean up if all users processed
        total_summary = self.progress_manager.get_progress_summary()
        if total_summary['completed'] + total_summary['failed'] >= len(usernames):
            print("[SUCCESS] All users in the original list have been processed!")
            self.progress_manager.cleanup_progress()

#!/usr/bin/env python3
"""
Priority Manager - Prioritize usernames based on account relationships
"""

import os
import sys
from src.config import DATA_DIR
from src.profile_access_tracker import ProfileAccessTracker

def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class PriorityManager:
    """Manage priority ordering of usernames for batch processing"""
    
    def __init__(self):
        self.access_tracker = ProfileAccessTracker()
        self.relationships = self._load_relationships()
    
    def _load_relationships(self):
        """Load relationships from the database."""
        try:
            from db.repositories.relationship_repository import RelationshipRepository
            rows = RelationshipRepository(_get_db()).get_relationships()
            print(f"[PRIORITY] Loaded {len(rows)} relationships for prioritization")
            return rows
        except Exception as e:
            print(f"[WARNING] Error loading relationships for prioritization: {e}")
        return []
    
    def get_account_connections(self, account_username):
        """
        Get followers and following for a specific account
        
        Args:
            account_username: The username of the account to analyze
            
        Returns:
            dict: {'followers': set(), 'following': set()}
        """
        connections = {
            'followers': set(),  # People who follow this account
            'following': set()   # People this account follows
        }
        
        for rel in self.relationships:
            source = rel.get('source')
            target = rel.get('target')
            rel_type = rel.get('type')
            
            if source == account_username:
                # This account is the source
                if rel_type == 'followers':
                    # Target is a follower of this account
                    connections['followers'].add(target)
                elif rel_type == 'following':
                    # Target is someone this account follows
                    connections['following'].add(target)
            elif target == account_username:
                # This account is the target - need to reverse the relationship
                if rel_type == 'followers':
                    # Source follows this account, so source is a follower
                    connections['followers'].add(source)
                elif rel_type == 'following':
                    # Source is followed by this account, so this account follows source
                    connections['following'].add(source)
        
        print(f"[PRIORITY] Found {len(connections['followers'])} followers and {len(connections['following'])} following for {account_username}")
        return connections
    
    def prioritize_usernames(self, usernames, account_username):
        """
        Prioritize usernames based on their relationship to the account
        
        Priority order:
        1. Mutual connections (people who follow you AND you follow back)
        2. Your followers (people who follow you)
        3. People you follow
        4. Public accounts (known accessible)
        5. Unknown/private accounts
        
        Args:
            usernames: List of usernames to prioritize
            account_username: Username of the account being used
            
        Returns:
            dict: Categorized and prioritized usernames
        """
        print(f"[PRIORITY] Prioritizing {len(usernames)} usernames for account: {account_username}")
        
        # Get account connections
        connections = self.get_account_connections(account_username)
        followers = connections['followers']
        following = connections['following']
        
        # Get access data
        access_stats = self.access_tracker.get_access_statistics()
        
        # Categorize usernames
        categories = {
            'mutual_connections': [],      # Follow each other
            'followers_only': [],          # They follow you
            'following_only': [],          # You follow them
            'public_accessible': [],       # Known public accounts
            'unknown_private': []          # Unknown or private accounts
        }
        
        for username in usernames:
            if username in followers and username in following:
                categories['mutual_connections'].append(username)
            elif username in followers:
                categories['followers_only'].append(username)
            elif username in following:
                categories['following_only'].append(username)
            else:
                # Check if it's a known public account
                profile_summary = self.access_tracker.get_profile_summary(username)
                if profile_summary.get('is_public', False):
                    categories['public_accessible'].append(username)
                else:
                    categories['unknown_private'].append(username)
        
        # Print prioritization summary
        self._print_prioritization_summary(categories, account_username)
        
        return categories
    
    def get_prioritized_list(self, usernames, account_username):
        """
        Get a single prioritized list of usernames
        
        Args:
            usernames: List of usernames to prioritize
            account_username: Username of the account being used
            
        Returns:
            list: Prioritized list of usernames
        """
        categories = self.prioritize_usernames(usernames, account_username)
        
        # Combine in priority order
        prioritized = []
        prioritized.extend(categories['mutual_connections'])
        prioritized.extend(categories['followers_only'])
        prioritized.extend(categories['following_only'])
        prioritized.extend(categories['public_accessible'])
        prioritized.extend(categories['unknown_private'])
        
        return prioritized
    
    def get_high_priority_users(self, usernames, account_username, max_users=None):
        """
        Get only high-priority users (mutual, followers, following) up to a limit
        
        Args:
            usernames: List of usernames to prioritize
            account_username: Username of the account being used
            max_users: Maximum number of users to return (None for all)
            
        Returns:
            list: High-priority users only
        """
        categories = self.prioritize_usernames(usernames, account_username)
        
        # Get high priority categories only
        high_priority = []
        high_priority.extend(categories['mutual_connections'])
        high_priority.extend(categories['followers_only'])  
        high_priority.extend(categories['following_only'])
        
        if max_users and len(high_priority) > max_users:
            high_priority = high_priority[:max_users]
            print(f"[LIMIT] Limited to {max_users} high-priority users")
        
        print(f"[HIGH-PRIORITY] Selected {len(high_priority)} high-priority users from {len(usernames)} total")
        return high_priority
    
    def _print_prioritization_summary(self, categories, account_username):
        """Print a summary of the prioritization"""
        print(f"\n[PRIORITY] Prioritization Summary for {account_username}")
        print("=" * 60)
        
        total = sum(len(cat) for cat in categories.values())
        
        print(f"[HIGH] Mutual connections (follow each other): {len(categories['mutual_connections'])}")
        print(f"[HIGH] Your followers: {len(categories['followers_only'])}")
        print(f"[MED]  People you follow: {len(categories['following_only'])}")
        print(f"[MED]  Known public accounts: {len(categories['public_accessible'])}")
        print(f"[LOW]  Unknown/private accounts: {len(categories['unknown_private'])}")
        print(f"[TOTAL] Total accounts: {total}")
        
        if len(categories['mutual_connections']) + len(categories['followers_only']) + len(categories['following_only']) > 0:
            high_priority_count = len(categories['mutual_connections']) + len(categories['followers_only']) + len(categories['following_only'])
            print(f"\n[SUCCESS] {high_priority_count}/{total} ({high_priority_count/total*100:.1f}%) accounts have high/medium priority access!")
        else:
            print(f"\n[WARNING] No high-priority accounts found. Consider building relationships first.")
    
    def get_category_stats(self, usernames, account_username):
        """Get detailed statistics about username categories"""
        categories = self.prioritize_usernames(usernames, account_username)
        
        stats = {}
        for category, users in categories.items():
            stats[category] = {
                'count': len(users),
                'percentage': len(users) / len(usernames) * 100 if usernames else 0,
                'usernames': users[:10]  # First 10 as examples
            }
        
        return stats


def print_priority_analysis(usernames, account_username):
    """Print detailed priority analysis for a list of usernames"""
    manager = PriorityManager()
    
    print(f"\n[ANALYSIS] Priority Analysis for {len(usernames)} usernames")
    print(f"[ACCOUNT] Using account: {account_username}")
    
    stats = manager.get_category_stats(usernames, account_username)
    
    print("\n[DETAILS] Category Breakdown:")
    print("-" * 40)
    
    for category, data in stats.items():
        print(f"\n{category.replace('_', ' ').title()}:")
        print(f"  Count: {data['count']} ({data['percentage']:.1f}%)")
        if data['usernames']:
            print(f"  Examples: {', '.join(data['usernames'][:5])}")




# Profile Access Tracker - Track which accounts can access which profiles
import os
import sys
import time
import random
from datetime import datetime

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import DATA_DIR


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class ProfileAccessTracker:
    """Track which Instagram accounts can access which profiles.

    All persistence is delegated to ProfileAccessRepository.
    """

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.profile_access_repository import ProfileAccessRepository
        self._repo = ProfileAccessRepository(_get_db())

        # Periodic cleanup (10% chance on startup, BUG-009 fix)
        if random.random() < 0.1:
            self.cleanup_old_profiles(days_inactive=30)

    # ── Deprecated persistence helpers (kept for backward compat) ─────────

    def _load_access_data(self):
        """Deprecated: data now lives in the DB."""
        return {"profiles": {}, "last_updated": datetime.now().isoformat(), "version": "1.0"}

    def save_access_data(self):
        """Deprecated: data is persisted immediately via the repository."""
        return True

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def record_profile_access(self, target_username, accessing_account, access_result):
        """Record the result of an access attempt.

        Args:
            target_username: The profile being accessed
            accessing_account: Account name attempting access
            access_result: Dict with keys like 'can_access', 'is_public', 'is_followed', 'error'
        """
        self._repo.record_attempt(
            target=target_username,
            account=accessing_account,
            can_access=bool(access_result.get('can_access', False)),
            is_public=access_result.get('is_public', None),
            is_followed=bool(access_result.get('is_followed', False)),
            error=access_result.get('error', None),
        )

    def get_best_account_for_profile(self, target_username, available_accounts):
        """Get the best account to use for accessing a specific profile.

        Args:
            target_username: The profile to access
            available_accounts: List of account names available for use

        Returns:
            str: Best account name to use, or None if no preference
        """
        summary = self.get_profile_summary(target_username)
        if summary.get('is_public'):
            return None  # Any account works for public profiles

        return self._repo.get_best_account(target_username, available_accounts)

    def get_profile_summary(self, target_username):
        """Get summary of what we know about a profile's accessibility."""
        return self._repo.get_profile_summary(target_username)

    def get_following_accounts(self, target_username):
        """Return the list of accounts known to follow (have access to) a profile.

        Requirements: 3.6, 3.7
        """
        summary = self.get_profile_summary(target_username)
        return summary.get('accessible_by', [])

    def get_access_statistics(self):
        """Get overall statistics about profile access."""
        stats = self._repo.get_statistics()
        return {
            'total_profiles_tracked': stats.get('unique_profiles', 0),
            'public_profiles': 0,   # not tracked separately in DB summary
            'private_profiles': 0,
            'unknown_visibility': 0,
            'account_access_counts': {},
        }

    def cleanup_old_data(self, days_old=30):
        """Remove access attempts older than specified days."""
        removed = self._repo.cleanup_old_attempts(days_old)
        print(f"[CLEAN] Cleaned up access data older than {days_old} days ({removed} rows)")

    def cleanup_old_profiles(self, days_inactive=30):
        """Remove inactive private profiles from tracking.

        Args:
            days_inactive: Remove private profiles inactive for this many days

        Returns:
            int: Number of profiles removed
        """
        removed = self._repo.cleanup_inactive_profiles(days_inactive)
        if removed:
            print(f"[CLEAN] Removed {removed} inactive private profiles (older than {days_inactive} days)")
        return removed


def print_access_statistics():
    """Print profile access statistics."""
    tracker = ProfileAccessTracker()
    stats = tracker.get_access_statistics()

    print("\n[STATS] Profile Access Statistics")
    print("=" * 50)
    print(f"Total profiles tracked: {stats['total_profiles_tracked']}")
    print(f"Public profiles: {stats['public_profiles']}")
    print(f"Private profiles: {stats['private_profiles']}")
    print(f"Unknown visibility: {stats['unknown_visibility']}")

    print("\n🔑 Account Access Counts:")
    for account, count in stats['account_access_counts'].items():
        print(f"  {account}: {count} profiles accessible")

"""Profile analyzer for generating network insights from metadata.

Analyzes profile metadata to identify influential users, network topology,
and provide actionable insights about scraped networks.
"""
import os
import csv
import time
Optional
from src.user_metadata_manager import UserMetadataManager
from src.config import DATA_DIR
from src.io_utils import safe_json_write


class ProfileAnalyzer:
    """Analyze profile metadata for network insights.
    
    Generates statistics, identifies influential users, and provides
    actionable insights about the scraped network.
    """
    
    def __init__(self):
        self.metadata_manager = UserMetadataManager()
        self.stats_file = os.path.join(DATA_DIR, 'profile_stats.json')
        self.csv_file = os.path.join(DATA_DIR, 'profile_stats.csv')
    
    def analyze_network(self) -> Dict[str, Any]:
        """Generate comprehensive network analysis.
        
        Returns:
            Dict with complete network statistics
        """
        stats = self.metadata_manager.get_network_stats()
        
        # Add additional analysis
        profiles = list(self.metadata_manager.get_all_profiles().values())
        
        if profiles:
            # Calculate ratios
            ratios = []
            for p in profiles:
                followers = p.get('followers_count', 0)
                following = p.get('following_count', 0)
                if following > 0:
                    ratios.append(followers / following)
            
            stats['avg_follower_to_following_ratio'] = sum(ratios) / len(ratios) if ratios else 0
            
            # Count by size tiers
            tiers = {
                'micro_influencers': 0,      # 1K-10K
                'small_influencers': 0,      # 10K-50K
                'medium_influencers': 0,     # 50K-100K
                'large_influencers': 0,      # 100K-500K
                'mega_influencers': 0,       # 500K-1M
                'celebrities': 0             # 1M+
            }
            
            for p in profiles:
                f = p.get('followers_count', 0)
                if 1000 <= f < 10000:
                    tiers['micro_influencers'] += 1
                elif 10000 <= f < 50000:
                    tiers['small_influencers'] += 1
                elif 50000 <= f < 100000:
                    tiers['medium_influencers'] += 1
                elif 100000 <= f < 500000:
                    tiers['large_influencers'] += 1
                elif 500000 <= f < 1000000:
                    tiers['mega_influencers'] += 1
                elif f >= 1000000:
                    tiers['celebrities'] += 1
            
            stats['influencer_tiers'] = tiers
            
            # Find high-engagement potential (high follower, low following)
            high_engagement = []
            for p in profiles:
                followers = p.get('followers_count', 0)
                following = p.get('following_count', 0)
                if followers > 5000 and following < 1000:
                    high_engagement.append({
                        'username': p['username'],
                        'followers_count': followers,
                        'following_count': following,
                        'ratio': followers / following if following > 0 else followers
                    })
            
            high_engagement.sort(key=lambda x: x['ratio'], reverse=True)
            stats['high_engagement_potential'] = high_engagement[:20]
        
        # Add timestamp
        stats['analysis_timestamp'] = time.time()
        stats['analysis_date'] = time.strftime('%Y-%m-%d %H:%M:%S')
        
        return stats
    
    def save_analysis(self, stats: Dict[str, Any]):
        """Save analysis results to files.
        
        Args:
            stats: Analysis statistics dict
        """
        # Save JSON
        safe_json_write(self.stats_file, stats)
        print(f"[ANALYSIS] Saved JSON stats to {self.stats_file}")
        
        # Save CSV with all profiles
        self._save_csv(stats)
    
    def _save_csv(self, stats: Dict[str, Any]):
        """Save profile data to CSV."""
        profiles = self.metadata_manager.get_all_profiles().values()
        
        fieldnames = [
            'username', 'full_name', 'followers_count', 'following_count',
            'is_public', 'is_verified', 'biography', 'external_url',
            'media_count', 'last_collected', 'collected_by_account'
        ]
        
        with open(self.csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for profile in profiles:
                row = {k: profile.get(k, '') for k in fieldnames}
                # Format timestamp
                ts = row.get('last_collected')
                if ts:
                    row['last_collected'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
                writer.writerow(row)
        
        print(f"[ANALYSIS] Saved CSV data to {self.csv_file}")
    
    def print_summary(self, stats: Dict[str, Any]):
        """Print formatted summary of analysis.
        
        Args:
            stats: Analysis statistics dict
        """
        print("\n" + "="*60)
        print("NETWORK PROFILE ANALYSIS")
        print("="*60)
        
        print(f"\nTotal Profiles Tracked: {stats.get('total_profiles', 0)}")
        print(f"Public Profiles: {stats.get('public_profiles', 0)}")
        print(f"Private Profiles: {stats.get('private_profiles', 0)}")
        print(f"Verified Accounts: {stats.get('verified_profiles', 0)}")
        
        print(f"\nAverage Followers: {stats.get('avg_followers', 0):.0f}")
        print(f"Average Following: {stats.get('avg_following', 0):.0f}")
        print(f"Avg F/F Ratio: {stats.get('avg_follower_to_following_ratio', 0):.2f}")
        
        # Influencer tiers
        tiers = stats.get('influencer_tiers', {})
        if any(tiers.values()):
            print("\n--- Influencer Tiers ---")
            for tier, count in tiers.items():
                if count > 0:
                    print(f"  {tier.replace('_', ' ').title()}: {count}")
        
        # Top followers
        print("\n--- Top 10 by Followers ---")
        for i, p in enumerate(stats.get('top_followers', [])[:10], 1):
            verified = " ✓" if p.get('is_verified') else ""
            print(f"  {i}. @{p['username']}{verified}: {p.get('followers_count', 0):,} followers")
        
        # Top following
        print("\n--- Top 10 by Following ---")
        for i, p in enumerate(stats.get('top_following', [])[:10], 1):
            print(f"  {i}. @{p['username']}: {p.get('following_count', 0):,} following")
        
        # High engagement potential
        high_eng = stats.get('high_engagement_potential', [])
        if high_eng:
            print("\n--- High Engagement Potential ---")
            print("  (High followers, low following - good targets)")
            for i, p in enumerate(high_eng[:5], 1):
                ratio = p.get('ratio', 0)
                print(f"  {i}. @{p['username']}: {p['followers_count']:,}/{p['following_count']:,} (ratio: {ratio:.1f})")
        
        print("\n" + "="*60)
        print(f"Analysis saved to:")
        print(f"  JSON: {self.stats_file}")
        print(f"  CSV: {self.csv_file}")
        print("="*60 + "\n")
    
    def get_influential_users(self, min_followers: int = 10000) -> List[Dict[str, Any]]:
        """Get list of influential users meeting criteria.
        
        Args:
            min_followers: Minimum follower count
            
        Returns:
            List of profile dicts
        """
        return [
            p for p in self.metadata_manager.get_all_profiles().values()
            if p.get('followers_count', 0) >= min_followers
        ]
    
    def get_reciprocal_relationships(self) -> List[tuple]:
        """Find mutual follow relationships (requires relationship data).
        
        Returns:
            List of (user1, user2) tuples where both follow each other
        """
        try:
            import os as _os
            from db.manager import DatabaseManager
            from db.repositories.relationship_repository import RelationshipRepository
            db = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
            repo = RelationshipRepository(db)
            relationships = repo.get_relationships()
        except Exception:
            return []
        
        if not relationships:
            return []
        
        # Build follow graph
        follows = {}  # user -> set of users they follow
        
        for rel in relationships:
            source = rel.get('source')
            target = rel.get('target')
            rel_type = rel.get('type')
            
            if source and target:
                if rel_type == 'following':
                    follows.setdefault(source, set()).add(target)
                elif rel_type == 'followers':
                    follows.setdefault(target, set()).add(source)
        
        # Find mutual follows
        mutual = []
        for user1, following in follows.items():
            for user2 in following:
                if user2 in follows and user1 in follows[user2]:
                    # Avoid duplicates (user1,user2) and (user2,user1)
                    if (user2, user1) not in mutual:
                        mutual.append((user1, user2))
        
        return mutual


def main():
    """CLI entry point for profile analysis."""
    analyzer = ProfileAnalyzer()
    
    print("[ANALYSIS] Starting profile network analysis...")
    
    stats = analyzer.analyze_network()
    analyzer.save_analysis(stats)
    analyzer.print_summary(stats)
    
    # Check if we have any data
    if stats.get('total_profiles', 0) == 0:
        print("\n[WARNING] No profile metadata found.")
        print("Run spider operations to collect profile data first.")
        print("Metadata will be automatically saved during spider runs.")


if __name__ == "__main__":
    main()

"""Profile photo change detection using perceptual hashing."""
import os
import io
import pathlib
Tuple

import requests
from src.PIL import Image
import imagehash

from src.resilience import _SHUTDOWN, wait_for_internet


class ProfilePhotoTracker:
    """Detects genuine profile photo changes using two-stage detection.

    Stage 1 - URL check (fast, zero download cost):
      If URL hasn't changed, skip entirely (no change)

    Stage 2 - pHash comparison (only when URL changed):
      Download new photo, compute pHash, compare with stored pHash.
      If Hamming distance > 10: genuine change detected.
      If Hamming distance <= 10: CDN rotation only (update URL, skip blob).

    The database (profile_photo_history table) is the source of truth.
    """
    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager instance with execute/fetchone methods
        """
        self.db = db_manager
        # Default 5GB limit for storing photo blobs in DB
        self.max_blob_size_mb = int(os.environ.get('PROFILE_PHOTO_BLOB_MAX_SIZE_MB', 5000))

    def check_for_change(self, username: str, new_photo_url: str) -> Tuple[bool, Optional[str]]:
        """
        Detect if profile photo has genuinely changed.

        Args:
            username: Instagram username
            new_photo_url: New profile photo URL from Instagram

        Returns:
            Tuple of (changed: bool, phash: str or None)
            - changed=True: genuine photo change detected
            - changed=False: no change or CDN rotation only
            - phash: latest pHash (if computed), None otherwise
        """
        # Stage 1: URL check (fast, zero download cost)
        stored_url = self._get_latest_photo_url(username)
        if stored_url == new_photo_url:
            return False, None  # No change at all, skip entirely

        # Stage 2: pHash comparison (only when URL changed)
        if _SHUTDOWN.is_set():
            return False, None

        # Download new photo with internet retry
        photo_bytes = self._download_photo(new_photo_url)
        if photo_bytes is None:
            return False, None

        # Compute pHash
        try:
            img = Image.open(io.BytesIO(photo_bytes))
            new_phash = str(imagehash.phash(img))
        except Exception as e:
            print(f"[ERROR] Failed to compute pHash for {username}: {e}")
            return False, None

        # Get stored phash
        stored_phash = self._get_latest_phash(username)
        if stored_phash is None:
            # First time seeing this user's photo
            self._store_photo(username, new_photo_url, new_phash, photo_bytes)
            return True, new_phash

        # Compute Hamming distance
        try:
            distance = imagehash.hex_to_hash(new_phash) - imagehash.hex_to_hash(stored_phash)
        except Exception as e:
            print(f"[ERROR] Failed to compare hashes for {username}: {e}")
            return False, None

        if distance > 10:
            # Genuine change detected
            self._store_photo(username, new_photo_url, new_phash, photo_bytes)
            print(f"[PHOTO CHANGE] {username}: profile photo changed (distance={distance})")
            return True, new_phash
        else:
            # CDN rotation only - update URL but don't store blob
            self._update_url_only(username, new_photo_url)
            print(f"[CDN ROTATION] {username}: cache refresh only (distance={distance})")
            return False, new_phash

    def _get_latest_photo_url(self, username: str) -> Optional[str]:
        """Get the most recent photo URL for this username."""
        try:
            row = self.db.fetchone(
                "SELECT photo_url FROM profile_photo_history WHERE username=? ORDER BY detected_at DESC LIMIT 1",
                (username,)
            )
            return row[0] if row else None
        except Exception as e:
            print(f"[ERROR] Failed to get photo URL for {username}: {e}")
            return None

    def _get_latest_phash(self, username: str) -> Optional[str]:
        """Get the most recent pHash for this username."""
        try:
            row = self.db.fetchone(
                "SELECT photo_phash FROM profile_photo_history WHERE username=? ORDER BY detected_at DESC LIMIT 1",
                (username,)
            )
            return row[0] if row else None
        except Exception as e:
            print(f"[ERROR] Failed to get pHash for {username}: {e}")
            return None

    def _download_photo(self, url: str) -> Optional[bytes]:
        """
        Download photo bytes with internet retry.

        Args:
            url: Photo URL to download

        Returns:
            Photo bytes or None on failure
        """
        try:
            # Wait for internet if needed
            if not wait_for_internet():
                return None

            # Check shutdown before download
            if _SHUTDOWN.is_set():
                return None

            response = requests.get(url, timeout=10, stream=True)
            response.raise_for_status()

            # Download with streaming to avoid loading full file into memory
            buffer = io.BytesIO()
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    buffer.write(chunk)
            return buffer.getvalue()

        except Exception as e:
            print(f"[ERROR] Failed to download photo from {url}: {e}")
            return None

    def _store_photo(self, username: str, photo_url: str, phash: str, photo_bytes: bytes) -> None:
        """
        Store photo in DB with blob (if under size limit).

        Args:
            username: Instagram username
            photo_url: Photo URL
            phash: Perceptual hash
            photo_bytes: Photo bytes
        """
        try:
            # Check DB size before storing blob
            db_file = self.db.db_path if hasattr(self.db, 'db_path') else None
            if db_file:
                try:
                    db_size_mb = os.path.getsize(db_file) / (1024 * 1024)
                    if db_size_mb > self.max_blob_size_mb:
                        print(f"[WARNING] DB size {db_size_mb:.1f}MB exceeds limit {self.max_blob_size_mb}MB.")
                        print(f"[WARNING] Skipping blob storage for {username} photo.")
                        # Store without blob
                        self.db.execute(
                            "INSERT INTO profile_photo_history (username, photo_url, photo_phash, detected_at) "
                            "VALUES (?, ?, ?, unixepoch())",
                            (username, photo_url, phash)
                        )
                        return
                except Exception as e:
                    print(f"[WARNING] Could not check DB size: {e}. Proceeding with blob storage.")

            # Store with blob
            self.db.execute(
                "INSERT INTO profile_photo_history (username, photo_url, photo_phash, photo_blob, detected_at) "
                "VALUES (?, ?, ?, ?, unixepoch())",
                (username, photo_url, phash, photo_bytes)
            )

            print(f"[PHOTO STORED] {username}: new photo tracked (pHash: {phash[:8]}...)")

        except Exception as e:
            print(f"[ERROR] Failed to store photo for {username}: {e}")

    def _update_url_only(self, username: str, new_url: str) -> None:
        """
        Update URL without storing blob (CDN rotation only).

        Args:
            username: Instagram username
            new_url: New photo URL (same photo, different CDN URL)
        """
        try:
            self.db.execute(
                "UPDATE profile_photo_history SET photo_url=? "
                "WHERE username=? AND detected_at=(SELECT MAX(detected_at) FROM profile_photo_history WHERE username=?)",
                (new_url, username, username)
            )
        except Exception as e:
            print(f"[ERROR] Failed to update URL for {username}: {e}")


__all__ = ["ProfilePhotoTracker"]

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
Optional

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
        self._repo.upsert_progress(self._operation_id, username, 'completed', details=details)

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

    def mark_failed(self, username: str, error_msg: str = "") -> None:
        """Mark a username as failed with an error message."""
        self._repo.upsert_progress(self._operation_id, username, 'failed', error=error_msg)

        failed = self.progress_data.setdefault('failed', [])
        if username not in failed:
            failed.append(username)
            self.progress_data['statistics']['failed'] += 1

        pending = self.progress_data.get('pending', [])
        if username in pending:
            pending.remove(username)

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

"""Centralized rate limiting utilities for Instagram operations.

Provides a single place to adjust delays and backoff strategies instead of
sprinkling time.sleep/random.uniform directly across modules.
"""
from __future__ import annotations

import math
import threading
import time
import random
Optional
from src.config import (
    MIN_DELAY, MAX_DELAY,
    MIN_RANDOM_DELAY, MAX_RANDOM_DELAY,
    HUMAN_REST_INTERVAL, HUMAN_REST_CHANCE,
    HUMAN_REST_MIN, HUMAN_REST_MAX,
    OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX,
    BREAK_DURATION_MIN, BREAK_DURATION_MAX,
)

# Module-level shutdown event — set this to wake all interruptible_sleep calls immediately.
# Signal handlers and InstagramProcessor both set this on Ctrl+C / SIGTERM.
_SHUTDOWN_EVENT = threading.Event()


class RateLimiter:
    """Rate limiter with configurable base delays and periodic long breaks.

    Usage patterns:
      limiter.short_delay()                 # between lightweight API calls
      limiter.user_delay()                  # between processing different users
      limiter.periodic(count, every=10)     # long delay every N items
      limiter.emergency_break(minutes=5)    # manual backoff on hard limit
      limiter.track_operation()             # auto long-break after random N ops
    """

    def __init__(self, min_delay: float = MIN_DELAY, max_delay: float = MAX_DELAY, label: str = "general"):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.label = label
        # Randomize thresholds to mimic human behaviour
        self._ops_before_long_pause = random.randint(OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX)
        self._long_pause_minutes = random.randint(BREAK_DURATION_MIN, BREAK_DURATION_MAX)
        self._op_counter = 0

    # ---------------- Core Sleep Helpers -----------------
    def interruptible_sleep(self, seconds: float, reason: Optional[str] = None, check_interval: float = 0.2):
        """Sleep in short slices so Ctrl+C or _SHUTDOWN_EVENT interrupts long waits immediately."""
        if seconds <= 0:
            return
        tag = f"[WAIT:{self.label}]"
        if reason:
            print(f"{tag} {reason} ({seconds:.1f}s)")
        else:
            print(f"{tag} Sleeping {seconds:.1f}s")

        end_time = time.time() + seconds
        while True:
            # Wake immediately if global shutdown requested
            if _SHUTDOWN_EVENT.is_set():
                return
            remaining = end_time - time.time()
            if remaining <= 0:
                return
            # Use time.sleep so existing mocks intercept it correctly.
            # The _SHUTDOWN_EVENT check at the top of each tick handles Ctrl+C.
            time.sleep(min(check_interval, remaining))

    def _sleep(self, seconds: float, reason: Optional[str] = None):
        self.interruptible_sleep(seconds, reason=reason)

    def _human_delay(self, mean: float, stddev: float = 0.0) -> float:
        """Generate a human-like delay using gaussian distribution clamped to [mean/3, mean*3]."""
        if stddev <= 0:
            stddev = mean * 0.3
        delay = random.gauss(mean, stddev)
        return max(mean / 3, min(delay, mean * 3))

    def short_delay(self):
        """Short randomized jitter delay for lightweight operations."""
        mean = (self.min_delay + self.max_delay) / 2
        self._sleep(self._human_delay(mean), reason="short delay")

    def user_delay(self, multiplier: float = 1.0):
        """Delay between user-level operations (can scale)."""
        mean = (self.min_delay + self.max_delay) / 2 * multiplier
        self._sleep(self._human_delay(mean), reason="between users")

    def periodic(self, current_index: int, every: int = 10, seconds: float = 10):
        """Optional longer pause every N items."""
        if every > 0 and current_index > 0 and current_index % every == 0:
            jittered = self._human_delay(seconds)
            self._sleep(jittered, reason=f"periodic pause after {current_index} items")

    def emergency_break(self, minutes: int):
        self._sleep(minutes * 60, reason=f"emergency backoff {minutes}m")

    # ---------------- Composite Behaviour -----------------
    def track_operation(self):
        """Track an operation and insert an occasional long break automatically."""
        self._op_counter += 1
        if self._op_counter >= self._ops_before_long_pause:
            self._sleep(self._long_pause_minutes * 60, reason=f"long pause after {self._op_counter} ops")
            # Reset counters / randomize next window
            self._op_counter = 0
            self._ops_before_long_pause = random.randint(OPS_BEFORE_BREAK_MIN, OPS_BEFORE_BREAK_MAX)
            self._long_pause_minutes = random.randint(BREAK_DURATION_MIN, BREAK_DURATION_MAX)

__all__ = ["RateLimiter", "_SHUTDOWN_EVENT"]

"""Database-to-disk reconciliation module.

Checks if downloaded files still exist on disk. Two-tier verification:
- Tier 1 (fast, automatic on startup): Path.exists() stat call
- Tier 2 (deep, opt-in via menu): SHA-256 re-hash and compare
"""
import hashlib
import os
from pathlib import Path
Tuple

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


"""Shared resilience utilities for Instagram Toolkit.

Provides:
- Graceful Ctrl+C handling with shutdown event
- Interruptible sleep for delays > 1s
- Internet outage detection and resilience
- Automatic retry on network errors

Based on CROSS_TOOLKIT_ANALYSIS.md Section 11.
"""
import signal
import threading
import time
import socket

# Global shutdown event — set on Ctrl+C
_SHUTDOWN = threading.Event()


def _handle_sigint(signum, frame):
    """First Ctrl+C: request graceful stop. Second Ctrl+C: force exit."""
    if _SHUTDOWN.is_set():
        print("\n[FORCE EXIT] Second Ctrl+C — forcing exit now.")
        raise SystemExit(1)
    _SHUTDOWN.set()
    print("\n[STOPPING] Ctrl+C received. Finishing current operation then stopping...")
    print("           Press Ctrl+C again to force exit immediately.")


# Install signal handler
signal.signal(signal.SIGINT, _handle_sigint)


def _interruptible_sleep(seconds: float, check_interval: float = 0.2) -> None:
    """Sleep in short slices so Ctrl+C is observed quickly.

    Args:
        seconds: Total sleep time in seconds
        check_interval: How often to check shutdown (default 0.2s)
    """
    if seconds <= 0:
        return
    end_time = time.time() + seconds
    while True:
        # Check shutdown at top of each tick
        if _SHUTDOWN.is_set():
            return
        remaining = end_time - time.time()
        if remaining <= 0:
            return
        time.sleep(min(check_interval, remaining))


def _is_internet_available(host: str = "8.8.8.8", port: int = 53, timeout: int = 3) -> bool:
    """Fast internet check via DNS socket (no HTTP overhead).

    Args:
        host: DNS server to connect to (default Google DNS)
        port: Port to connect (default DNS port 53)
        timeout: Connection timeout in seconds

    Returns:
        True if internet is available, False otherwise
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except (socket.error, OSError):
        return False


def wait_for_internet(
    check_interval: float = 5.0,
    shutdown_event=None,
    label: str = "internet"
) -> bool:
    """
    Block until internet is available or shutdown is requested.

    Args:
        check_interval: Seconds between connection checks
        shutdown_event: Optional threading.Event to check for shutdown
        label: Label for log messages

    Returns:
        True if internet came back, False if shutdown was requested
    """
    if _is_internet_available():
        return True

    print(f"\n[OFFLINE] No internet connection detected.")
    print(f"          Waiting for connection to restore...")
    print(f"          Press Ctrl+C to stop waiting and exit.")

    while not _is_internet_available():
        # Check shutdown if event provided
        if shutdown_event and shutdown_event.is_set():
            print(f"[STOPPED] Shutdown requested while waiting for internet.")
            return False
        # Also check global shutdown
        if _SHUTDOWN.is_set():
            print(f"[STOPPED] Global shutdown requested while waiting for internet.")
            return False
        _interruptible_sleep(check_interval)

    print(f"[ONLINE] Internet connection restored. Resuming...")
    return True


def with_internet_retry(
    func,
    *args,
    max_retries: int = 3,
    base_delay: float = 2.0,
    shutdown_event=None,
    **kwargs
):
    """
    Call func(*args, **kwargs) with retry on network errors.

    On connection error: wait for internet, then retry.
    On shutdown: return None immediately.

    Args:
        func: Function to call
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay for exponential backoff
        shutdown_event: Optional threading.Event to check for shutdown
        **kwargs: Keyword arguments for func

    Returns:
        Result of func(*args, **kwargs), or None on shutdown
    """
    for attempt in range(max_retries + 1):
        # Check shutdown at start of each attempt
        if shutdown_event and shutdown_event.is_set():
            return None
        if _SHUTDOWN.is_set():
            return None

        try:
            return func(*args, **kwargs)
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt == max_retries:
                raise
            # Check if it's an internet outage
            if not _is_internet_available():
                restored = wait_for_internet(shutdown_event=shutdown_event)
                if not restored:
                    return None
            else:
                # Transient error — exponential backoff
                delay = base_delay * (2 ** attempt)
                print(f"[RETRY] Attempt {attempt+1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
                _interruptible_sleep(delay, shutdown_event=shutdown_event)


__all__ = [
    "_SHUTDOWN",
    "_handle_sigint",
    "_interruptible_sleep",
    "_is_internet_available",
    "wait_for_internet",
    "with_internet_retry",
]

# Selective Download Manager - Manage custom download lists
import os
import json
from src.config import DATA_DIR
from src.io_utils import safe_json_write

class SelectiveDownloadManager:
    """Manages selective download lists for targeted media downloading"""
    
    def __init__(self):
        self.selective_list_file = os.path.join(DATA_DIR, 'selective_download_list.json')
        os.makedirs(DATA_DIR, exist_ok=True)
        self.selective_list = self._load_selective_list()
    
    def _load_selective_list(self):
        """Load selective download list from file"""
        try:
            if os.path.exists(self.selective_list_file):
                with open(self.selective_list_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get('usernames', [])
            return []
        except Exception as e:
            print(f"[WARNING] Error loading selective list: {e}")
            return []
    
    def _save_selective_list(self):
        """Save selective download list to file (atomic write)"""
        try:
            data = {
                'usernames': self.selective_list,
                'total_count': len(self.selective_list),
                'last_updated': self._get_timestamp()
            }
            safe_json_write(self.selective_list_file, data)
            print(f"[OK] Selective list saved ({len(self.selective_list)} usernames)")
        except Exception as e:
            print(f"[ERROR] Failed to save selective list: {e}")
    
    def _get_timestamp(self):
        """Get current timestamp for tracking"""
        from datetime import datetime
        return datetime.now().isoformat()
    
    def get_available_usernames(self):
        """Get all available usernames from the database."""
        try:
            from db.repositories.username_repository import UsernameRepository
            import os as _os
            from db.manager import DatabaseManager
            db = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
            rows = UsernameRepository(db).get_all()
            usernames = [r["username"] for r in rows]
            print(f"[INFO] Found {len(usernames)} available usernames")
            return usernames
        except Exception as e:
            print(f"[ERROR] Error loading usernames from database: {e}")
            return []
    
    def interactive_select(self):
        """Interactive username selection interface"""
        available_usernames = self.get_available_usernames()
        if not available_usernames:
            print("[ERROR] No usernames available for selection")
            return False
        
        print(f"\n📋 Selective Download - Username Selection")
        print("=" * 50)
        print(f"Available usernames: {len(available_usernames)}")
        print(f"Currently selected: {len(self.selective_list)}")
        print()
        
        # Show current selection if any
        if self.selective_list:
            print("🎯 Currently Selected:")
            for i, username in enumerate(self.selective_list, 1):
                print(f"   {i}. {username}")
            print()
        
        print("📋 Available Usernames:")
        print("=" * 30)
        
        # Show usernames in chunks for better readability
        chunk_size = 20
        for i in range(0, len(available_usernames), chunk_size):
            chunk = available_usernames[i:i + chunk_size]
            for j, username in enumerate(chunk, i + 1):
                status = "✅" if username in self.selective_list else "⬜"
                print(f"{status} {j:3d}. {username}")
            
            if i + chunk_size < len(available_usernames):
                choice = input(f"\nShowing {i+1}-{i+len(chunk)} of {len(available_usernames)}. Continue? (y/n/select): ").strip().lower()
                if choice == 'n':
                    break
                elif choice == 'select':
                    break
            print()
        
        print("\n🔧 Selection Options:")
        print("1. Enter numbers (comma-separated): e.g., 1,5,10-15")
        print("2. Enter usernames (comma-separated): e.g., user1,user2,user3")
        print("3. Type 'all' to select all usernames")
        print("4. Type 'clear' to clear current selection")
        print("5. Type 'done' to finish selection")
        
        while True:
            choice = input("\nEnter your choice: ").strip()
            
            if choice.lower() == 'done':
                break
            elif choice.lower() == 'clear':
                self.selective_list = []
                print("✅ Selection cleared")
            elif choice.lower() == 'all':
                self.selective_list = available_usernames.copy()
                print(f"✅ Selected all {len(self.selective_list)} usernames")
            elif ',' in choice:
                # Handle comma-separated input
                if choice.replace(',', '').replace('-', '').replace(' ', '').isdigit():
                    # Numbers
                    self._handle_number_selection(choice, available_usernames)
                else:
                    # Usernames
                    self._handle_username_selection(choice, available_usernames)
            elif choice.isdigit():
                # Single number
                self._handle_number_selection(choice, available_usernames)
            elif choice:
                # Single username
                self._handle_username_selection(choice, available_usernames)
            else:
                print("❌ Invalid input. Please try again.")
        
        # Save the selection
        self._save_selective_list()
        print(f"\n🎯 Final Selection: {len(self.selective_list)} usernames")
        return True
    
    def _handle_number_selection(self, choice, available_usernames):
        """Handle number-based selection (e.g., 1,3,5-10)"""
        try:
            numbers = []
            for part in choice.split(','):
                part = part.strip()
                if '-' in part:
                    # Range (e.g., 5-10)
                    start, end = map(int, part.split('-'))
                    numbers.extend(range(start, end + 1))
                else:
                    # Single number
                    numbers.append(int(part))
            
            added = 0
            for num in numbers:
                if 1 <= num <= len(available_usernames):
                    username = available_usernames[num - 1]
                    if username not in self.selective_list:
                        self.selective_list.append(username)
                        added += 1
            
            print(f"✅ Added {added} usernames to selection")
            
        except ValueError:
            print("❌ Invalid number format")
    
    def _handle_username_selection(self, choice, available_usernames):
        """Handle username-based selection"""
        usernames = [u.strip() for u in choice.split(',')]
        added = 0
        
        for username in usernames:
            if username in available_usernames:
                if username not in self.selective_list:
                    self.selective_list.append(username)
                    added += 1
            else:
                print(f"❌ Username '{username}' not found in available list")
        
        print(f"✅ Added {added} usernames to selection")
    
    def add_username(self, username):
        """Add a single username to selective list"""
        available_usernames = self.get_available_usernames()
        
        if username not in available_usernames:
            print(f"❌ Username '{username}' not found in available usernames")
            return False
        
        if username in self.selective_list:
            print(f"ℹ️  Username '{username}' already in selective list")
            return True
        
        self.selective_list.append(username)
        self._save_selective_list()
        print(f"✅ Added '{username}' to selective download list")
        return True
    
    def remove_username(self, username):
        """Remove a username from selective list"""
        if username in self.selective_list:
            self.selective_list.remove(username)
            self._save_selective_list()
            print(f"✅ Removed '{username}' from selective download list")
            return True
        else:
            print(f"❌ Username '{username}' not found in selective list")
            return False
    
    def clear_list(self):
        """Clear the selective download list"""
        count = len(self.selective_list)
        self.selective_list = []
        self._save_selective_list()
        print(f"✅ Cleared selective download list ({count} usernames removed)")
    
    def show_list(self):
        """Display current selective download list"""
        print(f"\n🎯 Selective Download List")
        print("=" * 30)
        
        if not self.selective_list:
            print("📝 No usernames selected")
            print("💡 Use 'selective-download --select' to choose usernames")
            return
        
        print(f"📊 Total Selected: {len(self.selective_list)} usernames")
        print()
        
        for i, username in enumerate(self.selective_list, 1):
            print(f"{i:3d}. {username}")
        
        print(f"\n💾 List saved to: {self.selective_list_file}")
    
    def get_selected_usernames(self):
        """Get the list of selected usernames"""
        return self.selective_list.copy()
    
    def has_selection(self):
        """Check if there are any selected usernames"""
        return len(self.selective_list) > 0

"""
Smart Account Selector - Selects optimal Instagram accounts based on operation
requirements and following relationships.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""

import logging
Optional

from src.operation_classifier import OperationType

logger = logging.getLogger(__name__)


class SmartAccountSelector:
    """
    Selects optimal Instagram accounts for operations based on following relationships.

    For PUBLIC operations, any available account is returned.
    For FOLLOWING_REQUIRED operations, the selector checks:
      1. following_status cache in UsernameRecord
      2. ProfileAccessTracker for accessible_by list
      3. Source account as fallback
      4. Returns None if no following relationship found

    Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7
    """

    def __init__(self, username_db=None, profile_tracker=None):
        """
        Initialize the selector with optional dependencies.

        Args:
            username_db: UsernameDatabase instance for following_status cache
            profile_tracker: ProfileAccessTracker instance for relationship data
        """
        self._username_db = username_db
        self._profile_tracker = profile_tracker

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def select_for_operation(
        self,
        operation_type: OperationType,
        target_username: str,
        available_accounts: list[str],
    ) -> Optional[str]:
        """
        Select the optimal account for a single-username operation.

        Args:
            operation_type: Type of operation (PUBLIC, FOLLOWING_REQUIRED, etc.)
            target_username: Instagram username being targeted
            available_accounts: List of account names available for use

        Returns:
            Account name to use, or None if no suitable account found
        """
        if not available_accounts:
            return None

        # PUBLIC operations: any account works (Requirement 3.1)
        if operation_type == OperationType.PUBLIC:
            return available_accounts[0]

        # FOLLOWING_REQUIRED / MUTUAL_FOLLOWING: need following relationship
        return self._select_following_account(target_username, available_accounts)

    def select_for_batch(
        self,
        operation_type: OperationType,
        target_usernames: list[str],
        available_accounts: list[str],
    ) -> dict[str, list[str]]:
        """
        Group usernames by optimal account for batch processing.

        Postconditions:
        - Every input username appears in exactly one account's list
        - For PUBLIC: all usernames assigned to a single account
        - For FOLLOWING_REQUIRED: grouped by following relationships

        Args:
            operation_type: Type of operation
            target_usernames: List of usernames to process
            available_accounts: List of available account names

        Returns:
            Dict mapping account name -> list of usernames
        """
        if not available_accounts or not target_usernames:
            return {}

        # PUBLIC: assign all to first available account (Requirement 3.5 / 7.2)
        if operation_type == OperationType.PUBLIC:
            return {available_accounts[0]: list(target_usernames)}

        # FOLLOWING_REQUIRED: group by optimal account (Requirement 7.3)
        assignment: dict[str, list[str]] = {}

        for username in target_usernames:
            account = self._select_following_account(username, available_accounts)
            if account is None:
                # Fallback: assign to first available account so no username is lost
                account = available_accounts[0]
                logger.warning(
                    "No following relationship found for '%s'; assigning to '%s'",
                    username,
                    account,
                )
            assignment.setdefault(account, []).append(username)

        return assignment

    def get_following_overlap(
        self,
        account: str,
        target_usernames: list[str],
    ) -> dict[str, bool]:
        """
        Return a mapping of username -> is_following for the given account.

        Checks the UsernameDatabase cache first, then ProfileAccessTracker.

        Args:
            account: Account name to check following status for
            target_usernames: List of usernames to check

        Returns:
            Dict mapping username -> True/False (is_following)
        """
        result: dict[str, bool] = {}

        for username in target_usernames:
            is_following = self._check_following(account, username)
            result[username] = is_following

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _select_following_account(
        self,
        target_username: str,
        available_accounts: list[str],
    ) -> Optional[str]:
        """
        Select an account that follows target_username using the fallback chain.

        Fallback order:
          1. following_status cache in UsernameRecord
          2. ProfileAccessTracker accessible_by list
          3. Source account (likely follows if it scraped this username)
          4. None

        Requirements: 3.2, 3.3, 3.4, 3.6, 3.7
        """
        record = None
        if self._username_db is not None:
            record = self._username_db.get_username_record(target_username)

        # Step 1: Check following_status cache (Requirement 3.6)
        if record and record.following_status:
            for account in available_accounts:
                if record.following_status.get(account, False):
                    logger.debug(
                        "Cache hit: account '%s' follows '%s'", account, target_username
                    )
                    return account

        # Step 2: Check ProfileAccessTracker (Requirement 3.6, 3.7)
        if self._profile_tracker is not None:
            profile_summary = self._profile_tracker.get_profile_summary(target_username)
            accessible_by = profile_summary.get("accessible_by", [])

            for account in available_accounts:
                if account in accessible_by:
                    logger.debug(
                        "Tracker hit: account '%s' can access '%s'", account, target_username
                    )
                    # Update cache (Requirement 3.7)
                    if record is not None and self._username_db is not None:
                        if not record.following_status:
                            record.following_status = {}
                        record.following_status[account] = True
                        self._username_db.update_metadata(
                            target_username,
                            {"following_status": record.following_status},
                        )
                    return account

        # Step 3: Fall back to source account (Requirement 3.3)
        if record and record.source_account in available_accounts:
            logger.debug(
                "Fallback: using source account '%s' for '%s'",
                record.source_account,
                target_username,
            )
            return record.source_account

        # Step 4: No following relationship found (Requirement 3.4)
        logger.debug(
            "No following relationship found for '%s' among %s",
            target_username,
            available_accounts,
        )
        return None

    def _check_following(self, account: str, username: str) -> bool:
        """Check if account follows username using cache then tracker."""
        # Check cache
        if self._username_db is not None:
            record = self._username_db.get_username_record(username)
            if record and record.following_status:
                cached = record.following_status.get(account)
                if cached is not None:
                    return cached

        # Check tracker
        if self._profile_tracker is not None:
            summary = self._profile_tracker.get_profile_summary(username)
            accessible_by = summary.get("accessible_by", [])
            return account in accessible_by

        return False

"""
Username Database - Structured storage for usernames with source account tracking.

This module provides the UsernameDatabase class for managing Instagram usernames
with metadata about which account scraped which users, enabling targeted re-scraping
and intelligent account selection.

Persistence is delegated to UsernameRepository (SQLite/PostgreSQL).
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
Optional

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import INSTAGRAM_ACCOUNTS, DATA_DIR


# Instagram username validation pattern
INSTAGRAM_USERNAME_PATTERN = re.compile(r'^[a-zA-Z0-9._]+$')


def is_valid_instagram_username(username: str) -> bool:
    """Validate Instagram username format."""
    if not username or not isinstance(username, str):
        return False
    return bool(INSTAGRAM_USERNAME_PATTERN.match(username))


@dataclass
class UsernameRecord:
    """Data model for a username with source account tracking and metadata."""
    username: str
    source_account: str
    added_timestamp: float
    added_datetime: str
    last_accessed: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    following_status: dict = field(default_factory=dict)

    def __post_init__(self):
        if not is_valid_instagram_username(self.username):
            raise ValueError(f"Invalid Instagram username format: {self.username}")
        account_names = [acc['name'] for acc in INSTAGRAM_ACCOUNTS]
        if self.source_account not in account_names and self.source_account not in ("migrated", "collected", "unknown"):
            raise ValueError(
                f"Invalid source account: {self.source_account}. "
                f"Available accounts: {', '.join(account_names)}"
            )
        if self.added_timestamp <= 0:
            raise ValueError(f"Invalid timestamp: {self.added_timestamp}")
        if not isinstance(self.metadata, dict):
            raise ValueError("Metadata must be a dictionary")
        if not isinstance(self.following_status, dict):
            raise ValueError("Following status must be a dictionary")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'UsernameRecord':
        return cls(**data)


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


class UsernameDatabase:
    """Structured storage for Instagram usernames with source account tracking.

    All persistence is delegated to UsernameRepository.
    The UsernameRecord dataclass and all public method signatures are preserved.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize username database.

        Args:
            db_path: Ignored (kept for backward compat). DB path is controlled by DATABASE_URL.
        """
        self.db_path = db_path or os.path.join(DATA_DIR, "username_database.json")
        from db.repositories.username_repository import UsernameRepository
        self._repo = UsernameRepository(_get_db())

        # In-memory caches for backward compat
        self._usernames: dict[str, UsernameRecord] = {}
        self._source_account_index: dict[str, list[str]] = {}
        self._loaded = False

    def _ensure_loaded(self):
        """Lazily populate in-memory caches from DB."""
        if self._loaded:
            return
        self._loaded = True
        rows = self._repo.get_all()
        for row in rows:
            try:
                record = self._row_to_record(row)
                self._usernames[record.username] = record
                src = record.source_account
                self._source_account_index.setdefault(src, []).append(record.username)
            except Exception:
                pass

    def _row_to_record(self, row: dict) -> UsernameRecord:
        """Convert a DB row dict to a UsernameRecord."""
        meta = json.loads(row.get("metadata_json") or "{}")
        # Fetch following status
        following_status: dict[str, bool] = {}
        try:
            fs_rows = _get_db().fetchall(
                "SELECT account_name, is_following FROM username_following_status WHERE username=?",
                (row["username"],),
            )
            for fs in fs_rows:
                following_status[fs["account_name"]] = bool(fs["is_following"])
        except Exception:
            pass

        # Determine source_account — fall back to "migrated" if not in known accounts
        source = row.get("source_account", "migrated")
        account_names = [acc['name'] for acc in INSTAGRAM_ACCOUNTS]
        if source not in account_names:
            source = "migrated"

        return UsernameRecord(
            username=row["username"],
            source_account=source,
            added_timestamp=float(row.get("added_ts") or time.time()),
            added_datetime=datetime.fromtimestamp(float(row.get("added_ts") or time.time())).isoformat(),
            last_accessed=row.get("last_accessed_ts"),
            metadata=meta,
            following_status=following_status,
        )

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def add_username(
        self,
        username: str,
        source_account: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Add a username to the database. Returns True if added, False if duplicate."""
        if not is_valid_instagram_username(username):
            print(f"[ERROR] Failed to add username {username}: Invalid Instagram username format: {username}")
            return False

        # Validate source account
        account_names = [acc['name'] for acc in INSTAGRAM_ACCOUNTS]
        if source_account not in account_names and source_account not in ("migrated", "collected", "unknown"):
            print(f"[ERROR] Failed to add username {username}: Invalid source account: {source_account}")
            return False

        added = self._repo.add_username(username, source_account, metadata)
        if added:
            # Update in-memory cache
            current_time = time.time()
            try:
                record = UsernameRecord(
                    username=username,
                    source_account=source_account,
                    added_timestamp=current_time,
                    added_datetime=datetime.now().isoformat(),
                    metadata=metadata or {},
                )
                self._usernames[username] = record
                self._source_account_index.setdefault(source_account, []).append(username)
            except Exception:
                pass
        return added

    def get_usernames_by_source(self, source_account: str) -> list:
        """Retrieve all usernames scraped by a specific account."""
        rows = self._repo.get_by_source(source_account)
        records = []
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except Exception:
                pass
        records.sort(key=lambda r: r.added_timestamp)
        return records

    def get_all_usernames(self) -> list:
        """Retrieve all username records."""
        rows = self._repo.get_all()
        records = []
        for row in rows:
            try:
                records.append(self._row_to_record(row))
            except Exception:
                pass
        records.sort(key=lambda r: r.added_timestamp)
        return records

    def get_username_record(self, username: str) -> Optional[UsernameRecord]:
        """Get a single username record."""
        row = _get_db().fetchone("SELECT * FROM usernames WHERE username=?", (username,))
        if row is None:
            return None
        try:
            return self._row_to_record(row)
        except Exception:
            return None

    def update_metadata(self, username: str, metadata: dict) -> bool:
        """Update metadata for a username (merges with existing metadata)."""
        try:
            json.dumps(metadata)
        except (TypeError, ValueError) as e:
            print(f"[ERROR] Metadata is not JSON-serializable for {username}: {e}")
            return False
        return self._repo.update_metadata(username, metadata)

    def update_last_accessed(self, username: str, timestamp: Optional[float] = None) -> bool:
        """Update the last_accessed timestamp for a username."""
        if timestamp is not None:
            _get_db().execute(
                "UPDATE usernames SET last_accessed_ts=? WHERE username=?",
                (timestamp, username),
            )
            row = _get_db().fetchone("SELECT 1 FROM usernames WHERE username=?", (username,))
            return row is not None
        return self._repo.update_last_accessed(username)

    def update_following_status(self, username: str, account_name: str, is_following: bool) -> bool:
        """Update the following status for a specific account-username pair."""
        return self._repo.update_following_status(username, account_name, is_following)

    def remove_username(self, username: str) -> bool:
        """Remove a username from the database."""
        self.create_backup()
        return self._repo.remove(username)

    def save(self, max_retries: int = 3) -> bool:
        """No-op: data is persisted immediately via the repository."""
        return True

    def load(self) -> bool:
        """No-op: data is loaded from DB on demand."""
        self._loaded = False
        return True

    def _load_from_backup(self) -> bool:
        return False

    def _rebuild_index(self):
        self._source_account_index = {}
        for username, record in self._usernames.items():
            src = record.source_account
            self._source_account_index.setdefault(src, []).append(username)

    def create_backup(self) -> bool:
        """No-op in DB mode."""
        return True

    def migrate_from_flat_file(self, filepath: str, default_source: str) -> dict:
        """Migrate usernames from flat file to structured database."""
        import shutil
        backup_path = f"{filepath}.backup.{int(time.time())}"
        try:
            shutil.copy2(filepath, backup_path)
        except Exception as e:
            return {"error": str(e)}

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return {"error": str(e)}

        stats = {"added": 0, "skipped": 0, "duplicates": 0, "invalid": 0}
        for line in lines:
            username = line.strip()
            if not username:
                stats["skipped"] += 1
                continue
            if not is_valid_instagram_username(username):
                stats["invalid"] += 1
                continue
            success = self.add_username(
                username=username,
                source_account=default_source,
                metadata={"migrated_from_flat_file": True, "migration_timestamp": time.time()},
            )
            if success:
                stats["added"] += 1
            else:
                stats["duplicates"] += 1

        return {
            "total_lines": len(lines),
            "backup_path": backup_path,
            "statistics": stats,
            "migration_timestamp": datetime.now().isoformat(),
        }

    def export_to_flat_file(self, filepath: str) -> int:
        """Export all usernames to flat file format (one per line)."""
        try:
            records = self.get_all_usernames()
            with open(filepath, 'w', encoding='utf-8') as f:
                for record in records:
                    f.write(f"{record.username}\n")
            return len(records)
        except Exception as e:
            print(f"[ERROR] Failed to export to flat file: {e}")
            return 0

"""User metadata management for storing profile information.

Manages follower/following counts and other profile metadata
for all scraped profiles, enabling network analysis and filtering.

Persistence is delegated to ProfileRepository (SQLite/PostgreSQL).
"""
import os
import sys
import time
List

# Ensure src/ is on the path when imported from tests
_LIB_DIR = os.path.dirname(os.path.abspath(__file__))
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from src.config import DATA_DIR


def _get_db():
    """Return a module-level DatabaseManager singleton."""
    import os as _os
    from db.manager import DatabaseManager
    _url = _os.environ.get("DATABASE_URL", "")
    # Use a module-level singleton
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(_url)
    return _get_db._instance


_get_db._instance = None


class UserMetadataManager:
    """Manage profile metadata for scraped users.

    Tracks follower counts, following counts, and other profile information
    for network analysis and influence identification.

    All persistence is delegated to ProfileRepository.
    """

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        from db.repositories.profile_repository import ProfileRepository
        self._repo = ProfileRepository(_get_db())

    # ── Private helpers (kept for backward compat; no-ops now) ────────────

    def _load_metadata(self) -> Dict[str, Any]:
        """Deprecated: data now lives in the DB."""
        return self._repo.get_all_profiles()

    def _save_metadata(self):
        """Deprecated: data is persisted immediately via the repository."""
        pass

    # ── Public API (unchanged signatures) ─────────────────────────────────

    def update_profile(self, username: str, profile_obj, account_name: str):
        """Update metadata for a specific profile.

        Args:
            username: Profile username
            profile_obj: Instaloader Profile object
            account_name: Account used to collect this data
        """
        try:
            followers_count = getattr(profile_obj, 'followers', 0) or 0
            following_count = getattr(profile_obj, 'followees', 0) or 0

            data = {
                'username': username,
                'followers_count': followers_count,
                'following_count': following_count,
                'is_public': not profile_obj.is_private,
                'is_verified': getattr(profile_obj, 'is_verified', False),
                'profile_pic_url': getattr(profile_obj, 'profile_pic_url', None),
                'biography': getattr(profile_obj, 'biography', ''),
                'full_name': getattr(profile_obj, 'full_name', username),
                'external_url': getattr(profile_obj, 'external_url', None),
                'last_collected_ts': time.time(),
                'collected_by': account_name,
                'media_count': getattr(profile_obj, 'mediacount', 0),
            }

            self._repo.upsert_profile(username, data)
            print(f"[METADATA] Saved profile for {username} ({followers_count} followers, {following_count} following)")
            # Insert snapshot row (one per scrape)
            user_id = getattr(profile_obj, 'userid', None)
            if user_id:
                db = _get_db()
                db.execute(
                    """INSERT INTO profile_snapshots 
                       (username, user_id, followers_count, following_count, media_count, is_public, scraped_by, snapshot_ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, unixepoch())""",
                    (
                        username,
                        str(user_id),  # Store as TEXT
                        followers_count,
                        following_count,
                        data['media_count'],
                        1 if data['is_public'] else 0,
                        account_name
                    )
                )
                print(f"[SNAPSHOT] Saved profile snapshot for {username}")


        except Exception as e:
            print(f"[WARNING] Could not update metadata for {username}: {e}")

    def get_profile(self, username: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific profile."""
        return self._repo.get_profile(username)

    def get_all_profiles(self) -> Dict[str, Any]:
        """Get all profile metadata."""
        return self._repo.get_all_profiles()

    def get_profile_count(self) -> int:
        """Get total number of profiles tracked."""
        rows = self._repo.get_all_profiles()
        return len(rows)

    def get_top_followers(self, n: int = 10) -> List[Dict[str, Any]]:
        """Get top N profiles by follower count."""
        return self._repo.get_top_by_followers(n)

    def get_top_following(self, n: int = 10) -> List[Dict[str, Any]]:
        """Get top N profiles by following count."""
        return self._repo.get_top_by_following(n)

    def get_public_profiles(self) -> List[str]:
        """Get list of public profile usernames."""
        rows = self._repo.get_all_profiles()
        return [u for u, d in rows.items() if d.get('is_public', 1)]

    def get_private_profiles(self) -> List[str]:
        """Get list of private profile usernames."""
        rows = self._repo.get_all_profiles()
        return [u for u, d in rows.items() if not d.get('is_public', 1)]

    def get_verified_profiles(self) -> List[str]:
        """Get list of verified profile usernames."""
        rows = self._repo.get_all_profiles()
        return [u for u, d in rows.items() if d.get('is_verified', 0)]

    def filter_by_follower_count(
        self, min_followers: int = 0, max_followers: int = None
    ) -> List[str]:
        """Filter profiles by follower count range."""
        return self._repo.filter_by_follower_range(min_followers, max_followers)

    def get_network_stats(self) -> Dict[str, Any]:
        """Generate network statistics from metadata."""
        profiles = self._repo.get_all_profiles()
        if not profiles:
            return {
                'total_profiles': 0,
                'public_profiles': 0,
                'private_profiles': 0,
                'verified_profiles': 0,
                'avg_followers': 0,
                'avg_following': 0,
                'top_followers': [],
                'top_following': [],
            }

        profile_list = list(profiles.values())
        public_count = sum(1 for p in profile_list if p.get('is_public', 1))
        verified_count = sum(1 for p in profile_list if p.get('is_verified', 0))
        follower_counts = [p.get('followers_count', 0) for p in profile_list]
        following_counts = [p.get('following_count', 0) for p in profile_list]

        return {
            'total_profiles': len(profile_list),
            'public_profiles': public_count,
            'private_profiles': len(profile_list) - public_count,
            'verified_profiles': verified_count,
            'avg_followers': sum(follower_counts) / len(follower_counts) if follower_counts else 0,
            'avg_following': sum(following_counts) / len(following_counts) if following_counts else 0,
            'top_followers': self.get_top_followers(10),
            'top_following': self.get_top_following(10),
        }

    def is_within_follower_limit(self, username: str, max_followers: int) -> bool:
        """Check if a profile's follower count is within the configured limit."""
        if max_followers <= 0:
            return True
        profile = self.get_profile(username)
        if profile is None:
            return True
        return profile.get('followers_count', 0) <= max_followers

    def clear_metadata(self):
        """Clear all metadata (use with caution)."""
        # Not supported in DB mode — would require DELETE FROM profiles
        print("[METADATA] clear_metadata() is a no-op in database mode")


__all__ = ['UserMetadataManager']

"""
Input validation utilities for Instagram Toolkit.

Provides centralized validation for usernames, file paths, and configuration
to prevent errors and ensure data integrity.
"""
from __future__ import annotations

import os
import re
Optional
from pathlib import Path


class ValidationError(Exception):
    """Raised when validation fails."""
    pass


def validate_username(username: str) -> Tuple[bool, str]:
    """
    Validate an Instagram username.
    
    Instagram usernames:
    - Can contain letters, numbers, periods, underscores, and hyphens
    - Cannot contain consecutive periods
    - Must be 1-30 characters long
    - Cannot start or end with a period
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not username:
        return False, "Username cannot be empty"
    
    if not isinstance(username, str):
        return False, "Username must be a string"
    
    username = username.strip()
    
    if len(username) < 1:
        return False, "Username too short (minimum 1 character)"
    
    if len(username) > 30:
        return False, "Username too long (maximum 30 characters)"
    
    # Instagram username pattern: letters, numbers, periods, underscores, hyphens
    # Cannot have consecutive periods
    pattern = r'^(?!.*\.\.)[a-zA-Z0-9._-]+$'
    if not re.match(pattern, username):
        return False, "Username contains invalid characters"
    
    # Cannot start or end with a period
    if username.startswith('.') or username.endswith('.'):
        return False, "Username cannot start or end with a period"
    
    return True, ""


def validate_username_list(usernames: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Validate a list of usernames.
    
    Returns:
        Tuple of (valid_usernames, list of (username, error_message) for invalid ones)
    """
    valid = []
    invalid = []
    
    for username in usernames:
        is_valid, error = validate_username(username)
        if is_valid:
            valid.append(username.strip())
        else:
            invalid.append((username, error))
    
    return valid, invalid


def validate_file_path(path: str, must_exist: bool = False) -> Tuple[bool, str]:
    """
    Validate a file path.
    
    Args:
        path: The path to validate
        must_exist: If True, the path must already exist
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not path:
        return False, "Path cannot be empty"
    
    if not isinstance(path, str):
        return False, "Path must be a string"
    
    path = path.strip()
    
    if not path:
        return False, "Path is empty after stripping"
    
    # Check for invalid characters (Windows and Unix)
    # Note: path separators (\ and /) are VALID and should not be checked here
    invalid_chars = ['<', '>', '"', '|', '?', '*']
    
    for char in invalid_chars:
        if char in path:
            return False, f"Path contains invalid character: '{char}'"
    
    # Check path length (Windows has 260 char limit, but we use a safer 200)
    if len(path) > 200:
        return False, "Path too long (maximum 200 characters for safety)"
    
    if must_exist and not os.path.exists(path):
        return False, f"Path does not exist: {path}"
    
    return True, ""


def validate_directory(path: str, must_be_writable: bool = True) -> Tuple[bool, str]:
    """
    Validate a directory path.
    
    Args:
        path: The directory path to validate
        must_be_writable: If True, the directory must be writable
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    is_valid, error = validate_file_path(path, must_exist=False)
    if not is_valid:
        return False, error
    
    # Create directory if it doesn't exist
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create directory: {e}"
    
    # Check if writable
    if must_be_writable:
        test_file = os.path.join(path, '.write_test')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except Exception as e:
            return False, f"Directory is not writable: {e}"
    
    return True, ""


def validate_instagram_accounts(accounts: List[dict]) -> Tuple[bool, str]:
    """
    Validate Instagram account configurations.
    
    Args:
        accounts: List of account dictionaries with 'name', 'username', 'password'
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not accounts:
        return False, "No accounts configured"
    
    if not isinstance(accounts, list):
        return False, "Accounts must be a list"
    
    for i, account in enumerate(accounts):
        if not isinstance(account, dict):
            return False, f"Account {i+1} must be a dictionary"
        
        # Check required fields
        for field in ['name', 'username', 'password']:
            if field not in account:
                return False, f"Account {i+1} missing required field: {field}"
            if not account[field]:
                return False, f"Account {i+1} has empty {field}"
        
        # Validate username format
        is_valid, error = validate_username(account['username'])
        if not is_valid:
            return False, f"Account {i+1} has invalid username: {error}"
        
        # Check for duplicate names
        if sum(1 for a in accounts if a['name'] == account['name']) > 1:
            return False, f"Duplicate account name: {account['name']}"
        
        # Check for duplicate usernames
        if sum(1 for a in accounts if a['username'] == account['username']) > 1:
            return False, f"Duplicate username: {account['username']}"
    
    return True, ""


def validate_download_limit(limit: Optional[int]) -> Tuple[bool, str]:
    """
    Validate a download limit parameter.
    
    Args:
        limit: The limit to validate (None means unlimited)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if limit is None:
        return True, ""
    
    if not isinstance(limit, int):
        return False, "Download limit must be an integer"
    
    if limit < 1:
        return False, "Download limit must be at least 1"
    
    if limit > 10000:
        return False, "Download limit too high (maximum 10000 for safety)"
    
    return True, ""


def validate_max_relationships(max_count: int) -> Tuple[bool, str]:
    """
    Validate maximum relationships to collect.
    
    Args:
        max_count: Maximum number of followers/following to collect
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(max_count, int):
        return False, "Maximum relationships must be an integer"
    
    if max_count < 0:
        return False, "Maximum relationships cannot be negative"
    
    if max_count > 100000:
        return False, "Maximum relationships too high (maximum 100000 for safety)"
    
    return True, ""


def safe_validate(func, *args, **kwargs):
    """
    Safely execute a validation function, returning default on exception.
    
    Args:
        func: The validation function to call
        *args, **kwargs: Arguments to pass to the function
        
    Returns:
        The result of the validation function, or (False, "Validation error") on exception
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        return False, f"Validation error: {e}"


__all__ = [
    "ValidationError",
    "validate_username",
    "validate_username_list",
    "validate_file_path",
    "validate_directory",
    "validate_instagram_accounts",
    "validate_download_limit",
    "validate_max_relationships",
    "safe_validate",
]


"""
Base command class for Instagram Toolkit.

Provides a common interface for all CLI commands with standardized
argument handling, validation, and error reporting.
"""
from __future__ import annotations

import argparse
from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod


class BaseCommand(ABC):
    """Base class for all CLI commands."""
    
    name: str = ""
    description: str = ""
    help_text: str = ""
    
    def __init__(self, parser: argparse.ArgumentParser):
        """
        Initialize command with argument parser.
        
        Args:
            parser: Subparser to add arguments to
        """
        self.parser = parser
        self._add_arguments()
    
    @abstractmethod
    def _add_arguments(self):
        """Add command-specific arguments to parser."""
        pass
    
    @abstractmethod
    def execute(self, args: argparse.Namespace) -> int:
        """
        Execute the command.
        
        Args:
            args: Parsed command arguments
            
        Returns:
            Exit code (0 for success, non-zero for failure)
        """
        pass
    
    def validate_args(self, args: argparse.Namespace) -> List[str]:
        """
        Validate command arguments.
        
        Args:
            args: Parsed command arguments
            
        Returns:
            List of validation error messages (empty if valid)
        """
        return []
    
    def print_error(self, message: str):
        """Print formatted error message."""
        print(f"[ERROR] {message}")
    
    def print_warning(self, message: str):
        """Print formatted warning message."""
        print(f"[WARNING] {message}")
    
    def print_info(self, message: str):
        """Print formatted info message."""
        print(f"[INFO] {message}")
    
    def print_success(self, message: str):
        """Print formatted success message."""
        print(f"[OK] {message}")


__all__ = ["BaseCommand"]

"""
Download command - Download media from user profiles.
"""
from src.commands.base import BaseCommand
import argparse


class DownloadCommand(BaseCommand):
    """Download media (photos, videos, stories, highlights) from profiles."""
    
    name = "download"
    description = "Download media from user profiles"
    help_text = "Download media (photos, videos, stories, highlights) from one or more users"
    
    def _add_arguments(self):
        """Add download-specific arguments."""
        self.parser.add_argument(
            'usernames',
            nargs='+',
            help='Usernames to download from'
        )
        self.parser.add_argument(
            '--post-limit',
            type=int,
            default=None,
            help='Maximum number of posts to download per user'
        )
        self.parser.add_argument(
            '--account',
            type=str,
            help='Specific Instagram account name to use'
        )
        self.parser.add_argument(
            '--operation',
            type=str,
            default='download_media',
            help='Operation type for smart routing (default: download_media)'
        )
        self.parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show account selection reasoning'
        )
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute media download using smart routing."""
        try:
            from src.commands.smart_routing_helper import run_operation
            from src.parallel_processor import InstagramProcessor

            # Build execute_fn that delegates to the existing processor
            processor = InstagramProcessor(
                account_name=args.account,
                operation_type="download"
            )

            def execute_fn(account_name: str, username: str) -> bool:
                try:
                    processor.process_batch_downloads(
                        [username],
                        post_limit=args.post_limit,
                    )
                    return True
                except Exception:
                    return False

            run_operation(
                operation_name=args.operation,
                target_usernames=args.usernames,
                execute_fn=execute_fn,
                available_accounts=[args.account] if args.account else None,
                verbose=getattr(args, 'verbose', False),
            )
            return 0

        except Exception as e:
            self.print_error(f"Download failed: {e}")
            return 1


__all__ = ["DownloadCommand"]

"""
Following download command - Download media from accounts you follow.
"""
from src.commands.base import BaseCommand
import argparse


class FollowingDownloadCommand(BaseCommand):
    """Download media only from accounts in your following list."""
    
    name = "following-download"
    description = "Download media from followed accounts"
    help_text = "Download media only from accounts you follow"
    
    def _add_arguments(self):
        """Add following-download-specific arguments."""
        self.parser.add_argument(
            '--profile-only',
            action='store_true',
            help='Only download profile pictures'
        )
        self.parser.add_argument(
            '--posts-only',
            action='store_true',
            help='Only download posts'
        )
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute following media download."""
        try:
            from src.following_media_downloader import FollowingMediaDownloader
            
            # Create downloader
            downloader = FollowingMediaDownloader()
            
            # Select account
            if not downloader.select_account():
                return 1
            
            # Set download directory
            downloader._get_downloads_dir()
            
            # Get following list
            downloader.get_following_list()
            
            self.print_info(f"Processing {len(downloader.following_list)} followed accounts...")
            
            # Download from all followed accounts
            for username in downloader.following_list:
                downloader.download_account_media(username)
            
            # Cleanup
            downloader.cleanup()
            return 0
            
        except Exception as e:
            self.print_error(f"Following download failed: {e}")
            return 1


__all__ = ["FollowingDownloadCommand"]

"""
Smart Routing Helper - Wraps process_operation_with_smart_routing for CLI commands.

Provides a thin helper that adds operation type display and verbose account
selection reasoning on top of the core routing function.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from src.operation_router import process_operation_with_smart_routing
from src.operation_classifier import OperationClassifier
from src.conservative_rate_limiter import ConservativeRateLimiter
from src.smart_account_selector import SmartAccountSelector
from src.username_database import UsernameDatabase

logger = logging.getLogger(__name__)


def run_operation(
    operation_name: str,
    target_usernames: list[str],
    execute_fn: Callable[[str, str], bool],
    *,
    username_db: Optional[UsernameDatabase] = None,
    rate_limiter: Optional[ConservativeRateLimiter] = None,
    account_selector: Optional[SmartAccountSelector] = None,
    available_accounts: Optional[list[str]] = None,
    verbose: bool = False,
) -> dict:
    """
    Wrap process_operation_with_smart_routing with CLI-friendly output.

    Displays the operation type before processing and, in verbose mode,
    shows account selection reasoning for each account assignment.

    Args:
        operation_name: Registered operation name (e.g. "download_stories")
        target_usernames: List of Instagram usernames to process
        execute_fn: Callable(account_name, username) -> bool
        username_db: Optional UsernameDatabase instance
        rate_limiter: Optional ConservativeRateLimiter instance
        account_selector: Optional SmartAccountSelector instance
        available_accounts: Optional list of account names to use
        verbose: If True, print account selection reasoning

    Returns:
        Result dict with total, success_count, failed_count, results
    """
    # Classify and display operation type
    classifier = OperationClassifier()
    operation_type = classifier.classify(operation_name)
    op_meta = classifier.get_operation_metadata(operation_name)

    print(f"[INFO] Operation: {operation_name}")
    print(f"[INFO] Type: {operation_type.value}  (rate limit weight: {op_meta.rate_limit_weight})")

    if verbose and available_accounts:
        _print_account_selection_reasoning(
            operation_name, operation_type, target_usernames,
            available_accounts, account_selector, username_db,
        )

    result = process_operation_with_smart_routing(
        operation_name=operation_name,
        target_usernames=target_usernames,
        execute_fn=execute_fn,
        username_db=username_db,
        rate_limiter=rate_limiter,
        account_selector=account_selector,
        available_accounts=available_accounts,
    )

    # Display summary
    print(
        f"[INFO] Done — total: {result['total']}, "
        f"success: {result['success_count']}, "
        f"failed: {result['failed_count']}"
    )

    return result


def _print_account_selection_reasoning(
    operation_name: str,
    operation_type,
    target_usernames: list[str],
    available_accounts: list[str],
    account_selector: Optional[SmartAccountSelector],
    username_db: Optional[UsernameDatabase],
) -> None:
    """Print verbose account selection reasoning."""
    from src.operation_classifier import OperationType

    print(f"[VERBOSE] Account selection reasoning for '{operation_name}':")
    print(f"[VERBOSE]   Available accounts: {', '.join(available_accounts)}")
    print(f"[VERBOSE]   Target usernames:   {len(target_usernames)}")

    if operation_type == OperationType.PUBLIC:
        print(
            f"[VERBOSE]   Strategy: PUBLIC — any account works; "
            f"assigning all to '{available_accounts[0]}'"
        )
        return

    # FOLLOWING_REQUIRED / MUTUAL_FOLLOWING — show per-username reasoning
    selector = account_selector or SmartAccountSelector(username_db=username_db)
    assignment = selector.select_for_batch(operation_type, target_usernames, available_accounts)

    print(f"[VERBOSE]   Strategy: {operation_type.value} — grouping by following relationships")
    for account, usernames in assignment.items():
        print(f"[VERBOSE]     {account} → {len(usernames)} username(s): {', '.join(usernames[:5])}"
              + (" ..." if len(usernames) > 5 else ""))


__all__ = ["run_operation"]

"""
Spider command - Collect followers/following data.
"""
from src.commands.base import BaseCommand
import argparse


class SpiderCommand(BaseCommand):
    """Collect followers and following data for user profiles."""
    
    name = "spider"
    description = "Collect followers and following data"
    help_text = "Collect followers and following data for one or more users"
    
    def _add_arguments(self):
        """Add spider-specific arguments."""
        self.parser.add_argument(
            'usernames',
            nargs='*',
            help='Usernames to spider (if none, reads from data/usernames.txt)'
        )
        self.parser.add_argument(
            '--max-followers',
            type=int,
            default=1000,
            help='Maximum followers to collect per user (default: 1000)'
        )
        self.parser.add_argument(
            '--max-following',
            type=int,
            default=1000,
            help='Maximum following to collect per user (default: 1000)'
        )
        self.parser.add_argument(
            '--account',
            type=str,
            help='Specific Instagram account name to use'
        )
    
    def execute(self, args: argparse.Namespace) -> int:
        """Execute spider collection."""
        try:
            from src.parallel_processor import InstagramProcessor
            
            # Determine usernames
            if args.usernames:
                usernames = args.usernames
            else:
                from src.config import USERNAMES_FILE
                usernames = self._load_usernames(USERNAMES_FILE)
                if not usernames:
                    self.print_error("No usernames provided and file not found")
                    return 1
            
            # Create processor
            processor = InstagramProcessor(
                account_name=args.account,
                operation_type="spider"
            )
            
            # Run spider
            processor.process_batch_relationships(
                usernames,
                max_followers=args.max_followers,
                max_following=args.max_following
            )
            
            return 0
            
        except Exception as e:
            self.print_error(f"Spider failed: {e}")
            return 1
    
    def _load_usernames(self, filepath: str) -> list:
        """Load usernames from file."""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []

"""
Username Database CLI Commands - Manage the username database from the command line.

Provides commands for:
- Viewing usernames by source account
- Migrating from flat file
- Exporting to flat file
- Viewing database statistics
"""
from __future__ import annotations

import argparse
import os
import sys

from src.commands.base import BaseCommand
from src.username_database import UsernameDatabase
from src.config import INSTAGRAM_ACCOUNTS, DATA_DIR


def _get_account_names() -> list[str]:
    """Return list of configured account names."""
    return [acc["name"] for acc in INSTAGRAM_ACCOUNTS]


def _get_db(db_path: str | None = None) -> UsernameDatabase:
    """Create a UsernameDatabase instance, optionally with a custom path."""
    if db_path:
        return UsernameDatabase(db_path=db_path)
    return UsernameDatabase()


# ---------------------------------------------------------------------------
# Command: username-db list
# ---------------------------------------------------------------------------

class UsernameDbListCommand(BaseCommand):
    """View usernames stored in the database, optionally filtered by source account."""

    name = "username-db-list"
    description = "View usernames by source account"
    help_text = "List usernames in the database, optionally filtered by source account"

    def _add_arguments(self):
        self.parser.add_argument(
            "--source",
            type=str,
            default=None,
            help="Filter by source account name",
        )
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of usernames to display",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            db = _get_db(args.db)

            if args.source:
                records = db.get_usernames_by_source(args.source)
                if not records:
                    self.print_info(f"No usernames found for source account: {args.source}")
                    return 0
                self.print_info(f"Usernames scraped by '{args.source}' ({len(records)} total):")
            else:
                records = db.get_all_usernames()
                if not records:
                    self.print_info("Database is empty.")
                    return 0
                self.print_info(f"All usernames ({len(records)} total):")

            if args.limit:
                records = records[: args.limit]

            for record in records:
                last = record.last_accessed
                last_str = f", last accessed: {record.added_datetime[:10]}" if last else ""
                print(f"  {record.username} (source: {record.source_account}{last_str})")

            return 0

        except Exception as e:
            self.print_error(f"Failed to list usernames: {e}")
            return 1


# ---------------------------------------------------------------------------
# Command: username-db migrate
# ---------------------------------------------------------------------------

class UsernameDbMigrateCommand(BaseCommand):
    """Migrate usernames from a flat file into the structured database."""

    name = "username-db-migrate"
    description = "Migrate usernames from flat file to database"
    help_text = "Import usernames from a flat text file (one per line) into the database"

    def _add_arguments(self):
        self.parser.add_argument(
            "filepath",
            type=str,
            help="Path to flat file with usernames (one per line)",
        )
        self.parser.add_argument(
            "--source",
            type=str,
            required=True,
            help="Source account name to attribute all migrated usernames to",
        )
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )

    def validate_args(self, args: argparse.Namespace) -> list[str]:
        errors = []
        if not os.path.exists(args.filepath):
            errors.append(f"File not found: {args.filepath}")
        account_names = _get_account_names()
        if account_names and args.source not in account_names:
            errors.append(
                f"Unknown source account '{args.source}'. "
                f"Available: {', '.join(account_names)}"
            )
        return errors

    def execute(self, args: argparse.Namespace) -> int:
        errors = self.validate_args(args)
        if errors:
            for err in errors:
                self.print_error(err)
            return 1

        try:
            db = _get_db(args.db)
            result = db.migrate_from_flat_file(
                filepath=args.filepath,
                default_source=args.source,
            )

            if "error" in result:
                self.print_error(f"Migration failed: {result['error']}")
                return 1

            stats = result["statistics"]
            self.print_success("Migration complete:")
            print(f"  Total lines:  {result['total_lines']}")
            print(f"  Added:        {stats['added']}")
            print(f"  Duplicates:   {stats['duplicates']}")
            print(f"  Invalid:      {stats['invalid']}")
            print(f"  Skipped:      {stats['skipped']}")
            print(f"  Backup:       {result['backup_path']}")
            return 0

        except Exception as e:
            self.print_error(f"Migration failed: {e}")
            return 1


# ---------------------------------------------------------------------------
# Command: username-db export
# ---------------------------------------------------------------------------

class UsernameDbExportCommand(BaseCommand):
    """Export all usernames from the database to a flat text file."""

    name = "username-db-export"
    description = "Export usernames to flat file"
    help_text = "Export all usernames from the database to a flat text file (one per line)"

    def _add_arguments(self):
        self.parser.add_argument(
            "filepath",
            type=str,
            help="Output file path",
        )
        self.parser.add_argument(
            "--source",
            type=str,
            default=None,
            help="Export only usernames from this source account",
        )
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            db = _get_db(args.db)

            if args.source:
                records = db.get_usernames_by_source(args.source)
                usernames = [r.username for r in records]
                try:
                    with open(args.filepath, "w", encoding="utf-8") as f:
                        for username in usernames:
                            f.write(f"{username}\n")
                    count = len(usernames)
                except Exception as e:
                    self.print_error(f"Failed to write file: {e}")
                    return 1
            else:
                count = db.export_to_flat_file(args.filepath)

            if count == 0:
                self.print_warning("No usernames exported (database may be empty).")
            else:
                self.print_success(f"Exported {count} usernames to {args.filepath}")
            return 0

        except Exception as e:
            self.print_error(f"Export failed: {e}")
            return 1


# ---------------------------------------------------------------------------
# Command: username-db stats
# ---------------------------------------------------------------------------

class UsernameDbStatsCommand(BaseCommand):
    """Display statistics about the username database."""

    name = "username-db-stats"
    description = "View database statistics"
    help_text = "Show statistics about the username database (total counts, per-account breakdown)"

    def _add_arguments(self):
        self.parser.add_argument(
            "--db",
            type=str,
            default=None,
            help="Path to database file (default: data/username_database.json)",
        )

    def execute(self, args: argparse.Namespace) -> int:
        try:
            db = _get_db(args.db)
            all_records = db.get_all_usernames()
            total = len(all_records)

            print("Username Database Statistics")
            print("=" * 40)
            print(f"Total usernames: {total}")

            if total == 0:
                self.print_info("Database is empty.")
                return 0

            # Per-account breakdown
            print("\nBy source account:")
            for account_name, usernames in db.source_account_index.items():
                count = len(usernames)
                pct = (count / total * 100) if total else 0
                print(f"  {account_name}: {count} ({pct:.1f}%)")

            # Accessed vs never accessed
            accessed = sum(1 for r in all_records if r.last_accessed is not None)
            print(f"\nAccessed:        {accessed}")
            print(f"Never accessed:  {total - accessed}")

            # Database file info
            db_path = db.db_path
            if os.path.exists(db_path):
                size_kb = os.path.getsize(db_path) / 1024
                print(f"\nDatabase file:   {db_path}")
                print(f"File size:       {size_kb:.1f} KB")

            return 0

        except Exception as e:
            self.print_error(f"Failed to get statistics: {e}")
            return 1


__all__ = [
    "UsernameDbListCommand",
    "UsernameDbMigrateCommand",
    "UsernameDbExportCommand",
    "UsernameDbStatsCommand",
]

"""
Command registry - Available CLI commands.
"""
from src.commands.base import BaseCommand
from src.commands.spider import SpiderCommand
from src.commands.download import DownloadCommand
from src.commands.following_download import FollowingDownloadCommand
from src.commands.username_db_commands import (
    UsernameDbListCommand,
    UsernameDbMigrateCommand,
    UsernameDbExportCommand,
    UsernameDbStatsCommand,
)


def get_commands() -> dict[str, type[BaseCommand]]:
    """Get all available commands.
    
    Returns:
        dict: Mapping of command name to command class
    """
    return {
        'spider': SpiderCommand,
        'download': DownloadCommand,
        'following-download': FollowingDownloadCommand,
        'username-db-list': UsernameDbListCommand,
        'username-db-migrate': UsernameDbMigrateCommand,
        'username-db-export': UsernameDbExportCommand,
        'username-db-stats': UsernameDbStatsCommand,
    }


__all__ = ["get_commands"]

"""Database backend implementations.

Provides BaseBackend ABC and concrete SQLiteBackend / PostgreSQLBackend classes.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from abc import ABC, abstractmethod
from typing import Any


class BaseBackend(ABC):
    """Abstract base class that all database backends must implement."""

    @abstractmethod
    def connect(self) -> Any:
        """Return a database connection."""
        ...

    @abstractmethod
    def placeholder(self) -> str:
        """Return the parameter placeholder string for this backend."""
        ...

    @abstractmethod
    def upsert_syntax(self) -> str:
        """Return the INSERT-or-replace prefix for this backend."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the backend connection / pool."""
        ...


class SQLiteBackend(BaseBackend):
    """SQLite backend using Python's built-in sqlite3 module.

    Uses WAL journal mode for concurrent read access and sets file
    permissions to 0o600 on creation.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # Ensure parent directory exists
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """Return (or create) the SQLite connection with WAL mode enabled."""
        if self._conn is None:
            is_new = self._db_path == ":memory:" or not os.path.exists(self._db_path)
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            # Set file permissions to 0o600 on creation
            if is_new and self._db_path != ":memory:":
                try:
                    os.chmod(self._db_path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
        return self._conn

    def placeholder(self) -> str:
        return "?"

    def upsert_syntax(self) -> str:
        return "INSERT OR REPLACE"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class PostgreSQLBackend(BaseBackend):
    """PostgreSQL backend using psycopg2.

    Raises ImportError with install instructions when psycopg2 is absent.
    """

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg2  # noqa: F401
            import psycopg2.extras  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "psycopg2 is required for PostgreSQL support. "
                "Install it with: pip install psycopg2-binary"
            ) from exc
        self._database_url = database_url
        self._conn = None

    def connect(self):
        """Return a psycopg2 connection."""
        import psycopg2
        import psycopg2.extras

        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(
                self._database_url,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
        return self._conn

    def placeholder(self) -> str:
        return "%s"

    def upsert_syntax(self) -> str:
        return "INSERT"

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
            self._conn = None


__all__ = ["BaseBackend", "SQLiteBackend", "PostgreSQLBackend"]

"""DatabaseManager — single entry point for all database access.

Parses DATABASE_URL, instantiates the correct backend, manages connection
lifecycle, and exposes a simple query interface that always returns dicts.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from typing import Any

from src.backends import SQLiteBackend, PostgreSQLBackend
from src.schema import SCHEMA_DDL

_DEFAULT_DB_PATH = os.path.join("data", "instagram_toolkit.db")


class DatabaseManager:
    """Backend-agnostic database manager.

    Usage::

        db = DatabaseManager()                          # SQLite default
        db = DatabaseManager("sqlite:///data/foo.db")  # explicit SQLite
        db = DatabaseManager("postgresql://...")        # PostgreSQL

    Or set the DATABASE_URL environment variable and call DatabaseManager()
    with no arguments.
    """

    def __init__(self, database_url: str | None = None) -> None:
        if database_url is None:
            database_url = os.environ.get("DATABASE_URL", "")

        if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
            self._backend = PostgreSQLBackend(database_url)
            self._is_sqlite = False
        else:
            # Parse sqlite:///path or use default
            if database_url.startswith("sqlite:///"):
                db_path = database_url[len("sqlite:///"):]
            elif database_url == "sqlite:///:memory:" or database_url == ":memory:":
                db_path = ":memory:"
            elif database_url == "":
                db_path = _DEFAULT_DB_PATH
            else:
                db_path = database_url  # bare path
            self._backend = SQLiteBackend(db_path)
            self._is_sqlite = True

        # Thread safety lock for concurrent access
        self._lock = threading.Lock()
        
        self.create_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def create_schema(self) -> None:
        """Apply all DDL statements idempotently (CREATE TABLE IF NOT EXISTS)."""
        conn = self._backend.connect()
        try:
            for ddl in SCHEMA_DDL:
                conn.execute(ddl)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Connection context manager ────────────────────────────────────────

    @contextlib.contextmanager
    def get_connection(self):
        """Context manager that yields a connection and commits/rolls back."""
        conn = self._backend.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── Query helpers ─────────────────────────────────────────────────────

    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite3.Row or psycopg2 RealDictRow to a plain dict."""
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return dict(row)
        # psycopg2 RealDictRow is already dict-like
        return dict(row)

    def execute(self, sql: str, params: tuple = ()) -> Any:
        """Execute a DML statement and return the cursor."""
        with self._lock:
            conn = self._backend.connect()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def executemany(self, sql: str, params_seq: list) -> None:
        """Execute a DML statement for each parameter set."""
        with self._lock:
            conn = self._backend.connect()
            conn.executemany(sql, params_seq)
            conn.commit()

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute a SELECT and return the first row as a dict, or None."""
        with self._lock:
            conn = self._backend.connect()
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
            return self._row_to_dict(row)

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT and return all rows as a list of dicts."""
        with self._lock:
            conn = self._backend.connect()
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying backend connection."""
        self._backend.close()


__all__ = ["DatabaseManager"]

"""One-shot JSON-to-database migration script.

migrate_json_to_db(data_dir, db_manager) reads all existing JSON flat files
from data_dir, inserts their records into the database, and renames each
source file to <name>.bak ONLY after a successful commit.

Safety guarantees:
- NEVER touches .env, sessions/, or the data/ directory itself
- Missing files are skipped (recorded as skipped, no exception)
- Per-record errors are caught, recorded, and processing continues
- Returns a report dict: {"migrated": {...}, "errors": {...}, "skipped": [...]}
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any


def migrate_json_to_db(data_dir: str, db_manager) -> dict:
    """Migrate all JSON flat files in *data_dir* into *db_manager*.

    Args:
        data_dir: Path to the data/ directory (e.g. "data").
        db_manager: An initialised DatabaseManager instance.

    Returns:
        dict with keys "migrated" (counts per table), "errors" (per-record),
        "skipped" (list of filenames that were absent).
    """
    from ..repositories.profile_repository import ProfileRepository
    from ..repositories.relationship_repository import RelationshipRepository
    from ..repositories.profile_access_repository import ProfileAccessRepository
    from ..repositories.operation_progress_repository import OperationProgressRepository
    from ..repositories.account_cooldown_repository import AccountCooldownRepository
    from ..repositories.account_quota_repository import AccountQuotaRepository
    from ..repositories.username_repository import UsernameRepository

    report: dict[str, Any] = {
        "migrated": {},
        "errors": {},
        "skipped": [],
    }

    profile_repo = ProfileRepository(db_manager)
    rel_repo = RelationshipRepository(db_manager)
    access_repo = ProfileAccessRepository(db_manager)
    progress_repo = OperationProgressRepository(db_manager)
    cooldown_repo = AccountCooldownRepository(db_manager)
    quota_repo = AccountQuotaRepository(db_manager)
    username_repo = UsernameRepository(db_manager)

    # ── 1. user_profiles.json ─────────────────────────────────────────────
    _migrate_profiles(data_dir, profile_repo, report)

    # ── 2. relationships.json ─────────────────────────────────────────────
    _migrate_relationships(data_dir, rel_repo, report)

    # ── 3. usernames.txt ──────────────────────────────────────────────────
    _migrate_usernames_txt(data_dir, username_repo, report)

    # ── 4. username_database.json ─────────────────────────────────────────
    _migrate_username_database(data_dir, username_repo, report)

    # ── 5. profile_access.json ────────────────────────────────────────────
    _migrate_profile_access(data_dir, access_repo, report)

    # ── 6. spider_progress.json ───────────────────────────────────────────
    _migrate_progress_file(data_dir, "spider_progress.json", "spider", progress_repo, report)

    # ── 7. download_progress.json ─────────────────────────────────────────
    _migrate_progress_file(data_dir, "download_progress.json", "download", progress_repo, report)

    # ── 8. account_cooldowns.json ─────────────────────────────────────────
    _migrate_cooldowns(data_dir, cooldown_repo, report)

    # ── 9. account_quotas.json ────────────────────────────────────────────
    _migrate_quotas(data_dir, quota_repo, report)

    return report


# ── Helper: safe file rename ──────────────────────────────────────────────

def _rename_to_bak(path: str) -> None:
    """Rename *path* to *path*.bak (only called after successful commit)."""
    bak = path + ".bak"
    os.rename(path, bak)


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── Migration helpers ─────────────────────────────────────────────────────

def _migrate_profiles(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "user_profiles.json")
    if not os.path.exists(path):
        report["skipped"].append("user_profiles.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("user_profiles.json", []).append(str(e))
        return

    count = 0
    errors = []
    for username, profile in data.items():
        try:
            if not isinstance(profile, dict):
                profile = {}
            profile.setdefault("collected_by", profile.pop("collected_by_account", "migrated"))
            profile.setdefault("last_collected_ts", time.time())
            repo.upsert_profile(username, profile)
            count += 1
        except Exception as e:
            errors.append({username: str(e)})

    report["migrated"]["profiles"] = count
    if errors:
        report["errors"]["user_profiles.json"] = errors
    _rename_to_bak(path)


def _migrate_relationships(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "relationships.json")
    if not os.path.exists(path):
        report["skipped"].append("relationships.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("relationships.json", []).append(str(e))
        return

    if not isinstance(data, list):
        report["errors"].setdefault("relationships.json", []).append("Expected a list")
        return

    # Normalise field names
    normalised = []
    errors = []
    for i, r in enumerate(data):
        try:
            normalised.append({
                "source": r.get("source", ""),
                "target": r.get("target", ""),
                "type": r.get("type", "followers"),
                "collected_by": r.get("collected_by_account", r.get("collected_by", "migrated")),
                "source_is_public": r.get("source_is_public", True),
            })
        except Exception as e:
            errors.append({f"row_{i}": str(e)})

    count = repo.bulk_upsert(normalised)
    report["migrated"]["relationships"] = count
    if errors:
        report["errors"]["relationships.json"] = errors
    _rename_to_bak(path)


def _migrate_usernames_txt(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "usernames.txt")
    if not os.path.exists(path):
        report["skipped"].append("usernames.txt")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        report["errors"].setdefault("usernames.txt", []).append(str(e))
        return

    count = 0
    errors = []
    for line in lines:
        username = line.strip()
        if not username:
            continue
        try:
            repo.add_username(username, source_account="migrated", metadata={"migrated": True})
            count += 1
        except Exception as e:
            errors.append({username: str(e)})

    report["migrated"]["usernames_txt"] = count
    if errors:
        report["errors"]["usernames.txt"] = errors
    _rename_to_bak(path)


def _migrate_username_database(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "username_database.json")
    if not os.path.exists(path):
        report["skipped"].append("username_database.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("username_database.json", []).append(str(e))
        return

    usernames_data = data.get("usernames", {})
    count = 0
    errors = []
    for username, record in usernames_data.items():
        try:
            source = record.get("source_account", "migrated")
            meta = record.get("metadata", {})
            repo.add_username(username, source_account=source, metadata=meta)
            # Restore following status
            for acct, following in record.get("following_status", {}).items():
                try:
                    repo.update_following_status(username, acct, following)
                except Exception:
                    pass
            count += 1
        except Exception as e:
            errors.append({username: str(e)})

    report["migrated"]["username_database"] = count
    if errors:
        report["errors"]["username_database.json"] = errors
    _rename_to_bak(path)


def _migrate_profile_access(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "profile_access.json")
    if not os.path.exists(path):
        report["skipped"].append("profile_access.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("profile_access.json", []).append(str(e))
        return

    profiles = data.get("profiles", {})
    count = 0
    errors = []
    for username, profile_data in profiles.items():
        for attempt in profile_data.get("access_attempts", []):
            try:
                repo.record_attempt(
                    target=username,
                    account=attempt.get("account", "migrated"),
                    can_access=bool(attempt.get("can_access", False)),
                    is_public=attempt.get("is_public"),
                    is_followed=bool(attempt.get("is_followed", False)),
                    error=attempt.get("error"),
                )
                count += 1
            except Exception as e:
                errors.append({username: str(e)})

    report["migrated"]["profile_access"] = count
    if errors:
        report["errors"]["profile_access.json"] = errors
    _rename_to_bak(path)


def _migrate_progress_file(
    data_dir: str,
    filename: str,
    operation_id: str,
    repo,
    report: dict,
) -> None:
    path = os.path.join(data_dir, filename)
    if not os.path.exists(path):
        report["skipped"].append(filename)
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault(filename, []).append(str(e))
        return

    count = 0
    errors = []

    def _extract(entry):
        if isinstance(entry, dict):
            return entry.get("username", str(entry))
        return str(entry)

    for username in data.get("completed", []):
        try:
            repo.upsert_progress(operation_id, _extract(username), "completed")
            count += 1
        except Exception as e:
            errors.append({str(username): str(e)})

    for username in data.get("failed", []):
        try:
            repo.upsert_progress(operation_id, _extract(username), "failed")
            count += 1
        except Exception as e:
            errors.append({str(username): str(e)})

    for username in data.get("pending", []):
        try:
            repo.upsert_progress(operation_id, _extract(username), "pending")
            count += 1
        except Exception as e:
            errors.append({str(username): str(e)})

    key = filename.replace(".json", "")
    report["migrated"][key] = count
    if errors:
        report["errors"][filename] = errors
    _rename_to_bak(path)


def _migrate_cooldowns(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "account_cooldowns.json")
    if not os.path.exists(path):
        report["skipped"].append("account_cooldowns.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("account_cooldowns.json", []).append(str(e))
        return

    count = 0
    errors = []
    for account, entry in data.items():
        try:
            until_ts = entry.get("until", time.time())
            reason = entry.get("reason", "rate-limit")
            repo.put_on_cooldown(account, until_ts, reason)
            count += 1
        except Exception as e:
            errors.append({account: str(e)})

    report["migrated"]["account_cooldowns"] = count
    if errors:
        report["errors"]["account_cooldowns.json"] = errors
    _rename_to_bak(path)


def _migrate_quotas(data_dir: str, repo, report: dict) -> None:
    path = os.path.join(data_dir, "account_quotas.json")
    if not os.path.exists(path):
        report["skipped"].append("account_quotas.json")
        return
    try:
        data = _load_json(path)
    except Exception as e:
        report["errors"].setdefault("account_quotas.json", []).append(str(e))
        return

    count = 0
    errors = []
    for account, entry in data.items():
        try:
            quota_date = entry.get("date", datetime.now().strftime("%Y-%m-%d"))
            profile_views = int(entry.get("profile_views", 0))
            actions = int(entry.get("actions", 0))
            # Insert row with correct date and counts
            _get_db_from_repo(repo).execute(
                """
                INSERT INTO account_quotas
                    (account_name, quota_date, profile_views, actions, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(account_name) DO UPDATE SET
                    quota_date    = excluded.quota_date,
                    profile_views = excluded.profile_views,
                    actions       = excluded.actions,
                    updated_at    = excluded.updated_at
                """,
                (account, quota_date, profile_views, actions, time.time()),
            )
            count += 1
        except Exception as e:
            errors.append({account: str(e)})

    report["migrated"]["account_quotas"] = count
    if errors:
        report["errors"]["account_quotas.json"] = errors
    _rename_to_bak(path)


def _get_db_from_repo(repo) -> Any:
    """Extract the DatabaseManager from a repository instance."""
    return repo._db


__all__ = ["migrate_json_to_db"]

"""Database schema DDL statements.

All CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS statements
for the Instagram Toolkit database.
"""

SCHEMA_DDL = [
    # ── profiles ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profiles (
        username            TEXT PRIMARY KEY,
        full_name           TEXT,
        biography           TEXT,
        external_url        TEXT,
        profile_pic_url     TEXT,
        followers_count     INTEGER NOT NULL DEFAULT 0,
        following_count     INTEGER NOT NULL DEFAULT 0,
        media_count         INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER NOT NULL DEFAULT 1,
        is_verified         INTEGER NOT NULL DEFAULT 0,
        last_collected_ts   REAL NOT NULL,
        collected_by        TEXT NOT NULL,
        created_at          REAL NOT NULL DEFAULT (unixepoch()),
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_profiles_followers ON profiles(followers_count DESC)",
    "CREATE INDEX IF NOT EXISTS idx_profiles_is_public  ON profiles(is_public)",

    # ── profile_snapshots ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        username            TEXT NOT NULL REFERENCES profiles(username) ON DELETE CASCADE,
        user_id             TEXT,
        followers_count     INTEGER NOT NULL,
        following_count     INTEGER NOT NULL,
        media_count         INTEGER NOT NULL DEFAULT 0,
        collected_by        TEXT NOT NULL,
        snapshot_ts         REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_username ON profile_snapshots(username, snapshot_ts DESC)",

    # ── relationships ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS relationships (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        source                      TEXT NOT NULL,
        target                      TEXT NOT NULL,
        type                        TEXT NOT NULL CHECK(type IN ('followers','following')),
        collected_by                TEXT NOT NULL,
        source_is_public            INTEGER NOT NULL DEFAULT 1,
        source_followed_by_collector INTEGER NOT NULL DEFAULT 0,
        collected_ts                REAL NOT NULL DEFAULT (unixepoch()),
        UNIQUE(source, target, type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rel_source    ON relationships(source, type)",
    "CREATE INDEX IF NOT EXISTS idx_rel_target    ON relationships(target, type)",
    "CREATE INDEX IF NOT EXISTS idx_rel_collected ON relationships(collected_ts DESC)",

    # ── usernames ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS usernames (
        username            TEXT PRIMARY KEY,
        source_account      TEXT NOT NULL,
        added_ts            REAL NOT NULL DEFAULT (unixepoch()),
        last_accessed_ts    REAL,
        metadata_json       TEXT NOT NULL DEFAULT '{}',
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usernames_source ON usernames(source_account)",

    # ── username_following_status ─────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS username_following_status (
        username            TEXT NOT NULL REFERENCES usernames(username) ON DELETE CASCADE,
        account_name        TEXT NOT NULL,
        is_following        INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (username, account_name)
    )
    """,

    # ── profile_access_attempts ───────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_access_attempts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        target_username     TEXT NOT NULL,
        accessing_account   TEXT NOT NULL,
        can_access          INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER,
        is_followed         INTEGER NOT NULL DEFAULT 0,
        error_msg           TEXT,
        attempt_ts          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_access_target  ON profile_access_attempts(target_username, attempt_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_access_account ON profile_access_attempts(accessing_account)",

    # ── profile_access_summary ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_access_summary (
        username                TEXT PRIMARY KEY,
        is_public               INTEGER,
        last_checked_ts         REAL,
        last_successful_ts      REAL,
        total_attempts          INTEGER NOT NULL DEFAULT 0,
        known_accessible_by_json TEXT NOT NULL DEFAULT '[]'
    )
    """,

    # ── operation_progress ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS operation_progress (
        operation_id        TEXT NOT NULL,
        username            TEXT NOT NULL,
        status              TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
        details_json        TEXT NOT NULL DEFAULT '{}',
        error_msg           TEXT,
        updated_at          REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (operation_id, username)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_progress_op_status ON operation_progress(operation_id, status)",

    # ── batch_state ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS batch_state (
        operation_id        TEXT PRIMARY KEY,
        operation_type      TEXT NOT NULL,
        state_json          TEXT NOT NULL DEFAULT '{}',
        started_at          REAL NOT NULL DEFAULT (unixepoch()),
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_cooldowns ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_cooldowns (
        account_name        TEXT PRIMARY KEY,
        until_ts            REAL NOT NULL,
        reason              TEXT NOT NULL DEFAULT 'rate-limit',
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_quotas ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_quotas (
        account_name        TEXT PRIMARY KEY,
        quota_date          TEXT NOT NULL,
        profile_views       INTEGER NOT NULL DEFAULT 0,
        actions             INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
]

__all__ = ["SCHEMA_DDL"]

# Schema migration statements for adding new columns to existing tables
# These are ALTER TABLE statements that run after CREATE TABLE IF NOT EXISTS
MIGRATION_DDL = [
    # ---- profiles table migrations ----
    """
    ALTER TABLE profiles ADD COLUMN user_id TEXT
    """,
    """
    ALTER TABLE profiles ADD COLUMN user_id TEXT UNIQUE
    """,
    """
    ALTER TABLE profiles ADD COLUMN profile_pic_phash TEXT
    """,
    """
    ALTER TABLE profiles ADD COLUMN status TEXT DEFAULT 'active'
    """,

    # ---- usernames table migrations ----
    """
    ALTER TABLE usernames ADD COLUMN user_id TEXT
    """,
    """
    ALTER TABLE usernames ADD COLUMN user_id TEXT UNIQUE
    """,
    """
    ALTER TABLE usernames ADD COLUMN spider_status TEXT DEFAULT 'pending'
    """,
    """
    ALTER TABLE usernames ADD COLUMN download_status TEXT DEFAULT 'pending'
    """,
    """
    ALTER TABLE usernames ADD COLUMN filter_reason TEXT
    """,

    # ---- profile_snapshots table migrations (add user_id) ----
    """
    ALTER TABLE profile_snapshots ADD COLUMN user_id TEXT
    """,

    # ---- profile_photo_history new table ----
    """
    CREATE TABLE IF NOT EXISTS profile_photo_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        user_id TEXT,
        photo_url TEXT NOT NULL,
        photo_phash TEXT NOT NULL,
        photo_blob BLOB,
        file_path TEXT,
        detected_at REAL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_photo_history_username ON profile_photo_history(username, detected_at DESC)",

    # ---- media_items new table ----
    """
    CREATE TABLE IF NOT EXISTS media_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        user_id TEXT,
        shortcode TEXT UNIQUE,
        media_type TEXT NOT NULL,
        media_url TEXT,
        file_path TEXT,
        file_hash TEXT,
        file_size INTEGER,
        taken_at REAL,
        downloaded_at REAL,
        download_status TEXT DEFAULT 'pending'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_media_username ON media_items(username, shortcode)",
    "CREATE INDEX IF NOT EXISTS idx_media_status ON media_items(download_status, username)",
]

"""Database schema DDL statements.

All CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS statements
for the Instagram Toolkit database.
"""

SCHEMA_DDL = [
    # ── profiles ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profiles (
        username            TEXT PRIMARY KEY,
        full_name           TEXT,
        biography           TEXT,
        external_url        TEXT,
        profile_pic_url     TEXT,
        followers_count     INTEGER NOT NULL DEFAULT 0,
        following_count     INTEGER NOT NULL DEFAULT 0,
        media_count         INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER NOT NULL DEFAULT 1,
        is_verified         INTEGER NOT NULL DEFAULT 0,
        last_collected_ts   REAL NOT NULL,
        collected_by        TEXT NOT NULL,
        created_at          REAL NOT NULL DEFAULT (unixepoch()),
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_profiles_followers ON profiles(followers_count DESC)",
    "CREATE INDEX IF NOT EXISTS idx_profiles_is_public  ON profiles(is_public)",

    # ── profile_snapshots ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        username            TEXT NOT NULL REFERENCES profiles(username) ON DELETE CASCADE,
        followers_count     INTEGER NOT NULL,
        following_count     INTEGER NOT NULL,
        media_count         INTEGER NOT NULL DEFAULT 0,
        collected_by        TEXT NOT NULL,
        snapshot_ts         REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_username ON profile_snapshots(username, snapshot_ts DESC)",

    # ── relationships ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS relationships (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        source                      TEXT NOT NULL,
        target                      TEXT NOT NULL,
        type                        TEXT NOT NULL CHECK(type IN ('followers','following')),
        collected_by                TEXT NOT NULL,
        source_is_public            INTEGER NOT NULL DEFAULT 1,
        source_followed_by_collector INTEGER NOT NULL DEFAULT 0,
        collected_ts                REAL NOT NULL DEFAULT (unixepoch()),
        UNIQUE(source, target, type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rel_source    ON relationships(source, type)",
    "CREATE INDEX IF NOT EXISTS idx_rel_target    ON relationships(target, type)",
    "CREATE INDEX IF NOT EXISTS idx_rel_collected ON relationships(collected_ts DESC)",

    # ── usernames ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS usernames (
        username            TEXT PRIMARY KEY,
        source_account      TEXT NOT NULL,
        added_ts            REAL NOT NULL DEFAULT (unixepoch()),
        last_accessed_ts    REAL,
        metadata_json       TEXT NOT NULL DEFAULT '{}',
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usernames_source ON usernames(source_account)",

    # ── username_following_status ─────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS username_following_status (
        username            TEXT NOT NULL REFERENCES usernames(username) ON DELETE CASCADE,
        account_name        TEXT NOT NULL,
        is_following        INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (username, account_name)
    )
    """,

    # ── profile_access_attempts ───────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_access_attempts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        target_username     TEXT NOT NULL,
        accessing_account   TEXT NOT NULL,
        can_access          INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER,
        is_followed         INTEGER NOT NULL DEFAULT 0,
        error_msg           TEXT,
        attempt_ts          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_access_target  ON profile_access_attempts(target_username, attempt_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_access_account ON profile_access_attempts(accessing_account)",

    # ── profile_access_summary ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_access_summary (
        username                TEXT PRIMARY KEY,
        is_public               INTEGER,
        last_checked_ts         REAL,
        last_successful_ts      REAL,
        total_attempts          INTEGER NOT NULL DEFAULT 0,
        known_accessible_by_json TEXT NOT NULL DEFAULT '[]'
    )
    """,

    # ── operation_progress ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS operation_progress (
        operation_id        TEXT NOT NULL,
        username            TEXT NOT NULL,
        status              TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
        details_json        TEXT NOT NULL DEFAULT '{}',
        error_msg           TEXT,
        updated_at          REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (operation_id, username)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_progress_op_status ON operation_progress(operation_id, status)",

    # ── batch_state ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS batch_state (
        operation_id        TEXT PRIMARY KEY,
        operation_type      TEXT NOT NULL,
        state_json          TEXT NOT NULL DEFAULT '{}',
        started_at          REAL NOT NULL DEFAULT (unixepoch()),
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_cooldowns ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_cooldowns (
        account_name        TEXT PRIMARY KEY,
        until_ts            REAL NOT NULL,
        reason              TEXT NOT NULL DEFAULT 'rate-limit',
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_quotas ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_quotas (
        account_name        TEXT PRIMARY KEY,
        quota_date          TEXT NOT NULL,
        profile_views       INTEGER NOT NULL DEFAULT 0,
        actions             INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
]

__all__ = ["SCHEMA_DDL"]

"""Database package — exports DatabaseManager and all repository classes."""
from src.manager import DatabaseManager
from src.repositories import (
    ProfileRepository,
    RelationshipRepository,
    ProfileAccessRepository,
    OperationProgressRepository,
    AccountCooldownRepository,
    AccountQuotaRepository,
    UsernameRepository,
)

__all__ = [
    "DatabaseManager",
    "ProfileRepository",
    "RelationshipRepository",
    "ProfileAccessRepository",
    "OperationProgressRepository",
    "AccountCooldownRepository",
    "AccountQuotaRepository",
    "UsernameRepository",
]

"""AccountCooldownRepository — replaces AccountCooldownManager JSON I/O."""
from __future__ import annotations

import time

from ..manager import DatabaseManager


class AccountCooldownRepository:
    """Repository for per-account cooldown tracking."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def put_on_cooldown(self, account: str, until_ts: float, reason: str = "rate-limit") -> None:
        """Insert or replace the cooldown row for *account*."""
        self._db.execute(
            """
            INSERT OR REPLACE INTO account_cooldowns
                (account_name, until_ts, reason, created_at)
            VALUES (?,?,?,?)
            """,
            (account, until_ts, reason, time.time()),
        )

    def is_on_cooldown(self, account: str) -> bool:
        """Return True if *account* is still cooling down.

        Lazily deletes the row when the cooldown has expired.
        """
        row = self._db.fetchone(
            "SELECT until_ts FROM account_cooldowns WHERE account_name=?", (account,)
        )
        if row is None:
            return False
        if time.time() >= row["until_ts"]:
            # Expired — clean up
            self._db.execute(
                "DELETE FROM account_cooldowns WHERE account_name=?", (account,)
            )
            return False
        return True

    def get_remaining(self, account: str) -> float:
        """Return seconds remaining on cooldown, or 0.0."""
        row = self._db.fetchone(
            "SELECT until_ts FROM account_cooldowns WHERE account_name=?", (account,)
        )
        if row is None:
            return 0.0
        remaining = row["until_ts"] - time.time()
        return max(remaining, 0.0)

    def clear_cooldown(self, account: str) -> None:
        """Delete the cooldown row for *account* if it exists."""
        self._db.execute(
            "DELETE FROM account_cooldowns WHERE account_name=?", (account,)
        )

    def get_available(self, accounts: list[str]) -> list[str]:
        """Return accounts from *accounts* that are NOT on cooldown."""
        return [a for a in accounts if not self.is_on_cooldown(a)]


__all__ = ["AccountCooldownRepository"]

"""AccountQuotaRepository — replaces AccountQuotaManager JSON I/O."""
from __future__ import annotations

import time
from datetime import date

from ..manager import DatabaseManager


def _today() -> str:
    return date.today().isoformat()


class AccountQuotaRepository:
    """Repository for per-account daily quota tracking."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def _ensure_row(self, account: str) -> None:
        """Insert a quota row for *account* if one doesn't exist."""
        self._db.execute(
            """
            INSERT OR IGNORE INTO account_quotas
                (account_name, quota_date, profile_views, actions, updated_at)
            VALUES (?,?,0,0,?)
            """,
            (account, _today(), time.time()),
        )

    def record_profile_view(self, account: str, count: int = 1) -> None:
        """Increment profile_views for *account* by *count*."""
        self._ensure_row(account)
        self._db.execute(
            """
            UPDATE account_quotas
            SET profile_views = profile_views + ?,
                updated_at    = ?
            WHERE account_name = ?
            """,
            (count, time.time(), account),
        )

    def record_action(self, account: str, count: int = 1) -> None:
        """Increment actions for *account* by *count*."""
        self._ensure_row(account)
        self._db.execute(
            """
            UPDATE account_quotas
            SET actions    = actions + ?,
                updated_at = ?
            WHERE account_name = ?
            """,
            (count, time.time(), account),
        )

    def get_usage(self, account: str) -> dict:
        """Return {profile_views, actions, quota_date} for *account*."""
        self._ensure_row(account)
        row = self._db.fetchone(
            "SELECT profile_views, actions, quota_date FROM account_quotas WHERE account_name=?",
            (account,),
        )
        if row is None:
            return {"profile_views": 0, "actions": 0, "quota_date": _today()}
        return dict(row)

    def reset_if_new_day(self, account: str) -> None:
        """Reset counters to 0 if the stored quota_date differs from today."""
        self._ensure_row(account)
        today = _today()
        self._db.execute(
            """
            UPDATE account_quotas
            SET profile_views = 0,
                actions       = 0,
                quota_date    = ?,
                updated_at    = ?
            WHERE account_name = ? AND quota_date != ?
            """,
            (today, time.time(), account, today),
        )


__all__ = ["AccountQuotaRepository"]

"""Media items repository for tracking downloaded media."""
import hashlib
from typing import Dict, List, Optional


class MediaItemRepository:
    """Repository for media_items table operations."""

    def __init__(self, db_manager):
        """
        Args:
            db_manager: DatabaseManager instance
        """
        self.db = db_manager

    def add_media_item(
        self,
        username: str,
        user_id: str,
        shortcode: str,
        media_type: str,
        media_url: str,
        file_path: str,
        file_hash: str,
        file_size: int,
        taken_at: Optional[int],
        downloaded_at: int,
        download_status: str = 'downloaded'
    ) -> int:
        """
        Insert or ignore a media item.

        Args:
            username: Instagram username
            user_id: Instagram user ID (profile.userid)
            shortcode: Media shortcode/post ID
            media_type: 'post', 'story', 'highlight', 'profile_photo'
            media_url: Source URL
            file_path: Local file path
            file_hash: SHA-256 hash
            file_size: File size in bytes
            taken_at: Unix timestamp when media was taken
            downloaded_at: Unix timestamp when downloaded
            download_status: 'downloaded', 'missing', 'corrupted'

        Returns:
            Row ID of inserted or existing item
        """
        try:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO media_items
                (username, user_id, shortcode, media_type, media_url, file_path,
                 file_hash, file_size, taken_at, downloaded_at, download_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username,
                    str(user_id),  # Store as TEXT
                    shortcode,
                    media_type,
                    media_url,
                    file_path,
                    file_hash,
                    file_size,
                    taken_at,
                    downloaded_at,
                    download_status,
                )
            )
            return cursor.lastrowid
        except Exception as e:
            print(f"[ERROR] Failed to insert media item for {username}: {e}")
            return -1

    def get_media_by_shortcode(self, shortcode: str) -> Optional[Dict]:
        """Get media item by shortcode."""
        rows = self.db.fetchall(
            "SELECT * FROM media_items WHERE shortcode=?",
            (shortcode,)
        )
        return rows[0] if rows else None

    def get_media_by_user(self, username: str, media_type: Optional[str] = None) -> List[Dict]:
        """
        Get all media items for a user.

        Args:
            username: Instagram username
            media_type: Optional filter by type
        """
        if media_type:
            rows = self.db.fetchall(
                "SELECT * FROM media_items WHERE username=? AND media_type=? ORDER BY downloaded_at DESC",
                (username, media_type)
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM media_items WHERE username=? ORDER BY downloaded_at DESC",
                (username,)
            )
        return rows

    def mark_missing(self, media_id: int):
        """Mark a media item as missing."""
        self.db.execute(
            "UPDATE media_items SET download_status='missing' WHERE id=?",
            (media_id,)
        )

    def mark_corrupted(self, media_id: int):
        """Mark a media item as corrupted (hash mismatch)."""
        self.db.execute(
            "UPDATE media_items SET download_status='corrupted' WHERE id=?",
            (media_id,)
        )

    def update_file_hash(self, media_id: int, new_hash: str):
        """Update file hash for a media item."""
        self.db.execute(
            "UPDATE media_items SET file_hash=? WHERE id=?",
            (new_hash, media_id)
        )

    def get_stats(self, username: Optional[str] = None) -> Dict[str, int]:
        """
        Get media item statistics.

        Args:
            username: Optional filter by username

        Returns:
            Dict with counts by status
        """
        if username:
            rows = self.db.fetchall(
                """SELECT download_status, COUNT(*) as count
                FROM media_items
                WHERE username=?
                GROUP BY download_status""",
                (username,)
            )
        else:
            rows = self.db.fetchall(
                """SELECT download_status, COUNT(*) as count
                FROM media_items
                GROUP BY download_status"""
            )

        stats = {
            'downloaded': 0,
            'missing': 0,
            'corrupted': 0
        }
        for row in rows:
            status = row['download_status']
            count = row['count']
            if status in stats:
                stats[status] = count

        return stats

    @staticmethod
    def compute_sha256_hash(file_path: str, chunk_size: int = 8192) -> str:
        """
        Compute SHA-256 hash of file using chunked reads.

        Args:
            file_path: Path to file
            chunk_size: Read chunk size in bytes

        Returns:
            Hexadecimal SHA-256 hash
        """
        sha256 = hashlib.sha256()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(chunk_size), b''):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception as e:
            print(f"[ERROR] Failed to compute hash for {file_path}: {e}")
            return ""

    @staticmethod
    def get_file_size(file_path: str) -> int:
        """Get file size in bytes."""
        try:
            return os.path.getsize(file_path)
        except Exception as e:
            print(f"[ERROR] Failed to get file size for {file_path}: {e}")
            return 0


__all__ = ["MediaItemRepository"]

"""OperationProgressRepository — replaces ProgressManager JSON I/O."""
from __future__ import annotations

import json
import time
from typing import Any

from ..manager import DatabaseManager


class OperationProgressRepository:
    """Repository for operation progress and batch state."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_progress(
        self,
        operation_id: str,
        username: str,
        status: str,
        details: dict | None = None,
        error: str | None = None,
    ) -> None:
        """Insert or update a progress row for (operation_id, username)."""
        self._db.execute(
            """
            INSERT INTO operation_progress
                (operation_id, username, status, details_json, error_msg, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(operation_id, username) DO UPDATE SET
                status       = excluded.status,
                details_json = excluded.details_json,
                error_msg    = excluded.error_msg,
                updated_at   = excluded.updated_at
            """,
            (
                operation_id,
                username,
                status,
                json.dumps(details or {}),
                error,
                time.time(),
            ),
        )

    def get_status(self, operation_id: str, username: str) -> str | None:
        """Return the current status string, or None if no row exists."""
        row = self._db.fetchone(
            "SELECT status FROM operation_progress WHERE operation_id=? AND username=?",
            (operation_id, username),
        )
        return row["status"] if row else None

    def get_completed(self, operation_id: str) -> list[str]:
        """Return all usernames with status='completed' for this operation."""
        rows = self._db.fetchall(
            "SELECT username FROM operation_progress WHERE operation_id=? AND status='completed'",
            (operation_id,),
        )
        return [r["username"] for r in rows]

    def get_failed(self, operation_id: str) -> list[str]:
        """Return all usernames with status='failed' for this operation."""
        rows = self._db.fetchall(
            "SELECT username FROM operation_progress WHERE operation_id=? AND status='failed'",
            (operation_id,),
        )
        return [r["username"] for r in rows]

    def get_pending(self, operation_id: str) -> list[str]:
        """Return all usernames with status='pending' for this operation."""
        rows = self._db.fetchall(
            "SELECT username FROM operation_progress WHERE operation_id=? AND status='pending'",
            (operation_id,),
        )
        return [r["username"] for r in rows]

    def get_remaining(self, operation_id: str, all_usernames: list[str]) -> list[str]:
        """Return usernames from all_usernames not yet completed or failed."""
        rows = self._db.fetchall(
            """
            SELECT username FROM operation_progress
            WHERE operation_id=? AND status IN ('completed','failed')
            """,
            (operation_id,),
        )
        done = {r["username"] for r in rows}
        return [u for u in all_usernames if u not in done]

    def get_statistics(self, operation_id: str) -> dict:
        """Return counts of pending/completed/failed for this operation."""
        rows = self._db.fetchall(
            """
            SELECT status, COUNT(*) AS cnt
            FROM operation_progress
            WHERE operation_id=?
            GROUP BY status
            """,
            (operation_id,),
        )
        stats = {"pending": 0, "completed": 0, "failed": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        return stats

    def upsert_batch_state(self, operation_id: str, state: dict) -> None:
        """Insert or update the batch state for an operation."""
        op_type = state.get("operation_type", state.get("current_operation", "general"))
        self._db.execute(
            """
            INSERT INTO batch_state
                (operation_id, operation_type, state_json, started_at, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(operation_id) DO UPDATE SET
                operation_type = excluded.operation_type,
                state_json     = excluded.state_json,
                updated_at     = excluded.updated_at
            """,
            (operation_id, op_type, json.dumps(state), time.time(), time.time()),
        )

    def get_batch_state(self, operation_id: str) -> dict:
        """Return the batch state dict for an operation, or {} if not found."""
        row = self._db.fetchone(
            "SELECT state_json FROM batch_state WHERE operation_id=?",
            (operation_id,),
        )
        if row is None:
            return {}
        return json.loads(row["state_json"] or "{}")

    def archive_operation(self, operation_id: str) -> None:
        """Delete all progress and batch_state rows for this operation only."""
        with self._db.get_connection() as conn:
            conn.execute(
                "DELETE FROM operation_progress WHERE operation_id=?", (operation_id,)
            )
            conn.execute(
                "DELETE FROM batch_state WHERE operation_id=?", (operation_id,)
            )


__all__ = ["OperationProgressRepository"]

"""ProfileAccessRepository — replaces ProfileAccessTracker JSON I/O."""
from __future__ import annotations

import json
import time
from typing import Any

from ..manager import DatabaseManager


class ProfileAccessRepository:
    """Repository for profile access attempt tracking."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def record_attempt(
        self,
        target: str,
        account: str,
        can_access: bool,
        is_public: bool | None,
        is_followed: bool,
        error: str | None = None,
    ) -> None:
        """Insert an access attempt row and upsert the summary in one transaction."""
        now = time.time()
        with self._db.get_connection() as conn:
            # Insert attempt row
            conn.execute(
                """
                INSERT INTO profile_access_attempts
                    (target_username, accessing_account, can_access, is_public,
                     is_followed, error_msg, attempt_ts)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    target,
                    account,
                    1 if can_access else 0,
                    None if is_public is None else (1 if is_public else 0),
                    1 if is_followed else 0,
                    error,
                    now,
                ),
            )

            # Fetch existing summary
            cursor = conn.execute(
                "SELECT * FROM profile_access_summary WHERE username=?", (target,)
            )
            row = cursor.fetchone()

            if row is None:
                # New summary row
                accessible_by = json.dumps([account] if can_access else [])
                conn.execute(
                    """
                    INSERT INTO profile_access_summary
                        (username, is_public, last_checked_ts, last_successful_ts,
                         total_attempts, known_accessible_by_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        target,
                        None if is_public is None else (1 if is_public else 0),
                        now,
                        now if can_access else None,
                        1,
                        accessible_by,
                    ),
                )
            else:
                # Update existing summary
                existing = dict(row)
                known = json.loads(existing.get("known_accessible_by_json") or "[]")
                if can_access and account not in known:
                    known.append(account)
                new_is_public = existing.get("is_public")
                if is_public is not None:
                    new_is_public = 1 if is_public else 0
                conn.execute(
                    """
                    UPDATE profile_access_summary SET
                        is_public               = ?,
                        last_checked_ts         = ?,
                        last_successful_ts      = CASE WHEN ? THEN ? ELSE last_successful_ts END,
                        total_attempts          = total_attempts + 1,
                        known_accessible_by_json = ?
                    WHERE username = ?
                    """,
                    (
                        new_is_public,
                        now,
                        1 if can_access else 0,
                        now,
                        json.dumps(known),
                        target,
                    ),
                )

    def get_profile_summary(self, username: str) -> dict:
        """Return the summary dict for *username*, or an empty default."""
        row = self._db.fetchone(
            "SELECT * FROM profile_access_summary WHERE username=?", (username,)
        )
        if row is None:
            return {
                "status": "unknown",
                "accessible_by": [],
                "is_public": None,
                "last_checked": None,
                "total_attempts": 0,
            }
        known = json.loads(row.get("known_accessible_by_json") or "[]")
        return {
            "status": "tracked",
            "is_public": row.get("is_public"),
            "accessible_by": known,
            "last_checked": row.get("last_checked_ts"),
            "last_successful_ts": row.get("last_successful_ts"),
            "total_attempts": row.get("total_attempts", 0),
        }

    def get_accessible_accounts(self, username: str) -> list[str]:
        """Return accounts known to be able to access *username*."""
        row = self._db.fetchone(
            "SELECT known_accessible_by_json FROM profile_access_summary WHERE username=?",
            (username,),
        )
        if row is None:
            return []
        return json.loads(row.get("known_accessible_by_json") or "[]")

    def get_best_account(self, username: str, available: list[str]) -> str | None:
        """Return the available account that most recently successfully accessed *username*."""
        if not available:
            return None
        # Find the most recent successful attempt for this username from an available account
        placeholders = ",".join("?" * len(available))
        row = self._db.fetchone(
            f"""
            SELECT accessing_account FROM profile_access_attempts
            WHERE target_username=? AND can_access=1
              AND accessing_account IN ({placeholders})
            ORDER BY attempt_ts DESC
            LIMIT 1
            """,
            (username, *available),
        )
        return row["accessing_account"] if row else None

    def cleanup_old_attempts(self, days: int = 30) -> int:
        """Delete attempt rows older than *days* days. Returns deleted count."""
        cutoff = time.time() - days * 86400
        cursor = self._db.execute(
            "DELETE FROM profile_access_attempts WHERE attempt_ts < ?", (cutoff,)
        )
        return cursor.rowcount

    def cleanup_inactive_profiles(self, days: int = 30) -> int:
        """Remove private profiles not checked in *days* days from summary."""
        cutoff = time.time() - days * 86400
        cursor = self._db.execute(
            """
            DELETE FROM profile_access_summary
            WHERE (is_public = 0 OR is_public IS NULL)
              AND (last_checked_ts IS NULL OR last_checked_ts < ?)
            """,
            (cutoff,),
        )
        return cursor.rowcount

    def get_statistics(self) -> dict:
        """Return aggregate statistics across all tracked profiles."""
        row = self._db.fetchone(
            """
            SELECT
                COUNT(*) AS total_attempts,
                SUM(can_access) AS successful_attempts
            FROM profile_access_attempts
            """
        )
        profiles_row = self._db.fetchone(
            "SELECT COUNT(*) AS unique_profiles FROM profile_access_summary"
        )
        return {
            "total_attempts": row["total_attempts"] if row else 0,
            "successful_attempts": row["successful_attempts"] if row else 0,
            "unique_profiles": profiles_row["unique_profiles"] if profiles_row else 0,
        }


__all__ = ["ProfileAccessRepository"]

"""ProfileRepository — replaces UserMetadataManager JSON I/O."""
from __future__ import annotations

import time
from typing import Any

from ..manager import DatabaseManager


class ProfileRepository:
    """Repository for profile data (profiles + profile_snapshots tables)."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_profile(self, username: str, data: dict) -> None:
        """Insert or update a profile row and always insert a snapshot."""
        now = time.time()
        with self._db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO profiles
                    (username, full_name, biography, external_url, profile_pic_url,
                     followers_count, following_count, media_count,
                     is_public, is_verified, last_collected_ts, collected_by,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET
                    full_name           = excluded.full_name,
                    biography           = excluded.biography,
                    external_url        = excluded.external_url,
                    profile_pic_url     = excluded.profile_pic_url,
                    followers_count     = excluded.followers_count,
                    following_count     = excluded.following_count,
                    media_count         = excluded.media_count,
                    is_public           = excluded.is_public,
                    is_verified         = excluded.is_verified,
                    last_collected_ts   = excluded.last_collected_ts,
                    collected_by        = excluded.collected_by,
                    updated_at          = excluded.updated_at
                """,
                (
                    username,
                    data.get("full_name") or data.get("username", username),
                    data.get("biography", ""),
                    data.get("external_url"),
                    data.get("profile_pic_url"),
                    int(data.get("followers_count", 0)),
                    int(data.get("following_count", 0)),
                    int(data.get("media_count", 0)),
                    1 if data.get("is_public", True) else 0,
                    1 if data.get("is_verified", False) else 0,
                    float(data.get("last_collected_ts", now)),
                    data.get("collected_by", data.get("collected_by_account", "")),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO profile_snapshots
                    (username, followers_count, following_count, media_count,
                     collected_by, snapshot_ts)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    username,
                    int(data.get("followers_count", 0)),
                    int(data.get("following_count", 0)),
                    int(data.get("media_count", 0)),
                    data.get("collected_by", data.get("collected_by_account", "")),
                    now,
                ),
            )

    def get_profile(self, username: str) -> dict | None:
        """Return the profile dict for *username*, or None if not found."""
        return self._db.fetchone(
            "SELECT * FROM profiles WHERE username = ?", (username,)
        )

    def get_all_profiles(self) -> dict[str, dict]:
        """Return all profiles as a {username: dict} mapping."""
        rows = self._db.fetchall("SELECT * FROM profiles")
        return {r["username"]: r for r in rows}

    def get_top_by_followers(self, n: int) -> list[dict]:
        """Return the top *n* profiles by follower count (descending)."""
        return self._db.fetchall(
            "SELECT * FROM profiles ORDER BY followers_count DESC LIMIT ?", (n,)
        )

    def get_top_by_following(self, n: int) -> list[dict]:
        """Return the top *n* profiles by following count (descending)."""
        return self._db.fetchall(
            "SELECT * FROM profiles ORDER BY following_count DESC LIMIT ?", (n,)
        )

    def filter_by_follower_range(
        self, min_f: int, max_f: int | None = None
    ) -> list[str]:
        """Return usernames whose follower count is in [min_f, max_f]."""
        if max_f is None:
            rows = self._db.fetchall(
                "SELECT username FROM profiles WHERE followers_count >= ?", (min_f,)
            )
        else:
            rows = self._db.fetchall(
                "SELECT username FROM profiles WHERE followers_count >= ? AND followers_count <= ?",
                (min_f, max_f),
            )
        return [r["username"] for r in rows]

    def get_snapshots(self, username: str, limit: int = 90) -> list[dict]:
        """Return up to *limit* snapshots for *username*, newest first."""
        return self._db.fetchall(
            """
            SELECT * FROM profile_snapshots
            WHERE username = ?
            ORDER BY snapshot_ts DESC
            LIMIT ?
            """,
            (username, limit),
        )


__all__ = ["ProfileRepository"]

"""RelationshipRepository — replaces RelationshipCollector JSON I/O."""
from __future__ import annotations

import time
from typing import Any

from ..manager import DatabaseManager


class RelationshipRepository:
    """Repository for follower/following relationship data."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def upsert_relationship(
        self,
        source: str,
        target: str,
        rel_type: str,
        collected_by: str,
        source_is_public: bool,
    ) -> None:
        """Insert or replace a single relationship row."""
        self._db.execute(
            """
            INSERT OR REPLACE INTO relationships
                (source, target, type, collected_by, source_is_public, collected_ts)
            VALUES (?,?,?,?,?,?)
            """,
            (source, target, rel_type, collected_by, 1 if source_is_public else 0, time.time()),
        )

    def bulk_upsert(self, relationships: list[dict]) -> int:
        """Insert or replace a batch of relationships, deduplicating within the batch.

        Returns the count of unique rows inserted/updated.
        """
        if not relationships:
            return 0

        seen: set[tuple] = set()
        deduped: list[dict] = []
        for r in relationships:
            key = (r["source"], r["target"], r["type"])
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        now = time.time()
        params_seq = [
            (
                r["source"],
                r["target"],
                r["type"],
                r.get("collected_by", r.get("collected_by_account", "")),
                1 if r.get("source_is_public", True) else 0,
                r.get("source_followed_by_collector", 0),
                now,
            )
            for r in deduped
        ]
        with self._db.get_connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO relationships
                    (source, target, type, collected_by, source_is_public,
                     source_followed_by_collector, collected_ts)
                VALUES (?,?,?,?,?,?,?)
                """,
                params_seq,
            )
        return len(deduped)

    def get_relationships(
        self,
        source: str | None = None,
        rel_type: str | None = None,
    ) -> list[dict]:
        """Return relationships filtered by optional source and/or type."""
        if source and rel_type:
            return self._db.fetchall(
                "SELECT * FROM relationships WHERE source=? AND type=?",
                (source, rel_type),
            )
        elif source:
            return self._db.fetchall(
                "SELECT * FROM relationships WHERE source=?", (source,)
            )
        elif rel_type:
            return self._db.fetchall(
                "SELECT * FROM relationships WHERE type=?", (rel_type,)
            )
        return self._db.fetchall("SELECT * FROM relationships")

    def get_followers(self, username: str) -> list[str]:
        """Return all sources that have a 'followers' row with target=username."""
        rows = self._db.fetchall(
            "SELECT source FROM relationships WHERE target=? AND type='followers'",
            (username,),
        )
        return [r["source"] for r in rows]

    def get_following(self, username: str) -> list[str]:
        """Return all targets that username follows (type='following')."""
        rows = self._db.fetchall(
            "SELECT target FROM relationships WHERE source=? AND type='following'",
            (username,),
        )
        return [r["target"] for r in rows]

    def get_mutual(self, username: str) -> list[str]:
        """Return usernames that both follow username and are followed by username."""
        rows = self._db.fetchall(
            """
            SELECT DISTINCT r1.source AS mutual
            FROM relationships r1
            JOIN relationships r2
              ON r1.source = r2.target
             AND r2.source = ?
             AND r2.type   = 'following'
            WHERE r1.target = ?
              AND r1.type   = 'followers'
            """,
            (username, username),
        )
        return [r["mutual"] for r in rows]

    def relationship_exists(self, source: str, target: str, rel_type: str) -> bool:
        """Return True if the given relationship row exists."""
        row = self._db.fetchone(
            "SELECT 1 FROM relationships WHERE source=? AND target=? AND type=?",
            (source, target, rel_type),
        )
        return row is not None

    def get_all_usernames(self) -> list[str]:
        """Return all distinct usernames appearing as source or target."""
        rows = self._db.fetchall(
            """
            SELECT DISTINCT username FROM (
                SELECT source AS username FROM relationships
                UNION
                SELECT target AS username FROM relationships
            )
            """
        )
        return [r["username"] for r in rows]


__all__ = ["RelationshipRepository"]

"""UsernameRepository — replaces UsernameDatabase JSON persistence."""
from __future__ import annotations

import json
import time

from ..manager import DatabaseManager


class UsernameRepository:
    """Repository for username records and per-account following status."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def add_username(
        self,
        username: str,
        source_account: str,
        metadata: dict | None = None,
    ) -> bool:
        """Insert *username* if it doesn't exist. Returns True on insert, False if duplicate."""
        existing = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if existing is not None:
            return False
        now = time.time()
        self._db.execute(
            """
            INSERT INTO usernames
                (username, source_account, added_ts, metadata_json, created_at)
            VALUES (?,?,?,?,?)
            """,
            (username, source_account, now, json.dumps(metadata or {}), now),
        )
        return True

    def get_by_source(self, source_account: str) -> list[dict]:
        """Return all username records for *source_account*."""
        return self._db.fetchall(
            "SELECT * FROM usernames WHERE source_account=? ORDER BY added_ts",
            (source_account,),
        )

    def get_all(self) -> list[dict]:
        """Return all username records ordered by added_ts."""
        return self._db.fetchall("SELECT * FROM usernames ORDER BY added_ts")

    def update_metadata(self, username: str, metadata: dict) -> bool:
        """Merge *metadata* into the existing metadata_json. Returns False if not found."""
        row = self._db.fetchone(
            "SELECT metadata_json FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        existing = json.loads(row["metadata_json"] or "{}")
        existing.update(metadata)
        self._db.execute(
            "UPDATE usernames SET metadata_json=? WHERE username=?",
            (json.dumps(existing), username),
        )
        return True

    def update_last_accessed(self, username: str) -> bool:
        """Update last_accessed_ts to now. Returns False if not found."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        self._db.execute(
            "UPDATE usernames SET last_accessed_ts=? WHERE username=?",
            (time.time(), username),
        )
        return True

    def update_following_status(
        self, username: str, account: str, following: bool
    ) -> bool:
        """Upsert the following status for (username, account). Returns False if username not found."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        self._db.execute(
            """
            INSERT INTO username_following_status
                (username, account_name, is_following, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(username, account_name) DO UPDATE SET
                is_following = excluded.is_following,
                updated_at   = excluded.updated_at
            """,
            (username, account, 1 if following else 0, time.time()),
        )
        return True

    def remove(self, username: str) -> bool:
        """Delete *username* and cascade-delete following_status rows. Returns False if not found."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        if row is None:
            return False
        # Cascade is handled by FK ON DELETE CASCADE (foreign_keys=ON)
        self._db.execute("DELETE FROM usernames WHERE username=?", (username,))
        return True

    def exists(self, username: str) -> bool:
        """Return True if *username* is in the database."""
        row = self._db.fetchone(
            "SELECT 1 FROM usernames WHERE username=?", (username,)
        )
        return row is not None


__all__ = ["UsernameRepository"]

"""Repository package — exports all repository classes."""
from src.profile_repository import ProfileRepository
from src.relationship_repository import RelationshipRepository
from src.profile_access_repository import ProfileAccessRepository
from src.operation_progress_repository import OperationProgressRepository
from src.account_cooldown_repository import AccountCooldownRepository
from src.account_quota_repository import AccountQuotaRepository
from src.username_repository import UsernameRepository
from src.media_item_repository import MediaItemRepository

__all__ = [
    "ProfileRepository",
    "RelationshipRepository",
    "ProfileAccessRepository",
    "OperationProgressRepository",
    "AccountCooldownRepository",
    "AccountQuotaRepository",
    "UsernameRepository",
    "MediaItemRepository",
]



