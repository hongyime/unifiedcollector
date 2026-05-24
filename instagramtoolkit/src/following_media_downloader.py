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





