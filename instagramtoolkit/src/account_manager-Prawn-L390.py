# Instagram account management using Instaloader
import os
import hashlib
import random
import uuid
import instaloader
from src.config import INSTAGRAM_ACCOUNTS, SESSIONS_DIR, PROXY_CONFIG, SESSION_MAX_AGE_DAYS

# Stable fingerprint profiles — each account gets one deterministically via name hash.
# Using distinct real-world browser + locale combinations so 5 accounts look like 5 humans.
_FINGERPRINT_PROFILES = [
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'accept_language': 'en-US,en;q=0.9',
        'accept_encoding': 'gzip, deflate, br',
        'timezone': 'America/New_York',
    },
    {
        'ua': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'accept_language': 'en-GB,en;q=0.9,en-US;q=0.8',
        'accept_encoding': 'gzip, deflate, br',
        'timezone': 'Europe/London',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'accept_language': 'en-AU,en;q=0.8,en-US;q=0.6',
        'accept_encoding': 'gzip, deflate, br',
        'timezone': 'Australia/Sydney',
    },
    {
        'ua': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
        'accept_language': 'en-CA,en;q=0.9,fr-CA;q=0.5',
        'accept_encoding': 'gzip, deflate, br',
        'timezone': 'America/Toronto',
    },
    {
        'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
        'accept_language': 'en-SG,en;q=0.9,zh-CN;q=0.5',
        'accept_encoding': 'gzip, deflate, br',
        'timezone': 'Asia/Singapore',
    },
]


def _get_account_fingerprint(account_name: str) -> dict:
    """Return a stable, deterministic fingerprint for the given account name."""
    idx = int(hashlib.md5(account_name.encode()).hexdigest(), 16) % len(_FINGERPRINT_PROFILES)
    profile = dict(_FINGERPRINT_PROFILES[idx])
    # Stable device ID seeded from account name — looks like a real device UUID
    seed = hashlib.md5(f"device_{account_name}".encode()).hexdigest()
    profile['device_id'] = str(uuid.UUID(seed))
    return profile


def _check_reauth_schedule(account_name: str) -> None:
    """Log a warning if re-auth is not yet due — helps operator avoid clustering."""
    import time
    try:
        from src.db.manager import DatabaseManager
        db = DatabaseManager(os.environ.get("DATABASE_URL", ""))
        row = db.fetchone(
            "SELECT next_reauth_ts FROM account_sessions WHERE account_name=?",
            (account_name,),
        )
        if row and row.get('next_reauth_ts'):
            days_left = (row['next_reauth_ts'] - time.time()) / 86400
            if days_left > 0:
                print(f"[SESSION] {account_name}: next re-auth in {days_left:.1f} days (stagger active)")
    except Exception:
        pass


def _record_auth_timestamp(account_name: str, ua: str) -> None:
    """Record a fresh auth event and schedule next re-auth with ±3 day jitter."""
    import random, time
    try:
        from src.db.manager import DatabaseManager
        db = DatabaseManager(os.environ.get("DATABASE_URL", ""))
        now = time.time()
        # Stagger: base 7 days + random ±3 days so accounts don't cluster
        jitter_days = random.uniform(-3, 3)
        next_reauth = now + (7 + jitter_days) * 86400
        db.execute(
            """INSERT INTO account_sessions (account_name, last_auth_ts, next_reauth_ts, fingerprint_ua)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(account_name) DO UPDATE SET
                   last_auth_ts=excluded.last_auth_ts,
                   next_reauth_ts=excluded.next_reauth_ts,
                   fingerprint_ua=excluded.fingerprint_ua""",
            (account_name, now, next_reauth, ua),
        )
        from datetime import datetime
        next_dt = datetime.fromtimestamp(next_reauth).strftime("%Y-%m-%d")
        print(f"[SESSION] {account_name}: next re-auth scheduled {next_dt} (±3d jitter)")
    except Exception as e:
        print(f"[WARNING] Could not record auth timestamp: {e}")

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
            
            # Stable per-account fingerprint + mobile app headers
            fp = _get_account_fingerprint(account['name'])
            self.loader.context._session.headers.update({
                # Browser fingerprint — stable per account
                'User-Agent': fp['ua'],
                'Accept-Language': fp['accept_language'],
                'Accept-Encoding': fp['accept_encoding'],
                'X-IG-Device-ID': fp['device_id'],
                'X-IG-Android-ID': fp['device_id'].replace('-', '')[:16],
                # Mobile app headers — makes web sessions look like the Instagram app
                'X-IG-App-ID': '936619743392459',   # Instagram Android app ID
                'X-Instagram-AJAX': '1',
                'X-Requested-With': 'XMLHttpRequest',
                'X-IG-Connection-Type': 'WIFI',
                'X-IG-Capabilities': '3brTv10=',    # Instagram capabilities bitmask
                'X-IG-Connection-Speed': f'{random.randint(1200, 8000)}kbps',
                'Accept': '*/*',
                'Origin': 'https://www.instagram.com',
                'Referer': 'https://www.instagram.com/',
            })
            print(f"[FINGERPRINT] Account {account['name']}: {fp['ua'][:55]}... | {fp['accept_language']} | tz={fp['timezone']}")
            
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
                
                # PHASE 2: Check session age and refresh if too old
                import time
                try:
                    session_age_days = (time.time() - os.path.getmtime(loaded_session_path)) / 86400
                    if session_age_days > SESSION_MAX_AGE_DAYS:
                        print(f"⚠️  Session is {session_age_days:.1f} days old (max: {SESSION_MAX_AGE_DAYS} days)")
                        print(f"🔄 Removing old session and forcing fresh login...")
                        try:
                            os.remove(loaded_session_path)
                            loaded_session_path = None
                        except OSError:
                            pass
                except Exception as e:
                    print(f"⚠️  Could not check session age: {e}")
                
                if loaded_session_path:  # Only proceed if session wasn't removed due to age
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
                            
                            # Session validation failure means authentication failed - do not assume valid
                            # Validate session actually works by making a test API call
                            try:
                                from config import INSTAGRAM_ACCOUNTS
                                test_account = INSTAGRAM_ACCOUNTS[0] if INSTAGRAM_ACCOUNTS else None
                                if test_account:
                                    test_username = test_account['username']
                                    try:
                                        self.loader.check_profile_id(test_username)
                                        print(f"✅ Session validated for {account['username']}")
                                    except Exception as validation_error:
                                        print(f"⚠️  Session load succeeded but validation failed: {validation_error}")
                                        print(f"→ Session may be expired, authentication failed")
                                        # Remove invalid session
                                        try:
                                            os.remove(loaded_session_path)
                                            loaded_session_path = None
                                        except OSError:
                                            pass
                                        # Session validation failure means authentication failed - do not assume valid
                                        return False
                            except Exception as test_error:
                                print(f"⚠️  Session validation error: {test_error}")
                                # Session validation failure means authentication failed - do not assume valid
                                return False
                            
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
            _record_auth_timestamp(account['name'], fp['ua'])

            # PHASE 2: Perform warm-up after successful login
            from src.warmup import warmup_session
            warmup_session(self.loader, account['username'])
            
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

        # Session stagger: warn if re-auth is not yet due (avoids clustering)
        if not force_fresh_login:
            _check_reauth_schedule(account['name'])

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


