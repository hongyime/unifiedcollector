"""YouTube Toolkit - Main entry point."""
import sys
import os
import signal
import subprocess
from pathlib import Path


def _handle_exit_signal(signum, frame):
    """SIGTERM / SIGBREAK → sys.exit so atexit (WAL checkpoint) fires before OS kills us."""
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_exit_signal)
try:
    signal.signal(signal.SIGBREAK, _handle_exit_signal)  # Windows: bat-window close button
except AttributeError:
    pass

# Add src/ to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent / 'scripts'))

def manage_target_channels(console, questionary):
    """Interactive channel management - select from subscriptions."""
    from app_paths import TARGET_CHANNELS_FILE, SUBSCRIPTIONS_FILE
    from rich.panel import Panel
    import json
    from datetime import datetime
    import webbrowser
    
    # Load existing target channels
    target_channels = []
    if TARGET_CHANNELS_FILE.exists():
        content = TARGET_CHANNELS_FILE.read_text()
        target_channels = [line.strip() for line in content.split('\n') 
                          if line.strip() and not line.strip().startswith('#')]
    
    while True:
        console.clear()
        console.print(Panel.fit(
            "[bold cyan]📺 Target Channels Management[/bold cyan]\n"
            "Select channels from your subscriptions to track",
            border_style="cyan"
        ))
        
        if target_channels:
            console.print("\n[yellow]Currently tracking:[/yellow]")
            for i, ch in enumerate(target_channels[:10], 1):
                # Show just the channel name/ID, not full URL
                display = ch.split('/')[-1] if '/' in ch else ch
                console.print(f"  {i}. {display}")
            if len(target_channels) > 10:
                console.print(f"  ... and {len(target_channels) - 10} more")
        else:
            console.print("\n[yellow]No channels selected yet.[/yellow]")
        
        console.print(f"\n[dim]Total: {len(target_channels)} channels[/dim]")
        
        action = questionary.select(
            "\nWhat would you like to do?",
            choices=[
                "Browse and select from my subscriptions",
                "Add channel manually (URL or ID)",
                "Remove channels",
                "Clear all channels",
                "View all selected channels",
                "Back to main menu"
            ]
        ).ask()
        
        if action == "Browse and select from my subscriptions":
            # Load subscriptions
            subscriptions = load_subscriptions(console)
            if not subscriptions:
                console.print("[red]❌ No subscriptions found. Please scrape subscriptions first (option 2 from main menu).[/red]")
                _safe_pause(console)
                continue
            
            # Let user search/filter
            search = questionary.text(
                "Search channels (leave empty to see all):",
                instruction="Type part of channel name to filter"
            ).ask()
            
            # Filter subscriptions
            if search:
                filtered = [s for s in subscriptions 
                           if search.lower() in s['channel_name'].lower()]
            else:
                filtered = subscriptions
            
            if not filtered:
                console.print(f"[yellow]No channels found matching '{search}'[/yellow]")
                input("\nPress Enter to continue...")
                continue
            
            # Browse mode - show one channel at a time with details
            browse_and_select_channels(console, questionary, filtered, target_channels)
                
        elif action == "Add channel manually (URL or ID)":
            url = questionary.text(
                "Enter channel URL or ID:",
                instruction="(e.g., https://www.youtube.com/@channelname or UCxxxxx)"
            ).ask()
            if url and url.strip():
                url = url.strip()
                if url not in target_channels:
                    target_channels.append(url)
                    console.print(f"[green]✓ Added: {url}[/green]")
                else:
                    console.print(f"[yellow]⚠️  Channel already in list[/yellow]")
            input("\nPress Enter to continue...")
                    
        elif action == "Remove channels":
            if not target_channels:
                console.print("[yellow]No channels to remove[/yellow]")
                input("\nPress Enter to continue...")
                continue
            
            # Show all channels with checkboxes
            choices = []
            for ch in target_channels:
                display = ch.split('/')[-1] if '/' in ch else ch
                choices.append({'name': display, 'value': ch})
            
            to_remove = questionary.checkbox(
                "Select channels to remove (Space to toggle, Enter to confirm):",
                choices=choices
            ).ask()
            
            if to_remove:
                for ch in to_remove:
                    target_channels.remove(ch)
                console.print(f"[green]✓ Removed {len(to_remove)} channels[/green]")
            input("\nPress Enter to continue...")
                
        elif action == "Clear all channels":
            confirm = questionary.confirm(
                f"Are you sure you want to clear all {len(target_channels)} channels?"
            ).ask()
            if confirm:
                target_channels.clear()
                console.print("[green]✓ All channels cleared[/green]")
            input("\nPress Enter to continue...")
        
        elif action == "View all selected channels":
            if not target_channels:
                console.print("[yellow]No channels selected[/yellow]")
            else:
                console.print(f"\n[cyan]All {len(target_channels)} selected channels:[/cyan]")
                for i, ch in enumerate(target_channels, 1):
                    display = ch.split('/')[-1] if '/' in ch else ch
                    console.print(f"  {i}. {display}")
            input("\nPress Enter to continue...")
                
        elif action == "Back to main menu":
            # Save before exiting
            save_channels(TARGET_CHANNELS_FILE, target_channels)
            console.print(f"[green]✓ Saved {len(target_channels)} channels[/green]")
            break

def setup_oauth_credentials(console, questionary):
    """Guide user through OAuth setup."""
    from app_paths import CLIENT_SECRET_FILE, OAUTH_CREDENTIALS_FILE
    from rich.panel import Panel
    import webbrowser
    
    console.clear()
    console.print(Panel.fit(
        "[bold cyan]🔐 OAuth Credentials Setup[/bold cyan]\n"
        "Required for: Liked videos & Subscriptions scraping",
        border_style="cyan"
    ))
    
    # Check if already set up
    if CLIENT_SECRET_FILE.exists():
        console.print("\n[green]✓ OAuth credentials file found![/green]")
        console.print(f"[dim]Location: {CLIENT_SECRET_FILE}[/dim]")
        
        if OAUTH_CREDENTIALS_FILE.exists():
            console.print("[green]✓ You are already signed in![/green]")
            
            action = questionary.select(
                "What would you like to do?",
                choices=[
                    "Test connection (scrape liked videos)",
                    "Sign out and re-authenticate",
                    "Replace client_secret.json with new credentials",
                    "Back to main menu"
                ]
            ).ask()
            
            if action == "Test connection (scrape liked videos)":
                console.print("\n[yellow]Testing OAuth connection...[/yellow]")
                import scrape_liked_videos_enhanced
                scrape_liked_videos_enhanced.main()
                return
            elif action == "Sign out and re-authenticate":
                import logout_account
                logout_account.clear_credentials()
                console.print("\n[green]✓ Signed out. Run option 1 or 2 to sign in again.[/green]")
                return
            elif action == "Replace client_secret.json with new credentials":
                pass  # Continue to setup instructions
            else:
                return
        else:
            console.print("[yellow]⚠️  Not signed in yet.[/yellow]")
            console.print("[cyan]💡 Run option 1 or 2 to sign in (browser will open)[/cyan]")
            input("\nPress Enter to continue...")
            return
    
    # Show setup instructions
    console.print("\n[bold yellow]📋 Setup Instructions:[/bold yellow]\n")
    
    console.print("[cyan]Step 1:[/cyan] Get OAuth credentials from Google Cloud Console")
    console.print("  1. Go to: [link=https://console.cloud.google.com]https://console.cloud.google.com[/link]")
    console.print("  2. Create a new project (or select existing)")
    console.print("  3. Enable 'YouTube Data API v3'")
    console.print("  4. Create OAuth 2.0 credentials")
    console.print("     → Application type: [bold]Desktop app[/bold]")
    console.print("  5. Download the JSON file\n")
    
    console.print("[cyan]Step 2:[/cyan] Save the file")
    console.print(f"  → Save as: [bold]{CLIENT_SECRET_FILE}[/bold]\n")
    
    console.print("[cyan]Step 3:[/cyan] Sign in")
    console.print("  → Run option 1 or 2 from main menu")
    console.print("  → Browser will open for authorization\n")
    
    console.print("[dim]📖 Detailed guide: docs/YOUTUBE_API_SETUP.md[/dim]\n")
    
    action = questionary.select(
        "What would you like to do?",
        choices=[
            "🌐 Open Google Cloud Console in browser",
            "📖 Open detailed setup guide",
            "📁 Open data folder (to place client_secret.json)",
            "✅ I've placed the file, test it now",
            "Back to main menu"
        ]
    ).ask()
    
    if action.startswith("🌐 Open Google Cloud Console"):
        console.print("[cyan]Opening Google Cloud Console...[/cyan]")
        webbrowser.open("https://console.cloud.google.com")
        console.print("[green]✓ Opened in browser[/green]")
        console.print("\n[yellow]After setting up:[/yellow]")
        console.print(f"1. Download the JSON file")
        console.print(f"2. Save it as: {CLIENT_SECRET_FILE}")
        console.print(f"3. Come back and select 'I've placed the file, test it now'")
        
    elif action.startswith("📖 Open detailed"):
        import subprocess
        guide_path = Path(__file__).parent / "docs" / "YOUTUBE_API_SETUP.md"
        if guide_path.exists():
            subprocess.run(['notepad.exe', str(guide_path)])
        else:
            console.print("[yellow]Guide not found. Check README.md for instructions.[/yellow]")
            
    elif action.startswith("📁 Open data folder"):
        import subprocess
        subprocess.run(['explorer.exe', str(CLIENT_SECRET_FILE.parent)])
        console.print(f"[green]✓ Opened: {CLIENT_SECRET_FILE.parent}[/green]")
        console.print(f"\n[yellow]Place your downloaded JSON file here and rename it to:[/yellow]")
        console.print(f"[bold]client_secret.json[/bold]")
        
    elif action.startswith("✅ I've placed"):
        if CLIENT_SECRET_FILE.exists():
            console.print("[green]✓ File found! Testing connection...[/green]")
            console.print("\n[yellow]Your browser will open for authorization...[/yellow]")
            import scrape_liked_videos_enhanced
            scrape_liked_videos_enhanced.main()
        else:
            console.print(f"[red]❌ File not found: {CLIENT_SECRET_FILE}[/red]")
            console.print("\n[yellow]Please place the file and try again.[/yellow]")

def browse_and_select_channels(console, questionary, subscriptions, target_channels):
    """Browse channels one by one with detailed info and selection."""
    import webbrowser
    from rich.table import Table
    from rich.panel import Panel
    
    current_idx = 0
    
    while current_idx < len(subscriptions):
        channel = subscriptions[current_idx]
        channel_url = channel['channel_url']
        is_selected = channel_url in target_channels
        
        console.clear()
        
        # Create info table
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")
        
        table.add_row("Channel Name", f"[bold]{channel['channel_name']}[/bold]")
        table.add_row("Channel ID", channel['channel_id'])
        table.add_row("URL", f"[link={channel_url}]{channel_url}[/link]")
        table.add_row("Status", "[green]✓ SELECTED[/green]" if is_selected else "[dim]Not selected[/dim]")
        
        console.print(Panel(
            table,
            title=f"[bold cyan]Channel {current_idx + 1} of {len(subscriptions)}[/bold cyan]",
            border_style="cyan"
        ))
        
        console.print(f"\n[dim]💡 Tip: Copy the URL above to open in your browser[/dim]")
        
        # Action choices
        choices = [
            "✓ Select this channel" if not is_selected else "✗ Unselect this channel",
            "🌐 Open channel in browser",
            "→ Next channel",
        ]
        
        if current_idx > 0:
            choices.append("← Previous channel")
        
        choices.extend([
            "🔍 Search/filter again",
            "💾 Save and return to menu"
        ])
        
        action = questionary.select(
            "What would you like to do?",
            choices=choices
        ).ask()
        
        if not action:
            break
        
        if action.startswith("✓ Select") or action.startswith("✗ Unselect"):
            if is_selected:
                target_channels.remove(channel_url)
                console.print(f"[yellow]✗ Unselected: {channel['channel_name']}[/yellow]")
            else:
                target_channels.append(channel_url)
                console.print(f"[green]✓ Selected: {channel['channel_name']}[/green]")
            import time
            time.sleep(0.5)
            
        elif action.startswith("🌐 Open"):
            console.print(f"[cyan]Opening {channel_url} in browser...[/cyan]")
            webbrowser.open(channel_url)
            console.print("[green]✓ Opened in browser[/green]")
            input("\nPress Enter to continue...")
            
        elif action.startswith("→ Next"):
            current_idx += 1
            
        elif action.startswith("← Previous"):
            current_idx -= 1
            
        elif action.startswith("🔍 Search"):
            break
            
        elif action.startswith("💾 Save"):
            console.print(f"[green]✓ Currently tracking {len(target_channels)} channels[/green]")
            break

def load_subscriptions(console):
    """Load subscriptions from cache file."""
    from app_paths import SUBSCRIPTIONS_FILE
    import json
    from datetime import datetime
    
    if not SUBSCRIPTIONS_FILE.exists():
        return None
    
    try:
        with open(SUBSCRIPTIONS_FILE, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
            cache_time = datetime.fromisoformat(cache_data['timestamp'])
            age_days = (datetime.now() - cache_time).days
            
            console.print(f"[dim]Loaded {len(cache_data['subscriptions'])} subscriptions (cached {age_days} days ago)[/dim]")
            return cache_data['subscriptions']
    except Exception as e:
        console.print(f"[red]Error loading subscriptions: {e}[/red]")
        return None

def save_channels(filepath, channels):
    """Save channels to file with header."""
    content = """# Target Channels for Scraping
# Add YouTube channel URLs here, one per line
# Lines starting with # are comments

# Examples:
# https://www.youtube.com/@channelname
# https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx
# UCxxxxxxxxxxxxxxxxxxxxxx

# Your channels:
"""
    content += '\n'.join(channels) + '\n'
    filepath.write_text(content)


def _safe_pause(console):
    """Safely pause for user input, handling Ctrl+C gracefully."""
    try:
        input("\nPress Enter to continue...")
    except (KeyboardInterrupt, EOFError):
        # Silently ignore - let the caller handle exit
        pass


def main():
    """Main entry point - show interactive menu."""
    try:
        import questionary
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
    except ImportError:
        print("ERROR: Required packages not installed. Run setup.bat first.")
        sys.exit(1)
    
    console = Console()
    
    # Add exit flag to control main loop
    should_exit = False
    
    while not should_exit:
        console.clear()
        console.print(Panel.fit(
            "[bold blue]YouTube Toolkit[/bold blue]\n"
            "Scrape, queue, and download YouTube content",
            border_style="blue"
        ))
        console.print()
        
        choice = questionary.select(
            "What would you like to do?",
            choices=[
                questionary.Separator("=== SCRAPING (Add to Queue) ==="),
                "1. Scrape: Liked videos (OAuth required)",
                "2. Scrape: Subscriptions - ALL videos (OAuth required)",
                "3. Scrape: Subscriptions - New videos since last scrape (Smart)",
                "4. Scrape: Target channels (from target_channels.txt)",
                "5. Scrape: Custom URL/playlist",
                questionary.Separator("=== DOWNLOADING ==="),
                "6. Download: All pending videos",
                "7. Download: All pending profile photos",
                "8. Download: Videos + Photos (everything pending)",
                "9. Download: Retry failed videos",
                "10. Download: Retry failed photos",
                questionary.Separator("=== MANAGEMENT ==="),
                "11. View database statistics",
                "12. Manage target channels (select from subscriptions)",
                "13. Setup OAuth credentials (for liked videos & subscriptions)",
                "14. Sign out (clear OAuth credentials)",
                "15. Exit"
            ]
        ).ask()
        
        if not choice or not choice[0].isdigit():
            continue
            
        # Extract the number from the choice
        choice_num = choice.split('.')[0]
        
        try:
            # === SCRAPING ===
            if choice_num == "1":
                # Scrape liked videos only
                console.print("\n[yellow]📺 Scraping liked videos...[/yellow]")
                import scrape_liked_videos_enhanced
                scrape_liked_videos_enhanced.main()
                console.print("[green]✓ Liked videos added to queue[/green]")
                
            elif choice_num == "2":
                # Scrape subscriptions - ALL videos
                console.print("\n[yellow]📺 Scraping ALL videos from subscriptions...[/yellow]")
                console.print("[dim]This will scrape all videos from all channels (may take a while)[/dim]")
                import subscription_processor
                subscription_processor.main()
                console.print("[green]✓ Subscription videos added to queue[/green]")
                
            elif choice_num == "3":
                # Scrape subscriptions - since last scrape (smart mode)
                console.print("\n[yellow]📺 Scraping subscriptions (smart mode - since last scrape)...[/yellow]")
                sys.argv = ['subscription_processor.py', '--since-last-scrape']
                import subscription_processor
                import importlib
                importlib.reload(subscription_processor)
                subscription_processor.main()
                console.print("[green]✓ New subscription videos added to queue[/green]")
                
            elif choice_num == "4":
                # Scrape target channels only
                console.print("\n[yellow]📺 Scraping target channels...[/yellow]")
                import scrape_targets
                scrape_targets.main()
                console.print("[green]✓ Target channel videos added to queue[/green]")
                
            elif choice_num == "5":
                # Scrape custom URL only
                console.print("\n[yellow]📺 Scrape custom URL or playlist[/yellow]")
                url = questionary.text("Enter YouTube URL (channel, playlist, or video):").ask()
                if url:
                    sys.argv = ['scrape_custom_playlist.py', url]
                    import scrape_custom_playlist
                    scrape_custom_playlist.main()
                    console.print("[green]✓ Videos added to queue[/green]")
            
            # === DOWNLOADING ===
            elif choice_num == "6":
                # Download videos only
                console.print("\n[yellow]⬇️  Downloading pending videos...[/yellow]")
                sys.argv = ['batch_downloader.py']
                import batch_downloader
                batch_downloader.main()
                
            elif choice_num == "7":
                # Download photos only
                console.print("\n[yellow]⬇️  Downloading pending profile photos...[/yellow]")
                sys.argv = ['batch_downloader.py', '--photos-only']
                import batch_downloader
                batch_downloader.main()
                
            elif choice_num == "8":
                # Download everything
                console.print("\n[yellow]⬇️  Downloading videos...[/yellow]")
                sys.argv = ['batch_downloader.py']
                import batch_downloader
                batch_downloader.main()
                
                console.print("\n[yellow]⬇️  Downloading profile photos...[/yellow]")
                # Need to reload the module
                import importlib
                importlib.reload(batch_downloader)
                sys.argv = ['batch_downloader.py', '--photos-only']
                batch_downloader.main()
                
            elif choice_num == "9":
                # Retry failed videos
                console.print("\n[yellow]🔄 Retrying failed video downloads...[/yellow]")
                sys.argv = ['batch_downloader.py', '--retry-failed']
                import batch_downloader
                batch_downloader.main()
                
            elif choice_num == "10":
                # Retry failed photos
                console.print("\n[yellow]🔄 Retrying failed photo downloads...[/yellow]")
                sys.argv = ['batch_downloader.py', '--retry-failed', '--photos-only']
                import batch_downloader
                batch_downloader.main()
            
            # === MANAGEMENT ===
            elif choice_num == "11":
                # View database statistics
                from data_manager_streamlined import DatabaseManager
                db = DatabaseManager()
                stats = db.get_video_statistics()
                console.print("\n[bold cyan]📊 Database Statistics[/bold cyan]")
                console.print(stats)
                
            elif choice_num == "12":
                # Manage target channels
                manage_target_channels(console, questionary)
                
            elif choice_num == "13":
                # Setup OAuth credentials
                setup_oauth_credentials(console, questionary)
                
            elif choice_num == "14":
                # Sign out
                console.print("\n[yellow]🔐 Signing out...[/yellow]")
                import logout_account
                logout_account.clear_credentials()
                
            elif choice_num == "15":
                # Exit
                console.print("\n[blue]👋 Goodbye![/blue]")
                should_exit = True
                break
                
        except KeyboardInterrupt:
            console.print("\n[yellow]⚠️  Operation cancelled[/yellow]")
            # Set exit flag and break to terminate main loop
            should_exit = True
            break
        except Exception as e:
            console.print(f"\n[red]❌ Error: {e}[/red]")
            import traceback
            traceback.print_exc()
        
        # Only prompt for continuation if not exiting
        try:
            if choice_num != "15":
                input("\nPress Enter to continue...")
        except (KeyboardInterrupt, EOFError):
            # Handle Ctrl+C during the "Press Enter" prompt
            console.print("\n[yellow]Exiting...[/yellow]")
            should_exit = True
            break
    
    # Ensure clean exit with code 0
    sys.exit(0)

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        # Handle Ctrl+C or EOF at startup
        print("\n[yellow]Exiting YouTube Toolkit...[/yellow]")
    except Exception as e:
        print(f"\n[red]Fatal error: {e}[/red]")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")
