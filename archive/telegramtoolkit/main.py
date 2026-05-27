#!/usr/bin/env python3
import asyncio
import os
import sys
import logging
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
for _logger_name in ('telethon', 'telethon.network', 'telethon.client.updates', 'asyncio'):
    logging.getLogger(_logger_name).setLevel(logging.ERROR)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.core.console import configure_console_output

configure_console_output()

from src.managers.join_groups import GroupJoiner
from src.managers.leave_groups import GroupCleaner
from src.managers.download_profile_photos import ProfilePhotoDownloader
from src.managers.manage_download_state import DownloadStateManager
from src.managers.account_manager import AccountManager
from src.managers.send_photos import PhotoSender, collect_send_photos_inputs, verify_and_get_accounts
from src.core.parallel_processor import TelegramParallelProcessor, AccountManager as ParallelAccountManager
from src.core.login_verifier import verify_accounts_for_feature
from src.core.progress_logger import log_start, log_step, log_info, log_success, log_error, log_warning, log_complete
from src.core.resilience import atomic_json_write
from src.core.state_manager import get_state_manager, shutdown_state_manager, ensure_database_exists
from src.core.feature_registry import build_processors
from src.core.dynamic_config import get_config_value
from src.runners.multi_platform_links import MultiPlatformLinkCollector
# Unified Orchestrator imports
from src.core.message_orchestrator import MessageOrchestrator


# ============================================================================
# INPUT VALIDATION HELPERS
# ============================================================================

def validate_numeric_input(prompt: str, min_val: int, max_val: int, allow_empty: bool = False) -> Optional[int]:
    """
    Validate numeric input within a specified range.
    
    Args:
        prompt: The prompt to display to the user
        min_val: Minimum acceptable value (inclusive)
        max_val: Maximum acceptable value (inclusive)
        allow_empty: If True, empty input returns None
    
    Returns:
        Validated integer value or None if empty and allow_empty=True
    """
    while True:
        user_input = input(prompt).strip()
        
        if allow_empty and user_input == '':
            return None
        
        try:
            value = int(user_input)
            if min_val <= value <= max_val:
                return value
            else:
                print(f"❌ Please enter a number between {min_val} and {max_val}")
        except ValueError:
            print(f"❌ Invalid input. Please enter a number.")


def validate_choice(prompt: str, valid_choices: list) -> str:
    """
    Validate input against a list of valid choices.
    
    Args:
        prompt: The prompt to display to the user
        valid_choices: List of valid string choices
    
    Returns:
        Validated choice from the list
    """
    while True:
        user_input = input(prompt).strip()
        
        if user_input in valid_choices:
            return user_input
        else:
            print(f"❌ Invalid choice. Valid options: {', '.join(valid_choices)}")


def validate_path_input(prompt: str, must_exist: bool = False, default: str = None) -> str:
    """
    Validate file/directory path input.
    
    Args:
        prompt: The prompt to display to the user
        must_exist: If True, path must exist
        default: Default value if input is empty
    
    Returns:
        Validated path string
    """
    while True:
        user_input = input(prompt).strip()
        
        if user_input == '' and default is not None:
            return default
        
        if user_input == '' and not must_exist:
            return user_input
        
        if must_exist and not os.path.exists(user_input):
            print(f"❌ Path not found: {user_input}")
            if default:
                print(f"   Press Enter to use default: {default}")
            continue
        
        return user_input


# ============================================================================
# MAIN TOOLKIT CLASS
# ============================================================================

class TelegramToolkit:
    def __init__(self):
        self.base_dir = Path(__file__).parent
        
        # Ensure database exists and is healthy (sync version)
        try:
            # Try to get existing event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If loop is running, schedule as a task
                loop.create_task(ensure_database_exists())
            else:
                # If loop exists but not running, use it
                loop.run_until_complete(ensure_database_exists())
        except RuntimeError:
            # No event loop, create one
            asyncio.run(ensure_database_exists())
        
        self.parallel_processor = TelegramParallelProcessor(
            max_concurrent_per_account=3,
            max_total_concurrent=10,
            delay_between_batches=1.0,
            min_delay_per_chat=0.5
        )
        self._stats_cache = None
        self._stats_cache_time = 0
    
    def invalidate_stats_cache(self):
        """Invalidate the stats cache to force refresh on next show_stats() call."""
        self._stats_cache = None
        self._stats_cache_time = 0

    @staticmethod
    def should_auto_export_csv() -> bool:
        """Whether to auto-export CSV artifacts after DB-backed scans."""
        return bool(get_config_value("AUTO_EXPORT_ANALYSIS_CSV", False))
    
    async def verify_and_gate_accounts(self, feature_name: str) -> bool:
        log_step(f"Verifying account logins before: {feature_name}")
        all_valid, valid_accounts = await verify_accounts_for_feature(feature_name)
        
        if not valid_accounts:
            log_error(f"No working accounts available for '{feature_name}'. Please fix account issues and try again.")
            return False
        
        if not all_valid:
            log_warning(f"Some accounts failed verification — proceeding with {len(valid_accounts)} working account(s)")
        else:
            log_success(f"All accounts verified for '{feature_name}'")
        
        return True
    
    def select_accounts(self, task_name: str = "processing") -> tuple[Optional[list[str]], Optional[list[dict[str, Any]]]]:
        """Shared account selection menu. Returns (account_names, account_dicts) or (None, None) on failure."""
        available_accounts = ParallelAccountManager.get_available_accounts()
        
        if not available_accounts:
            print("❌ No accounts found! Please run Account Manager first.")
            return None, None
        
        print(f"👥 Found {len(available_accounts)} accounts for {task_name}")
        
        print("\n⚙️ Parallel Processing Options:")
        print("1. Use all available accounts (maximum speed)")
        print("2. Use optimal number of accounts (recommended)")
        print("3. Choose specific number of accounts")
        print("4. Use single account (traditional mode)")
        
        choice = validate_choice("Choose option (1-4): ", ["1", "2", "3", "4"])
        
        if choice == "1":
            selected = available_accounts
        elif choice == "2":
            optimal = min(len(available_accounts), 5)
            selected = available_accounts[:optimal]
        elif choice == "3":
            max_accts = min(len(available_accounts), 8)
            num = validate_numeric_input(f"Enter number of accounts to use (1-{max_accts}): ", 1, max_accts)
            selected = available_accounts[:num]
        else:
            selected = available_accounts[:1]
        
        print(f"🚀 Using {len(selected)} accounts: {', '.join(selected)}")
        
        account_dicts = ParallelAccountManager.get_accounts_by_names(selected)
        if not account_dicts:
            print("❌ Failed to load account data! Check your configuration.")
            return None, None
        
        return selected, account_dicts
        
    def show_menu(self):
        """Display main menu"""
        print("\n" + "="*60)
        print("   UNIFIED TELEGRAM TOOLKIT")
        print("="*60)
        
        print("\n  🔥 CORE OPERATIONS")
        print("  ─────────────────────────────────────────────")
        print("  1.  Unified Scan (Fastest!)")
        print("      Extracts users, links, and media simultaneously.")
        print("  2.  Join Groups")
        print("      Auto-join groups from collected links.")
        print("  3.  Leave Groups")
        print("      Bulk leave groups you no longer need.")
        
        print("\n  📥 TARGETED EXTRACTION")
        print("  ─────────────────────────────────────────────")
        print("  4.  Download Media Only")
        print("  5.  Analyze Users Only")
        print("  6.  Collect Telegram Links Only")
        print("  7.  Collect Multi-Platform Links")
        print("  8.  Download Profile Photos")
        print("  9.  Send Photos to Chat")
        
        print("\n  📊 VISUALIZATION & DATA")
        print("  ─────────────────────────────────────────────")
        print("  10. Open Dashboard")
        print("  11. Open Visualizer")
        print("  12. Data Export")
        
        print("\n  ⚙️  TOOLS & MANAGEMENT")
        print("  ─────────────────────────────────────────────")
        print("  13. Account Manager")
        print("  14. Manage Tracking / Reset State")
        print("  15. Backup Deleted Messages")
        print("  16. Resend Backed-up Messages")
        
        print("\n  💡 TIP: Run 'python main.py pipeline' for full automated sequence")
        print("\n  0.  Exit")
        print("="*60)
        
    def show_stats(self):
        """Show current data statistics from SQLite (cached 30s)."""
        import time as _time
        try:
            now = _time.time()
            if self._stats_cache and (now - self._stats_cache_time) < 30:
                stats = self._stats_cache
            else:
                state = get_state_manager()
                stats = {
                    'links': state.get_link_count(),
                    'users': state.get_user_count(),
                    'memberships': state.get_membership_count(),
                }

                # joined_links has no SQLite equivalent yet — fall back to file
                joined_file = self.base_dir / "data" / "joined_links.txt"
                if joined_file.exists():
                    with open(joined_file, 'r', encoding='utf-8') as f:
                        stats['joined'] = sum(1 for _ in f)
                else:
                    stats['joined'] = 0

                # Count downloaded files on disk
                downloads_dir = self.base_dir / "downloads"
                downloaded_files = 0
                if downloads_dir.exists():
                    for account_dir in downloads_dir.iterdir():
                        if account_dir.is_dir():
                            for group_dir in account_dir.iterdir():
                                if group_dir.is_dir():
                                    downloaded_files += sum(1 for _ in group_dir.glob("*"))
                stats['downloaded_files'] = downloaded_files

                self._stats_cache = stats
                self._stats_cache_time = now

            print("\n📊 CURRENT DATA STATS:")
            print(f"🔗 Links Collected: {stats['links']:,}")
            print(f"✅ Groups Joined: {stats['joined']:,}")
            print(f"👥 Users Analyzed: {stats['users']:,}")
            print(f"🔗 Memberships: {stats['memberships']:,}")
            print(f"📁 Files Downloaded: {stats['downloaded_files']:,}")

        except Exception as e:
            print(f"❌ Error reading stats: {e}")

    def show_policy_summary(self, option_name: str):
        print("\n📜 POLICY SUMMARY")
        if option_name == "collect_links":
            print("• For channels, scan linked discussion groups instead of channel feeds")
            print("• Skip channels without linked discussion groups")
            print("• If auto-joined discussion group, leave after scan; keep existing memberships")
            print("• Deduplicate by chat so same target is not scanned twice")
        elif option_name == "join_groups":
            print("• Channel links must have a usable linked discussion group")
            print("• If no discussion group can be joined, channel join is rolled back")
            print("• Discussion groups discovered from channels are recorded")
        elif option_name == "download_media":
            print("• For channels, download from linked discussion groups only")
            print("• Skip channels without linked discussion groups")
            print("• If auto-joined discussion group, leave after download; keep existing memberships")
            print("• Deduplicate chat targets to avoid duplicate media scraping")
        elif option_name == "analyze_users":
            print("• Analyze linked discussion groups for channels")
            print("• Temporarily join only when needed")
            print("• Leave only if this run auto-joined; keep existing memberships")
            print("• Keep channel membership persistent while collecting discussion users")
        print("=" * 60)

    @staticmethod
    def _resolve_csv(data_dir: Path, *candidates: str) -> Path:
        """Return the first existing CSV file from candidates, or the last candidate as fallback."""
        for name in candidates:
            p = data_dir / name
            if p.exists():
                return p
        return data_dir / candidates[-1]

    def build_unified_orchestrator(
        self,
        processor_keys: Optional[list[str]] = None,
        *,
        runtime_options_by_key: Optional[dict[str, dict[str, Any]]] = None,
    ) -> MessageOrchestrator:
        """
        Build a unified orchestrator from the central processor registry.

        This keeps targeted feature runs and the all-features unified mode wired
        to the exact same processor implementations.
        """
        orchestrator = MessageOrchestrator()
        for processor in build_processors(
            processor_keys,
            runtime_options_by_key=runtime_options_by_key,
        ):
            orchestrator.register_processor(processor)
        return orchestrator
    
    async def collect_links(self):
        """Run link collection with unified orchestrator"""
        log_start("Unified Link Collection")
        self.show_policy_summary("collect_links")
        
        if not await self.verify_and_gate_accounts("Link Collection"):
            return
        
        _, account_dicts = self.select_accounts("link collection")
        if not account_dicts:
            return
        
        orchestrator = self.build_unified_orchestrator(["links"])
        
        await orchestrator.run(account_dicts)
        log_complete("Link Collection finished")
        self.invalidate_stats_cache()  # Refresh stats after operation
        
    async def join_groups(self):
        """Run group joining"""
        log_start("Group Joining")
        self.show_policy_summary("join_groups")
        
        if not await self.verify_and_gate_accounts("Group Joining"):
            return
        
        links_file = self.base_dir / "data" / "collected_links.txt"
        if not links_file.exists():
            log_error("No links file found. Please run 'Collect Links' first.")
            return
            
        valid_links_file = self.base_dir / "data" / "valid_links.txt"
        if not valid_links_file.exists():
            import shutil
            shutil.copy(links_file, valid_links_file)
            log_info(f"Copied {links_file} to {valid_links_file}")
        
        _, account_dicts = self.select_accounts("group joining")
        if not account_dicts:
            return
            
        joiner = GroupJoiner()
        joiner.selected_accounts = account_dicts
        await joiner.join_groups()
        
        log_complete("Group Joining finished")
        self.invalidate_stats_cache()  # Refresh stats after operation
        
    async def leave_groups(self):
        """Run group cleanup"""
        log_start("Group Cleanup")
        
        if not await self.verify_and_gate_accounts("Group Cleanup"):
            return
            
        _, account_dicts = self.select_accounts("group cleanup")
        if not account_dicts:
            return
        
        cleaner = GroupCleaner()
        cleaner.selected_accounts = account_dicts
        await cleaner.run()
        
        log_complete("Group Cleanup finished")
    
    async def collect_multi_platform_links(self):
        """Collect multi-platform links from all accounts"""
        log_start("Multi-Platform Link Collection")
        
        if not await self.verify_and_gate_accounts("Multi-Platform Link Collection"):
            return
            
        _, account_dicts = self.select_accounts("multi-platform link collection")
        if not account_dicts:
            return
        
        # Initialize link collector
        collector = MultiPlatformLinkCollector(parallel_processor=self.parallel_processor)
        await collector.collect_all_multi_platform_links(accounts=account_dicts)
        
        log_complete("Multi-Platform Link Collection finished")
        
    async def download_media(self):
        """Run media download with unified orchestrator"""
        log_start("Unified Media Download")
        self.show_policy_summary("download_media")
        
        if not await self.verify_and_gate_accounts("Media Download"):
            return
        
        _, account_dicts = self.select_accounts("media download")
        if not account_dicts:
            return
        
        # Get download directory from user
        print("\n📁 Please specify where to save media:")
        download_path = validate_path_input("Enter download directory path (default: downloads): ", must_exist=False, default="downloads")
        if not download_path:
            download_path = "downloads"
        
        orchestrator = self.build_unified_orchestrator(
            ["media"],
            runtime_options_by_key={"media": {"save_path": download_path}},
        )
        
        await orchestrator.run(account_dicts)
        log_complete("Media Download finished")
        self.invalidate_stats_cache()  # Refresh stats after operation
        
    async def analyze_users(self):
        """Run user analysis with unified orchestrator"""
        log_start("Unified User Analysis")
        self.show_policy_summary("analyze_users")
        
        if not await self.verify_and_gate_accounts("User Analysis"):
            return
        
        _, account_dicts = self.select_accounts("user analysis")
        if not account_dicts:
            return
        
        orchestrator = self.build_unified_orchestrator(["users"])
        
        await orchestrator.run(account_dicts)
        
        # Optional CSV export (DB remains source of truth)
        if self.should_auto_export_csv():
            get_state_manager().export_all_to_csv(str(self.base_dir / "data"))
            log_info("CSV export complete (AUTO_EXPORT_ANALYSIS_CSV enabled)")
        else:
            log_info("Skipping CSV export (set AUTO_EXPORT_ANALYSIS_CSV=true to enable)")
        
        self.generate_web_indices()
        log_complete("User Analysis finished")
        self.invalidate_stats_cache()  # Refresh stats after operation
    
    async def scan_all_features(self):
        """Run unified scanning for all features simultaneously (greedy approach)"""
        log_start("Unified All-Features Scan (Greedy)")
        
        if not await self.verify_and_gate_accounts("Unified All-Features Scan"):
            return
        
        _, account_dicts = self.select_accounts("unified scanning")
        if not account_dicts:
            return
            
        # Get download directory from user for media
        print("\n📁 Please specify where to save media:")
        download_path = validate_path_input("Enter download directory path (default: downloads): ", must_exist=False, default="downloads")
        if not download_path:
            download_path = "downloads"
        
        orchestrator = self.build_unified_orchestrator(
            runtime_options_by_key={"media": {"save_path": download_path}},
        )
        
        await orchestrator.run(account_dicts, unified_mode=True)
        
        # Optional CSV export (DB remains source of truth)
        if self.should_auto_export_csv():
            get_state_manager().export_all_to_csv(str(self.base_dir / "data"))
            log_info("CSV export complete (AUTO_EXPORT_ANALYSIS_CSV enabled)")
        else:
            log_info("Skipping CSV export (set AUTO_EXPORT_ANALYSIS_CSV=true to enable)")
        
        self.generate_web_indices()
        log_complete("Unified All-Features Scan finished")
        self.invalidate_stats_cache()  # Refresh stats after operation
    
    async def download_profile_photos(self):
        """Download profile photos using DB-backed users with parallel processing"""
        log_start("Parallel Profile Photo Download")
        
        if not await self.verify_and_gate_accounts("Profile Photo Download"):
            return
        
        user_count = get_state_manager().get_user_count()
        if user_count <= 0:
            log_error("No users found in database! Please run user analysis first.")
            return
            
        _, account_dicts = self.select_accounts("profile photo download")
        if not account_dicts:
            return
            
        # Get download directory from user
        print("\n📁 Please specify where to save profile photos:")
        download_path = validate_path_input("Enter download directory path: ", must_exist=False, default=None)
        
        if not download_path:
            print("❌ Error: Download directory is required!")
            return
        
        # Expand user path and make absolute
        download_path = os.path.abspath(os.path.expanduser(download_path))
        
        # Create directory if it doesn't exist
        try:
            os.makedirs(download_path, exist_ok=True)
            print(f"✅ Download directory set to: {download_path}")
        except Exception as e:
            print(f"❌ Error creating directory: {e}")
            return
            
        downloader = ProfilePhotoDownloader(
            download_path, 
            parallel_processor=self.parallel_processor
        )
        
        print("⚡ Starting profile photo download...")
        await downloader.download_all_profile_photos_parallel(account_dicts)
        
        log_complete("Profile Photo Download finished")
        self.invalidate_stats_cache()  # Refresh stats after operation
        
    def start_web_server(self):
        """Start HTTP server for dashboard and visualizer"""
        import threading
        import http.server
        import socketserver
        import time
        import socket
        
        def is_port_in_use(port: int) -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                return s.connect_ex(('localhost', int(port))) == 0
        
        port = 8000
        if is_port_in_use(port):
            print(f"✅ Server already running on http://localhost:{port}")
            return port
            
        base_dir_str = str(self.base_dir)
        
        BLOCKED_PATTERNS = ('.env', 'sessions/', '.git/', 'config.py', '.session', '.db', '__pycache__', '.pyc', '.backup')

        class CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, directory=base_dir_str, **kwargs)
            def do_GET(self):
                # Block access to sensitive files
                path_lower = self.path.lower()
                for pattern in BLOCKED_PATTERNS:
                    if pattern in path_lower:
                        self.send_error(403, "Forbidden")
                        return
                super().do_GET()
            def end_headers(self):
                self.send_header('Access-Control-Allow-Origin', 'http://localhost:8000')
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                super().end_headers()

        def run_server():
            with socketserver.TCPServer(("127.0.0.1", port), CORSRequestHandler) as httpd:
                print(f"🚀 Started HTTP server on http://localhost:{port}")
                httpd.serve_forever()
        
        # Start server in background thread
        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        time.sleep(2)  # Give server time to start
        return port
        
    def _open_web_tool(self, html_file: str, label: str):
        """Open a web tool (dashboard/visualizer) via local HTTP server"""
        path = self.base_dir / "web" / html_file
        if not path.exists():
            print(f"❌ {label} file not found!")
            return
        try:
            port = self.start_web_server()
            import webbrowser
            url = f"http://localhost:{port}/web/{html_file}"
            webbrowser.open(url)
            print(f"🌐 Opening {label} in web browser...")
            print(f"🔗 URL: {url}")
            print("📄 CSV files will now load correctly!")
            input(f"\n📌 Press Enter when you're done with the {label}...")
        except Exception as e:
            print(f"❌ Error opening {label}: {e}")
            print(f"📁 Try manually: Open {html_file} in your browser")

    def open_dashboard(self):
        """Open web dashboard with server"""
        self._open_web_tool("enhanced_dashboard.html", "dashboard")

    def open_visualizer(self):
        """Open network visualizer with server"""
        self._open_web_tool("visualize.html", "network visualizer")
            
    async def run_full_pipeline(self):
        """Run complete pipeline"""
        log_start("Full Pipeline")
        
        if not await self.verify_and_gate_accounts("Full Pipeline"):
            return
        
        print("\n" + "="*50)
        print("STEP 1/4: COLLECTING LINKS")
        print("="*50)
        await self.collect_links()
        
        print("\n" + "="*50)
        print("STEP 2/4: JOINING GROUPS")
        print("="*50)
        await self.join_groups()
        
        print("\n" + "="*50)
        print("STEP 3/4: DOWNLOADING MEDIA")
        print("="*50)
        await self.download_media()
        
        print("\n" + "="*50)
        print("STEP 4/4: ANALYZING USERS")
        print("="*50)
        await self.analyze_users()
        
        log_complete("Full Pipeline finished")
        print("📊 Opening dashboard for results...")
        self.open_dashboard()
        
    async def send_photos(self):
        """Launch photo sender"""
        try:
            print("\n📸 Launching Photo Sender...")
            # Verify accounts first
            all_valid, valid_accounts = await verify_accounts_for_feature("Photo Sender")
            
            if not valid_accounts:
                print("❌ No valid accounts available. Please check your sessions.")
                return
                
            request = collect_send_photos_inputs(valid_accounts)
            if request is None:
                return

            sender = PhotoSender()
            await sender.send_photos(
                request['accounts'],
                request['directory'],
                request['chat_id'],
                request['delete_after'],
                delete_skipped_already_sent=request['delete_skipped_already_sent'],
            )
                
        except Exception as e:
            print(f"❌ Error launching photo sender: {e}")
        
    async def manage_accounts(self):
        """Launch account manager"""
        try:
            print("\n👥 Launching Account Manager...")
            manager = AccountManager()
            await manager.show_menu()
            
            # After account management, suggest restarting
            print("\n💡 If you added/removed accounts, consider restarting the toolkit")
            print("   to ensure all features use the updated account configuration.")
            
        except Exception as e:
            print(f"❌ Error launching account manager: {e}")

    async def manage_download_state(self):
        """Launch download state manager"""
        try:
            print("\n🔧 Launching Download State Manager...")
            manager = DownloadStateManager()
            manager.show_menu()
        except Exception as e:
            print(f"❌ Error launching state manager: {e}")
    
    def export_data(self):
        """Export data in various formats"""
        print("\n📤 Data Export Options")
        print("1. Export to JSON")
        print("2. Export to Excel")
        print("3. Generate summary report")
        
        choice = validate_choice("Choose option (1-3): ", ["1", "2", "3"])
        
        if choice == "1":
            self.export_to_json()
        elif choice == "2":
            self.export_to_excel()
        elif choice == "3":
            self.generate_report()
    
    def export_to_json(self):
        """Export all data to JSON format"""
        import json
        import csv
        
        try:
            data_dir = self.base_dir / "data"
            export_file = data_dir / "telegram_data_export.json"
            users_file = self._resolve_csv(data_dir, "users.csv", "Users.csv")
            memberships_file = self._resolve_csv(data_dir, "memberships.csv", "Memberships.csv")

            with open(export_file, 'w', encoding='utf-8') as f:
                f.write('{\n')
                f.write('  "users": [\n')
                users_count = 0
                if users_file.exists():
                    with open(users_file, 'r', encoding='utf-8') as users_fp:
                        reader = csv.DictReader(users_fp)
                        first = True
                        for row in reader:
                            if not first:
                                f.write(',\n')
                            f.write('    ')
                            json.dump(row, f, ensure_ascii=False)
                            first = False
                            users_count += 1
                f.write('\n  ],\n')
                f.write('  "memberships": [\n')
                memberships_count = 0
                if memberships_file.exists():
                    with open(memberships_file, 'r', encoding='utf-8') as memberships_fp:
                        reader = csv.DictReader(memberships_fp)
                        first = True
                        for row in reader:
                            if not first:
                                f.write(',\n')
                            f.write('    ')
                            json.dump(row, f, ensure_ascii=False)
                            first = False
                            memberships_count += 1
                f.write('\n  ]\n')
                f.write('}\n')
            
            print(f"✅ Data exported to: {export_file}")
            print(f"✅ Users rows: {users_count}, Membership rows: {memberships_count}")
            self.generate_web_indices()
            
        except Exception as e:
            print(f"❌ Export failed: {e}")
    
    def export_to_excel(self):
        """Export data to Excel format"""
        try:
            import pandas as pd  # type: ignore[import-untyped]
            
            data_dir = self.base_dir / "data"
            users_file = self._resolve_csv(data_dir, "users.csv", "Users.csv")
            memberships_file = self._resolve_csv(data_dir, "memberships.csv", "Memberships.csv")
            
            with pd.ExcelWriter(data_dir / "telegram_data_export.xlsx") as writer:
                if users_file.exists():
                    start_row = 0
                    for idx, users_df in enumerate(pd.read_csv(users_file, chunksize=20000)):
                        users_df.to_excel(
                            writer,
                            sheet_name='Users',
                            index=False,
                            header=(idx == 0),
                            startrow=start_row
                        )
                        start_row += len(users_df) + (1 if idx == 0 else 0)
                
                if memberships_file.exists():
                    start_row = 0
                    for idx, memberships_df in enumerate(pd.read_csv(memberships_file, chunksize=20000)):
                        memberships_df.to_excel(
                            writer,
                            sheet_name='Memberships',
                            index=False,
                            header=(idx == 0),
                            startrow=start_row
                        )
                        start_row += len(memberships_df) + (1 if idx == 0 else 0)
            
            print(f"✅ Excel export saved to: {data_dir / 'telegram_data_export.xlsx'}")
            
        except ImportError:
            print("❌ pandas not installed. Install with: pip install pandas openpyxl")
        except Exception as e:
            print(f"❌ Excel export failed: {e}")
    
    def generate_report(self):
        """Generate summary report"""
        try:
            self.show_stats()
            print("📋 Basic stats displayed above")
            
        except Exception as e:
            print(f"❌ Report generation failed: {e}")

    _USER_OPTIONAL_KEYS = (
        'current_name', 'historical_usernames', 'historical_names',
        'username_history_count', 'name_history_count',
        'has_username_changes', 'has_name_changes', 'total_changes',
        'last_seen', 'first_seen', 'last_username_change', 'last_name_change',
    )

    @staticmethod
    def _parse_user_row(row: dict) -> tuple[str, dict]:
        """Parse a CSV row into (user_id, user_dict). Returns ('', {}) if invalid."""
        user_id = str(row.get('user_id', '')).strip()
        if not user_id:
            return '', {}
        user_row = {
            'user_id': user_id,
            'username': (row.get('username') or '').strip(),
            'first_name': (row.get('first_name') or '').strip(),
            'last_name': (row.get('last_name') or '').strip(),
            'phone': (row.get('phone') or '').strip(),
            'is_bot': (row.get('is_bot') or '').strip(),
            'is_verified': (row.get('is_verified') or '').strip(),
            'is_premium': (row.get('is_premium') or '').strip(),
        }
        for key in TelegramToolkit._USER_OPTIONAL_KEYS:
            if key in row:
                user_row[key] = (row.get(key) or '').strip()
        return user_id, user_row

    @staticmethod
    def _parse_membership_row(row: dict) -> tuple[str, str, dict]:
        """Parse a CSV row into (user_id, group_name, membership_dict). Returns ('','',{}) if invalid."""
        user_id = str(row.get('user_id', '')).strip()
        group_name = (row.get('group_name') or '').strip()
        if not user_id or not group_name:
            return '', '', {}
        return user_id, group_name, {
            'user_id': user_id,
            'group_name': group_name,
            'group_id': str(row.get('group_id', '')).strip() or 'unknown',
            'account': (row.get('account') or 'unknown').strip() or 'unknown',
        }

    def generate_web_indices(self):
        """Generate compact JSON indices for dashboard and visualizer.

        Splits work into two passes so only one output's data is in memory at a time.
        """
        try:
            data_dir = self.base_dir / "data"
            state = get_state_manager()

            # Pass 1: dashboard_index.json
            users = []
            users_cursor = state.conn.execute(
                """
                SELECT
                    user_id, username, first_name, last_name, phone,
                    is_bot, is_verified, is_premium,
                    last_seen
                FROM users
                ORDER BY user_id
                """
            )
            for row in users_cursor:
                user_row = {
                    'user_id': str(row['user_id']),
                    'username': row['username'] or '',
                    'first_name': row['first_name'] or '',
                    'last_name': row['last_name'] or '',
                    'phone': row['phone'] or '',
                    'is_bot': str(row['is_bot'] if row['is_bot'] is not None else 0),
                    'is_verified': str(row['is_verified'] if row['is_verified'] is not None else 0),
                    'is_premium': str(row['is_premium'] if row['is_premium'] is not None else 0),
                    'last_seen': row['last_seen'] or '',
                    'current_name': '',
                    'historical_usernames': '',
                    'historical_names': '',
                    'username_history_count': '',
                    'name_history_count': '',
                    'has_username_changes': '',
                    'has_name_changes': '',
                    'total_changes': '',
                    'first_seen': '',
                    'last_username_change': '',
                    'last_name_change': '',
                }
                users.append(user_row)

            user_memberships: dict = {}
            group_counts: dict = {}
            memberships_cursor = state.conn.execute(
                """
                SELECT user_id, group_name, group_id
                FROM memberships
                ORDER BY user_id, group_id
                """
            )
            for row in memberships_cursor:
                user_id = str(row['user_id'])
                group_name = (row['group_name'] or '').strip()
                if not user_id or not group_name:
                    continue
                m_row = {
                    'user_id': user_id,
                    'group_name': group_name,
                    'group_id': str(row['group_id'] or 'unknown').strip() or 'unknown',
                    'account': 'unknown',
                }
                user_memberships.setdefault(user_id, []).append(m_row)
                group_key = f"{group_name}|||{m_row['account']}"
                group_counts[group_key] = group_counts.get(group_key, 0) + 1

            dashboard_index_file = data_dir / "dashboard_index.json"
            atomic_json_write(str(dashboard_index_file), {
                'users': users,
                'user_memberships': user_memberships,
                'group_counts': group_counts,
            })
            del users, user_memberships, group_counts
            print(f"✅ Dashboard index saved to: {dashboard_index_file}")

            # Pass 2: visualize_index.json
            users_map: dict = {}
            users_cursor = state.conn.execute(
                """
                SELECT user_id, username, first_name, last_name, is_bot
                FROM users
                ORDER BY user_id
                """
            )
            for row in users_cursor:
                user_id = str(row['user_id'])
                users_map[user_id] = {
                    'username': row['username'] or '',
                    'first_name': row['first_name'] or '',
                    'last_name': row['last_name'] or '',
                    'is_bot': str(row['is_bot'] if row['is_bot'] is not None else 0),
                    'current_name': '',
                    'historical_usernames': '',
                    'historical_names': '',
                }

            memberships_slim: list = []
            memberships_cursor = state.conn.execute(
                """
                SELECT user_id, group_name, group_id
                FROM memberships
                ORDER BY user_id, group_id
                """
            )
            for row in memberships_cursor:
                user_id = str(row['user_id'])
                group_name = (row['group_name'] or '').strip()
                if user_id and group_name:
                    memberships_slim.append(
                        {
                            'user_id': user_id,
                            'group_name': group_name,
                            'group_id': str(row['group_id'] or 'unknown').strip() or 'unknown',
                            'account': 'unknown',
                        }
                    )

            visualize_index_file = data_dir / "visualize_index.json"
            atomic_json_write(str(visualize_index_file), {
                'users': users_map,
                'memberships': memberships_slim,
            })
            print(f"✅ Visualizer index saved to: {visualize_index_file}")
        except Exception as e:
            print(f"⚠️ Failed to build web indices: {e}")

    async def run_backup(self):
        """Launch backup tool"""
        try:
            print("\n💾 Launching Backup Tool...")
            from src.managers.backup import get_user_inputs, initialize_client_and_folder, export_messages
            from src.core.config import ACCOUNTS
            get_user_inputs()
            client = initialize_client_and_folder()
            await client.start(ACCOUNTS[0]['phone'])
            await export_messages(client)
            await client.disconnect()
        except Exception as e:
            print(f"❌ Error launching backup tool: {e}")

    async def run_resender(self):
        """Launch resender tool"""
        try:
            print("\n📤 Launching Resender Tool...")
            from src.managers.resender import main as resender_main
            await resender_main()
        except Exception as e:
            print(f"❌ Error launching resender tool: {e}")

    async def run_interactive(self):
        """Run interactive menu"""
        while True:
            self.show_stats()
            self.show_menu()
            
            choice_num = validate_numeric_input("\nEnter your choice (0-16): ", 0, 16, allow_empty=False)
            choice = str(choice_num) if choice_num is not None else "0"
            
            try:
                if choice == "0":
                    print("👋 Goodbye!")
                    break
                elif choice == "1":
                    await self.scan_all_features()
                elif choice == "2":
                    await self.join_groups()
                elif choice == "3":
                    await self.leave_groups()
                elif choice == "4":
                    await self.download_media()
                elif choice == "5":
                    await self.analyze_users()
                elif choice == "6":
                    await self.collect_links()
                elif choice == "7":
                    await self.collect_multi_platform_links()
                elif choice == "8":
                    await self.download_profile_photos()
                elif choice == "9":
                    await self.send_photos()
                elif choice == "10":
                    self.open_dashboard()
                elif choice == "11":
                    self.open_visualizer()
                elif choice == "12" or choice.lower() in ["export", "data"]:
                    self.export_data()
                elif choice == "13":
                    await self.manage_accounts()
                elif choice == "14" or choice.lower() in ["manage", "state", "download"]:
                    await self.manage_download_state()
                elif choice == "15" or choice.lower() == "backup":
                    await self.run_backup()
                elif choice == "16" or choice.lower() == "resend":
                    await self.run_resender()
                else:
                    print("❌ Invalid choice. Please try again.")
                    
            except KeyboardInterrupt:
                print("\n\n⚠️ Operation interrupted by user")
            except Exception as e:
                print(f"\n❌ Error: {e}")
                
            if choice != "0":
                input("\nPress Enter to continue...")

async def main():
    """Main entry point"""
    try:
        toolkit = TelegramToolkit()
        
        # Check if command line arguments provided
        if len(sys.argv) > 1:
            command = sys.argv[1].lower()
            
            if command in ["unified", "1"]:
                await toolkit.scan_all_features()
            elif command in ["join", "2"]:
                await toolkit.join_groups()
            elif command in ["leave", "3"]:
                await toolkit.leave_groups()
            elif command in ["media", "4"]:
                await toolkit.download_media()
            elif command in ["users", "5"]:
                await toolkit.analyze_users()
            elif command in ["links", "6"]:
                await toolkit.collect_links()
            elif command in ["multi", "external", "7"]:
                await toolkit.collect_multi_platform_links()
            elif command in ["profiles", "8"]:
                await toolkit.download_profile_photos()
            elif command in ["photos", "9"]:
                await toolkit.send_photos()
            elif command in ["dashboard", "10"]:
                toolkit.open_dashboard()
            elif command in ["visualize", "11"]:
                toolkit.open_visualizer()
            elif command in ["export", "data", "12"]:
                toolkit.export_data()
            elif command in ["accounts", "13"]:
                await toolkit.manage_accounts()
            elif command in ["state", "14"]:
                await toolkit.manage_download_state()
            elif command in ["backup", "15"]:
                await toolkit.run_backup()
            elif command in ["resend", "16"]:
                await toolkit.run_resender()
            elif command == "pipeline":
                await toolkit.run_full_pipeline()
            else:
                print(f"❌ Unknown command: {command}")
                print("Available commands: unified (1), join (2), leave (3), media (4), users (5), links (6), multi (7), profiles (8), photos (9), dashboard (10), visualize (11), export (12), accounts (13), state (14), backup (15), resend (16), pipeline")
        else:
            # Interactive mode
            await toolkit.run_interactive()
            
    except KeyboardInterrupt:
        print("\n\n⚠️ Operation interrupted by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Clean shutdown of state manager
        shutdown_state_manager()
        # Disconnect any Telethon clients still active to prevent session file corruption
        for client in getattr(toolkit, '_active_clients', []):
            try:
                await client.disconnect()
            except Exception:
                pass

if __name__ == "__main__":
    import signal as _signal

    def _shutdown_handler(signum, frame):
        print(f"\n[main] Signal {signum} — shutting down cleanly...")
        shutdown_state_manager()
        sys.exit(0)

    for _sig in (
        getattr(_signal, 'SIGTERM', None),
        getattr(_signal, 'SIGBREAK', None),   # Windows console-close
    ):
        if _sig is not None:
            try:
                _signal.signal(_sig, _shutdown_handler)
            except (OSError, ValueError):
                pass

    asyncio.run(main())
