"""
Group Cleanup Script
Scans connected accounts for groups matching ban criteria (Russian/Japanese titles)
and allows the user to leave them in batch after confirmation.
"""
import asyncio
import os
import signal
import sys
import random
from telethon import TelegramClient
from telethon import functions
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.errors import FloodWaitError
from src.core.account_health import AccountHealthPolicy
from src.core.dynamic_config import get_accounts
import src.core.utils as utils

class GroupCleaner:
    def __init__(self):
        self.clients = {}
        self.selected_accounts = []
        self.should_exit = False
        self.account_health = AccountHealthPolicy()
        
        # Setup signal handlers for graceful exit
        signal.signal(signal.SIGINT, self.handle_exit)
        signal.signal(signal.SIGTERM, self.handle_exit)

    def handle_exit(self, signum, frame):
        """Handle graceful exit on Ctrl+C"""
        print(f"\n🛑 Received exit signal. Exiting safely...")
        self.should_exit = True
        sys.exit(0)

    def select_accounts(self):
        """Allow user to select which accounts to use for cleanup"""
        print("\n👥 Available Accounts:")
        accounts = get_accounts()
        for i, account in enumerate(accounts, 1):
            print(f"{i}️⃣  {account['name']} ({account['phone']})")
        
        print("\n📋 Account Selection Options:")
        print("1️⃣  Use specific account (enter number)")
        print("2️⃣  Use multiple accounts (enter numbers separated by commas)")
        print("3️⃣  Use all accounts")
        
        choice = input("\nEnter your choice (1-3): ").strip()
        
        if choice == "1":
            try:
                account_num = int(input("Enter account number: ").strip())
                if 1 <= account_num <= len(accounts):
                    self.selected_accounts = [accounts[account_num - 1]]
                else:
                    print("❌ Invalid account number")
                    return False
            except ValueError:
                print("❌ Invalid input")
                return False
                
        elif choice == "2":
            try:
                numbers = input("Enter account numbers (e.g., 1,3): ").strip().split(',')
                selected = []
                for num_str in numbers:
                    num = int(num_str.strip())
                    if 1 <= num <= len(accounts):
                        selected.append(accounts[num - 1])
                if selected:
                    self.selected_accounts = selected
                else:
                    print("❌ No valid accounts selected")
                    return False
            except ValueError:
                print("❌ Invalid input format")
                return False
                
        elif choice == "3":
            self.selected_accounts = accounts.copy()
        else:
            print("❌ Invalid choice")
            return False
        
        print(f"✅ Selected {len(self.selected_accounts)} accounts")
        return True

    async def initialize_clients(self):
        """Initialize selected Telegram clients"""
        print("🔧 Initializing clients...")
        for account in self.selected_accounts:
            if self.account_health.is_retired(account['name']):
                continue
            client = None
            try:
                client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
                await client.start(account['phone'])
                self.clients[account['name']] = client
                print(f"✅ Connected: {account['name']}")
            except Exception as e:
                recovered = await self.account_health.handle_account_failure(client, account, e, "leave_groups startup")
                if not recovered:
                    print(f"❌ Failed to connect {account['name']}: {e}")

    async def scan_groups(self):
        """Scan groups and identify candidates for leaving"""
        candidates = [] # List of tuples: (account_name, entity, title, reason)
        
        print(f"\n🔍 Scanning {len(self.clients)} accounts for unwanted groups...")
        
        for name, client in self.clients.items():
            print(f"   Scanning account: {name}...")
            try:
                async for dialog in client.iter_dialogs():
                    if self.should_exit: break
                    
                    # Skip private chats (Users)
                    if dialog.is_user:
                        continue
                        
                    title = dialog.title
                    entity = dialog.entity
                    
                    is_valid, reason = utils.is_valid_group_name(title)
                    
                    if is_valid and isinstance(entity, (Channel, Chat)):
                        try:
                            full = await client(functions.channels.GetFullChannelRequest(entity))
                            if getattr(full.full_chat, 'participants_hidden', False):
                                is_valid = False
                                reason = "Members List Hidden"
                        except Exception:
                            pass
                    
                    if not is_valid:
                        print(f"   ⚠️ Found blocked group: '{title}' ({reason})")
                        candidates.append({
                            'account': name,
                            'client': client,
                            'entity': entity,
                            'title': title,
                            'reason': reason,
                            'id': dialog.id
                        })
                        
            except Exception as e:
                print(f"❌ Error scanning {name}: {e}")
        
        return candidates

    async def leave_candidate_groups(self, candidates):
        """Leave the confirmed groups"""
        print(f"\n🚀 Starting cleanup process for {len(candidates)} groups...")
        
        left_count = 0
        failed_count = 0
        
        for i, item in enumerate(candidates, 1):
            if self.should_exit: break
            
            client = item['client']
            entity = item['entity']
            title = item['title']
            account = item['account']
            
            print(f"[{i}/{len(candidates)}] {account} leaving '{title}'...")
            
            try:
                # Leave mechanism depending on type
                if isinstance(entity, (Channel, Chat)):
                    await client(LeaveChannelRequest(entity))
                else:
                    # For basic chats or if type is ambiguous, delete dialog
                    await client.delete_dialog(entity)
                    
                print(f"✅ Left: {title}")
                left_count += 1
                
                # Small delay to prevent rate limits
                await asyncio.sleep(random.uniform(2, 5))
                
            except FloodWaitError as e:
                print(f"⏳ Rate limited. Waiting {e.seconds}s...")
                await asyncio.sleep(e.seconds)
                # Retry once
                try:
                    await client(LeaveChannelRequest(entity))
                    print(f"✅ Left (after retry): {title}")
                    left_count += 1
                except:
                    print(f"❌ Failed to leave '{title}' after retry")
                    failed_count += 1
            except Exception as e:
                print(f"❌ Failed to leave '{title}': {e}")
                failed_count += 1
        
        print(f"\n📈 Cleanup Summary:")
        print(f"✅ Left: {left_count}")
        print(f"❌ Failed: {failed_count}")

    async def run(self):
        """Main execution flow"""
        print("🧹 Telegram Group Cleanup Check")
        print("This tool will scan for groups with Russian/Japanese titles and help you leave them.")
        
        if not self.selected_accounts:
            if not self.select_accounts():
                return

        await self.initialize_clients()
        
        if not self.clients:
            print("❌ No clients connected. Exiting.")
            return

        # Phase 1: Scan
        candidates = await self.scan_groups()
        
        if not candidates:
            print("\n✅ Good news! No unwanted groups found.")
            # Disconnect
            for client in self.clients.values():
                await client.disconnect()
            return

        # Phase 2: Confirmation
        print(f"\n🚨 FOUND {len(candidates)} GROUPS TO LEAVE:")
        print("-" * 60)
        print(f"{'Account':<15} | {'Reason':<20} | {'Group Name'}")
        print("-" * 60)
        
        # Show first 20 items to avoid spamming console if too many
        for item in candidates[:20]:
            print(f"{item['account']:<15} | {item['reason']:<20} | {item['title']}")
        
        if len(candidates) > 20:
            print(f"... and {len(candidates) - 20} more groups.")
        
        print("-" * 60)
        confirm = input(f"\n⚠️ Do you want to leave these {len(candidates)} groups? [y/N]: ").strip().lower()
        
        # Phase 3: Execution
        if confirm == 'y':
            await self.leave_candidate_groups(candidates)
        else:
            print("🛑 Operation cancelled. No groups were left.")

        # Cleanup
        for client in self.clients.values():
            await client.disconnect()
        print("\n👋 Done.")

if __name__ == "__main__":
    cleaner = GroupCleaner()
    asyncio.run(cleaner.run())
