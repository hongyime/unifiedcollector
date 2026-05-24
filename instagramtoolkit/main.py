# Main launcher for Unified Instagram Toolkit
import atexit
import argparse
import io
import os
import signal
import sys
import glob
from typing import List

# Force UTF-8 stdout/stderr on Windows so emojis in print() don't crash.
# The bat file also sets PYTHONUTF8=1 + chcp 65001, but this covers
# direct `python main.py` invocations too.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# Add src/ to Python path so all modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))

# Import resilience module early - this installs the Ctrl+C signal handler
from src import resilience  # noqa: F401

from config import INSTAGRAM_ACCOUNTS, DATA_DIR
from account_manager import InstagramAccountManager
from cli_helpers import load_usernames, get_account_username
from profile_analyzer import ProfileAnalyzer


def _get_db():
    """Return the module-level DatabaseManager singleton (same as progress_manager uses)."""
    from db.manager import DatabaseManager
    if not hasattr(_get_db, "_instance") or _get_db._instance is None:
        _get_db._instance = DatabaseManager(os.environ.get("DATABASE_URL", ""))
    return _get_db._instance


_get_db._instance = None


def _shutdown_db(signum=None, frame=None) -> None:
    """Checkpoint WAL and close DB on SIGTERM/SIGINT before exit.
    WAL mode auto-recovers from SIGKILL, but explicit close is cleaner."""
    try:
        if _get_db._instance is not None:
            _get_db._instance.close()
            print("\n[DB] WAL checkpointed — safe to close.", flush=True)
    except Exception:
        pass
    if signum in (signal.SIGTERM,):
        sys.exit(0)


# Register for both normal exit and signals
atexit.register(_shutdown_db)
try:
    signal.signal(signal.SIGTERM, _shutdown_db)
    # Windows: SIGBREAK = Ctrl+Break
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, _shutdown_db)
except (OSError, ValueError):
    pass  # non-main thread or unsupported platform


def _close_db():
    """atexit handler — flush WAL and close DB on any exit."""
    try:
        if hasattr(_get_db, "_instance") and _get_db._instance is not None:
            _get_db._instance.close()
    except Exception:
        pass


atexit.register(_close_db)

def list_accounts():
    print("Configured Instagram accounts:")
    print("=" * 50)
    for i, acc in enumerate(INSTAGRAM_ACCOUNTS):
        default_marker = " (DEFAULT)" if i == 0 else ""
        print(f"{i+1}. {acc['name']} ({acc['username']}){default_marker}")
    
    print("\n[INFO] The first account in the list is used as default for batch processing")
    print("[INFO] To change default: Edit config.py and move your preferred account to the top of INSTAGRAM_ACCOUNTS list")

def login_account(name):
    manager = InstagramAccountManager()
    account = next((a for a in INSTAGRAM_ACCOUNTS if a['name'] == name), None)
    if not account:
        print(f"Account '{name}' not found.")
        return
    
    success = manager.login(account)
    if success:
        print(f"[OK] Logged in as {account['username']}")
        manager.logout()
    else:
        print(f"[ERROR] Login failed for {account['username']}")

def test_all_accounts():
    """Test login for all configured accounts"""
    print("🔐 Testing login for all configured accounts...")
    print("=" * 50)
    
    successful = []
    failed = []
    
    for account in INSTAGRAM_ACCOUNTS:
        print(f"\nTesting {account['name']} ({account['username']})...")
        manager = InstagramAccountManager()
        success = manager.login(account)
        
        if success:
            print(f"✅ Login successful for {account['name']}")
            successful.append(account['name'])
            manager.logout()
        else:
            print(f"❌ Login failed for {account['name']}")
            failed.append(account['name'])
    
    print(f"\n📊 Login Test Summary:")
    print(f"✅ Successful: {len(successful)} accounts")
    for name in successful:
        print(f"   - {name}")
    
    if failed:
        print(f"❌ Failed: {len(failed)} accounts")
        for name in failed:
            print(f"   - {name}")
    
    print(f"\n📈 Success rate: {len(successful)}/{len(INSTAGRAM_ACCOUNTS)} ({len(successful)/len(INSTAGRAM_ACCOUNTS)*100:.1f}%)")

def refresh_sessions(account_name=None, confirm=False):
    """Delete old sessions and force fresh login for accounts"""
    from config import SESSIONS_DIR
    
    # Determine which accounts to refresh
    if account_name:
        accounts_to_refresh = [a for a in INSTAGRAM_ACCOUNTS if a['name'] == account_name]
        if not accounts_to_refresh:
            print(f"❌ Account '{account_name}' not found")
            return
    else:
        accounts_to_refresh = INSTAGRAM_ACCOUNTS
    
    # Confirmation prompt
    if not confirm:
        print(f"⚠️  This will delete session files for {len(accounts_to_refresh)} account(s) and force fresh login:")
        for acc in accounts_to_refresh:
            print(f"   - {acc['name']} ({acc['username']})")
        response = input("\nContinue? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print("❌ Cancelled")
            return
    
    print(f"\n🔄 Refreshing sessions for {len(accounts_to_refresh)} account(s)...")
    print("=" * 50)
    
    deleted = []
    not_found = []
    login_success = []
    login_failed = []
    
    for account in accounts_to_refresh:
        username = account['username']
        session_file = os.path.join(SESSIONS_DIR, username)
        
        # Delete session file if exists
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                print(f"🗑️  Deleted session for {account['name']} ({username})")
                deleted.append(account['name'])
            except Exception as e:
                print(f"❌ Failed to delete session for {account['name']}: {e}")
                continue
        else:
            print(f"ℹ️  No session file found for {account['name']} ({username})")
            not_found.append(account['name'])
        
        # Force fresh login
        print(f"🔐 Logging in to {account['name']} ({username})...")
        manager = InstagramAccountManager()
        success = manager.login(account)
        
        if success:
            print(f"✅ Fresh login successful for {account['name']}")
            login_success.append(account['name'])
            manager.logout()
        else:
            print(f"❌ Fresh login failed for {account['name']}")
            login_failed.append(account['name'])
    
    # Summary
    print(f"\n📊 Session Refresh Summary:")
    print(f"🗑️  Sessions deleted: {len(deleted)}")
    print(f"ℹ️  No session found: {len(not_found)}")
    print(f"✅ Fresh login successful: {len(login_success)}")
    if login_failed:
        print(f"❌ Fresh login failed: {len(login_failed)}")
        for name in login_failed:
            print(f"   - {name}")
    
    print(f"\n📈 Success rate: {len(login_success)}/{len(accounts_to_refresh)} ({len(login_success)/len(accounts_to_refresh)*100:.1f}%)")


def main():
    """
    Main CLI entry point.
    
    TODO: Refactor to use src/commands/ module architecture.
    Current implementation uses monolithic dispatcher pattern.
    src/commands/ modules exist but are not fully integrated.
    
    Future refactoring will migrate to command-based dispatch system
    where each command is a separate module inheriting from BaseCommand.
    """
    parser = argparse.ArgumentParser(description="Unified Instagram Toolkit CLI")
    subparsers = parser.add_subparsers(dest='command', required=True)

    subparsers.add_parser('list', help='List configured accounts')
    login_parser = subparsers.add_parser('login', help='Login to an account')
    login_parser.add_argument('name', help='Account name to login')
    subparsers.add_parser('test-all', help='Test login for all configured accounts')
    
    # Session management
    refresh_parser = subparsers.add_parser('refresh-sessions', help='Delete old sessions and force fresh login for all accounts')
    refresh_parser.add_argument('--account', help='Refresh specific account only (default: all accounts)')
    refresh_parser.add_argument('--confirm', action='store_true', help='Skip confirmation prompt')
    
    # Profile access statistics
    subparsers.add_parser('access-stats', help='Show profile access statistics')
    
    # Priority analysis
    priority_parser = subparsers.add_parser('priority-analysis', help='Analyze username priorities for batch processing')
    priority_parser.add_argument('--account', help='Account name to use for priority analysis')
    
    # Spider relationships
    spider_parser = subparsers.add_parser('spider', help='Collect followers/following relationships')
    spider_parser.add_argument('--account', help='Account name to use for authentication')
    spider_parser.add_argument('--username', help='Specific username to collect relationships for')
    spider_parser.add_argument('--batch', action='store_true', help='Process all usernames from data folder')
    spider_parser.add_argument('--seed', action='store_true', help='Seed usernames.txt from logged-in accounts followers/following, then spider them')
    spider_parser.add_argument('--seed-only', action='store_true', help='Seed usernames.txt from logged-in accounts but do NOT spider afterwards')
    spider_parser.add_argument('--reset', action='store_true', help='Clear usernames.txt, relationships, and spider progress before running')
    spider_parser.add_argument('--high-priority-only', action='store_true', help='Process only high-priority users (followers/following)')
    spider_parser.add_argument('--max-users', type=int, help='Max number of users to process in batch mode')
    spider_parser.add_argument('--max-followers', type=int, default=1000, help='Max followers to collect per user')
    spider_parser.add_argument('--max-following', type=int, default=1000, help='Max following to collect per user')
    spider_parser.add_argument('--seed-accounts', help='Comma-separated account names to seed from (default: all)')
    spider_parser.add_argument('--seed-followers-only', action='store_true', help='Only collect followers during seed (skip following)')
    spider_parser.add_argument('--seed-following-only', action='store_true', help='Only collect following during seed (skip followers)')
    spider_parser.add_argument('--seed-mutual', action='store_true', help='Collect only mutual connections (users followed by multiple accounts)')
    spider_parser.add_argument('--min-mutual', type=int, default=2, help='Min accounts following to consider mutual (default: 2)')
    
    # Download media (posts, stories, highlights, profile photos)
    download_parser = subparsers.add_parser('download', help='Download media for users')
    download_parser.add_argument('--account', help='Account name to use for authentication')
    download_parser.add_argument('--username', help='Specific username to download media for')
    download_parser.add_argument('--batch', action='store_true', help='Download for all usernames from data folder')
    download_parser.add_argument('--high-priority-only', action='store_true', help='Download only high-priority users (followers/following)')
    download_parser.add_argument('--limit', type=int, help='Max number of posts to download')
    download_parser.add_argument('--profile-only', action='store_true', help='Download only profile photos')
    download_parser.add_argument('--posts-only', action='store_true', help='Download only posts')
    download_parser.add_argument('--stories-only', action='store_true', help='Download only stories')
    download_parser.add_argument('--highlights-only', action='store_true', help='Download only highlights')
    # B8: browser stealth download flags
    download_parser.add_argument('--browser', action='store_true',
                                 help='Use browser (stealth) downloader instead of Instaloader')
    download_parser.add_argument('--browser-fallback', action='store_true',
                                 help='Try browser downloader automatically if Instaloader fails')
    
    # NEW: Following-based media downloader
    following_parser = subparsers.add_parser('following-download', help='Download media from accounts you follow (with account selection)')
    following_parser.add_argument('--username', help='Specific username to download from (must be in following)')
    following_parser.add_argument('--all', action='store_true', help='Download from ALL accounts you follow')
    following_parser.add_argument('--progress', action='store_true', help='Show download progress')
    following_parser.add_argument('--reset', action='store_true', help='Reset download progress')
    following_parser.add_argument('--interactive', action='store_true', help='Start interactive menu')
    
    # NEW: Selective download feature
    selective_parser = subparsers.add_parser('selective-download', help='Download media from selected usernames (interactive selection)')
    selective_parser.add_argument('--account', help='Account name to use for authentication')
    selective_parser.add_argument('--select', action='store_true', help='Select usernames for selective download')
    selective_parser.add_argument('--list', action='store_true', help='Show current selective download list')
    selective_parser.add_argument('--add', help='Add username to selective download list')
    selective_parser.add_argument('--remove', help='Remove username from selective download list')
    selective_parser.add_argument('--clear', action='store_true', help='Clear selective download list')
    selective_parser.add_argument('--download', action='store_true', help='Download media for selected usernames')
    selective_parser.add_argument('--limit', type=int, help='Max number of posts to download per user')
    selective_parser.add_argument('--profile-only', action='store_true', help='Download only profile photos')
    # T14: filter/sort/search args for the interactive picker
    selective_parser.add_argument('--filter', dest='filter_vis', default='all',
                                  choices=['all', 'public', 'private'],
                                  help='Filter by visibility (default: all)')
    selective_parser.add_argument('--sort', dest='sort_by', default='name',
                                  choices=['name', 'followers', 'priority'],
                                  help='Sort order (default: name)')
    selective_parser.add_argument('--search', default='', help='Substring search on username')
    selective_parser.add_argument('--posts-only', action='store_true', help='Download only posts')
    selective_parser.add_argument('--stories-only', action='store_true', help='Download only stories')
    selective_parser.add_argument('--highlights-only', action='store_true', help='Download only highlights')
    
    # Profile metadata scan (lightweight — no follower/following collection)
    scan_parser = subparsers.add_parser('scan-profiles', help='Fetch profile info (followers, posts, bio) for tracked usernames')
    scan_parser.add_argument('--account', help='Use a specific account only (default: all accounts in parallel)')
    scan_parser.add_argument('--workers', type=int, help='Number of parallel workers (default: auto, max 3)')
    scan_parser.add_argument('--username', help='Scan a single username')
    scan_parser.add_argument('--force', action='store_true', help='Re-scan even if profile data is fresh')
    scan_parser.add_argument('--max-age', type=float, default=24.0, help='Hours before a profile is considered stale (default: 24)')
    scan_parser.add_argument('--max-users', type=int, help='Stop after scanning this many profiles')
    scan_parser.add_argument('--report', help='Print a detailed report for a specific username')

    # Analyze user network from collected relationships
    analyze_parser = subparsers.add_parser('analyze', help='Analyze collected user network')
    analyze_parser.add_argument('--json', help='Output JSON report', default=os.path.join(DATA_DIR, 'users_summary.json'))
    analyze_parser.add_argument('--csv', help='Output CSV report', default=os.path.join(DATA_DIR, 'users_summary.csv'))
    
    # Profile metadata analysis / enrichment
    ap_parser = subparsers.add_parser('analyze-profiles', help='Analyze or fetch profile metadata')
    ap_parser.add_argument('--fetch', action='store_true', help='Fetch live metadata from Instagram (public/private, follower count, post count) for all tracked usernames')
    ap_parser.add_argument('--account', help='Account to use for fetching (default: first configured)')
    ap_parser.add_argument('--limit', type=int, default=0, help='Max profiles to fetch in this run (0=all)')
    
    # Progress management commands
    progress_parser = subparsers.add_parser('progress', help='Manage progress and resume operations')
    progress_subparsers = progress_parser.add_subparsers(dest='progress_command', required=True)
    
    # Show progress
    progress_subparsers.add_parser('show', help='Show current progress for all operations')
    
    # Resume operations
    resume_parser = progress_subparsers.add_parser('resume', help='Resume interrupted operations')
    resume_parser.add_argument('--operation', choices=['spider', 'download'], help='Specific operation to resume')
    resume_parser.add_argument('--retry-failed', action='store_true', help='Retry previously failed users')
    
    # Clear progress
    clear_parser = progress_subparsers.add_parser('clear', help='Clear progress data')
    clear_parser.add_argument('--operation', choices=['spider', 'download'], help='Specific operation to clear')
    clear_parser.add_argument('--confirm', action='store_true', help='Confirm deletion without prompt')

    # Database migration command
    db_migrate_parser = subparsers.add_parser('db-migrate', help='Migrate JSON flat files to the database')
    db_migrate_parser.add_argument('--data-dir', default=DATA_DIR, help='Path to data directory (default: data/)')

    # Cleanup .bak files left over from migration
    subparsers.add_parser('cleanup-bak', help='Delete .bak files left over from JSON-to-DB migration')

    # Reset all DB tables (keeps schema, clears data)
    subparsers.add_parser('db-reset', help='Clear all data from the database (keeps schema intact)')

    # Retry queue — re-attempt rate-limited downloads
    retry_q_parser = subparsers.add_parser('retry-queue', help='Re-attempt downloads that were rate-limited')
    retry_q_parser.add_argument('--account', help='Account to use (default: first configured)')

    # Add a username to the DB tracking list
    add_username_parser = subparsers.add_parser('add-username', help='Add a username to the tracking database')
    add_username_parser.add_argument('username', help='Instagram username to add')

    # List all tracked usernames from the DB
    subparsers.add_parser('list-usernames', help='List all tracked usernames from the database')

    args = parser.parse_args()
    
    if args.command == 'list':
        list_accounts()
        
    elif args.command == 'login':
        login_account(args.name)
        
    elif args.command == 'test-all':
        test_all_accounts()
        
    elif args.command == 'refresh-sessions':
        refresh_sessions(args.account, args.confirm)
        
    elif args.command == 'access-stats':
        from profile_access_tracker import print_access_statistics
        print_access_statistics()
        
    elif args.command == 'priority-analysis':
        from priority_manager import print_priority_analysis

        usernames: List[str] = load_usernames()
        if not usernames:
            return

        account_username = get_account_username(args.account)
        if not account_username:
            return

        print_priority_analysis(usernames, account_username)
        
    elif args.command == 'spider':
        try:
            from config import (
                get_account_by_name, get_default_account,
                ENUM_PAUSE_EVERY, ENUM_PAUSE_SECONDS, MAX_RETRIES,
                RETRY_BASE_DELAY, RETRY_MAX_DELAY,
            )
            
            # Legacy file paths (data now in DB, but files may still exist)
            USERNAMES_FILE = f"{DATA_DIR}/usernames.txt"
            RELATIONSHIPS_FILE = f"{DATA_DIR}/relationships.json"
            SPIDER_PROGRESS_FILE = f"{DATA_DIR}/spider_progress.json"

            # ---- reset ----
            if args.reset:
                for path in (USERNAMES_FILE, RELATIONSHIPS_FILE, SPIDER_PROGRESS_FILE):
                    if os.path.exists(path):
                        os.remove(path)
                        print(f"[RESET] Deleted {path}")
                print("[RESET] Spider data cleared")
                # If only --reset with no other mode, we're done
                if not (args.batch or args.seed or args.seed_only or args.username):
                    return

            # ---- seed mode ----
            if args.seed or args.seed_only:
                import instaloader
                import time
                from io_utils import retry_with_backoff
                from rate_limiter import RateLimiter

                rate = RateLimiter()
                all_usernames: set[str] = set()

                # DB repo for immediate username persistence
                from db.repositories.username_repository import UsernameRepository
                _usr_repo = UsernameRepository(_get_db())

                def _save_username_immediately(uname: str, source: str):
                    """Save a single username to DB and in-memory set right away."""
                    all_usernames.add(uname)
                    try:
                        _usr_repo.add_username(uname, source_account=source)
                    except Exception:
                        pass

                def _flush_usernames_to_file():
                    """Write current in-memory set to usernames.txt (safe to call anytime)."""
                    if not all_usernames:
                        return
                    os.makedirs(DATA_DIR, exist_ok=True)
                    sorted_names = sorted(all_usernames)
                    with open(USERNAMES_FILE, 'w', encoding='utf-8') as f:
                        for u in sorted_names:
                            f.write(f"{u}\n")

                # Determine which accounts to seed from
                if args.seed_accounts:
                    seed_names = [n.strip() for n in args.seed_accounts.split(',')]
                    seed_accts = [a for a in INSTAGRAM_ACCOUNTS if a['name'] in seed_names]
                    not_found = [n for n in seed_names if not any(a['name'] == n for a in INSTAGRAM_ACCOUNTS)]
                    if not_found:
                        print(f"[WARN] Accounts not found: {', '.join(not_found)}")
                    if not seed_accts:
                        print("[ERROR] No valid accounts specified for seeding")
                        return
                    print(f"[SEED] Using {len(seed_accts)} account(s): {', '.join(a['name'] for a in seed_accts)}")
                else:
                    seed_accts = INSTAGRAM_ACCOUNTS
                    print(f"[SEED] Using all {len(seed_accts)} account(s)")

                # Determine what to collect
                collect_followers = not getattr(args, 'seed_following_only', False)
                collect_following = not getattr(args, 'seed_followers_only', False)
                collect_mutual_only = getattr(args, 'seed_mutual', False)
                min_mutual_accounts = getattr(args, 'min_mutual', 2)
                
                if collect_mutual_only:
                    print("[SEED] Collecting: MUTUAL CONNECTIONS ONLY (users followed by multiple accounts)")
                    print(f"[SEED] Minimum {min_mutual_accounts} accounts following to qualify")
                elif collect_followers and collect_following:
                    print("[SEED] Collecting: followers + following")
                elif collect_followers:
                    print("[SEED] Collecting: followers only")
                else:
                    print("[SEED] Collecting: following only")

                for account in seed_accts:
                    print(f"\n[SEED] Fetching connections for {account['username']}...")

                    # Skip if we already have usernames from this account in the DB
                    # (avoids wasting API calls on a re-seed without --reset)
                    existing_count = _get_db().fetchone(
                        "SELECT COUNT(*) as cnt FROM usernames WHERE source_account=?",
                        (account['name'],),
                    )
                    if existing_count and existing_count['cnt'] > 0:
                        print(f"[SKIP] Already have {existing_count['cnt']} usernames from {account['name']} — skipping re-fetch (use --reset to force)")
                        # Still load them into all_usernames so they're included in the final file
                        rows = _get_db().fetchall(
                            "SELECT username FROM usernames WHERE source_account=?",
                            (account['name'],),
                        )
                        for r in rows:
                            all_usernames.add(r['username'])
                        continue

                    mgr = InstagramAccountManager()
                    loader = mgr.get_authenticated_loader(account['name'])
                    if not loader:
                        print(f"[ERROR] Could not login to {account['username']}, skipping")
                        continue

                    try:
                        profile = retry_with_backoff(
                            instaloader.Profile.from_username,
                            loader.context,
                            account['username'],
                            max_retries=MAX_RETRIES,
                            base_delay=RETRY_BASE_DELAY,
                            max_delay=RETRY_MAX_DELAY,
                            label=f"seed-profile:{account['username']}",
                        )
                        if profile is None:
                            print(f"[ERROR] Could not load own profile {account['username']}")
                            continue

                        # MUTUAL ONLY MODE - use following to build mutual connections
                        if collect_mutual_only:
                            print(f"[SEED] Collecting following of {account['username']} for mutual analysis...")
                            count = 0
                            for followee in profile.get_followees():
                                username = followee.username
                                _save_username_immediately(username, account['name'])
                                # Save profile info — 0 extra API calls, we have the object
                                try:
                                    from user_metadata_manager import UserMetadataManager
                                    UserMetadataManager().update_profile(username, followee, account['name'])
                                except Exception:
                                    pass
                                count += 1
                                if count % 50 == 0:
                                    print(f"  ... {count} following so far for mutual analysis")
                                    _flush_usernames_to_file()
                                    print(f"  💾 {len(all_usernames)} usernames saved to database (safe to Ctrl+C)")
                                rate.periodic(count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                            _flush_usernames_to_file()
                            print(f"[OK] {count} following collected for mutual analysis")
                        
                        # Followers
                        elif collect_followers:
                            print(f"[SEED] Collecting followers of {account['username']}...")
                            count = 0
                            for follower in profile.get_followers():
                                _save_username_immediately(follower.username, account['name'])
                                try:
                                    from user_metadata_manager import UserMetadataManager
                                    UserMetadataManager().update_profile(follower.username, follower, account['name'])
                                except Exception:
                                    pass
                                count += 1
                                if count % 50 == 0:
                                    print(f"  ... {count} followers so far")
                                    _flush_usernames_to_file()
                                    print(f"  💾 {len(all_usernames)} usernames saved to database (safe to Ctrl+C)")
                                rate.periodic(count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                            _flush_usernames_to_file()
                            print(f"[OK] {count} followers collected")

                        # Following
                        elif collect_following:
                            print(f"[SEED] Collecting following of {account['username']}...")
                            count = 0
                            for followee in profile.get_followees():
                                _save_username_immediately(followee.username, account['name'])
                                try:
                                    from user_metadata_manager import UserMetadataManager
                                    UserMetadataManager().update_profile(followee.username, followee, account['name'])
                                except Exception:
                                    pass
                                count += 1
                                if count % 50 == 0:
                                    print(f"  ... {count} following so far")
                                    _flush_usernames_to_file()
                                    print(f"  💾 {len(all_usernames)} usernames saved to database (safe to Ctrl+C)")
                                rate.periodic(count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                            _flush_usernames_to_file()
                            print(f"[OK] {count} following collected")

                    except Exception as e:
                        print(f"[ERROR] Failed to seed from {account['username']}: {e}")
                    finally:
                        mgr.logout()
                        if len(INSTAGRAM_ACCOUNTS) > 1:
                            rate.short_delay()

                if collect_mutual_only:
                    # Track which follow each account made
                    from collections import defaultdict
                    follow_counts = defaultdict(int)
                    
                    # Re-collect to track per-account follows
                    print(f"\n[MUTUAL] Re-scanning to identify mutual connections (followed by {min_mutual_accounts}+ accounts)...")
                    
                    for account in seed_accts:
                        print(f"\n[MUTUAL] Scanning {account['username']}...")
                        mgr = InstagramAccountManager()
                        loader = mgr.get_authenticated_loader(account['name'])
                        if not loader:
                            print(f"[ERROR] Could not login to {account['username']}, skipping")
                            continue
                        
                        try:
                            profile = retry_with_backoff(
                                instaloader.Profile.from_username,
                                loader.context,
                                account['username'],
                                max_retries=MAX_RETRIES,
                                base_delay=RETRY_BASE_DELAY,
                                max_delay=RETRY_MAX_DELAY,
                                label=f"mutual-profile:{account['username']}",
                            )
                            if profile:
                                count = 0
                                for followee in profile.get_followees():
                                    follow_counts[followee.username] += 1
                                    count += 1
                                    if count % 50 == 0:
                                        print(f"  ... {count} following scanned")
                                    rate.periodic(count, every=ENUM_PAUSE_EVERY, seconds=ENUM_PAUSE_SECONDS)
                                print(f"[OK] {account['username']}: {count} following scanned")
                        except Exception as e:
                            print(f"[ERROR] Failed to scan {account['username']}: {e}")
                        finally:
                            mgr.logout()
                            if len(INSTAGRAM_ACCOUNTS) > 1:
                                rate.short_delay()
                    
                    # Filter to only mutual connections
                    mutual_usernames = {u for u, count in follow_counts.items() if count >= min_mutual_accounts}
                    all_usernames = mutual_usernames
                    print(f"\n[MUTUAL] Found {len(mutual_usernames)} mutual connections (followed by {min_mutual_accounts}+ accounts)")
                    
                    if not all_usernames:
                        print("[NOTE] No mutual connections found")
                        return
                else:
                    # Remove own account usernames from the seed list
                    own_usernames = {a['username'] for a in INSTAGRAM_ACCOUNTS}
                    all_usernames -= own_usernames

                if not all_usernames:
                    print("[NOTE] No usernames collected from any account")
                    return

                # Write final usernames.txt (already saved incrementally to DB above)
                os.makedirs(DATA_DIR, exist_ok=True)
                sorted_names = sorted(all_usernames)
                with open(USERNAMES_FILE, 'w', encoding='utf-8') as f:
                    for u in sorted_names:
                        f.write(f"{u}\n")
                print(f"\n[SEED] Wrote {len(sorted_names)} unique usernames to {USERNAMES_FILE}")

                if args.seed_only:
                    print("[DONE] Seed-only mode — not spidering. Run `spider --batch` to spider.")
                    return

                # Continue to batch spider the seeded usernames
                print(f"\n[SPIDER] Continuing to spider {len(sorted_names)} seeded usernames...")
                from parallel_processor import InstagramProcessor
                InstagramProcessor(args.account, operation_type="spider").process_batch_relationships(
                    sorted_names,
                    args.max_followers,
                    args.max_following,
                )

            elif args.batch:
                # Batch processing
                from collect_relationships import RelationshipCollector
                from parallel_processor import InstagramProcessor
                from priority_manager import PriorityManager

                print(f"[SPIDER] Starting batch relationship collection...")

                collector = RelationshipCollector(args.account)
                processed = {r['source'] for r in collector.relationships}
                to_process = [u for u in collector.usernames if u not in processed]
                collector.cleanup()

                if not to_process:
                    print("[NOTE] All usernames have been processed already")
                    return

                account_cfg = get_account_by_name(args.account) if args.account else get_default_account()
                if not account_cfg:
                    print("[ERROR] No valid account found")
                    return
                account_username = account_cfg['username']

                if args.high_priority_only:
                    to_process = PriorityManager().get_high_priority_users(to_process, account_username, args.max_users)
                    print("[FILTER] Using high-priority-only mode")
                elif args.max_users:
                    to_process = to_process[:args.max_users]

                print(f"[LIST] Found {len(to_process)} unprocessed usernames")

                InstagramProcessor(args.account, operation_type="spider").process_batch_relationships(
                    to_process,
                    args.max_followers,
                    args.max_following,
                )
                
            elif args.username:
                # Single user processing using the enhanced processor
                from parallel_processor import InstagramProcessor
                
                print(f"[SPIDER] Collecting relationships for {args.username}...")
                processor = InstagramProcessor(args.account, operation_type="spider")
                success = processor.collect_relationships(args.username, args.max_followers, args.max_following)
                
                if success:
                    print(f"[OK] Successfully collected relationships for {args.username}")
                else:
                    print(f"[ERROR] Failed to collect relationships for {args.username}")
                
            else:
                print("[ERROR] Please specify --username, --batch, --seed, or --seed-only")
                
        except Exception as e:
            print(f"[ERROR] Spider error: {e}")
    
    elif args.command == 'download':
        try:
            # B8: browser-only mode
            use_browser = getattr(args, 'browser', False)
            use_browser_fallback = getattr(args, 'browser_fallback', False)

            if use_browser:
                from src.browser_downloader import BrowserDownloader
                account_name = args.account or (INSTAGRAM_ACCOUNTS[0]['name'] if INSTAGRAM_ACCOUNTS else None)
                if not account_name:
                    print("[ERROR] No account configured")
                    return
                bd = BrowserDownloader(account_name=account_name)
                if args.username:
                    bd.download(args.username, post_limit=args.limit or 0)
                elif args.batch:
                    usernames = load_usernames()
                    bd.download_batch(usernames, post_limit=args.limit or 0)
                else:
                    print("[ERROR] Specify --username or --batch with --browser")
                bd.close()
                return

            if args.batch:
                # Batch processing
                from parallel_processor import InstagramProcessor
                from priority_manager import PriorityManager
                from config import get_account_by_name, get_default_account

                print(f"[DOWNLOAD] Starting batch media download...")

                all_usernames = load_usernames()
                if not all_usernames:
                    return

                account_cfg = get_account_by_name(args.account) if args.account else get_default_account()
                if not account_cfg:
                    print("[ERROR] No valid account found")
                    return
                account_username = account_cfg['username']

                if args.high_priority_only:
                    to_process = PriorityManager().get_high_priority_users(all_usernames, account_username)
                    print("[FILTER] Using high-priority-only mode")
                else:
                    to_process = all_usernames

                print(f"[LIST] Found {len(to_process)} usernames to download")

                processor = InstagramProcessor(args.account, operation_type="download")
                if use_browser_fallback:
                    processor._browser_fallback_enabled = True
                processor.process_batch_downloads(to_process, args.limit)
                
            elif args.username:
                # Single user processing
                from download_media import MediaDownloader
                
                print(f"[DOWNLOAD] Downloading media for {args.username}...")
                downloader = MediaDownloader(args.account)
                
                if args.profile_only:
                    success = downloader.download_profile_photo(args.username)
                elif args.posts_only:
                    success = downloader.download_posts(args.username, args.limit)
                elif args.stories_only:
                    success = downloader.download_stories(args.username)
                elif args.highlights_only:
                    success = downloader.download_highlights(args.username)
                else:
                    # download_all() returns dict with success/partial_success keys - check explicitly
                    download_result = downloader.download_all(args.username, args.limit)
                    success = download_result['success'] or download_result['partial_success']
                
                downloader.cleanup()
                if success:
                    print(f"[OK] Download completed for {args.username}")
                else:
                    print(f"[ERROR] Download failed for {args.username}")
                
            else:
                print("[ERROR] Please specify either --username for single user or --batch for batch processing")
                
        except Exception as e:
            print(f"[ERROR] Download error: {e}")
    
    elif args.command == 'following-download':
        try:
            from following_media_downloader import FollowingMediaDownloader, interactive_menu
            
            # Handle different modes
            if args.interactive:
                # Start interactive menu
                interactive_menu()
                
            elif args.progress:
                # Show progress
                downloader = FollowingMediaDownloader()
                downloader.show_progress()
                
            elif args.reset:
                # Reset progress
                downloader = FollowingMediaDownloader()
                downloader.reset_progress()
                
            elif args.username:
                # Download from specific account
                downloader = FollowingMediaDownloader()
                success = downloader.download_single_account(args.username)
                if success:
                    print(f"✅ Successfully downloaded media from {args.username}")
                else:
                    print(f"❌ Failed to download media from {args.username}")
                downloader.cleanup()
                
            elif args.all:
                # Download from all followed accounts
                downloader = FollowingMediaDownloader()
                success = downloader.download_all_following()
                if success:
                    print("🎉 Batch download completed!")
                else:
                    print("❌ Batch download failed or was interrupted")
                downloader.cleanup()
                
            else:
                # Default to interactive menu
                print("🚀 Starting Following Media Downloader...")
                print("💡 Use --help to see all available options")
                interactive_menu()
                
        except Exception as e:
            print(f"[ERROR] Following download error: {e}")
    
    elif args.command == 'selective-download':
        try:
            from selective_download_manager import SelectiveDownloadManager
            
            manager = SelectiveDownloadManager()
            
            if args.select:
                # Interactive selection
                manager.interactive_select(
                    filter_vis=getattr(args, 'filter_vis', 'all'),
                    sort_by=getattr(args, 'sort_by', 'name'),
                    search=getattr(args, 'search', ''),
                )
                
            elif args.list:
                # Show current list
                manager.show_list()
                
            elif args.add:
                # Add username to list
                manager.add_username(args.add)
                
            elif args.remove:
                # Remove username from list
                manager.remove_username(args.remove)
                
            elif args.clear:
                # Clear the list
                manager.clear_list()
                
            elif args.download:
                # Download media for selected usernames
                if not manager.has_selection():
                    print("❌ No usernames selected for download")
                    print("💡 Use 'selective-download --select' to choose usernames first")
                    return
                
                selected_usernames = manager.get_selected_usernames()
                print(f"🎯 Starting selective download for {len(selected_usernames)} usernames...")
                
                # Use existing download infrastructure
                from parallel_processor import InstagramProcessor
                from download_media import MediaDownloader
                
                if len(selected_usernames) == 1:
                    # Single user - use MediaDownloader directly
                    username = selected_usernames[0]
                    print(f"[DOWNLOAD] Downloading media for {username}...")
                    downloader = MediaDownloader(args.account)
                    
                    if args.profile_only:
                        success = downloader.download_profile_photo(username)
                    elif args.posts_only:
                        success = downloader.download_posts(username, args.limit)
                    elif args.stories_only:
                        success = downloader.download_stories(username)
                    elif args.highlights_only:
                        success = downloader.download_highlights(username)
                    else:
                        download_result = downloader.download_all(username, args.limit)
                        success = download_result['success'] or download_result['partial_success']
                    
                    downloader.cleanup()
                    if success:
                        print("🎉 Selective download completed!")
                    else:
                        print("[ERROR] Selective download failed!")
                    
                else:
                    # Multiple users - use parallel processor
                    processor = InstagramProcessor(args.account, operation_type="selective_download")
                    if args.profile_only or args.posts_only or args.stories_only or args.highlights_only:
                        print("[INFO] Selective media type flags ignored in batch mode (downloading all)")
                    processor.process_batch_downloads(selected_usernames, args.limit)
                    print("🎉 Selective download completed!")
                
            else:
                # Default: show current list and options
                manager.show_list()
                print("\n🔧 Available Options:")
                print("  --select     Interactive username selection")
                print("  --download   Download media for selected usernames")
                print("  --add USER   Add username to selection")
                print("  --remove USER Remove username from selection")
                print("  --clear      Clear selection")
                print("  --list       Show current selection")
                
        except Exception as e:
            print(f"[ERROR] Selective download error: {e}")
    
    elif args.command == 'scan-profiles':
        try:
            from profile_scanner import ProfileScanner, MAX_SCAN_WORKERS
            scanner = ProfileScanner(
                account_name=args.account,
                max_age_hours=args.max_age,
                workers=args.workers,
            )
            if args.report:
                scanner.print_profile_report(args.report)
            elif args.username:
                scanner.scan_one(args.username, force=args.force)
                scanner.print_profile_report(args.username)
            else:
                scanner.scan_all(force=args.force, max_users=args.max_users)
        except Exception as e:
            print(f"[ERROR] scan-profiles error: {e}")

    elif args.command == 'analyze':
        try:
            from analyze_users import UserAnalyzer
            analyzer = UserAnalyzer()
            # Always print summary from DB
            analyzer.print_summary()
            # Only write files if explicitly requested via --json / --csv flags
            json_path = getattr(args, 'json', None)
            csv_path = getattr(args, 'csv', None)
            default_json = os.path.join(DATA_DIR, 'users_summary.json')
            default_csv = os.path.join(DATA_DIR, 'users_summary.csv')
            if json_path and json_path != default_json:
                analyzer.save_json(json_path)
            if csv_path and csv_path != default_csv:
                analyzer.save_csv(csv_path)
        except Exception as e:
            print(f"[ERROR] Analysis error: {e}")
    
    elif args.command == 'analyze-profiles':
        if getattr(args, 'fetch', False):
            # Live fetch mode: pull metadata from Instagram for tracked usernames
            try:
                from db.repositories.username_repository import UsernameRepository
                from db.repositories.profile_repository import ProfileRepository
                import instaloader as _il
                import time as _time

                db = _get_db()
                usr_repo = UsernameRepository(db)
                profile_repo = ProfileRepository(db)
                mgr = InstagramAccountManager()
                loader = mgr.get_authenticated_loader(getattr(args, 'account', None))
                if not loader:
                    print("[ERROR] Could not authenticate for profile fetch")
                    return

                all_usernames = [r['username'] for r in usr_repo.get_all()]
                limit = getattr(args, 'limit', 0)
                if limit > 0:
                    all_usernames = all_usernames[:limit]

                print(f"[FETCH] Fetching metadata for {len(all_usernames)} usernames...")
                fetched = 0
                skipped = 0
                failed = 0
                for i, uname in enumerate(all_usernames, 1):
                    # Skip if we have recent data (< 7 days old)
                    existing = db.fetchone(
                        "SELECT updated_at FROM profiles WHERE username=?", (uname,)
                    )
                    if existing and (_time.time() - (existing['updated_at'] or 0)) < 7 * 86400:
                        skipped += 1
                        continue
                    try:
                        prof = retry_with_backoff(
                            _il.Profile.from_username,
                            loader.context,
                            uname,
                            max_retries=2,
                            base_delay=RETRY_BASE_DELAY,
                            max_delay=60.0,
                            label=f"fetch-profile:{uname}",
                        )
                        if prof:
                            profile_repo.upsert_profile(uname, {
                                'full_name': prof.full_name or '',
                                'biography': prof.biography or '',
                                'followers_count': prof.followers,
                                'following_count': prof.followees,
                                'media_count': prof.mediacount,
                                'is_public': int(not prof.is_private),
                                'is_verified': int(prof.is_verified),
                                'collected_by': getattr(args, 'account', None) or INSTAGRAM_ACCOUNTS[0]['name'],
                            })
                            fetched += 1
                    except Exception as e:
                        failed += 1
                        if 'ProfileNotExistsException' in type(e).__name__:
                            db.execute(
                                "UPDATE usernames SET spider_status='not-found' WHERE username=?",
                                (uname,),
                            )
                    # Brief per-profile delay to avoid rate limits
                    import random as _rand
                    import time as _t
                    _t.sleep(_rand.uniform(1.0, 3.0))
                    if i % 20 == 0:
                        print(f"[FETCH] {i}/{len(all_usernames)} — fetched={fetched} skipped={skipped} failed={failed}")

                print(f"[FETCH] Done. fetched={fetched} skipped(recent)={skipped} failed={failed}")
            except Exception as e:
                print(f"[ERROR] Profile fetch error: {e}")
        else:
            try:
                analyzer = ProfileAnalyzer()
                stats = analyzer.analyze_network()
                analyzer.save_analysis(stats)
                analyzer.print_summary(stats)
            except Exception as e:
                print(f"[ERROR] Profile analysis error: {e}")
    
    elif args.command == 'progress':
        try:
            from progress_manager import ProgressManager
            
            if args.progress_command == 'show':
                # Show progress for all operations
                print("[STATS] Current Progress Status")
                print("=" * 50)
                
                for operation in ['spider', 'download']:
                    print(f"\n[SEARCH] {operation.title()} Operations:")
                    manager = ProgressManager(operation)
                    if manager.can_resume():
                        manager.print_progress_summary()
                        
                        # Show failed users if any
                        failed_users = manager.get_failed_users()
                        if failed_users:
                            print(f"[ERROR] Failed users: {', '.join(failed_users[:5])}")
                            if len(failed_users) > 5:
                                print(f"   ... and {len(failed_users) - 5} more")
                    else:
                        print("   No progress data found")
            
            elif args.progress_command == 'resume':
                if args.operation:
                    # Resume specific operation
                    print(f"[RESUME] Resuming {args.operation} operations...")
                    
                    if args.operation == 'spider':
                        from db.repositories.username_repository import UsernameRepository
                        rows = UsernameRepository(_get_db()).get_all()
                        all_usernames = [r['username'] for r in rows]
                        if not all_usernames:
                            print("[ERROR] No usernames in database. Add usernames first.")
                            return
                        
                        from parallel_processor import InstagramProcessor
                        processor = InstagramProcessor(operation_type="spider")
                        
                        if args.retry_failed:
                            failed_users = processor.progress_manager.get_failed_users()
                            if failed_users:
                                print(f"[RESUME] Retrying {len(failed_users)} failed users...")
                                processor.progress_manager.clear_failed_users()
                                processor.process_batch_relationships(failed_users)
                            else:
                                print("No failed users to retry")
                        else:
                            processor.process_batch_relationships(all_usernames)
                    
                    elif args.operation == 'download':
                        from db.repositories.username_repository import UsernameRepository
                        rows = UsernameRepository(_get_db()).get_all()
                        all_usernames = [r['username'] for r in rows]
                        if not all_usernames:
                            print("[ERROR] No usernames in database. Add usernames first.")
                            return
                        
                        from parallel_processor import InstagramProcessor
                        processor = InstagramProcessor(operation_type="download")
                        
                        if args.retry_failed:
                            failed_users = processor.progress_manager.get_failed_users()
                            if failed_users:
                                print(f"[RESUME] Retrying {len(failed_users)} failed users...")
                                processor.progress_manager.clear_failed_users()
                                processor.process_batch_downloads(failed_users)
                            else:
                                print("No failed users to retry")
                        else:
                            processor.process_batch_downloads(all_usernames)
                else:
                    print("[ERROR] Please specify --operation (spider or download)")
            
            elif args.progress_command == 'clear':
                from db.repositories.operation_progress_repository import OperationProgressRepository
                
                if args.operation:
                    # Clear specific operation
                    if not args.confirm:
                        response = input(f"[WARNING]  Are you sure you want to clear {args.operation} progress? (y/N): ")
                        if response.lower() != 'y':
                            print("[ERROR] Operation cancelled")
                            return
                    
                    try:
                        repo = OperationProgressRepository(_get_db())
                        repo.archive_operation(args.operation)
                        print(f"[OK] Cleared {args.operation} progress from database")
                    except Exception as e:
                        print(f"[ERROR] Error clearing progress: {e}")
                else:
                    # Clear all progress
                    if not args.confirm:
                        response = input("[WARNING]  Are you sure you want to clear ALL progress data? (y/N): ")
                        if response.lower() != 'y':
                            print("[ERROR] Operation cancelled")
                            return
                    
                    try:
                        repo = OperationProgressRepository(_get_db())
                        for op in ('spider', 'download', 'general'):
                            repo.archive_operation(op)
                        print("[OK] Cleared all progress from database")
                    except Exception as e:
                        print(f"[ERROR] Error clearing progress: {e}")
        
        except Exception as e:
            print(f"[ERROR] Progress management error: {e}")

    elif args.command == 'db-migrate':
        try:
            from db.manager import DatabaseManager
            from db.migrate_json import migrate_json_to_db
            import json as _json

            data_dir = args.data_dir
            print(f"[DB-MIGRATE] Starting migration from {data_dir} ...")
            db = DatabaseManager()
            report = migrate_json_to_db(data_dir, db)
            db.close()

            print("\n[DB-MIGRATE] Migration complete.")
            print("[DB-MIGRATE] Migrated records:")
            for table, count in report.get("migrated", {}).items():
                print(f"  {table}: {count}")
            if report.get("skipped"):
                print("[DB-MIGRATE] Skipped (files not found):", ", ".join(report["skipped"]))
            if report.get("errors"):
                print("[DB-MIGRATE] Errors:")
                print(_json.dumps(report["errors"], indent=2))
        except Exception as e:
            print(f"[ERROR] Migration error: {e}")

    elif args.command == 'cleanup-bak':
        bak_files = glob.glob(os.path.join(DATA_DIR, "*.bak"))
        if not bak_files:
            print("[OK] No .bak files found in data/ — nothing to clean up.")
        else:
            for path in sorted(bak_files):
                try:
                    os.remove(path)
                    print(f"[DELETED] {os.path.basename(path)}")
                except Exception as e:
                    print(f"[WARNING] Could not delete {path}: {e}")
            print(f"[OK] Cleaned up {len(bak_files)} .bak file(s).")

    elif args.command == 'db-reset':
        try:
            db = _get_db()
            tables = [
                'relationships',
                'usernames',
                'username_following_status',
                'profiles',
                'profile_snapshots',
                'profile_access_attempts',
                'profile_access_summary',
                'operation_progress',
                'batch_state',
                'account_cooldowns',
                'account_quotas',
            ]
            for table in tables:
                db.execute(f"DELETE FROM {table}")
            print("[DB-RESET] All database tables cleared (schema preserved).")
            print("[DB-RESET] Sessions are NOT affected.")
        except Exception as e:
            print(f"[ERROR] DB reset error: {e}")

    elif args.command == 'retry-queue':
        try:
            from parallel_processor import InstagramProcessor
            proc = InstagramProcessor(
                account_name=getattr(args, 'account', None),
                operation_type='download',
            )
            proc.process_retry_queue()
        except Exception as e:
            print(f"[ERROR] retry-queue error: {e}")

    elif args.command == 'add-username':
        try:
            from db.repositories.username_repository import UsernameRepository
            db = _get_db()
            repo = UsernameRepository(db)
            username = args.username.strip()
            if not username:
                print("[ERROR] Username cannot be empty.")
            elif repo.add_username(username, source_account="manual"):
                print(f"[OK] Added '{username}' to tracking database.")
            else:
                print(f"[INFO] '{username}' is already in the tracking database.")
        except Exception as e:
            print(f"[ERROR] add-username error: {e}")

    elif args.command == 'list-usernames':
        try:
            from db.repositories.username_repository import UsernameRepository
            db = _get_db()
            repo = UsernameRepository(db)
            rows = repo.get_all()
            if not rows:
                print("[INFO] No usernames in tracking database.")
                print("[INFO] Add usernames with: python main.py add-username <name>")
                print("[INFO] Or seed from your accounts with: python main.py spider --seed-only")
            else:
                for row in rows:
                    print(row['username'])
                print(f"\n[INFO] Total: {len(rows)} usernames")
        except Exception as e:
            print(f"[ERROR] list-usernames error: {e}")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user.")
        _close_db()
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] {e}")
        _close_db()
        sys.exit(1)
