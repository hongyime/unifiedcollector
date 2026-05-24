#!/usr/bin/env python3
"""
Unified Website Toolkit - Functional Implementation 
Features: Photo Scraping, Link Spider, Website Management, Statistics
"""
import os
import subprocess
import sys
import json
import asyncio
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
import signal

# Add src/ to Python path so imports work
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from dotenv import load_dotenv
load_dotenv()

from resilience import _SHUTDOWN


def _handle_sigint(signum, frame):
    if _SHUTDOWN.is_set():
        print("\n[FORCE EXIT] Forcing exit now.")
        raise SystemExit(1)
    _SHUTDOWN.set()
    print("\n[STOPPING] Finishing current operation... Ctrl+C again to force exit.")


def _handle_sigterm(signum, frame):
    """SIGTERM / SIGBREAK (closing bat window) → clean shutdown → atexit fires → WAL checkpoint."""
    _SHUTDOWN.set()
    sys.exit(0)


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigterm)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, _handle_sigterm)

from config import get_config, get_websites, get_enabled_websites, save_config
from utils import validate_website_url
from download_helper import prompt_for_download_location

# Dynamic path resolution
TOOLKIT_ROOT = Path(__file__).resolve().parent

# Settings file path
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def load_settings():
    """Load global settings from settings.json"""
    if not os.path.exists(SETTINGS_FILE):
        # Create default settings if missing
        default_settings = {
            "logging_level": "INFO",
            "feature_toggles": {
                "photo_scraper": True,
                "link_spider": True,
                "proxy_management": True
            },
            "timeout": 30,
            "max_concurrent_tasks": 5
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_settings, f, indent=2)
        return default_settings
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(settings):
    """Save global settings to settings.json"""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception:
        return False

def handle_settings_menu():
    """Settings & Configuration menu"""
    settings = load_settings()
    while True:
        clear_screen()
        print_banner()
        print("SETTINGS & CONFIGURATION")
        print("=" * 50)
        print("1. View Current Settings")
        print("2. Edit Logging Level")
        print("3. Edit Timeout")
        print("4. Toggle Features")
        print("5. Save and Return to Main Menu")
        print()
        print("⚠️  Note: Download paths are set per-session, not saved in settings")
        print()
        choice = input("Select an option: ").strip()
        if choice == "1":
            print("\nCurrent Settings:")
            print(json.dumps(settings, indent=2))
            input("\nPress Enter to continue...")
        elif choice == "2":
            new_level = input(f"Enter logging level (DEBUG/INFO/WARNING/ERROR, current: {settings.get('logging_level', 'INFO')}): ").strip().upper()
            if new_level in ["DEBUG", "INFO", "WARNING", "ERROR"]:
                settings["logging_level"] = new_level
                print("Logging level updated.")
            else:
                print("Invalid logging level.")
            input("Press Enter to continue...")
        elif choice == "3":
            try:
                new_timeout = int(input(f"Enter timeout in seconds (current: {settings.get('timeout', 30)}): ").strip())
                settings["timeout"] = new_timeout
                print("Timeout updated.")
            except ValueError:
                print("Invalid timeout value.")
            input("Press Enter to continue...")
        elif choice == "4":
            toggles = settings.get("feature_toggles", {})
            print("\nFeature Toggles:")
            for idx, (feature, enabled) in enumerate(toggles.items(), 1):
                print(f"{idx}. {feature} [{'ON' if enabled else 'OFF'}]")
            try:
                sel = int(input("Select feature to toggle (number, 0 to cancel): ").strip())
                if sel == 0:
                    continue
                features = list(toggles.keys())
                if 1 <= sel <= len(features):
                    feat = features[sel-1]
                    toggles[feat] = not toggles[feat]
                    settings["feature_toggles"] = toggles
                    print(f"Feature '{feat}' toggled to {'ON' if toggles[feat] else 'OFF'}.")
                else:
                    print("Invalid selection.")
            except ValueError:
                print("Invalid input.")
            input("Press Enter to continue...")
        elif choice == "5":
            if save_settings(settings):
                print("Settings saved.")
            else:
                print("Failed to save settings.")
            input("Press Enter to return to main menu...")
            break
        else:
            print("Invalid option.")
            input("Press Enter to continue...")

def clear_screen():
    """Clear the terminal screen"""
    os.system('cls' if os.name == 'nt' else 'clear')

def open_folder_in_file_manager(path: str) -> None:
    """Open a folder in the system file manager without shell interpolation."""
    if os.name == 'nt':
        os.startfile(path)
        return

    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
    subprocess.run([opener, path], check=False)

def print_banner():
    """Print application banner"""
    print("=" * 70)
    print("🌐 UNIFIED WEBSITE TOOLKIT")
    print("=" * 70)
    print()


# Automated Cycle Functions
async def run_automated_cycle():
    """Run automated discovery and scraping cycle"""
    clear_screen()
    print_banner()
    print("AUTOMATED CYCLE CONFIGURATION")
    print("=" * 50)
    
    websites = get_enabled_websites()
    if not websites:
        print("ERROR: No enabled websites found.")
        print("Please add and enable websites in Website Management first.")
        input("Press Enter to continue...")
        return
    
    print(f"WEBSITES: Found {len(websites)} enabled websites")
    print()
    
    # Configuration options
    print("CYCLE CONFIGURATION:")
    print("1. Discovery + Scraping (Full Cycle)")
    print("2. Discovery Only (Find new websites)")
    print("3. Scraping Only (Download photos)")
    
    try:
        cycle_type = input("SELECT Cycle type (1-3, default: 1): ").strip()
        if not cycle_type:
            cycle_type = '1'
        
        discovery_enabled = cycle_type in ['1', '2']
        scraping_enabled = cycle_type in ['1', '3']
        
        # Get cycle count
        max_cycles_input = input("How many cycles to run (default: 1): ").strip()
        max_cycles = int(max_cycles_input) if max_cycles_input.isdigit() else 1
        
        # Get concurrency level
        concurrent_input = input("Max concurrent websites (default: 3): ").strip()
        concurrent_websites = int(concurrent_input) if concurrent_input.isdigit() else 3
        
        print(f"\nCONFIGURATION SUMMARY:")
        print(f"  Discovery: {discovery_enabled}")
        print(f"  Scraping: {scraping_enabled}")
        print(f"  Cycles: {max_cycles}")
        print(f"  Concurrent: {concurrent_websites}")
        print(f"  Websites: {len(websites)}")
        
        confirm = input("\nStart automated cycle? (y/N): ").strip().lower()
        if confirm != 'y':
            print("Cancelled.")
            input("Press Enter to continue...")
            return
        
        # Import and run cycle manager
        try:
            from cycle_manager import run_automated_cycle as run_cycle_manager
            
            print("\nSTARTING AUTOMATED CYCLE...")
            print("This may take a while depending on the number of websites.")
            print("Press Ctrl+C to cancel (not recommended during scraping)")
            print()
            
            # Run the automated cycle
            cycle_stats = await run_cycle_manager(
                max_cycles=max_cycles,
                concurrent_websites=concurrent_websites,
                discovery_enabled=discovery_enabled,
                scraping_enabled=scraping_enabled
            )
            
            # Show final summary - handle both dict and object
            print("\nCYCLE COMPLETED SUCCESSFULLY!")
            print("=" * 50)
            
            try:
                # Try to access as object attributes first
                if hasattr(cycle_stats, 'cycle_id'):
                    print(f"Cycle ID: {cycle_stats.cycle_id}")
                    print(f"Duration: {cycle_stats.cycle_duration_seconds:.1f} seconds")
                    print(f"Websites crawled: {cycle_stats.websites_crawled}")
                    print(f"Websites scraped: {cycle_stats.websites_scraped}")
                    print(f"Links discovered: {cycle_stats.links_discovered}")
                    print(f"New websites added: {cycle_stats.new_websites_added}")
                    print(f"Photos downloaded: {cycle_stats.photos_downloaded}")
                    
                    if cycle_stats.new_websites_added > 0:
                        print(f"\n🎉 DISCOVERY: {cycle_stats.new_websites_added} new websites added to your configuration!")
                        print("You can enable them in Website Management to include in future cycles.")
                
                # Try to access as dictionary if object access fails
                elif isinstance(cycle_stats, dict):
                    print(f"Cycle ID: {cycle_stats.get('cycle_id', 'Unknown')}")
                    print(f"Duration: {cycle_stats.get('cycle_duration_seconds', 0):.1f} seconds")
                    print(f"Websites crawled: {cycle_stats.get('websites_crawled', 0)}")
                    print(f"Websites scraped: {cycle_stats.get('websites_scraped', 0)}")
                    print(f"Links discovered: {cycle_stats.get('links_discovered', 0)}")
                    print(f"New websites added: {cycle_stats.get('new_websites_added', 0)}")
                    print(f"Photos downloaded: {cycle_stats.get('photos_downloaded', 0)}")
                    
                    if cycle_stats.get('new_websites_added', 0) > 0:
                        print(f"\n🎉 DISCOVERY: {cycle_stats['new_websites_added']} new websites added to your configuration!")
                        print("You can enable them in Website Management to include in future cycles.")
                
                else:
                    print("Cycle completed but could not parse statistics.")
                    print(f"Stats type: {type(cycle_stats)}")
                    if hasattr(cycle_stats, '__dict__'):
                        print(f"Available attributes: {list(cycle_stats.__dict__.keys())}")
                    
            except Exception as stats_error:
                print(f"Cycle completed but error displaying stats: {stats_error}")
                print(f"Stats object type: {type(cycle_stats)}")
                
                # Try to show basic info
                try:
                    print(f"Raw stats object: {cycle_stats}")
                except:
                    print("Could not display raw stats object")
            
        except ImportError:
            print("ERROR: Cycle manager module not found.")
            print("Please ensure cycle_manager.py is available.")
        except KeyboardInterrupt:
            print("\n\nCYCLE CANCELLED by user")
            print("Some operations may have completed. Check logs for details.")
        except Exception as e:
            print(f"\nCYCLE FAILED: {e}")
            print("Check error logs for detailed information.")
        
    except ValueError:
        print("ERROR: Invalid input.")
    except Exception as e:
        print(f"ERROR: Configuration failed: {e}")
    
    input("\nPress Enter to continue...")


def show_automation_summary():
    """Show automation cycle summary"""
    clear_screen()
    print_banner()
    print("AUTOMATION SUMMARY")
    print("=" * 50)
    
    try:
        from cycle_manager import get_automation_summary
        summary = get_automation_summary()
        
        if summary.get('message'):
            print(summary['message'])
            input("Press Enter to continue...")
            return
        
        print(f"RECENT CYCLES: {summary.get('recent_cycles', 0)}")
        print(f"TOTAL WEBSITES DISCOVERED: {summary.get('total_websites_discovered', 0)}")
        print(f"TOTAL PHOTOS DOWNLOADED: {summary.get('total_photos_downloaded', 0)}")
        print(f"TOTAL LINKS DISCOVERED: {summary.get('total_links_discovered', 0)}")
        print()
        
        if summary.get('last_cycle'):
            last = summary['last_cycle']
            print("LAST CYCLE:")
            
            # Handle both dictionary and object access patterns
            try:
                # Try dictionary-style access first
                if isinstance(last, dict):
                    print(f"  ID: {last.get('cycle_id', 'Unknown')}")
                    print(f"  Duration: {last.get('cycle_duration_seconds', 0):.1f}s")
                    print(f"  Websites crawled: {last.get('websites_crawled', 0)}")
                    print(f"  Websites scraped: {last.get('websites_scraped', 0)}")
                    print(f"  Photos downloaded: {last.get('photos_downloaded', 0)}")
                    print(f"  New websites: {last.get('new_websites_added', 0)}")
                else:
                    # Handle object attribute access
                    print(f"  ID: {getattr(last, 'cycle_id', 'Unknown')}")
                    print(f"  Duration: {getattr(last, 'cycle_duration_seconds', 0):.1f}s")
                    print(f"  Websites crawled: {getattr(last, 'websites_crawled', 0)}")
                    print(f"  Websites scraped: {getattr(last, 'websites_scraped', 0)}")
                    print(f"  Photos downloaded: {getattr(last, 'photos_downloaded', 0)}")
                    print(f"  New websites: {getattr(last, 'new_websites_added', 0)}")
            except Exception as e:
                print(f"  ERROR: Could not display last cycle details: {e}")
                # Fallback: try to convert object to dict if it has __dict__
                if hasattr(last, '__dict__'):
                    cycle_dict = last.__dict__
                    print(f"  ID: {cycle_dict.get('cycle_id', 'Unknown')}")
                    print(f"  Duration: {cycle_dict.get('cycle_duration_seconds', 0):.1f}s")
                    print(f"  Websites crawled: {cycle_dict.get('websites_crawled', 0)}")
                    print(f"  Websites scraped: {cycle_dict.get('websites_scraped', 0)}")
                    print(f"  Photos downloaded: {cycle_dict.get('photos_downloaded', 0)}")
                    print(f"  New websites: {cycle_dict.get('new_websites_added', 0)}")
        
        print()
        print("RECENT CYCLE HISTORY:")
        cycles = summary.get('cycles', [])
        
        if cycles:
            for i, cycle in enumerate(cycles, 1):
                try:
                    # Handle both dictionary and object access patterns
                    if isinstance(cycle, dict):
                        cycle_id = cycle.get('cycle_id', f'cycle_{i}')
                        photos = cycle.get('photos_downloaded', 0)
                        websites = cycle.get('new_websites_added', 0)
                    else:
                        cycle_id = getattr(cycle, 'cycle_id', f'cycle_{i}')
                        photos = getattr(cycle, 'photos_downloaded', 0)
                        websites = getattr(cycle, 'new_websites_added', 0)
                    
                    print(f"  {i}. {cycle_id} - {photos} photos, {websites} websites")
                except Exception as e:
                    print(f"  {i}. Error displaying cycle: {e}")
        else:
            print("  No recent cycles found")
        
    except ImportError:
        print("ERROR: Cycle manager module not found.")
    except Exception as e:
        print(f"ERROR: Failed to load summary: {e}")
        # Show debug information
        import traceback
        print(f"DEBUG: Full error traceback:")
        traceback.print_exc()
    
    input("\nPress Enter to continue...")


# Menu Functions

def print_main_menu():
    """Print main menu options"""
    print("MAIN MENU:")
    print("1. Start Automated Discovery & Scraping Cycle")
    print("2. View Automation Summary & Analytics")
    print("3. Manual Photo Scraper")
    print("4. Manual Link Spider")
    print("5. Proxy Management") 
    print("6. Website Management")
    print("7. Quick Bulk Import Websites")
    print("8. Data Management & Export")
    print("9. Advanced Statistics & Reports")
    print("10. Exit")
    print()

def print_website_menu():
    """Print website management menu"""
    print("WEBSITE MANAGEMENT:")
    print("1. Add New Website")
    print("2. Bulk Import Websites from File")
    print("3. List All Websites")
    print("4. Edit Website")
    print("5. Remove Website")
    print("6. Back to Main Menu")
    print()

def print_photo_scraper_menu():
    """Print photo scraper menu"""
    print("PHOTO SCRAPER:")
    print("1. Start Photo Scraping")
    print("2. Select Specific Website")
    print("3. Configure Settings")
    print("4. Open Downloads Folder")
    print("5. Back to Main Menu")
    print()

def print_link_spider_menu():
    """Print link spider menu"""
    print("LINK SPIDER:")
    print("1. Start Link Crawling (with Website Discovery)")
    print("2. Select Specific Website")
    print("3. Configure Settings")
    print("4. View Saved Links")
    print("5. View Recent Discovery Report")
    print("6. Back to Main Menu")
    print()

# Website Management Functions
def add_new_website():
    """Add a new website to configuration"""
    print("\nADD NEW WEBSITE")
    print("-" * 40)
    
    name = input("Website name: ").strip()
    if not name:
        print("ERROR: Website name cannot be empty.")
        input("Press Enter to continue...")
        return
    
    url = input("Website URL: ").strip()
    if not url:
        print("ERROR: Website URL cannot be empty.")
        input("Press Enter to continue...")
        return
    
    # Validate URL
    is_valid, validation_message = validate_website_url(url)
    if not is_valid:
        print(f"ERROR: {validation_message}")
        input("Press Enter to continue...")
        return
    
    max_depth_input = input("Max crawl depth (default: 3): ").strip()
    try:
        max_depth = int(max_depth_input) if max_depth_input else 3
    except ValueError:
        max_depth = 3
    
    description = input("Description (optional): ").strip()
    
    # Add website using the config API
    config = get_config()
    success = config.add_website(
        name_or_url=name,
        url=url,
        max_depth=max_depth,
        notes=description
    )
    
    if success:
        print(f"SUCCESS: Website '{name}' added successfully!")
    else:
        print(f"ERROR: Failed to add website '{name}'. It may already exist.")
    
    input("Press Enter to continue...")

def list_all_websites():
    """List all configured websites"""
    print("\nLIST ALL WEBSITES")
    print("-" * 40)
    
    websites = get_websites()
    
    if not websites:
        print("ERROR: No websites configured.")
        print(" Add some websites first using the 'Add New Website' option.")
    else:
        for i, website in enumerate(websites, 1):
            name = website.get('name', 'Unknown')
            url = website.get('url', 'N/A')
            enabled = website.get('enabled', True)
            status = "SUCCESS: Enabled" if enabled else "ERROR: Disabled"
            
            print(f"{i}. {name}")
            print(f"   URL: {url}")
            print(f"   Status: {status}")
            print()
    
    input("Press Enter to continue...")

def remove_website():
    """Remove a website from configuration"""
    print("\nREMOVE WEBSITE")
    print("-" * 40)
    
    websites = get_websites()
    
    if not websites:
        print("ERROR: No websites configured.")
        input("Press Enter to continue...")
        return
    
    print("Select website to remove:")
    for i, website in enumerate(websites, 1):
        name = website.get('name', 'Unknown')
        url = website.get('url', 'N/A')
        print(f"{i}. {name} - {url}")
    
    try:
        choice = int(input("Enter website number: ").strip())
        if 1 <= choice <= len(websites):
            website_to_remove = websites[choice - 1]
            website_name = website_to_remove.get('name', 'Unknown')
            
            confirm = input(f"WARNING: Remove '{website_name}'? (y/n): ").strip().lower()
            if confirm == 'y':
                config = get_config()
                success = config.remove_website(website_name)
                
                if success:
                    print(f"SUCCESS: Website '{website_name}' removed successfully!")
                else:
                    print(f"ERROR: Failed to remove website '{website_name}'.")
            else:
                print("ERROR: Removal cancelled.")
        else:
            print("ERROR: Invalid choice.")
    except ValueError:
        print("ERROR: Invalid input.")
    
    input("Press Enter to continue...")

def edit_website():
    """Edit an existing website configuration"""
    print("\nEDIT WEBSITE")
    print("-" * 40)
    
    websites = get_websites()
    
    if not websites:
        print("ERROR: No websites configured.")
        input("Press Enter to continue...")
        return
    
    print("Select website to edit:")
    for i, website in enumerate(websites, 1):
        name = website.get('name', 'Unknown')
        url = website.get('url', 'N/A')
        enabled = website.get('enabled', True)
        status = "✅ Enabled" if enabled else "❌ Disabled"
        print(f"{i}. {name} - {url} [{status}]")
    
    try:
        choice = int(input("Enter website number: ").strip())
        if 1 <= choice <= len(websites):
            website_to_edit = websites[choice - 1]
            old_name = website_to_edit.get('name', 'Unknown')
            
            print(f"\nEditing: {old_name}")
            print("Press Enter to keep current value, or type new value:")
            print()
            
            # Edit name
            current_name = website_to_edit.get('name', '')
            new_name = input(f"Name ({current_name}): ").strip()
            if not new_name:
                new_name = current_name
            
            # Edit URL
            current_url = website_to_edit.get('url', '')
            new_url = input(f"URL ({current_url}): ").strip()
            if not new_url:
                new_url = current_url
            else:
                is_valid, validation_message = validate_website_url(new_url)
                if not is_valid:
                    print(f"ERROR: {validation_message}")
                    input("Press Enter to continue...")
                    return
            
            # Edit max depth
            current_depth = website_to_edit.get('max_depth', 3)
            depth_input = input(f"Max crawl depth ({current_depth}): ").strip()
            try:
                new_depth = int(depth_input) if depth_input else current_depth
            except ValueError:
                new_depth = current_depth
            
            # Edit enabled status
            current_enabled = website_to_edit.get('enabled', True)
            enabled_input = input(f"Enabled ({'y' if current_enabled else 'n'}): ").strip().lower()
            if enabled_input:
                new_enabled = enabled_input == 'y'
            else:
                new_enabled = current_enabled
            
            # Edit description/notes
            current_notes = website_to_edit.get('notes', '')
            new_notes = input(f"Description ({current_notes}): ").strip()
            if not new_notes:
                new_notes = current_notes
            
            # Show summary of changes
            print(f"\nCHANGES SUMMARY:")
            print(f"  Name: {current_name} → {new_name}")
            print(f"  URL: {current_url} → {new_url}")
            print(f"  Max Depth: {current_depth} → {new_depth}")
            print(f"  Enabled: {current_enabled} → {new_enabled}")
            print(f"  Description: {current_notes} → {new_notes}")
            
            confirm = input(f"\nSave changes? (y/N): ").strip().lower()
            if confirm == 'y':
                # Update the website configuration
                config = get_config()
                
                # Remove old website and add updated one
                config.remove_website(old_name)
                success = config.add_website(
                    name_or_url=new_name,
                    url=new_url,
                    max_depth=new_depth,
                    notes=new_notes,
                    enabled=new_enabled
                )
                
                if success:
                    print(f"SUCCESS: Website '{new_name}' updated successfully!")
                else:
                    print(f"ERROR: Failed to update website.")
            else:
                print("CANCELLED: Changes cancelled.")
        else:
            print("ERROR: Invalid choice.")
    except ValueError:
        print("ERROR: Invalid input.")
    
    input("Press Enter to continue...")

def handle_website_management():
    """Handle website management menu"""
    while True:
        clear_screen()
        print_banner()
        print_website_menu()
        
        choice = input("SELECT Select option (1-6): ").strip()
        
        if choice == '1':
            add_new_website()
        elif choice == '2':
            handle_bulk_import()
        elif choice == '3':
            list_all_websites()
        elif choice == '4':
            edit_website()
        elif choice == '5':
            remove_website()
        elif choice == '6':
            break
        else:
            print("ERROR: Invalid option selected.")
            input("Press Enter to continue...")

def handle_bulk_import():
    """Handle bulk website import"""
    clear_screen()
    print_banner()
    
    try:
        from bulk_website_importer import interactive_bulk_import
        interactive_bulk_import()
    except ImportError:
        print("ERROR: Bulk importer module not found.")
        input("Press Enter to continue...")
    except Exception as e:
        print(f"ERROR: Bulk import failed: {e}")
        input("Press Enter to continue...")

def handle_quick_bulk_import():
    """Handle quick bulk import - streamlined for main menu"""
    clear_screen()
    print_banner()
    print("🚀 QUICK BULK IMPORT")
    print("=" * 50)
    
    try:
        from bulk_website_importer import BulkWebsiteImporter
        
        print("1. Import from existing file")
        print("2. Import from sample file (sample_websites_import.txt)")
        print("3. Create sample file and exit")
        print("4. Back to main menu")
        print()
        
        choice = input("Select option (1-4): ").strip()
        
        if choice == "1":
            file_path = input("Enter path to import file: ").strip()
            if not file_path:
                print("No file path provided.")
                input("Press Enter to continue...")
                return
            
            print(f"\nImporting from: {file_path}")
            importer = BulkWebsiteImporter()
            result = importer.import_from_file(file_path)
            
            if 'error' in result:
                print(f"❌ Import failed: {result['error']}")
            else:
                print(f"\n📊 QUICK IMPORT SUMMARY:")
                print(f"  ✅ Imported: {result['imported']} websites")
                print(f"  ⏭️ Skipped: {result['skipped']} websites")
                print(f"  ❌ Errors: {result['errors']} websites")
                
                if result['imported'] > 0:
                    print(f"\n🎉 Successfully imported {result['imported']} new websites!")
        
        elif choice == "2":
            sample_file = "sample_websites_import.txt"
            if not os.path.exists(sample_file):
                print(f"Sample file '{sample_file}' not found.")
                print("Creating sample file first...")
                importer = BulkWebsiteImporter()
                created = importer.create_sample_import_file(sample_file)
                if created:
                    print(f"✅ Created: {created}")
                    print("Edit this file and run the import again.")
                else:
                    print("❌ Failed to create sample file.")
                input("Press Enter to continue...")
                return
            
            print(f"Importing from: {sample_file}")
            importer = BulkWebsiteImporter()
            result = importer.import_from_file(sample_file)
            
            if 'error' in result:
                print(f"❌ Import failed: {result['error']}")
            else:
                print(f"\n📊 QUICK IMPORT SUMMARY:")
                print(f"  ✅ Imported: {result['imported']} websites")
                print(f"  ⏭️ Skipped: {result['skipped']} websites")
                print(f"  ❌ Errors: {result['errors']} websites")
                
                if result['imported'] > 0:
                    print(f"\n🎉 Successfully imported {result['imported']} new websites!")
        
        elif choice == "3":
            importer = BulkWebsiteImporter()
            created = importer.create_sample_import_file()
            if created:
                print(f"✅ Created sample file: {created}")
                print("Edit this file with your websites and use option 2 to import them.")
            else:
                print("❌ Failed to create sample file.")
        
        elif choice == "4":
            return
        
        else:
            print("Invalid option.")
        
    except ImportError:
        print("ERROR: Bulk importer module not found.")
    except Exception as e:
        print(f"ERROR: Quick bulk import failed: {e}")
    
    input("\nPress Enter to continue...")

# Photo Scraper Functions
async def start_photo_scraping():
    """Start photo scraping for all enabled websites"""
    print("\nSTARTING PHOTO SCRAPING")
    print("-" * 40)

    websites = get_enabled_websites()

    if not websites:
        print("ERROR: No enabled websites found.")
        print(" Add and enable some websites first.")
        input("Press Enter to continue...")
        return

    # Always prompt for download location
    # Mandatory download path prompting
    custom_download_dir = prompt_for_download_location(
        context="website photos",
        default_fallback="downloads"
    )

    print(f"\n✅ Using download location: {custom_download_dir}")
    print(f"📋 Processing {len(websites)} enabled websites...\n")

    for i, website in enumerate(websites, 1):
        print(f" {i}. {website.get('name', 'Unknown')} - {website.get('url', 'N/A')}")

    confirm = input("\nPROCEED Proceed with photo scraping? (y/n): ").strip().lower()
    if confirm != 'y':
        print("ERROR: Photo scraping cancelled.")
        input("Press Enter to continue...")
        return

    # Import photo scraper
    try:
        from photo_scraper import PhotoScraper
    except ImportError:
        print("ERROR: Photo scraper module not found.")
        input("Press Enter to continue...")
        return

    # Process each website
    successful_sites = 0
    total_photos = 0

    for i, website in enumerate(websites, 1):
        website_name = website.get('name', f'site_{i}')
        website_url = website.get('url')

        if not website_url:
            print(f"WARNING: Skipping {website_name}: No URL configured")
            continue

        print(f"\nPROCESSING Processing {i}/{len(websites)}: {website_name}")
        
        try:
            # Create scraper with custom download directory and website URL
            scraper = PhotoScraper(website_name, custom_download_dir, website_url)
            result = await scraper.scrape_website_images([website_url])
            photos_downloaded = result.get('total_images_downloaded', 0)

            if photos_downloaded > 0:
                successful_sites += 1
                total_photos += photos_downloaded
                print(f"SUCCESS: {website_name}: {photos_downloaded} photos downloaded")
            else:
                print(f"WARNING: {website_name}: No photos found")

        except Exception as e:
            print(f"ERROR: Error processing {website_name}: {e}")
            continue

    # Final summary
    print(f"\nSUMMARY Photo Scraping Summary:")
    print(f" SUCCESS: Successful sites: {successful_sites}/{len(websites)}")
    print(f" TOTAL: Total photos downloaded: {total_photos}")
    print(f" DIRECTORY: Saved to: {custom_download_dir}")
    
    input("\nPress Enter to continue...")

async def select_website_for_photos():
    """Select specific website for photo scraping"""
    print("\nSELECT WEBSITE FOR PHOTO SCRAPING")
    print("-" * 40)

    websites = get_enabled_websites()

    if not websites:
        print("ERROR: No enabled websites found.")
        print(" Add and enable some websites first.")
        input("Press Enter to continue...")
        return

    print("Available websites:")
    for i, website in enumerate(websites, 1):
        print(f"{i}. {website.get('name', 'Unknown')} - {website.get('url', 'N/A')}")

    try:
        choice = int(input("Select website number: ").strip())
        if 1 <= choice <= len(websites):
            selected_website = websites[choice - 1]
            website_name = selected_website.get('name', 'Unknown')
            website_url = selected_website.get('url')
            
            print(f"\nSelected: {website_name}")
            
            # Always prompt for download location
            # Mandatory download path prompting
            custom_download_dir = prompt_for_download_location(
                context="photo scraping",
                default_fallback="downloads"
            )
            
            # Ask for photo limits
            max_photos_input = input("Maximum photos to download (default: 50): ").strip()
            try:
                max_photos = int(max_photos_input) if max_photos_input else 50
            except ValueError:
                max_photos = 50
            
            print(f"\nCONFIGURATION:")
            print(f"  Website: {website_name}")
            print(f"  URL: {website_url}")
            print(f"  Download Directory: {custom_download_dir}")
            print(f"  Max Photos: {max_photos}")
            
            confirm = input("\nPROCEED Start photo scraping? (y/N): ").strip().lower()
            if confirm != 'y':
                print("CANCELLED: Photo scraping cancelled.")
                input("Press Enter to continue...")
                return
            
            # Import and run photo scraper
            try:
                from photo_scraper import PhotoScraper
                
                print(f"\nSTARTING Photo scraping for: {website_name}")
                print("This may take a few minutes...")
                
                # Create scraper with custom download directory and website URL
                scraper = PhotoScraper(website_name, custom_download_dir=custom_download_dir, website_url=website_url)
                
                # Ensure website_url is string
                url_to_scrape = str(website_url) if website_url else ""
                
                # Run async scrape
                result = await scraper.scrape_website_images([url_to_scrape])
                
                photos_downloaded = result.get('total_images_downloaded', 0)
                
                print(f"\nSCRAPING COMPLETED!")
                print("=" * 50)
                print(f"Website: {website_name}")
                print(f"Photos downloaded: {photos_downloaded}")
                print(f"Saved to: {custom_download_dir}")
                
                if photos_downloaded > 0:
                    print(f"SUCCESS: {photos_downloaded} photos downloaded successfully!")
                    
                    # Offer to open download folder
                    open_folder = input("\nOpen download folder? (y/N): ").strip().lower()
                    if open_folder == 'y':
                        try:
                            if os.name == 'nt':  # Windows
                                os.startfile(custom_download_dir)
                            else:
                                open_folder_in_file_manager(custom_download_dir)
                            print("SUCCESS: Download folder opened.")
                        except Exception as e:
                            print(f"ERROR: Could not open folder: {e}")
                            print(f"MANUAL: Navigate to: {custom_download_dir}")
                else:
                    print("WARNING: No photos found or downloaded.")
                
            except ImportError:
                print("ERROR: Photo scraper module not found.")
            except Exception as e:
                print(f"ERROR: Failed to run photo scraper: {e}")
                import traceback
                traceback.print_exc()
                
        else:
            print("ERROR: Invalid choice.")
    except ValueError:
        print("ERROR: Invalid input.")
    
    input("\nPress Enter to continue...")

def configure_photo_scraper_settings():
    """Configure photo scraper settings"""
    clear_screen()
    print_banner()
    print("PHOTO SCRAPER SETTINGS")
    print("=" * 50)
    print()
    
    try:
        config = get_config()
        
        print("CURRENT SETTINGS:")
        current_settings = {
            'max_images_per_site': config.settings.get('photo_scraper', {}).get('max_images_per_site', 50),
            'image_formats': config.settings.get('photo_scraper', {}).get('image_formats', ['jpg', 'jpeg', 'png', 'gif', 'webp']),
            'min_image_size': config.settings.get('photo_scraper', {}).get('min_image_size', 1024),  # bytes
            'max_concurrent': config.settings.get('photo_scraper', {}).get('max_concurrent', 5),
            'timeout_seconds': config.settings.get('photo_scraper', {}).get('timeout_seconds', 30),
            'respect_robots': config.settings.get('photo_scraper', {}).get('respect_robots', True),
            'download_subdirs': config.settings.get('photo_scraper', {}).get('download_subdirs', True)
        }
        
        print(f"  Max images per site: {current_settings['max_images_per_site']}")
        print(f"  Image formats: {', '.join(current_settings['image_formats'])}")
        print(f"  Min image size: {current_settings['min_image_size']} bytes")
        print(f"  Max concurrent downloads: {current_settings['max_concurrent']}")
        print(f"  Timeout: {current_settings['timeout_seconds']} seconds")
        print(f"  Respect robots.txt: {current_settings['respect_robots']}")
        print(f"  Create subdirectories: {current_settings['download_subdirs']}")
        print()
        
        print("CONFIGURATION OPTIONS:")
        print("1. Change max images per site")
        print("2. Configure image formats")
        print("3. Set minimum image size")
        print("4. Adjust concurrent downloads")
        print("5. Set timeout duration")
        print("6. Toggle robots.txt respect")
        print("7. Toggle subdirectory creation")
        print("8. Reset to defaults")
        print("9. Back to photo scraper menu")
        print()
        
        choice = input("SELECT Choose option (1-9): ").strip()
        
        if choice == '1':
            # Max images per site
            current = current_settings['max_images_per_site']
            new_value = input(f"Max images per site ({current}): ").strip()
            try:
                if new_value:
                    new_max = int(new_value)
                    if new_max > 0:
                        current_settings['max_images_per_site'] = new_max
                        print(f"SUCCESS: Max images set to {new_max}")
                    else:
                        print("ERROR: Must be positive number")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '2':
            # Image formats
            current = current_settings['image_formats']
            print(f"Current formats: {', '.join(current)}")
            print("Available formats: jpg, jpeg, png, gif, webp, bmp, tiff, svg")
            new_formats = input("Enter formats (comma-separated): ").strip()
            if new_formats:
                formats_list = [f.strip().lower() for f in new_formats.split(',')]
                current_settings['image_formats'] = formats_list
                print(f"SUCCESS: Image formats set to {', '.join(formats_list)}")
                
        elif choice == '3':
            # Min image size
            current = current_settings['min_image_size']
            new_value = input(f"Minimum image size in bytes ({current}): ").strip()
            try:
                if new_value:
                    new_size = int(new_value)
                    if new_size >= 0:
                        current_settings['min_image_size'] = new_size
                        print(f"SUCCESS: Minimum image size set to {new_size} bytes")
                    else:
                        print("ERROR: Must be non-negative number")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '4':
            # Max concurrent downloads
            current = current_settings['max_concurrent']
            new_value = input(f"Max concurrent downloads ({current}): ").strip()
            try:
                if new_value:
                    new_concurrent = int(new_value)
                    if 1 <= new_concurrent <= 20:
                        current_settings['max_concurrent'] = new_concurrent
                        print(f"SUCCESS: Max concurrent downloads set to {new_concurrent}")
                    else:
                        print("ERROR: Must be between 1 and 20")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '5':
            # Timeout duration
            current = current_settings['timeout_seconds']
            new_value = input(f"Timeout in seconds ({current}): ").strip()
            try:
                if new_value:
                    new_timeout = int(new_value)
                    if new_timeout > 0:
                        current_settings['timeout_seconds'] = new_timeout
                        print(f"SUCCESS: Timeout set to {new_timeout} seconds")
                    else:
                        print("ERROR: Must be positive number")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '6':
            # Toggle robots.txt respect
            current = current_settings['respect_robots']
            toggle = input(f"Respect robots.txt? ({'y' if current else 'n'}): ").strip().lower()
            if toggle:
                new_value = toggle == 'y'
                current_settings['respect_robots'] = new_value
                print(f"SUCCESS: Robots.txt respect set to {new_value}")
                
        elif choice == '7':
            # Toggle subdirectory creation
            current = current_settings['download_subdirs']
            toggle = input(f"Create subdirectories? ({'y' if current else 'n'}): ").strip().lower()
            if toggle:
                new_value = toggle == 'y'
                current_settings['download_subdirs'] = new_value
                print(f"SUCCESS: Subdirectory creation set to {new_value}")
                
        elif choice == '8':
            # Reset to defaults
            confirm = input("Reset all settings to defaults? (y/N): ").strip().lower()
            if confirm == 'y':
                default_settings = {
                    'max_images_per_site': 50,
                    'image_formats': ['jpg', 'jpeg', 'png', 'gif', 'webp'],
                    'min_image_size': 1024,
                    'max_concurrent': 5,
                    'timeout_seconds': 30,
                    'respect_robots': True,
                    'download_subdirs': True
                }
                current_settings = default_settings
                print("SUCCESS: Settings reset to defaults")
                
        elif choice == '9':
            return
        else:
            print("ERROR: Invalid option")
            input("Press Enter to continue...")
            return
        
        # Save settings if changed
        if choice in ['1', '2', '3', '4', '5', '6', '7', '8']:
            try:
                # Ensure photo_scraper section exists
                if 'photo_scraper' not in config.settings:
                    config.settings['photo_scraper'] = {}
                
                # Update all settings
                config.settings['photo_scraper'].update(current_settings)
                
                # Save configuration
                save_config()
                print("SUCCESS: Settings saved to configuration file")
                
            except Exception as e:
                print(f"ERROR: Failed to save settings: {e}")
        
    except Exception as e:
        print(f"ERROR: Failed to load settings: {e}")
    
    input("\nPress Enter to continue...")

def open_downloads_folder():
    """Open the downloads folder"""
    # Prompt user for download folder to open
    print("\n📁 Opening Downloads Folder")
    print("=" * 50)
    
    DOWNLOADS_DIR = prompt_for_download_location(
        context="folder to open",
        default_fallback="downloads"
    )
    
    if not os.path.exists(DOWNLOADS_DIR):
        print(f"ERROR: Downloads folder does not exist: {DOWNLOADS_DIR}")
        print(" Run photo scraping first to create the folder.")
        input("Press Enter to continue...")
        return

    print(f"OPENING Opening downloads folder: {DOWNLOADS_DIR}")
    
    try:
        open_folder_in_file_manager(DOWNLOADS_DIR)
        print("SUCCESS: Downloads folder opened.")
    except Exception as e:
        print(f"ERROR: Could not open folder: {e}")
        print(f"MANUAL: Please manually navigate to: {DOWNLOADS_DIR}")
    else:
        print(f"MANUAL: Downloads folder: {DOWNLOADS_DIR}")
    
    input("Press Enter to continue...")

def handle_photo_scraper():
    """Handle photo scraper menu"""
    while True:
        clear_screen()
        print_banner()
        print_photo_scraper_menu()
        
        choice = input("SELECT Select option (1-5): ").strip()
        
        if choice == '1':
            asyncio.run(start_photo_scraping())
        elif choice == '2':
            asyncio.run(select_website_for_photos())
        elif choice == '3':
            configure_photo_scraper_settings()
        elif choice == '4':
            open_downloads_folder()
        elif choice == '5':
            break
        else:
            print("ERROR: Invalid option selected.")
            input("Press Enter to continue...")

# Link Spider Functions
async def start_link_crawling():
    """Start link crawling for all enabled websites"""
    print("\nSTARTING LINK CRAWLING")
    print("-" * 40)

    websites = get_enabled_websites()

    if not websites:
        print("ERROR: No enabled websites found.")
        print(" Add and enable some websites first.")
        input("Press Enter to continue...")
        return

    print(f"PROCESSING Processing {len(websites)} enabled websites...\n")

    for i, website in enumerate(websites, 1):
        print(f" {i}. {website.get('name', 'Unknown')} - {website.get('url', 'N/A')}")

    confirm = input("\nPROCEED Proceed with link crawling? (y/n): ").strip().lower()
    if confirm != 'y':
        print("ERROR: Link crawling cancelled.")
        input("Press Enter to continue...")
        return

    # Import link spider
    try:
        from link_spider import LinkSpider
    except ImportError:
        print("ERROR: Link spider module not found.")
        input("Press Enter to continue...")
        return

    # Get user preferences for website discovery
    print("\nWEBSITE DISCOVERY OPTIONS:")
    print("1. Auto-add discovered websites to configuration (disabled)")
    print("2. Auto-add discovered websites to configuration (enabled)")
    print("3. Just discover but don't add to configuration")
    
    try:
        discovery_choice = input("SELECT Choose discovery option (1-3, default: 1): ").strip()
        if not discovery_choice:
            discovery_choice = '1'
            
        auto_add_websites = discovery_choice in ['1', '2']
        auto_enable_websites = discovery_choice == '2'
        
        print(f"DISCOVERY: Auto-add websites: {auto_add_websites}")
        print(f"DISCOVERY: Auto-enable websites: {auto_enable_websites}")
        
    except Exception:
        auto_add_websites = True
        auto_enable_websites = False

    # Process each website
    successful_sites = 0
    total_links = 0
    total_discovered_websites = 0
    total_added_websites = 0

    for i, website in enumerate(websites, 1):
        website_name = website.get('name', f'site_{i}')
        website_url = website.get('url')

        if not website_url:
            print(f"WARNING: Skipping {website_name}: No URL configured")
            continue

        print(f"\nPROCESSING Processing {i}/{len(websites)}: {website_name}")
        
        try:
            # Create spider and run with discovery options
            spider = LinkSpider(website_name)
            result = await spider.crawl_website_urls([website_url], auto_add_websites, auto_enable_websites)

            links_found = result.get('total_links_found', 0)
            discovered_count = result.get('discovered_websites_count', 0)
            added_count = result.get('websites_added_to_config', 0)

            if links_found > 0:
                successful_sites += 1
                total_links += links_found
                total_discovered_websites += discovered_count
                total_added_websites += added_count
                
                print(f"SUCCESS: {website_name}: {links_found} links found")
                if discovered_count > 0:
                    print(f"DISCOVERY: {website_name}: {discovered_count} new websites discovered")
                if added_count > 0:
                    print(f"ADDED: {website_name}: {added_count} websites added to config")
            else:
                print(f"WARNING: {website_name}: No links found")

        except Exception as e:
            print(f"ERROR: Error processing {website_name}: {e}")
            continue

    # Final summary
    print(f"\nSUMMARY Link Crawling Summary:")
    print(f" SUCCESS: Successful sites: {successful_sites}/{len(websites)}")
    print(f" TOTAL: Total links found: {total_links}")
    print(f" DISCOVERY: Total websites discovered: {total_discovered_websites}")
    print(f" ADDED: Total websites added to config: {total_added_websites}")
    
    if total_added_websites > 0:
        print(f"\nCONFIG: {total_added_websites} new websites have been added to your configuration!")
        print("You can enable/disable them in the website management menu.")
    
    input("\nPress Enter to continue...")

def handle_data_management_menu():
    """Data Management & Export menu"""
    from data_manager import export_system_report, cleanup_system_data, show_data_summary

    while True:
        clear_screen()
        print_banner()
        print("DATA MANAGEMENT & EXPORT")
        print("=" * 50)
        print("1. View Data Summary")
        print("2. Export System Report")
        print("3. Cleanup Old Data")
        print("4. Search Data")
        print("5. Return to Main Menu")
        print()

        choice = input("Select an option: ").strip()

        if choice == "1":
            print("\nDATA SUMMARY:")
            metrics = show_data_summary()
            if isinstance(metrics, dict):
                print(f"  Total Websites: {metrics.get('total_websites', 0)}")
                print(f"  Enabled Websites: {metrics.get('enabled_websites', 0)}")
                print(f"  Total Links Stored: {metrics.get('total_links_stored', 0)}")
                print(f"  Total Photos Downloaded: {metrics.get('total_photos_downloaded', 0)}")
                print(f"  Storage Used: {metrics.get('storage_used_mb', 0):.1f} MB")
                print(f"  Health Score: {metrics.get('data_health_score', 0)}/100")
            input("\nPress Enter to continue...")

        elif choice == "2":
            print("\nEXPORT SYSTEM REPORT:")
            try:
                report_path = export_system_report()
                print(f"Report successfully exported to: {report_path}")
            except Exception as e:
                print(f"Failed to export report: {e}")
            input("\nPress Enter to continue...")

        elif choice == "3":
            print("\nCLEANUP OLD DATA:")
            try:
                days = int(input("Enter the number of days to keep data (default: 30): ").strip() or 30)
                cleanup_stats = cleanup_system_data(days)
                print("Cleanup completed:")
                print(f"  Cycles deleted: {cleanup_stats['cycles_deleted']}")
                print(f"  Links deleted: {cleanup_stats['links_deleted']}")
                print(f"  Chunk files deleted: {cleanup_stats['chunk_files_deleted']}")
            except ValueError:
                print("Invalid input. Please enter a valid number.")
            except Exception as e:
                print(f"Failed to cleanup data: {e}")
            input("\nPress Enter to continue...")

        elif choice == "4":
            print("\nSEARCH DATA:")
            search_query = input("Enter search query: ").strip()
            if not search_query:
                print("Search query cannot be empty.")
            else:
                search_data(search_query)

        elif choice == "5":
            break

        else:
            print("Invalid option. Please try again.")
            input("\nPress Enter to continue...")

def search_data(query: str) -> None:
    """Search data based on a query and display results"""
    from data_manager import DataReadabilityManager

    manager = DataReadabilityManager()
    print(f"Searching for: {query}")

    # Example: Search websites by name or URL
    with sqlite3.connect(manager.db.db_path) as conn:
        conn.row_factory = sqlite3.Row
        results = conn.execute(
            """
            SELECT name, url, total_links_found, total_photos_downloaded
            FROM websites
            WHERE name LIKE ? OR url LIKE ?
            """,
            (f"%{query}%", f"%{query}%")
        ).fetchall()

        if results:
            print("\nSearch Results:")
            for result in results:
                print(f"  Name: {result['name']}")
                print(f"  URL: {result['url']}")
                print(f"  Links Found: {result['total_links_found']}")
                print(f"  Photos Downloaded: {result['total_photos_downloaded']}")
                print("-")
        else:
            print("No results found.")

    input("\nPress Enter to continue...")

def handle_link_spider():
    """Handle link spider menu"""
    while True:
        clear_screen()
        print_banner()
        print_link_spider_menu()
        
        choice = input("SELECT Select option (1-6): ").strip()
        
        if choice == '1':
            asyncio.run(start_link_crawling())
        elif choice == '2':
            asyncio.run(select_website_for_links())
        elif choice == '3':
            configure_link_spider_settings()
        elif choice == '4':
            view_saved_links()
        elif choice == '5':
            show_discovery_report()
        elif choice == '6':
            break
        else:
            print("ERROR: Invalid option selected.")
            input("Press Enter to continue...")

async def select_website_for_links():
    """Select specific website for link crawling"""
    print("\nSELECT WEBSITE FOR LINK CRAWLING")
    print("-" * 40)

    websites = get_enabled_websites()

    if not websites:
        print("ERROR: No enabled websites found.")
        print(" Add and enable some websites first.")
        input("Press Enter to continue...")
        return

    print("Available websites:")
    for i, website in enumerate(websites, 1):
        print(f"{i}. {website.get('name', 'Unknown')} - {website.get('url', 'N/A')}")

    try:
        choice = int(input("Select website number: ").strip())
        if 1 <= choice <= len(websites):
            selected_website = websites[choice - 1]
            website_name = selected_website.get('name', 'Unknown')
            website_url = selected_website.get('url')
            
            print(f"\nSelected: {website_name}")
            
            # Configuration options
            print("\nCRAWL CONFIGURATION:")
            
            # Max depth
            max_depth_input = input("Maximum crawl depth (default: 3): ").strip()
            try:
                max_depth = int(max_depth_input) if max_depth_input else 3
            except ValueError:
                max_depth = 3
            
            # Max pages
            max_pages_input = input("Maximum pages to crawl (default: 100): ").strip()
            try:
                max_pages = int(max_pages_input) if max_pages_input else 100
            except ValueError:
                max_pages = 100
            
            # Website discovery options
            print("\nWEBSITE DISCOVERY:")
            print("1. Discover but don't add to config")
            print("2. Auto-add discovered websites (disabled)")
            print("3. Auto-add discovered websites (enabled)")
            
            discovery_choice = input("Choose discovery option (1-3, default: 1): ").strip()
            if not discovery_choice:
                discovery_choice = '1'
                
            auto_add_websites = discovery_choice in ['2', '3']
            auto_enable_websites = discovery_choice == '3'
            
            print(f"\nCONFIGURATION:")
            print(f"  Website: {website_name}")
            print(f"  URL: {website_url}")
            print(f"  Max Depth: {max_depth}")
            print(f"  Max Pages: {max_pages}")
            print(f"  Auto-add websites: {auto_add_websites}")
            print(f"  Auto-enable websites: {auto_enable_websites}")
            
            confirm = input("\nPROCEED Start link crawling? (y/N): ").strip().lower()
            if confirm != 'y':
                print("CANCELLED: Link crawling cancelled.")
                input("Press Enter to continue...")
                return
            
            # Import and run link spider
            try:
                from link_spider import LinkSpider
                
                print(f"\nSTARTING Link crawling for: {website_name}")
                print("This may take a few minutes...")
                
                # Create spider and run
                spider = LinkSpider(website_name)
                
                # Ensure website_url is string
                url_to_crawl = str(website_url) if website_url else ""
                
                # Run async crawl
                result = await spider.crawl_website_urls([url_to_crawl], auto_add_websites, auto_enable_websites)
                
                links_found = result.get('total_links_found', 0)
                pages_crawled = result.get('pages_crawled', 0)
                discovered_count = result.get('discovered_websites_count', 0)
                added_count = result.get('websites_added_to_config', 0)
                
                print(f"\nCRAWLING COMPLETED!")
                print("=" * 50)
                print(f"Website: {website_name}")
                print(f"Pages crawled: {pages_crawled}")
                print(f"Links found: {links_found}")
                print(f"Websites discovered: {discovered_count}")
                print(f"Websites added to config: {added_count}")
                
                if links_found > 0:
                    print(f"SUCCESS: {links_found} links found and saved!")
                    
                    # Show sample links
                    if result.get('links'):
                        print(f"\nSAMPLE LINKS (first 5):")
                        for i, link in enumerate(result['links'][:5], 1):
                            print(f"  {i}. {link}")
                        if len(result['links']) > 5:
                            print(f"  ... and {len(result['links']) - 5} more links")
                else:
                    print("WARNING: No links found.")
                
                if discovered_count > 0:
                    print(f"\nDISCOVERY: Found {discovered_count} new websites!")
                    if added_count > 0:
                        print(f"ADDED: {added_count} websites added to configuration")
                
            except ImportError:
                print("ERROR: Link spider module not found.")
            except Exception as e:
                print(f"ERROR: Failed to run link spider: {e}")
                import traceback
                traceback.print_exc()
                
        else:
            print("ERROR: Invalid choice.")
    except ValueError:
        print("ERROR: Invalid input.")
    
    input("\nPress Enter to continue...")

def configure_link_spider_settings():
    """Configure link spider settings"""
    clear_screen()
    print_banner()
    print("LINK SPIDER SETTINGS")
    print("=" * 50)
    print()
    
    try:
        config = get_config()
        
        print("CURRENT SETTINGS:")
        current_settings = {
            'max_depth': config.settings.get('link_spider', {}).get('max_depth', 3),
            'max_pages_per_site': config.settings.get('link_spider', {}).get('max_pages_per_site', 100),
            'max_concurrent': config.settings.get('link_spider', {}).get('max_concurrent', 5),
            'timeout_seconds': config.settings.get('link_spider', {}).get('timeout_seconds', 30),
            'respect_robots': config.settings.get('link_spider', {}).get('respect_robots', True),
            'follow_external': config.settings.get('link_spider', {}).get('follow_external', True),
            'save_page_content': config.settings.get('link_spider', {}).get('save_page_content', False),
            'auto_discover_websites': config.settings.get('link_spider', {}).get('auto_discover_websites', True)
        }
        
        print(f"  Max crawl depth: {current_settings['max_depth']}")
        print(f"  Max pages per site: {current_settings['max_pages_per_site']}")
        print(f"  Max concurrent requests: {current_settings['max_concurrent']}")
        print(f"  Timeout: {current_settings['timeout_seconds']} seconds")
        print(f"  Respect robots.txt: {current_settings['respect_robots']}")
        print(f"  Follow external links: {current_settings['follow_external']}")
        print(f"  Save page content: {current_settings['save_page_content']}")
        print(f"  Auto-discover websites: {current_settings['auto_discover_websites']}")
        print()
        
        print("CONFIGURATION OPTIONS:")
        print("1. Change max crawl depth")
        print("2. Set max pages per site")
        print("3. Adjust concurrent requests")
        print("4. Set timeout duration")
        print("5. Toggle robots.txt respect")
        print("6. Toggle external link following")
        print("7. Toggle page content saving")
        print("8. Toggle auto website discovery")
        print("9. Reset to defaults")
        print("10. Back to link spider menu")
        print()
        
        choice = input("SELECT Choose option (1-10): ").strip()
        
        if choice == '1':
            # Max crawl depth
            current = current_settings['max_depth']
            new_value = input(f"Max crawl depth ({current}): ").strip()
            try:
                if new_value:
                    new_depth = int(new_value)
                    if 1 <= new_depth <= 10:
                        current_settings['max_depth'] = new_depth
                        print(f"SUCCESS: Max crawl depth set to {new_depth}")
                    else:
                        print("ERROR: Must be between 1 and 10")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '2':
            # Max pages per site
            current = current_settings['max_pages_per_site']
            new_value = input(f"Max pages per site ({current}): ").strip()
            try:
                if new_value:
                    new_pages = int(new_value)
                    if new_pages > 0:
                        current_settings['max_pages_per_site'] = new_pages
                        print(f"SUCCESS: Max pages per site set to {new_pages}")
                    else:
                        print("ERROR: Must be positive number")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '3':
            # Max concurrent requests
            current = current_settings['max_concurrent']
            new_value = input(f"Max concurrent requests ({current}): ").strip()
            try:
                if new_value:
                    new_concurrent = int(new_value)
                    if 1 <= new_concurrent <= 20:
                        current_settings['max_concurrent'] = new_concurrent
                        print(f"SUCCESS: Max concurrent requests set to {new_concurrent}")
                    else:
                        print("ERROR: Must be between 1 and 20")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '4':
            # Timeout duration
            current = current_settings['timeout_seconds']
            new_value = input(f"Timeout in seconds ({current}): ").strip()
            try:
                if new_value:
                    new_timeout = int(new_value)
                    if new_timeout > 0:
                        current_settings['timeout_seconds'] = new_timeout
                        print(f"SUCCESS: Timeout set to {new_timeout} seconds")
                    else:
                        print("ERROR: Must be positive number")
            except ValueError:
                print("ERROR: Invalid number")
                
        elif choice == '5':
            # Toggle robots.txt respect
            current = current_settings['respect_robots']
            toggle = input(f"Respect robots.txt? ({'y' if current else 'n'}): ").strip().lower()
            if toggle:
                new_value = toggle == 'y'
                current_settings['respect_robots'] = new_value
                print(f"SUCCESS: Robots.txt respect set to {new_value}")
                
        elif choice == '6':
            # Toggle external link following
            current = current_settings['follow_external']
            toggle = input(f"Follow external links? ({'y' if current else 'n'}): ").strip().lower()
            if toggle:
                new_value = toggle == 'y'
                current_settings['follow_external'] = new_value
                print(f"SUCCESS: External link following set to {new_value}")
                
        elif choice == '7':
            # Toggle page content saving
            current = current_settings['save_page_content']
            toggle = input(f"Save page content? ({'y' if current else 'n'}): ").strip().lower()
            if toggle:
                new_value = toggle == 'y'
                current_settings['save_page_content'] = new_value
                print(f"SUCCESS: Page content saving set to {new_value}")
                
        elif choice == '8':
            # Toggle auto website discovery
            current = current_settings['auto_discover_websites']
            toggle = input(f"Auto-discover websites? ({'y' if current else 'n'}): ").strip().lower()
            if toggle:
                new_value = toggle == 'y'
                current_settings['auto_discover_websites'] = new_value
                print(f"SUCCESS: Auto website discovery set to {new_value}")
                
        elif choice == '9':
            # Reset to defaults
            confirm = input("Reset all settings to defaults? (y/N): ").strip().lower()
            if confirm == 'y':
                default_settings = {
                    'max_depth': 3,
                    'max_pages_per_site': 100,
                    'max_concurrent': 5,
                    'timeout_seconds': 30,
                    'respect_robots': True,
                    'follow_external': True,
                    'save_page_content': False,
                    'auto_discover_websites': True
                }
                current_settings = default_settings
                print("SUCCESS: Settings reset to defaults")
                
        elif choice == '10':
            return
        else:
            print("ERROR: Invalid option")
            input("Press Enter to continue...")
            return
        
        # Save settings if changed
        if choice in ['1', '2', '3', '4', '5', '6', '7', '8', '9']:
            try:
                # Ensure link_spider section exists
                if 'link_spider' not in config.settings:
                    config.settings['link_spider'] = {}
                
                # Update all settings
                config.settings['link_spider'].update(current_settings)
                
                # Save configuration
                save_config()
                print("SUCCESS: Settings saved to configuration file")
                
            except Exception as e:
                print(f"ERROR: Failed to save settings: {e}")
        
    except Exception as e:
        print(f"ERROR: Failed to load settings: {e}")
    
    input("\nPress Enter to continue...")

def view_saved_links():
    """View saved links from link spider"""
    clear_screen()
    print_banner()
    print("SAVED LINKS VIEWER")
    print("=" * 50)
    print()
    
    # Check for link spider data
    data_dir = os.path.join(os.getcwd(), 'data', 'link_spider')
    
    if not os.path.exists(data_dir):
        print("WARNING: No link spider data found.")
        print("Run link crawling first to generate saved links.")
        input("Press Enter to continue...")
        return
    
    try:
        # List available websites
        website_dirs = [d for d in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, d))]
        
        if not website_dirs:
            print("WARNING: No website data found in link spider directory.")
            input("Press Enter to continue...")
            return
        
        print("AVAILABLE WEBSITES:")
        for i, website_dir in enumerate(website_dirs, 1):
            # Check for links file
            links_file = os.path.join(data_dir, website_dir, 'discovered_links.json')
            if os.path.exists(links_file):
                try:
                    with open(links_file, 'r', encoding='utf-8') as f:
                        links_data = json.load(f)
                    link_count = len(links_data.get('links', []))
                    print(f"{i}. {website_dir} ({link_count} links)")
                except:
                    print(f"{i}. {website_dir} (error reading links)")
            else:
                print(f"{i}. {website_dir} (no links file)")
        
        print(f"{len(website_dirs) + 1}. View all links summary")
        print(f"{len(website_dirs) + 2}. Search links")
        print(f"{len(website_dirs) + 3}. Export links")
        print(f"{len(website_dirs) + 4}. Back to link spider menu")
        print()
        
        choice = input(f"SELECT Choose option (1-{len(website_dirs) + 4}): ").strip()
        
        try:
            choice_num = int(choice)
            
            if 1 <= choice_num <= len(website_dirs):
                # View specific website links
                selected_website = website_dirs[choice_num - 1]
                view_website_links(selected_website, data_dir)
                
            elif choice_num == len(website_dirs) + 1:
                # View all links summary
                view_all_links_summary(website_dirs, data_dir)
                
            elif choice_num == len(website_dirs) + 2:
                # Search links
                search_saved_links(website_dirs, data_dir)
                
            elif choice_num == len(website_dirs) + 3:
                # Export links
                export_saved_links(website_dirs, data_dir)
                
            elif choice_num == len(website_dirs) + 4:
                return
            else:
                print("ERROR: Invalid choice.")
                
        except ValueError:
            print("ERROR: Invalid input.")
        
    except Exception as e:
        print(f"ERROR: Error accessing link data: {e}")
    
    input("\nPress Enter to continue...")

def view_website_links(website_name, data_dir):
    """View links for a specific website"""
    print(f"\nLINKS FOR: {website_name}")
    print("-" * 60)
    
    links_file = os.path.join(data_dir, website_name, 'discovered_links.json')
    
    if not os.path.exists(links_file):
        print("ERROR: No links file found for this website.")
        return
    
    try:
        with open(links_file, 'r', encoding='utf-8') as f:
            links_data = json.load(f)
        
        links = links_data.get('links', [])
        metadata = links_data.get('metadata', {})
        
        print(f"METADATA:")
        print(f"  Total links: {len(links)}")
        print(f"  Crawled on: {metadata.get('crawl_date', 'Unknown')}")
        print(f"  Base URL: {metadata.get('base_url', 'Unknown')}")
        print(f"  Crawl depth: {metadata.get('max_depth', 'Unknown')}")
        print()
        
        if not links:
            print("WARNING: No links found.")
            return
        
        # Show links with pagination
        page_size = 20
        total_pages = (len(links) + page_size - 1) // page_size
        current_page = 1
        
        while True:
            start_idx = (current_page - 1) * page_size
            end_idx = min(start_idx + page_size, len(links))
            
            print(f"LINKS (Page {current_page}/{total_pages}):")
            for i, link in enumerate(links[start_idx:end_idx], start_idx + 1):
                if isinstance(link, dict):
                    url = link.get('url', 'Unknown')
                    title = link.get('title', 'No title')
                    print(f"  {i}. {url}")
                    if title != 'No title':
                        print(f"      Title: {title}")
                else:
                    print(f"  {i}. {link}")
            
            print()
            if total_pages > 1:
                print("OPTIONS:")
                if current_page > 1:
                    print("  p - Previous page")
                if current_page < total_pages:
                    print("  n - Next page")
                print("  s - Search in these links")
                print("  e - Export these links")
                print("  q - Back to website list")
                
                nav_choice = input("Choose option: ").strip().lower()
                
                if nav_choice == 'p' and current_page > 1:
                    current_page -= 1
                elif nav_choice == 'n' and current_page < total_pages:
                    current_page += 1
                elif nav_choice == 's':
                    search_term = input("Enter search term: ").strip()
                    if search_term:
                        matching_links = []
                        for link in links:
                            link_str = str(link).lower()
                            if search_term.lower() in link_str:
                                matching_links.append(link)
                        
                        print(f"\nFOUND {len(matching_links)} matching links:")
                        for i, link in enumerate(matching_links[:10], 1):
                            if isinstance(link, dict):
                                print(f"  {i}. {link.get('url', 'Unknown')}")
                            else:
                                print(f"  {i}. {link}")
                        if len(matching_links) > 10:
                            print(f"  ... and {len(matching_links) - 10} more")
                elif nav_choice == 'e':
                    export_filename = f"links_{website_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    try:
                        with open(export_filename, 'w', encoding='utf-8') as f:
                            for link in links:
                                if isinstance(link, dict):
                                    f.write(f"{link.get('url', 'Unknown')}\n")
                                else:
                                    f.write(f"{link}\n")
                        print(f"SUCCESS: Links exported to {export_filename}")
                    except Exception as e:
                        print(f"ERROR: Failed to export links: {e}")
                elif nav_choice == 'q':
                    break
                else:
                    print("ERROR: Invalid option.")
            else:
                break
        
    except Exception as e:
        print(f"ERROR: Failed to read links file: {e}")

def view_all_links_summary(website_dirs, data_dir):
    """View summary of all saved links"""
    print(f"\nALL LINKS SUMMARY")
    print("-" * 60)
    
    total_links = 0
    website_stats = []
    
    for website_dir in website_dirs:
        links_file = os.path.join(data_dir, website_dir, 'discovered_links.json')
        
        if os.path.exists(links_file):
            try:
                with open(links_file, 'r', encoding='utf-8') as f:
                    links_data = json.load(f)
                
                links = links_data.get('links', [])
                metadata = links_data.get('metadata', {})
                
                link_count = len(links)
                total_links += link_count
                
                website_stats.append({
                    'name': website_dir,
                    'links': link_count,
                    'crawl_date': metadata.get('crawl_date', 'Unknown'),
                    'base_url': metadata.get('base_url', 'Unknown')
                })
                
            except Exception as e:
                print(f"WARNING: Error reading {website_dir}: {e}")
    
    print(f"SUMMARY:")
    print(f"  Total websites: {len(website_stats)}")
    print(f"  Total links: {total_links}")
    print()
    
    print("WEBSITE BREAKDOWN:")
    for stat in sorted(website_stats, key=lambda x: x['links'], reverse=True):
        print(f"  {stat['name']}: {stat['links']} links")
        print(f"    URL: {stat['base_url']}")
        print(f"    Crawled: {stat['crawl_date']}")
        print()

def search_saved_links(website_dirs, data_dir):
    """Search through all saved links"""
    search_term = input("Enter search term: ").strip()
    
    if not search_term:
        print("ERROR: No search term provided.")
        return
    
    print(f"\nSEARCHING for '{search_term}'...")
    print("-" * 60)
    
    total_matches = 0
    
    for website_dir in website_dirs:
        links_file = os.path.join(data_dir, website_dir, 'discovered_links.json')
        
        if os.path.exists(links_file):
            try:
                with open(links_file, 'r', encoding='utf-8') as f:
                    links_data = json.load(f)
                
                links = links_data.get('links', [])
                matches = []
                
                for link in links:
                    link_str = str(link).lower()
                    if search_term.lower() in link_str:
                        matches.append(link)
                
                if matches:
                    print(f"\n{website_dir} ({len(matches)} matches):")
                    for i, link in enumerate(matches[:5], 1):
                        if isinstance(link, dict):
                           
                            print(f"  {i}. {link.get('url', 'Unknown')}")
                        else:
                            print(f"  {i}. {link}")
                    
                    if len(matches) > 5:
                        print(f"  ... and {len(matches) - 5} more matches")
                    
                    total_matches += len(matches)
                
            except Exception as e:
                print(f"WARNING: Error searching {website_dir}: {e}")
    
    print(f"\nSEARCH COMPLETE: Found {total_matches} total matches")

def export_saved_links(website_dirs, data_dir):
    """Export all saved links"""
    export_format = input("Export format (txt/json/csv) [txt]: ").strip().lower()
    if not export_format:
        export_format = 'txt'
    
    if export_format not in ['txt', 'json', 'csv']:
        print("ERROR: Invalid format. Using txt.")
        export_format = 'txt'
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    export_filename = f"all_links_{timestamp}.{export_format}"
    
    try:
        all_links = []
        
        # Collect all links
        for website_dir in website_dirs:
            links_file = os.path.join(data_dir, website_dir, 'discovered_links.json')
            
            if os.path.exists(links_file):
                try:
                    with open(links_file, 'r', encoding='utf-8') as f:
                        links_data = json.load(f)
                    
                    links = links_data.get('links', [])
                    metadata = links_data.get('metadata', {})
                    
                    for link in links:
                        if isinstance(link, dict):
                            link_entry = {
                                'website': website_dir,
                                'url': link.get('url', 'Unknown'),
                                'title': link.get('title', ''),
                                'source': metadata.get('base_url', ''),
                                'crawl_date': metadata.get('crawl_date', '')
                            }
                        else:
                            link_entry = {
                                'website': website_dir,
                                'url': str(link),
                                'title': '',
                                'source': metadata.get('base_url', ''),
                                'crawl_date': metadata.get('crawl_date', '')
                            }
                        all_links.append(link_entry)
                        
                except Exception as e:
                    print(f"WARNING: Error processing {website_dir}: {e}")
        
        # Export in chosen format
        if export_format == 'txt':
            with open(export_filename, 'w', encoding='utf-8') as f:
                for link_entry in all_links:
                    f.write(f"{link_entry['url']}\n")
                    
        elif export_format == 'json':
            with open(export_filename, 'w', encoding='utf-8') as f:
                json.dump(all_links, f, indent=2, ensure_ascii=False)
                
        elif export_format == 'csv':
            import csv
            with open(export_filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=['website', 'url', 'title', 'source', 'crawl_date'])
                writer.writeheader()
                writer.writerows(all_links)
        
        print(f"SUCCESS: {len(all_links)} links exported to {export_filename}")
        
    except Exception as e:
        print(f"ERROR: Failed to export links: {e}")

def show_discovery_report():
    """Show recent website discovery report"""
    print("\nWEBSITE DISCOVERY REPORT")
    print("-" * 40)
    
    # Look for recent discovery data in crawl summaries
    data_dir = os.path.join(os.getcwd(), 'data', 'link_spider')
    
    if not os.path.exists(data_dir):
        print("WARNING: No link spider data found.")
        print("Run link crawling first to generate discovery reports.")
        input("Press Enter to continue...")
        return
    
    recent_discoveries = []
    
    # Check all website directories for recent crawl data
    try:
        for site_dir in os.listdir(data_dir):
            site_path = os.path.join(data_dir, site_dir)
            if os.path.isdir(site_path):
                metadata_file = os.path.join(site_path, 'page_metadata.json')
                if os.path.exists(metadata_file):
                    try:
                        import json
                        with open(metadata_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            
                        crawl_summary = data.get('crawl_summary', {})
                        discovered_count = crawl_summary.get('discovered_websites', 0)
                        
                        if discovered_count > 0:
                            recent_discoveries.append({
                                'website': site_dir,
                                'discovered_count': discovered_count,
                                'total_pages': crawl_summary.get('total_pages_crawled', 0),
                                'total_links': crawl_summary.get('total_links_found', 0)
                            })
                    except Exception as e:
                        print(f"WARNING: Error reading data for {site_dir}: {e}")
    
    except Exception as e:
        print(f"ERROR: Error accessing discovery data: {e}")
        input("Press Enter to continue...")
        return
    
    if not recent_discoveries:
        print("INFO: No recent website discoveries found.")
        print("Run link crawling to discover new websites.")
        input("Press Enter to continue...")
        return
    
    # Show discovery summary
    print(f"FOUND: {len(recent_discoveries)} sites have recent discoveries")
    print()
    
    total_discovered = 0
    for discovery in recent_discoveries:
        website = discovery['website']
        count = discovery['discovered_count']
        pages = discovery['total_pages']
        links = discovery['total_links']
        
        print(f"SITE: {website}")
        print(f"  Discovered: {count} new websites")
        print(f"  Crawled: {pages} pages, {links} total links")
        print()
        
        total_discovered += count
    
    print(f"TOTAL: {total_discovered} websites discovered across all crawls")
    print()
    print("TIP: New websites are automatically added to your configuration")
    print("     when you choose auto-add options during link crawling.")
    
    input("\nPress Enter to continue...")

# Statistics and Reports
def show_statistics():
    """Show statistics and reports"""
    clear_screen()
    print_banner()
    print("📊 STATISTICS & REPORTS")
    print("=" * 50)
    
    try:
        # Show data readability summary
        from data_manager import show_data_summary, export_system_report, cleanup_system_data
        
        metrics = show_data_summary()
        print()
        
        # Additional statistics
        websites = get_websites()
        enabled_websites = len([w for w in websites if w.get('enabled', True)])
        
        print("📈 DETAILED STATISTICS:")
        print(f"  Configuration file: {len(websites)} total websites")
        print(f"  Active websites: {enabled_websites} enabled")
        print(f"  Inactive websites: {len(websites) - enabled_websites} disabled")
        
        # Check downloads directory
        DOWNLOADS_DIR = os.path.join(os.getcwd(), 'downloads')
        if os.path.exists(DOWNLOADS_DIR):
            subdirs = [d for d in os.listdir(DOWNLOADS_DIR) if os.path.isdir(os.path.join(DOWNLOADS_DIR, d))]
            print(f"  Download folders: {len(subdirs)} website directories")
            
            # Calculate download folder sizes
            total_size = 0
            for subdir in subdirs:
                subdir_path = os.path.join(DOWNLOADS_DIR, subdir)
                try:
                    for root, dirs, files in os.walk(subdir_path):
                        for file in files:
                            total_size += os.path.getsize(os.path.join(root, file))
                except OSError:
                    continue
            
            print(f"  Download size: {total_size / (1024*1024):.1f} MB")
        else:
            print("  Download folders: None (no downloads yet)")
        
        print()
        print("🔧 MAINTENANCE OPTIONS:")
        print("1. Export detailed system report")
        print("2. Clean up old data (30+ days)")
        print("3. View automation summary")
        print("4. Return to main menu")
        
        choice = input("SELECT Option (1-4): ").strip()
        
        if choice == '1':
            print("\n📄 EXPORTING SYSTEM REPORT...")
            report_path = export_system_report()
            print(f"SUCCESS: Report exported to: {report_path}")
            
        elif choice == '2':
            print("\n🧹 CLEANING UP OLD DATA...")
            cleanup_results = cleanup_system_data(30)
            print(f"CLEANED: {cleanup_results.get('cycles_deleted', 0)} old cycles")
            print(f"CLEANED: {cleanup_results.get('links_deleted', 0)} orphaned links")
            print(f"CLEANED: {cleanup_results.get('chunk_files_deleted', 0)} old chunk files")
            
        elif choice == '3':
            show_automation_summary()
            return
            
        elif choice == '4':
            return
        
    except ImportError:
        print("WARNING: Advanced data management not available.")
        print("Using basic statistics only.")
        
        websites = get_websites()
        enabled_websites = len([w for w in websites if w.get('enabled', True)])
        
        print(f"TOTAL: Total websites configured: {len(websites)}")
        print(f"SUCCESS: Enabled websites: {enabled_websites}")
        print(f"ERROR: Disabled websites: {len(websites) - enabled_websites}")
        
        # Check downloads directory
        DOWNLOADS_DIR = os.path.join(os.getcwd(), 'downloads')
        if os.path.exists(DOWNLOADS_DIR):
            subdirs = [d for d in os.listdir(DOWNLOADS_DIR) if os.path.isdir(os.path.join(DOWNLOADS_DIR, d))]
            print(f"FOLDER: Downloaded content folders: {len(subdirs)}")
        else:
            print("FOLDER: No downloads folder found")
    
    except Exception as e:
        print(f"ERROR: Failed to load statistics: {e}")
    
    input("\nPress Enter to continue...")

def handle_proxy_management():
    """Handle proxy management functionality"""
    clear_screen()
    print_banner()
    print("PROXY MANAGEMENT")
    print("=" * 50)
    print()
    
    try:
        from proxy_manager import ProxyManager
        
        proxy_file = os.path.join('data', 'proxies.txt')
        
        print("PROXY OPTIONS:")
        print("1. Test Existing Proxies")
        print("2. View Proxy File")
        print("3. View Proxy Statistics")
        print("4. Add New Proxy")
        print("5. Create Sample Proxy File")
        print("6. Back to Main Menu")
        print()
        
        choice = input("SELECT Choose option (1-6): ").strip()
        
        if choice == '1':
            # Test existing proxies
            clear_screen()
            print_banner()
            print("TESTING PROXIES")
            print("=" * 50)
            print()
            
            if not os.path.exists(proxy_file):
                print("❌ No proxy file found at data/proxies.txt")
                print("💡 Use option 5 to create a sample proxy file")
                input("Press Enter to continue...")
                return
            
            print("🔄 Loading and testing proxies...")
            proxy_manager = ProxyManager(proxy_file)
            
            test_results = proxy_manager.test_all_proxies()
            working_count = test_results.get('working', 0)
            total_count = test_results.get('total', 0)
            
            print(f"\n✅ Found {working_count} working proxies out of {total_count}")
            
            if working_count > 0:
                print("\nWORKING PROXIES:")
                # Get working proxies from the manager
                working_proxies = proxy_manager.working_proxies
                for i, proxy in enumerate(list(working_proxies)[:5], 1):  # Show first 5
                    print(f"  {i}. {proxy.get('url', proxy.get('http', 'Unknown'))}")
                if len(working_proxies) > 5:
                    print(f"  ... and {len(working_proxies) - 5} more")
            
        elif choice == '2':
            # View proxy file
            clear_screen()
            print_banner()
            print("PROXY FILE CONTENTS")
            print("=" * 50)
            print()
            
            if os.path.exists(proxy_file):
                try:
                    with open(proxy_file, 'r') as f:
                        content = f.read().strip()
                    
                    if content:
                        lines = content.split('\n')
                        print(f"📄 File: {proxy_file}")
                        print(f"📝 Lines: {len(lines)}")
                        print()
                        print("CONTENT:")
                        print(content)
                    else:
                        print("📄 Proxy file exists but is empty")
                        
                except Exception as e:
                    print(f"❌ Error reading proxy file: {e}")
            else:
                print("❌ No proxy file found at data/proxies.txt")
                print("💡 Use option 5 to create a sample proxy file")
        
        elif choice == '3':
            # View proxy statistics  
            clear_screen()
            print_banner()
            print("PROXY STATISTICS")
            print("=" * 50)
            print()
            
            if not os.path.exists(proxy_file):
                print("❌ No proxy file found")
                input("Press Enter to continue...")
                return
            
            proxy_manager = ProxyManager(proxy_file)
            stats = proxy_manager.get_proxy_stats()
            
            print(f"📊 PROXY STATISTICS:")
            print(f"  Total proxies loaded: {stats.get('total_proxies', 0)}")
            print(f"  Working proxies: {stats.get('working_proxies', 0)}")
            print(f"  Failed proxies: {stats.get('failed_proxies', 0)}")
            print(f"  Tests performed: {stats.get('tests_performed', 0)}")
            print(f"  Average response time: {stats.get('avg_response_time', 0):.2f}s")
            
        elif choice == '4':
            # Add new proxy
            clear_screen()
            print_banner()
            print("ADD NEW PROXY")
            print("=" * 50)
            print()
            
            print("Enter proxy details:")
            print("Format examples:")
            print("  http://proxy-server:port")
            print("  http://username:password@proxy-server:port")
            print("  socks5://proxy-server:port")
            print()
            
            proxy_url = input("PROXY URL: ").strip()
            if not proxy_url:
                print("❌ No proxy URL provided")
                input("Press Enter to continue...")
                return
            
            # Ensure data directory exists
            os.makedirs('data', exist_ok=True)
            
            # Append to proxy file
            with open(proxy_file, 'a') as f:
                f.write(f"\n{proxy_url}")
            
            print(f"✅ Proxy added to {proxy_file}")
            
        elif choice == '5':
            # Create sample proxy file
            clear_screen()
            print_banner()
            print("CREATE SAMPLE PROXY FILE")
            print("=" * 50)
            print()
            
            if os.path.exists(proxy_file):
                overwrite = input("⚠️ Proxy file already exists. Overwrite? (y/N): ").strip().lower()
                if overwrite != 'y':
                    print("❌ Operation cancelled")
                    input("Press Enter to continue...")
                    return
            
            # Ensure data directory exists
            os.makedirs('data', exist_ok=True)
            
            sample_content = """# Proxy Configuration File
# Format: protocol://[username:password@]host:port
# Supported protocols: http, https, socks4, socks5

# Example free proxies (may not work)
http://8.210.83.33:80
http://47.91.45.198:80
http://103.127.1.130:80

# Example premium proxy services (replace with your credentials)
# http://username:password@premium-proxy.com:8080
# socks5://username:password@premium-proxy.com:1080

# Notes:
# - Free proxies are unreliable and often blocked
# - For production use, consider premium proxy services like:
#   * ProxyMesh, Bright Data, Smartproxy, Oxylabs
# - Always test proxies before relying on them
# - Some proxies may require authentication"""
            
            with open(proxy_file, 'w') as f:
                f.write(sample_content)
            
            print(f"✅ Sample proxy file created at {proxy_file}")
            print("📝 Edit the file to add your own proxy servers")
            
        elif choice == '6':
            return
        else:
            print("❌ Invalid option selected")
            
    except ImportError as e:
        print(f"❌ Error: Could not import proxy manager: {e}")
        print("Make sure proxy_manager.py exists in the current directory")
    except Exception as e:
        print(f"❌ Error in proxy management: {e}")
        import traceback
        traceback.print_exc()
    
    input("\nPress Enter to continue...")

def handle_advanced_statistics_menu():
    """Advanced Statistics & Reports menu"""
    from data_manager import DataReadabilityManager

    manager = DataReadabilityManager()

    while True:
        clear_screen()
        print_banner()
        print("ADVANCED STATISTICS & REPORTS")
        print("=" * 50)
        print("1. View Advanced Statistics")
        print("2. Export Detailed Report")
        print("3. Return to Main Menu")
        print()

        choice = input("Select an option: ").strip()

        if choice == "1":
            print("\nADVANCED STATISTICS:")
            stats = manager.get_advanced_statistics()
            print(f"Total Websites: {stats['total_websites']}")
            print(f"Enabled Websites: {stats['enabled_websites']}")
            print(f"Average Links per Site: {stats['avg_links_per_site']:.2f}")
            print(f"Average Photos per Site: {stats['avg_photos_per_site']:.2f}")
            print("\nTop Websites by Activity:")
            for site in stats['top_sites']:
                print(f"  {site['name']}: {site['total_links_found']} links, {site['total_photos_downloaded']} photos")
            print("\nRecent Activity:")
            for cycle in stats['recent_cycles']:
                print(f"  {cycle['cycle_id']}: {cycle['websites_processed']} sites, {cycle['photos_downloaded']} photos, {cycle['new_websites_added']} new sites")
            input("\nPress Enter to continue...")

        elif choice == "2":
            print("\nEXPORT DETAILED REPORT:")
            try:
                report_path = manager.export_readable_report()
                print(f"Report successfully exported to: {report_path}")
            except Exception as e:
                print(f"Failed to export report: {e}")
            input("\nPress Enter to continue...")

        elif choice == "3":
            break

        else:
            print("Invalid option. Please try again.")
            input("\nPress Enter to continue...")

def handle_analytics_report():
    """Generate and display analytics report"""
    from data_manager import DataReadabilityManager

    manager = DataReadabilityManager()
    print("\nGENERATING ANALYTICS REPORT:")

    try:
        stats = manager.get_advanced_statistics()
        print("\nANALYTICS REPORT:")
        print(f"Total Websites: {stats['total_websites']}")
        print(f"Enabled Websites: {stats['enabled_websites']}")
        print(f"Average Links per Site: {stats['avg_links_per_site']:.2f}")
        print(f"Average Photos per Site: {stats['avg_photos_per_site']:.2f}")
        print("\nTop Websites by Activity:")
        for site in stats['top_sites']:
            print(f"  {site['name']}: {site['total_links_found']} links, {site['total_photos_downloaded']} photos")
        print("\nRecent Activity:")
        for cycle in stats['recent_cycles']:
            print(f"  {cycle['cycle_id']}: {cycle['websites_processed']} sites, {cycle['photos_downloaded']} photos, {cycle['new_websites_added']} new sites")
    except Exception as e:
        print(f"Failed to generate analytics report: {e}")

    input("\nPress Enter to continue...")

# Main Application Loop
def main():
    """Main application entry point"""
    while True:
        clear_screen()
        print_banner()
        print_main_menu()
        
        choice = input("SELECT Select option (1-10): ").strip()
        
        if choice == '1':
            asyncio.run(run_automated_cycle())
        elif choice == '2':
            show_automation_summary()
        elif choice == '3':
            handle_photo_scraper()
        elif choice == '4':
            handle_link_spider()
        elif choice == '5':
            handle_proxy_management()
        elif choice == '6':
            handle_website_management()
        elif choice == '7':
            handle_quick_bulk_import()
        elif choice == '8':
            handle_data_management_menu()
        elif choice == '9':
            handle_advanced_statistics_menu()
        elif choice == '10':
            print("EXIT Exiting Unified Website Toolkit. Goodbye!")
            break
        else:
            print("ERROR: Invalid option selected.")
            input("Press Enter to continue...")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nINTERRUPT Program interrupted by user. Goodbye!")
    except Exception as e:
        print(f"\nERROR: Fatal error: {e}")
        input("Press Enter to exit...")
