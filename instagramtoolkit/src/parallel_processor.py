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


def _enqueue_retry(username: str, account_name: str, reason: str) -> None:
    """Add a failed download to the retry queue with cooldown delay."""
    import time, os
    from src.db.manager import DatabaseManager
    from src.config import ACCOUNT_COOLDOWN_MINUTES
    try:
        db = DatabaseManager(os.environ.get("DATABASE_URL", ""))
        retry_after = time.time() + ACCOUNT_COOLDOWN_MINUTES * 60
        db.execute(
            """INSERT INTO download_retry_queue (username, account_name, fail_ts, retry_after_ts, reason, status)
               VALUES (?, ?, ?, ?, ?, 'pending')
               ON CONFLICT DO NOTHING""",
            (username, account_name, time.time(), retry_after, reason[:200]),
        )
    except Exception as e:
        print(f"[WARNING] Could not enqueue retry for {username}: {e}")


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

        # Graceful shutdown flag — unified: checks both resilience._SHUTDOWN and rate_limiter._SHUTDOWN_EVENT
        self._shutdown_requested = _SHUTDOWN_EVENT
        # Note: _SHUTDOWN (resilience) is the primary flag set by signal handler;
        # _SHUTDOWN_EVENT (rate_limiter) is legacy. Both are checked in loops via _is_shutdown().
        
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
        
        # PHASE 2: Perform warm-up after account switch
        if self.manager and self.manager.loader:
            from src.warmup import warmup_session
            warmup_session(self.manager.loader, current_account['username'])
        
        return True
    
    def get_best_account_for_public(self):
        """Return the name of the first available (non-cooldown) account, or None.

        Used for public profiles where any account will work — prefers accounts
        not currently on cooldown.
        """
        available_names = [acc['name'] for acc in self.available_accounts]
        available = self.cooldown_manager.get_available_accounts(available_names)
        if available:
            return available[0]
        return None  # all accounts on cooldown

    def _get_best_account_for_user(self, username):
        """Pick the best account for a target: prefers accounts that follow them (T9 affinity),
        falls back to profile-access tracker, then current account."""
        available_account_names = [acc['name'] for acc in self.available_accounts]

        # T9: check account_access map — prefer an account that follows this user
        try:
            from src.db.manager import DatabaseManager
            import os
            db = DatabaseManager(os.environ.get("DATABASE_URL", ""))
            rows = db.fetchall(
                """SELECT account_name FROM account_access
                   WHERE username=? AND follows=1 AND account_name IN ({})
                   ORDER BY last_checked_ts DESC""".format(
                    ','.join('?' for _ in available_account_names)
                ),
                (username, *available_account_names),
            )
            candidates = [r['account_name'] for r in rows]
            if candidates:
                # Pick the candidate with the most remaining quota
                best = min(
                    candidates,
                    key=lambda n: self.quota_manager.get_daily_usage(n).get('profile_views', 0),
                )
                for i, acc in enumerate(self.available_accounts):
                    if acc['name'] == best:
                        return i
        except Exception:
            pass

        # Fallback: profile access tracker (historical success/failure data)
        best_account = self.access_tracker.get_best_account_for_profile(username, available_account_names)
        if best_account:
            for i, acc in enumerate(self.available_accounts):
                if acc['name'] == best_account:
                    return i

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

        # T10: skip usernames confirmed inaccessible by all accounts
        try:
            import os as _os
            from src.db.manager import DatabaseManager as _DBM
            _db = _DBM(_os.environ.get("DATABASE_URL", ""))
            _row = _db.fetchone(
                "SELECT spider_status FROM usernames WHERE username=?", (username,)
            )
            if _row and _row.get('spider_status') == 'inaccessible-all':
                print(f"[SKIP] {username} — confirmed inaccessible by all accounts")
                return True
        except Exception:
            pass

        # Apply follower count filter if enabled
        if FILTER_MAX_FOLLOWERS > 0:
            if not self.metadata_manager.is_within_follower_limit(username, FILTER_MAX_FOLLOWERS):
                followers = self.metadata_manager.get_profile(username).get('followers_count', '?')
                print(f"[FILTER] Skipping {username} - {followers} followers exceeds limit of {FILTER_MAX_FOLLOWERS}")
                return True  # treat as skipped-success so it doesn't show as failed
        
        # Mark as pending
        self.progress_manager.mark_pending(username)

        # T9 affinity: switch to the account that follows this user before starting
        best_idx = self._get_best_account_for_user(username)
        if best_idx != self.current_account_index:
            best_name = self.available_accounts[best_idx]['name']
            print(f"[AFFINITY] Switching to {best_name} — follows {username}")
            self.current_account_index = best_idx

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
                
                # T11: rate-limit / challenge — enqueue for later retry, don't lose slot
                if any(phrase in error_msg for phrase in RATE_LIMIT_PHRASES + ACCOUNT_SWITCH_PHRASES + CHALLENGE_PHRASES):
                    print(f"[RESUME] Instagram API issue — queuing {username} for retry after cooldown")
                    _enqueue_retry(username, self.available_accounts[self.current_account_index]['name'], str(e))
                    return False
                else:
                    print(f"[ERROR] Non-API error for {username}: {e}")
                    return False
        
        success = self._execute_with_retry(_download_operation)

        # B7: auto-fallback to browser when Instaloader fails all retries
        if not success and getattr(self, '_browser_fallback_enabled', False):
            print(f"[FALLBACK] Instaloader exhausted — trying browser downloader for {username}")
            try:
                from src.browser_downloader import BrowserDownloader
                current_acct = self.available_accounts[self.current_account_index]['name']
                bd = BrowserDownloader(account_name=current_acct)
                result = bd.download(username, post_limit=post_limit or 0)
                bd.close()
                if result.get('downloaded', 0) > 0:
                    success = True
                    print(f"[FALLBACK] Browser download succeeded: {result['downloaded']} files")
            except Exception as e:
                print(f"[FALLBACK] Browser download failed: {e}")

        if success:
            self.progress_manager.mark_completed(username, {
                'post_limit': post_limit,
                'downloads_directory': self.downloads_dir,
                'account_used': self.available_accounts[self.current_account_index]['name']
            })
            self._handle_rate_limiting()
        else:
            # T10: if every account tried and none could access, mark inaccessible-all
            try:
                import os as _os
                from src.db.manager import DatabaseManager as _DBM
                _db = _DBM(_os.environ.get("DATABASE_URL", ""))
                _row = _db.fetchone(
                    "SELECT spider_status FROM usernames WHERE username=?", (username,)
                )
                # Only mark inaccessible-all if no account_access row has follows=1
                _access = _db.fetchone(
                    "SELECT 1 FROM account_access WHERE username=? AND follows=1", (username,)
                )
                if not _access:
                    _db.execute(
                        "UPDATE usernames SET spider_status='inaccessible-all' WHERE username=?",
                        (username,),
                    )
                    print(f"[ACCESS] {username} — no following account found; marked inaccessible-all")
            except Exception:
                pass
            self.progress_manager.mark_failed(username, "All retry attempts failed")

        return success

    @handle_graceful_exit()
    def process_batch_relationships(self, usernames, max_followers=1000, max_following=1000):
        """Process multiple users for relationship collection with improved rate limiting and progress tracking"""
        if not usernames:
            print("[ERROR] No usernames provided")
            return
        
        # Session tracking banner
        from src.session_tracker import SessionTracker
        current_account = self.available_accounts[self.current_account_index]
        session = SessionTracker("Spider (Relationship Collection)", current_account['name'])
        
        # PHASE 2: Perform warm-up before heavy batch operation
        if self.manager and self.manager.loader:
            from src.warmup import should_warmup, warmup_session
            if should_warmup('batch_spider'):
                current_account = self.available_accounts[self.current_account_index]
                warmup_session(self.manager.loader, current_account['username'])
        
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
        
        session.print_start_banner(total_items=len(remaining_usernames))
        
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
            # Check for graceful shutdown (resilience._SHUTDOWN or legacy _SHUTDOWN_EVENT)
            if _SHUTDOWN.is_set() or self._shutdown_requested.is_set():
                print("\n[STOP] Shutdown requested — stopping batch spider gracefully.")
                session.print_end_banner(success=False, items_processed=successful + failed)
                break

            print(f"\n[{i}/{len(remaining_usernames)}] Processing {username}...")
            
            # Update current position
            self.progress_manager.update_batch_state(current_user_index=i)
            
            try:
                success = self.collect_relationships(username, max_followers, max_following)
                session.record_operation()
                if success:
                    successful += 1
                    session.record_save()
                    print(f"[OK] [{i}/{len(remaining_usernames)}] Successfully processed {username}")
                else:
                    failed += 1
                    print(f"[ERROR] [{i}/{len(remaining_usernames)}] Failed to process {username}")
                
                # Progress indicator every 5 users
                if i % 5 == 0:
                    session.print_progress(i, len(remaining_usernames), f"| ✓{successful} ✗{failed}")
                
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
        
        session.print_end_banner(success=True, items_processed=successful + failed)
        
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
        
        # Session tracking banner
        from src.session_tracker import SessionTracker
        current_account = self.available_accounts[self.current_account_index]
        session = SessionTracker("Batch Download", current_account['name'])
        
        # PHASE 2: Perform warm-up before heavy batch operation
        if self.manager and self.manager.loader:
            from src.warmup import should_warmup, warmup_session
            if should_warmup('batch_download'):
                current_account = self.available_accounts[self.current_account_index]
                warmup_session(self.manager.loader, current_account['username'])
        
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
        
        session.print_start_banner(total_items=len(remaining_usernames))
        
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
            # Check for graceful shutdown (resilience._SHUTDOWN or legacy _SHUTDOWN_EVENT)
            if _SHUTDOWN.is_set() or self._shutdown_requested.is_set():
                print("\n[STOP] Shutdown requested — stopping batch download gracefully.")
                session.print_end_banner(success=False, items_processed=successful + failed)
                break

            print(f"\n[{i}/{len(remaining_usernames)}] Downloading media for {username}...")
            
            # Update current position
            self.progress_manager.update_batch_state(current_user_index=i)
            
            try:
                result = self.download_media(username, post_limit)
                session.record_operation()
                if result:
                    successful += 1
                    session.record_save()
                    print(f"[OK] [{i}/{len(remaining_usernames)}] Successfully downloaded media for {username}")
                else:
                    failed += 1
                    print(f"[ERROR] [{i}/{len(remaining_usernames)}] Failed to download media for {username}")
                
                # Progress indicator every 3 users
                if i % 3 == 0:
                    session.print_progress(i, len(remaining_usernames), f"| ✓{successful} ✗{failed}")
                
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
        
        session.print_end_banner(success=True, items_processed=successful + failed)
        
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

    def process_retry_queue(self) -> int:
        """Re-attempt downloads that were rate-limited and enqueued.
        Returns number of items processed."""
        import time, os
        from src.db.manager import DatabaseManager
        db = DatabaseManager(os.environ.get("DATABASE_URL", ""))
        now = time.time()
        rows = db.fetchall(
            """SELECT id, username, account_name FROM download_retry_queue
               WHERE status='pending' AND retry_after_ts <= ?
               ORDER BY fail_ts ASC LIMIT 50""",
            (now,),
        )
        if not rows:
            print("[RETRY-Q] No items ready for retry.")
            return 0
        print(f"[RETRY-Q] Processing {len(rows)} queued downloads...")
        processed = 0
        for row in rows:
            uid, uname, acct = row['id'], row['username'], row['account_name']
            # Mark as in-progress so parallel runs don't double-process
            db.execute(
                "UPDATE download_retry_queue SET status='in-progress' WHERE id=?", (uid,)
            )
            success = self.download_media(uname)
            status = 'done' if success else 'failed'
            db.execute(
                "UPDATE download_retry_queue SET status=? WHERE id=?", (status, uid)
            )
            processed += 1
        print(f"[RETRY-Q] Done — {processed} retried.")
        return processed

