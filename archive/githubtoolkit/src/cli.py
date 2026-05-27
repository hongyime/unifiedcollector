"""Command-line interface for GitHub Toolkit."""
import asyncio
import sys
from pathlib import Path

from src.config import Config
from src.database import init_database, reset_in_progress_spiders, get_stats, get_downloaded_hashes
from src.pat_manager import PATManager
from src.github_client import GitHubAPIClient
from src.spider import SocialGraphSpider
from src.contribution_spider import ContributionSpider
from src.avatar_downloader import AvatarDownloader
from src.profile_photo_tracker import ProfilePhotoTracker
from src.reconciler import Reconciler
from src.web.app import run_server
from src.download_path_manager import prompt_for_download_path
import aiosqlite


def print_header():
    """Print toolkit header."""
    print("=" * 70)
    print("GitHub Toolkit - Social Graph & Avatar Downloader")
    print("=" * 70)
    print()


def print_menu():
    """Print main menu."""
    print("\n" + "=" * 70)
    print("MAIN MENU")
    print("=" * 70)
    print()
    print("[1] Social Graph")
    print("[2] Avatar Downloads")
    print("[3] Profile History")
    print("[4] Search Users")
    print("[5] Contributions (repos/co-contributors)")
    print("[6] Frontend")
    print("[7] Database")
    print("[8] Authentication")
    print("[9] System")
    print("[0] Exit")
    print()


def social_graph_menu():
    """Social graph submenu."""
    while True:
        print("\n" + "=" * 70)
        print("SOCIAL GRAPH")
        print("=" * 70)
        print()
        print("[1] Seed from my account (authenticated)")
        print("[2] Seed from username")
        print("[3] Seed from numeric user ID")
        print("[4] Spider batch (pending users)")
        print("[5] Spider all (until queue empty)")
        print("[6] Reset spider progress")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()

        if choice == '1':
            seed_from_self()
        elif choice == '2':
            seed_from_account()
        elif choice == '3':
            seed_from_id()
        elif choice == '4':
            spider_batch()
        elif choice == '5':
            spider_all()
        elif choice == '6':
            reset_spider()
        elif choice == '0':
            break


def avatar_downloads_menu():
    """Avatar downloads submenu."""
    while True:
        session_dir = _SESSION_AVATAR_DIR or "(not set — will prompt)"
        print("\n" + "=" * 70)
        print("AVATAR DOWNLOADS")
        print("=" * 70)
        print(f"   Session dir: {session_dir}")
        print()
        print("[1] Download by ID range (sequential)")
        print("[2] Download for scraped users")
        print("[3] Reconcile (re-download missing)")
        print("[4] Import existing photos into DB")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()

        if choice == '1':
            download_by_range()
        elif choice == '2':
            download_for_users()
        elif choice == '3':
            reconcile_avatars()
        elif choice == '4':
            import_existing_avatars()
        elif choice == '0':
            break


def profile_history_menu():
    """Profile history submenu."""
    while True:
        print("\n" + "=" * 70)
        print("PROFILE HISTORY")
        print("=" * 70)
        print()
        print("[1] View avatar change history (single user)")
        print("[2] Track photo changes for all known users")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()

        if choice == '1':
            view_photo_history()
        elif choice == '2':
            track_all_users()
        elif choice == '0':
            break


def frontend_menu():
    """Frontend submenu."""
    while True:
        print("\n" + "=" * 70)
        print("FRONTEND")
        print("=" * 70)
        print()
        print("[1] Start graph viewer")
        print("[0] Back")
        print()
        
        choice = input("Choose an option: ").strip()
        
        if choice == '1':
            start_frontend()
        elif choice == '0':
            break


def database_menu():
    """Database submenu."""
    while True:
        print("\n" + "=" * 70)
        print("DATABASE")
        print("=" * 70)
        print()
        print("[1] Show stats")
        print("[2] Verify integrity")
        print("[3] Reset database")
        print("[0] Back")
        print()
        
        choice = input("Choose an option: ").strip()
        
        if choice == '1':
            show_stats()
        elif choice == '2':
            verify_integrity()
        elif choice == '3':
            reset_database()
        elif choice == '0':
            break


def authentication_menu():
    """Authentication submenu."""
    while True:
        print("\n" + "=" * 70)
        print("AUTHENTICATION")
        print("=" * 70)
        print()
        print("[1] Add PAT token")
        print("[2] List all PATs")
        print("[3] Remove a PAT")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()

        if choice == '1':
            set_pat()
        elif choice == '2':
            view_pat_status()
        elif choice == '3':
            clear_pat()
        elif choice == '0':
            break


# Session-only avatar dir cache — prompts once per run, never persisted to disk
_SESSION_AVATAR_DIR: str = ""


def _pick_avatar_dir() -> str:
    """
    Prompt for avatar save directory once per session.
    Cleared on exit — never written to .env or any file.
    """
    global _SESSION_AVATAR_DIR
    if _SESSION_AVATAR_DIR:
        print(f"   Using session avatar dir: {_SESSION_AVATAR_DIR}")
        return _SESSION_AVATAR_DIR

    default = str(Config.AVATARS_DIR)
    raw = input(
        f"\nAvatar save directory\n"
        f"  Default: {default}\n"
        f"  Custom (e.g. Z:\\media\\github\\avatars.githubusercontent.com\\u): "
    ).strip()

    chosen = raw if raw else default
    p = Path(chosen)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"⚠️  Could not create {p}: {e}. Using default.")
        chosen = default

    _SESSION_AVATAR_DIR = chosen
    print(f"   Session avatar dir set: {chosen}  (cleared on exit)")
    return chosen


def _make_spider() -> SocialGraphSpider:
    """Build spider with all stored PATs loaded."""
    pm = PATManager()
    pats = pm.load_all_pats()
    return SocialGraphSpider(Config.DB_PATH, pats=pats)


def seed_from_self():
    """Seed from the authenticated user's own account."""
    if not PATManager().load_all_pats():
        print("❌ PAT required. Go to Authentication > Add PAT token first.")
        return
    asyncio.run(_make_spider().seed_from_self())


def seed_from_account():
    """Seed spider from a GitHub username."""
    username = input("\nEnter GitHub username to seed from: ").strip()
    if not username:
        print("❌ Username required")
        return
    asyncio.run(_make_spider().seed_from_user(username))


def seed_from_id():
    """Seed spider from a numeric GitHub user ID."""
    raw = input("\nEnter numeric GitHub user ID: ").strip()
    if not raw.isdigit():
        print("❌ Must be a numeric ID")
        return
    asyncio.run(_make_spider().seed_from_id(int(raw)))


def spider_batch():
    """Spider a batch of pending users."""
    batch_size = input("\nBatch size (default 10): ").strip()
    batch_size = int(batch_size) if batch_size else 10
    asyncio.run(_make_spider().spider_batch(batch_size))


def spider_all():
    """Spider all pending users."""
    if input("\n⚠️  Spider until queue empty? (y/n): ").strip().lower() != 'y':
        return
    try:
        asyncio.run(_make_spider().spider_all())
    except KeyboardInterrupt:
        print("\n✅ Spider stopped. DB is safe — progress is committed per user.")


def reset_spider():
    """Reset spider progress."""
    confirm = input("\n⚠️  Reset all 'in_progress' to 'pending'? (y/n): ").strip().lower()
    if confirm != 'y':
        return
    
    asyncio.run(reset_in_progress_spiders(Config.DB_PATH))
    print("✅ Spider progress reset")


def download_by_range():
    """Download avatars by ID range."""
    start_id = input("\nStart user ID: ").strip()
    end_id = input("End user ID: ").strip()

    if not start_id or not end_id:
        print("❌ Both IDs required")
        return

    start_id = int(start_id)
    end_id = int(end_id)

    concurrency = input(f"Concurrency (default {Config.MAX_CONCURRENT_DOWNLOADS}): ").strip()
    concurrency = int(concurrency) if concurrency else Config.MAX_CONCURRENT_DOWNLOADS

    delay = input(f"Delay between batches (default {Config.AVATAR_DOWNLOAD_DELAY}s): ").strip()
    delay = float(delay) if delay else Config.AVATAR_DOWNLOAD_DELAY

    save_dir = _pick_avatar_dir()

    async def download():
        async with AvatarDownloader(Config.DB_PATH, concurrency, save_dir=save_dir) as dl:
            await dl.download_range(start_id, end_id, delay)

    asyncio.run(download())


def download_for_users():
    """Download avatars for all scraped users that have a numeric user_id."""
    async def get_users():
        async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE user_id IS NOT NULL ORDER BY followers_count DESC"
            )
            return [r[0] for r in await cursor.fetchall()]

    user_ids = asyncio.run(get_users())
    if not user_ids:
        print("\n❌ No users with IDs in DB. Seed and spider first.")
        return

    print(f"\n📋 {len(user_ids):,} users with IDs in database.")
    save_dir = _pick_avatar_dir()

    async def download():
        # AvatarDownloader.__aenter__ scans dir and builds _existing_ids set —
        # no per-file stat calls during the loop.
        async with AvatarDownloader(Config.DB_PATH, Config.MAX_CONCURRENT_DOWNLOADS, save_dir=save_dir) as dl:
            for i, uid in enumerate(user_ids):
                await dl.download_avatar(uid)   # skips if in _existing_ids
                if (i + 1) % 500 == 0:
                    print(f"   {i+1:,}/{len(user_ids):,} "
                          f"new={dl.downloaded:,} "
                          f"skipped={dl.skipped_existing:,} "
                          f"err={dl.errors:,}")

        print(f"\n✅ Done — new={dl.downloaded:,} "
              f"skipped={dl.skipped_existing:,} err={dl.errors:,}")

    asyncio.run(download())


def reconcile_avatars():
    """Reconcile avatar downloads — re-download files missing from disk."""
    save_dir = _pick_avatar_dir()
    reconciler = Reconciler(Config.DB_PATH, avatars_dir=save_dir)
    asyncio.run(reconciler.reconcile_avatars())


def import_existing_avatars():
    """Scan existing photo directory and register files in DB."""
    save_dir = _pick_avatar_dir()
    print(f"\n📥 Will import files from: {save_dir}")
    confirm = input("Proceed? (y/n): ").strip().lower()
    if confirm != 'y':
        return

    async def run():
        async with AvatarDownloader(Config.DB_PATH, save_dir=save_dir) as dl:
            await dl.import_existing_to_db()

    asyncio.run(run())




def view_photo_history():
    """View photo change history for a user."""
    username = input("\nEnter GitHub username: ").strip()
    if not username:
        print("❌ Username required")
        return
    
    async def get_history():
        tracker = ProfilePhotoTracker(Config.DB_PATH)
        async with tracker:
            history = await tracker.get_photo_history(username)
            
            if not history:
                print(f"\n📭 No photo history for {username}")
                return
            
            print(f"\n📸 Photo history for {username}:")
            print(f"   Total changes: {len(history)}")
            print()
            
            for i, record in enumerate(history, 1):
                print(f"   [{i}] {record['detected_at']}")
                print(f"       MD5: {record['md5']}")
                print(f"       pHash: {record['phash']}")
                print()
    
    asyncio.run(get_history())


def track_all_users():
    """Track profile photo changes for all known users."""
    async def run():
        async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
            cursor = await db.execute(
                "SELECT username, user_id, avatar_url FROM users "
                "WHERE user_id IS NOT NULL AND avatar_url IS NOT NULL"
            )
            users = await cursor.fetchall()

        if not users:
            print("\n❌ No users found. Seed and spider first.")
            return

        print(f"\n📸 Tracking photo changes for {len(users):,} users...")
        changed = 0
        errors = 0

        async with ProfilePhotoTracker(Config.DB_PATH) as tracker:
            for i, (username, user_id, avatar_url) in enumerate(users):
                try:
                    if await tracker.track_photo_change(username, user_id, avatar_url):
                        changed += 1
                except Exception:
                    errors += 1

                if (i + 1) % 100 == 0:
                    print(f"   {i+1:,}/{len(users):,} | Changes: {changed:,} | Errors: {errors:,}")

        print(f"\n✅ Done! Changes detected: {changed:,} | Errors: {errors:,}")

    asyncio.run(run())


def start_frontend():
    """Start Flask web server."""
    print("\n🌐 Starting web server...")
    print(f"   URL: http://{Config.FLASK_HOST}:{Config.FLASK_PORT}")
    print("   Press Ctrl+C to stop")
    print()
    
    try:
        run_server()
    except KeyboardInterrupt:
        print("\n✅ Server stopped")


def show_stats():
    """Show database statistics."""
    async def get_data():
        async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
            return await get_stats(db)
    
    stats = asyncio.run(get_data())
    
    print("\n📊 Database Statistics:")
    print(f"   Total users: {stats['total_users']:,}")
    print(f"   Total edges: {stats['total_edges']:,}")
    print(f"   Avatars downloaded: {stats['avatars_downloaded']:,}")
    print()
    print("   Spider status:")
    for status, count in stats.get('spider_status', {}).items():
        print(f"      {status}: {count:,}")
    print()
    print("   Top users by followers:")
    for user in stats.get('top_users', [])[:5]:
        print(f"      {user['username']}: {user['followers']:,} followers")


def verify_integrity():
    """Verify database integrity."""
    reconciler = Reconciler(Config.DB_PATH)
    asyncio.run(reconciler.verify_integrity())


def reset_database():
    """Reset database."""
    confirm = input("\n⚠️  This will DELETE ALL DATA. Are you sure? (type 'yes'): ").strip()
    if confirm != 'yes':
        print("❌ Cancelled")
        return
    
    Config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(init_database(Config.DB_PATH))
    print("✅ Database reset")


def set_pat():
    """Add a GitHub PAT token to the pool."""
    print("\n🔑 GitHub Personal Access Token")
    print("   Required scopes: read:user, user:follow")
    print("   Multiple PATs = higher combined rate limit (5000 req/hr each)")
    print()

    pat = input("Enter PAT token: ").strip()
    if not pat:
        print("❌ Token required")
        return

    pat_manager = PATManager()
    if not pat_manager.validate_pat_format(pat):
        print("⚠️  Warning: Token format doesn't match GitHub PAT pattern")
        if input("Continue anyway? (y/n): ").strip().lower() != 'y':
            return

    pat_manager.store_pat(pat)


def view_pat_status():
    """List all stored PAT tokens."""
    pat_manager = PATManager()
    pats = pat_manager.list_pats()
    if not pats:
        print("\n❌ No PAT tokens configured")
        return
    print(f"\n✅ {len(pats)} PAT token(s) stored:")
    for i, display in enumerate(pats):
        print(f"   [{i}] {display}")


def clear_pat():
    """Remove a PAT from the pool."""
    pat_manager = PATManager()
    pats = pat_manager.list_pats()
    if not pats:
        print("❌ No PATs stored")
        return
    print("\nStored PATs:")
    for i, display in enumerate(pats):
        print(f"   [{i}] {display}")
    idx = input("Enter index to remove (or 'all'): ").strip()
    if idx == 'all':
        if input("⚠️  Remove ALL PATs? (yes/no): ").strip() == 'yes':
            for i in range(len(pats) - 1, -1, -1):
                pat_manager.delete_pat(i)
    elif idx.isdigit():
        pat_manager.delete_pat(int(idx))


def system_menu():
    """System info submenu."""
    while True:
        print("\n" + "=" * 70)
        print("SYSTEM")
        print("=" * 70)
        print()
        print("[1] GitHub API rate limit status")
        print("[2] Config summary")
        print("[3] View log file (last 50 lines)")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()

        if choice == '1':
            check_rate_limit()
        elif choice == '2':
            show_config()
        elif choice == '3':
            view_log()
        elif choice == '0':
            break


def check_rate_limit():
    """Show live GitHub API rate limit."""
    pats = PATManager().load_all_pats()

    async def run():
        async with GitHubAPIClient(pats=pats) as client:
            return await client.get_rate_limit()

    data = asyncio.run(run())

    if not data:
        print("\n❌ Could not fetch rate limit (check network/PAT)")
        return

    import datetime
    for resource, info in data.get('resources', {}).items():
        reset_ts = info.get('reset')
        reset_str = datetime.datetime.fromtimestamp(reset_ts).strftime('%H:%M:%S') if reset_ts else '?'
        print(f"   {resource:<20} {info.get('remaining'):>5}/{info.get('limit'):<6}  resets {reset_str}")


def show_config():
    """Display current configuration."""
    print("\n📋 Current Configuration:")
    print(f"   DB path:           {Config.DB_PATH}")
    print(f"   Avatars dir:       {Config.AVATARS_DIR}")
    print(f"   Log file:          {Config.LOG_FILE}")
    print(f"   Max users:         {Config.GITHUB_MAX_USERS:,}")
    print(f"   Spider depth:      {Config.DEFAULT_SPIDER_DEPTH}")
    print(f"   Concurrency dl:    {Config.MAX_CONCURRENT_DOWNLOADS}")
    print(f"   Concurrency API:   {Config.MAX_CONCURRENT_API_REQUESTS}")
    print(f"   Avatar size:       {Config.AVATAR_SIZE}px")
    print(f"   DL delay:          {Config.AVATAR_DOWNLOAD_DELAY}s")
    print(f"   API delay:         {Config.API_REQUEST_DELAY}s")
    print(f"   Rate limit buffer: {Config.API_RATE_LIMIT_BUFFER}")
    print(f"   Flask:             http://{Config.FLASK_HOST}:{Config.FLASK_PORT}")
    pm = PATManager()
    all_pats = pm.load_all_pats()
    if all_pats:
        print(f"   PATs:              {len(all_pats)} configured ({', '.join(pm.list_pats())})")
    else:
        print(f"   PATs:              ❌ not configured")


def view_log():
    """Show last 50 lines of the log file."""
    log_path = Config.LOG_FILE
    if not log_path.exists():
        print(f"\n📭 No log file at {log_path}")
        print("   The log file is created automatically when the toolkit writes to it.")
        return

    try:
        lines = log_path.read_text(encoding='utf-8').splitlines()
        tail = lines[-50:] if len(lines) > 50 else lines
        print(f"\n📄 Log: {log_path}  (last {len(tail)} of {len(lines)} lines)")
        print("-" * 70)
        for line in tail:
            print(line)
        print("-" * 70)
    except Exception as e:
        print(f"❌ Could not read log: {e}")


def _make_contrib_spider() -> ContributionSpider:
    return ContributionSpider(Config.DB_PATH, pats=PATManager().load_all_pats())


def spider_user_repos():
    """Spider repos for a specific user."""
    username = input("\nGitHub username: ").strip()
    if not username:
        return
    asyncio.run(_make_contrib_spider().spider_user_repos(username))


def spider_all_repos():
    """Spider repos for all completed spider users."""
    batch_size = input("Batch size (default 20): ").strip()
    batch_size = int(batch_size) if batch_size else 20
    asyncio.run(_make_contrib_spider().spider_all_users_repos(batch_size))


def spider_repo_contributors():
    """Add co-contributor edges for one repo."""
    repo = input("\nRepo (e.g. torvalds/linux): ").strip()
    if '/' not in repo:
        print("❌ Format: owner/repo")
        return
    owner, name = repo.split('/', 1)
    asyncio.run(_make_contrib_spider().spider_repo_contributors(owner, name))


def cohort_analysis():
    """Analyze connections within a cohort (email domain or location)."""
    print("\nCohort filter:")
    print("[1] Email domain  [2] Location")
    kind = input("Choice: ").strip()

    if kind == '1':
        val = input("Email domain (e.g. uts.edu.au): ").strip()
        kwargs = {'email_domain': val}
    elif kind == '2':
        val = input("Location keyword: ").strip()
        kwargs = {'location': val}
    else:
        return

    from src.database import search_users as db_search

    async def run():
        async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
            members = await db_search(db, **kwargs, limit=500)
            if not members:
                print("   No cohort members found.")
                return

            usernames = {m['username'] for m in members}
            print(f"\n   Cohort size: {len(members)}")

            # Find internal edges
            placeholders = ','.join('?' * len(usernames))
            un_list = list(usernames)
            cursor = await db.execute(f"""
                SELECT source_username, target_username, edge_type
                FROM graph_edges
                WHERE source_username IN ({placeholders})
                  AND target_username IN ({placeholders})
            """, un_list + un_list)
            edges = await cursor.fetchall()

            # Connection scores per member
            scores = {}
            for src, tgt, etype in edges:
                scores[src] = scores.get(src, 0) + 1
                scores[tgt] = scores.get(tgt, 0) + 1

            print(f"   Internal connections: {len(edges)}")
            print(f"\n   {'Username':<20} {'Email':<30} Connections")
            print("   " + "-" * 65)
            ranked = sorted(members, key=lambda m: scores.get(m['username'], 0), reverse=True)
            for m in ranked[:30]:
                conns = scores.get(m['username'], 0)
                print(f"   {m['username']:<20} {(m['email'] or ''):<30} {conns}")

    asyncio.run(run())


def search_menu():
    """Search users submenu."""
    while True:
        print("\n" + "=" * 70)
        print("SEARCH USERS")
        print("=" * 70)
        print()
        print("[1] Full-text search (bio / username / name)")
        print("[2] Filter by email domain")
        print("[3] Filter by location")
        print("[4] Filter by company")
        print("[5] Export results to CSV")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()
        if choice == '1':
            search_users_fts()
        elif choice == '2':
            search_by_field('email_domain')
        elif choice == '3':
            search_by_field('location')
        elif choice == '4':
            search_by_field('company')
        elif choice == '5':
            export_search_csv()
        elif choice == '0':
            break


def search_users_fts():
    """Full-text search across all user profile fields."""
    query = input("\nSearch query: ").strip()
    if not query:
        return
    _run_search(query=query)


def search_by_field(field: str):
    labels = {'email_domain': 'Email domain (e.g. uts.edu.au)',
              'location': 'Location keyword', 'company': 'Company keyword'}
    val = input(f"\n{labels[field]}: ").strip()
    if not val:
        return
    kwargs = {field: val}
    _run_search(**kwargs)


def _run_search(**kwargs):
    """Run search and print results table."""
    from src.database import search_users as db_search
    async def run():
        async with aiosqlite.connect(Config.DB_PATH, timeout=30) as db:
            return await db_search(db, **kwargs, limit=100)
    results = asyncio.run(run())
    if not results:
        print("   No results found.")
        return
    print(f"\n   Found {len(results)} user(s):")
    print(f"   {'Username':<20} {'Email':<30} {'Location':<20} Followers")
    print("   " + "-" * 80)
    for r in results:
        print(f"   {r['username']:<20} {(r['email'] or ''):<30} {(r['location'] or ''):<20} {r['followers']:,}")
    # Store last results for export
    import json
    _last_search_cache = Config.DATA_DIR / '_last_search.json'
    _last_search_cache.write_text(json.dumps(results, ensure_ascii=False))
    print(f"\n   (Results cached for export)")


def export_search_csv():
    """Export last search results to CSV."""
    import json, csv
    cache = Config.DATA_DIR / '_last_search.json'
    if not cache.exists():
        print("❌ No search results to export. Run a search first.")
        return
    out_path = input("CSV output path (e.g. C:\\Users\\you\\results.csv): ").strip()
    if not out_path:
        return
    results = json.loads(cache.read_text())
    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['username', 'display_name', 'email',
                                                'bio', 'location', 'company',
                                                'followers', 'following', 'avatar_url'])
        writer.writeheader()
        writer.writerows(results)
    print(f"✅ Exported {len(results)} rows to {out_path}")


def contributions_menu():
    """Contributions spider submenu."""
    while True:
        print("\n" + "=" * 70)
        print("CONTRIBUTIONS")
        print("=" * 70)
        print()
        print("[1] Spider repos for a user")
        print("[2] Spider repos for all known users (batch)")
        print("[3] Find co-contributors for a repo")
        print("[4] Cohort analysis (by email domain / location)")
        print("[0] Back")
        print()

        choice = input("Choose an option: ").strip()
        if choice == '1':
            spider_user_repos()
        elif choice == '2':
            spider_all_repos()
        elif choice == '3':
            spider_repo_contributors()
        elif choice == '4':
            cohort_analysis()
        elif choice == '0':
            break


def main():
    """Main CLI entry point."""
    # Initialize database
    asyncio.run(init_database(Config.DB_PATH))
    asyncio.run(reset_in_progress_spiders(Config.DB_PATH))
    
    print_header()
    
    while True:
        print_menu()
        choice = input("Choose an option: ").strip()
        
        if choice == '1':
            social_graph_menu()
        elif choice == '2':
            avatar_downloads_menu()
        elif choice == '3':
            profile_history_menu()
        elif choice == '4':
            search_menu()
        elif choice == '5':
            contributions_menu()
        elif choice == '6':
            frontend_menu()
        elif choice == '7':
            database_menu()
        elif choice == '8':
            authentication_menu()
        elif choice == '9':
            system_menu()
        elif choice == '0':
            print("\n👋 Goodbye!")
            sys.exit(0)
        else:
            print("\n❌ Invalid choice")


if __name__ == '__main__':
    main()
