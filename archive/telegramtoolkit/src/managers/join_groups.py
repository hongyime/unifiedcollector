"""
Unified Group Joining Tool
Automatically joins Telegram groups/channels from collected links
"""
import asyncio
import os
import re
import random
import signal
import sys
from telethon import TelegramClient
from telethon import functions
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.errors import (
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    FloodWaitError,
    ChannelPrivateError,
    InviteHashExpiredError
)
from telethon.tl.types import Channel, Chat
from src.core.account_health import AccountHealthPolicy
from src.core.dynamic_config import get_accounts, VALID_LINKS_FILE, JOINED_LINKS_FILE, MIN_DELAY, MAX_DELAY, GROUP_LINK_PATTERN
from src.core.dynamic_config import get_config_value
import src.core.utils as utils

class GroupJoiner:
    def __init__(self):
        self.clients = {}
        self.validation_pool = []  # List of all initialized clients for validation
        self.should_exit = False
        self.selected_accounts = []
        self.account_health = AccountHealthPolicy()
        self.discussion_groups_found = 0  # Track discussion groups found this session
        self.enable_filtering = False     # Language filtering flag
        
        # Setup signal handlers for graceful exit
        signal.signal(signal.SIGINT, self.handle_exit)
        signal.signal(signal.SIGTERM, self.handle_exit)

    def _is_bot_style_link(self, link):
        if not link:
            return False
        return 'bot' in link.lower()
    
    def select_accounts(self):
        """Allow user to select which accounts to use for joining"""
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
                    print(f"✅ Selected: {self.selected_accounts[0]['name']}")
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
                    else:
                        print(f"❌ Invalid account number: {num}")
                        return False
                
                if selected:
                    self.selected_accounts = selected
                    account_names = [acc['name'] for acc in selected]
                    print(f"✅ Selected: {', '.join(account_names)}")
                else:
                    print("❌ No valid accounts selected")
                    return False
            except ValueError:
                print("❌ Invalid input format")
                return False
                
        elif choice == "3":
            self.selected_accounts = accounts.copy()
            account_names = [acc['name'] for acc in self.selected_accounts]
            print(f"✅ Selected all accounts: {', '.join(account_names)}")
            
        else:
            print("❌ Invalid choice")
            return False
        
        return True
    
    def handle_exit(self, signum, frame):
        """Handle graceful exit on Ctrl+C"""
        print(f"\n🛑 Received exit signal ({signum}). Finishing current operations...")
        self.should_exit = True
    
    def extract_links_list(self, file_path):
        """Extract all links from file"""
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return []
        
        links = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):  # Skip empty lines and comments
                        match = re.search(GROUP_LINK_PATTERN, line)
                        if match:
                            candidate = match.group(0).strip().rstrip('`').rstrip('.,;:!?)')
                            if not self._is_bot_style_link(candidate):
                                links.append(candidate)
        except Exception as e:
            print(f"❌ Error reading file {file_path}: {e}")
        
        return links
    
    def load_joined_links(self):
        """Load already joined links"""
        if not os.path.exists(JOINED_LINKS_FILE):
            return set()
        
        try:
            with open(JOINED_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(line.strip() for line in f if line.strip() and not line.strip().startswith('#'))
        except Exception as e:
            print(f"❌ Error reading joined links file: {e}")
            return set()
    
    def save_joined_link(self, link, account_name):
        """Save successfully joined link"""
        try:
            os.makedirs(os.path.dirname(JOINED_LINKS_FILE), exist_ok=True)
            with open(JOINED_LINKS_FILE, 'a', encoding='utf-8') as f:
                f.write(f"{link}  # Joined by: {account_name}\n")
        except Exception as e:
            print(f"❌ Error saving joined link: {e}")
    
    def save_discovered_discussion_group(self, original_link, discussion_link, account_name):
        """Save discovered discussion group link"""
        try:
            discussion_file = os.path.join(os.path.dirname(JOINED_LINKS_FILE), 'discovered_discussion_groups.txt')
            os.makedirs(os.path.dirname(discussion_file), exist_ok=True)
            with open(discussion_file, 'a', encoding='utf-8') as f:
                f.write(f"{discussion_link}  # Discussion for: {original_link} | Found by: {account_name}\n")
        except Exception as e:
            print(f"❌ Error saving discovered discussion group: {e}")
    
    def write_remaining_links(self, links):
        """Write remaining unprocessed links back to file"""
        try:
            with open(VALID_LINKS_FILE, 'w', encoding='utf-8') as f:
                for link in links:
                    f.write(link + '\n')
        except Exception as e:
            print(f"❌ Error writing remaining links: {e}")
    
    async def initialize_clients(self):
        """Initialize selected Telegram clients"""
        print("🔧 Initializing selected Telegram clients...")
        
        # Connect ALL accounts for validation pool
        all_accounts = get_accounts()
        print(f"🔧 Connecting all {len(all_accounts)} accounts for validation pool...")
        
        for account in all_accounts:
            if self.account_health.is_retired(account['name']):
                continue
            client = None
            try:
                client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
                await client.start(account['phone'])
                
                # Add to validation pool
                self.validation_pool.append({
                    'client': client,
                    'account': account,
                    'name': account['name']
                })
                
                # If this account is selected for joining, add to main clients map
                if any(sa['phone'] == account['phone'] for sa in self.selected_accounts):
                    self.clients[account['name']] = {
                        'client': client,
                        'account': account
                    }
                    
                print(f"✅ Connected: {account['name']}")
                
            except Exception as e:
                recovered = await self.account_health.handle_account_failure(client, account, e, "join_groups startup")
                if not recovered:
                    print(f"❌ Failed to connect {account['name']}: {e}")
        
        print(f"📊 Validation pool size: {len(self.validation_pool)}")
        print(f"📊 Active joining accounts: {len(self.clients)}")
    
    async def join_linked_discussion_group(self, entity, account_name, original_link):
        """Try to find and join the linked discussion group for a channel"""
        client_info = self.clients[account_name]
        client = client_info['client']
        
        try:
            # Check if this is a channel and has a linked discussion group
            if not isinstance(entity, Channel) or not hasattr(entity, 'linked_chat_id') or not entity.linked_chat_id:
                return False, "No linked discussion group"
            
            # Get the linked discussion group
            linked_chat_id = entity.linked_chat_id
            
            try:
                # Try to get the linked chat entity
                linked_entity = await client.get_entity(linked_chat_id)
                
                # Check if we're already a member
                try:
                    participants = await client.get_participants(linked_entity, limit=1)
                    # If we can get participants, we're likely already a member
                    print(f"ℹ️ [{account_name}] Already in linked discussion group for: {original_link}")
                    return True, "Already member of discussion group"
                except:
                    # If we can't get participants, we're probably not a member
                    pass
                
                # Try to join the linked discussion group
                if hasattr(linked_entity, 'username') and linked_entity.username:
                    # Public group - join by username
                    await client(functions.channels.JoinChannelRequest(linked_entity.username))
                    discussion_link = f"https://t.me/{linked_entity.username}"
                else:
                    # Private group - try to join by ID (this might not always work)
                    try:
                        await client(functions.channels.JoinChannelRequest(linked_entity))
                        discussion_link = f"Discussion group (ID: {linked_chat_id})"
                    except:
                        return False, "Cannot join private discussion group"
                
                print(f"🎉 [{account_name}] Auto-joined linked discussion group: {discussion_link}")
                
                # Increment counter for session statistics
                self.discussion_groups_found += 1
                
                # Save the discussion group link if it's public
                if hasattr(linked_entity, 'username') and linked_entity.username:
                    discussion_url = f"https://t.me/{linked_entity.username}"
                    self.save_joined_link(discussion_url, f"{account_name} (auto-discovered)")
                    self.save_discovered_discussion_group(original_link, discussion_url, account_name)
                
                return True, f"Joined discussion group: {discussion_link}"
                
            except UserAlreadyParticipantError:
                print(f"ℹ️ [{account_name}] Already in linked discussion group for: {original_link}")
                return True, "Already member of discussion group"
            except Exception as e:
                print(f"⚠️ [{account_name}] Could not join discussion group for {original_link}: {e}")
                return False, f"Discussion group join failed: {str(e)}"
                
        except Exception as e:
            print(f"⚠️ [{account_name}] Error checking for linked discussion group: {e}")
            return False, f"Error checking discussion group: {str(e)}"



    async def validate_link_globally(self, link):
        """
        Validate link using ALL available accounts (Validation Pool).
        Returns: (is_valid, title, invite_info/entity)
        """
        if not self.validation_pool:
            return False, "No clients in pool", None

        # Shuffle pool to distribute load
        pool = self.validation_pool.copy()
        random.shuffle(pool)

        last_error = None
        
        for validator in pool:
            client = validator['client']
            acc_name = validator['name']
            
            try:
                # Case 1: Private Invite Link
                if '/joinchat/' in link or '/+' in link or (len(link.split('/')[-1]) > 20 and '+' in link):
                    hash_part = link.split('/')[-1]
                    if hash_part.startswith('+'):
                        hash_part = hash_part[1:]
                    
                    try:
                        invite_info = await client(CheckChatInviteRequest(hash_part))
                        
                        # Extract title
                        title = "Unknown"
                        if hasattr(invite_info, 'title'):
                            title = invite_info.title
                        elif hasattr(invite_info, 'chat') and hasattr(invite_info.chat, 'title'):
                            title = invite_info.chat.title
                            
                        # print(f"🔍 [{acc_name}] Validated invite: '{title}'")
                        return True, title, hash_part  # Return hash for private
                        
                    except (InviteHashInvalidError, InviteHashExpiredError):
                        return False, "Invalid/Expired Invite", None
                    except Exception as e:
                        # If this specific error is usually fatal for the link, break?
                        # No, continue to next account in case it's a ban/block
                        last_error = e
                        continue

                # Case 2: Public Username
                else:
                    username = link.split('/')[-1].split('?')[0].strip().strip('`').strip() # Clean params
                    username = username.lstrip('@')
                    username = username.rstrip('.,;:!?)')
                    if not username:
                        return False, "Invalid Username", None
                    if 'bot' in username.lower():
                        return False, "Bot link", None
                    try:
                        entity = await client.get_entity(username)
                        if getattr(entity, 'bot', False):
                            return False, "Bot link", None
                        if not isinstance(entity, Channel):
                            return False, "Not a joinable channel/group", None
                        title = getattr(entity, 'title', username)
                        return True, title, entity # Return entity for public
                        
                    except ValueError:
                         return False, "Invalid Username", None
                    except Exception as e:
                         last_error = e
                         continue
                         
            except Exception as e:
                # General client error, try next
                last_error = e
                continue
        
        # If we reach here, all accounts failed
        return False, f"Validation failed on all accounts ({last_error})", None

    async def join_link_with_account(self, link, account_name, pre_validated_info=None):
        """Try to join a link with a specific account using pre-validated info"""
        if account_name not in self.clients:
            return False, "Account not available", None
        
        client_info = self.clients[account_name]
        client = client_info['client']
        
        try:
             # Logic split based on pre_validated_info type
             # pre_validated_info is either hash_string (private) or entity (public)
             
            entity = None

            if isinstance(pre_validated_info, str): 
                # Private Invite Hash
                hash_part = pre_validated_info
                try:
                    result = await client(functions.messages.ImportChatInviteRequest(hash_part))
                    if hasattr(result, 'chats') and result.chats:
                        entity = result.chats[0]
                    return True, "Joined private group", entity
                except UserAlreadyParticipantError:
                    return True, "Already member", None # Can't get entity easily for private if already in
                    
            elif pre_validated_info:
                # Public Entity
                target_entity = pre_validated_info
                if getattr(target_entity, 'bot', False):
                    return False, "Bot link", None
                if not isinstance(target_entity, Channel):
                    return False, "Not a joinable channel/group", None
                try:
                    await client(functions.channels.JoinChannelRequest(target_entity))
                    
                    # Check for linked discussion
                    if hasattr(target_entity, 'title'):
                         # We already have the entity, can check linked chat directly if we fetch full?
                         # Or just return success. Logic for discussion group is separate.
                         pass
                         
                    return True, "Joined public channel", target_entity
                except UserAlreadyParticipantError:
                    return True, "Already member", target_entity

            # Fallback (shouldn't really happen if validation worked)
            return False, "Join logic fallback", None

        except (InviteHashInvalidError, InviteHashExpiredError):
            return False, "Invalid invite", None
        except ChannelPrivateError:
            return False, "Private channel", None
        except FloodWaitError as e:
            return False, f"Rate limited ({e.seconds}s)", None

    async def try_join_link(self, link):
        """Try to join link using selected accounts with fallback and validation"""
        if self._is_bot_style_link(link):
            self.save_joined_link(link, "SYSTEM (Status: Skipped bot link)")
            return False, "Skipped bot link"

        is_valid, title, invite_or_entity = await self.validate_link_globally(link)
        if not is_valid:
            self.save_joined_link(link, f"SYSTEM (Status: {title})")
            return False, title

        if self.enable_filtering:
            group_name = title if title else link
            allowed, filter_reason = utils.is_valid_group_name(group_name)
            if not allowed:
                self.save_joined_link(link, f"SYSTEM (Filtered: {filter_reason})")
                return False, f"Filtered: {filter_reason}"

        account_names = list(self.clients.keys())
        random.shuffle(account_names)

        last_error = None
        for acc_name in account_names:
            if self.should_exit:
                return False, "Interrupted"

            try:
                success, reason, entity = await self.join_link_with_account(link, acc_name, invite_or_entity)
                if success:
                    if entity and isinstance(entity, Channel) and not getattr(entity, 'megagroup', False):
                        discussion_ok, discussion_reason = await self.join_linked_discussion_group(entity, acc_name, link)
                        if not discussion_ok:
                            if reason.lower().startswith("joined"):
                                try:
                                    client = self.clients[acc_name]['client']
                                    await client(functions.channels.LeaveChannelRequest(entity))
                                    print(f"   ↩️ [{acc_name}] Left channel because no usable discussion group was found")
                                except Exception as leave_error:
                                    print(f"   ⚠️ [{acc_name}] Failed to leave channel after discussion check: {leave_error}")
                            last_error = f"Channel skipped (discussion required): {discussion_reason}"
                            continue

                    self.save_joined_link(link, acc_name)
                    return True, f"{reason} with {acc_name}"

                last_error = reason
                print(f"   ⚠️ [{acc_name}] {reason}")

            except FloodWaitError as e:
                wait_time = e.seconds
                print(f"   ⏳ [{acc_name}] Rate limited. Must wait {wait_time}s. Trying another account...")
                last_error = f"Rate limited ({wait_time}s)"
                continue

            except (InviteHashExpiredError, InviteHashInvalidError):
                self.save_joined_link(link, "SYSTEM (Status: Dead/Expired Link)")
                return False, "Dead/Expired Link"

            except Exception as e:
                print(f"   ❌ [{acc_name}] Failed: {str(e)}")
                last_error = str(e)
                continue

        final_reason = f"Failed to join with any selected account (Last Error: {last_error})"
        self.save_joined_link(link, f"SYSTEM (Status: {final_reason})")
        return False, final_reason
    
    async def join_groups(self):
        """Main joining process"""
        print("🚀 Starting group joining process")
        
        # Select accounts to use
        if not self.selected_accounts:
            if not self.select_accounts():
                print("❌ No accounts selected. Exiting.")
                return
            
        # Prompt for language filtering
        print("\n🛡️  Language Filtering:")
        print("   Blocks groups with Russian (Cyrillic) or Japanese characters in the title.")
        print("   Allowed: English, Chinese, Korean, Emojis, Numbers, etc.")
        filter_choice = input("   Enable language filtering? [y/N]: ").strip().lower()
        self.enable_filtering = filter_choice == 'y'
        
        if self.enable_filtering:
            print("✅ Language filtering ENABLED")
        else:
            print("⚠️ Language filtering DISABLED")
        
        # Initialize clients
        await self.initialize_clients()
        
        if not self.clients:
            print("❌ No clients available. Exiting.")
            return
        
        # Load links and progress
        all_links = self.extract_links_list(VALID_LINKS_FILE)
        bot_links = [link for link in all_links if self._is_bot_style_link(link)]
        if bot_links:
            for bot_link in bot_links:
                self.save_joined_link(bot_link, "SYSTEM (Status: Removed bot link before join)")
            all_links = [link for link in all_links if not self._is_bot_style_link(link)]
            self.write_remaining_links(all_links)
            print(f"🤖 Removed {len(bot_links)} bot-like links from join queue")
        joined_links = self.load_joined_links()
        
        # Filter out already joined links
        remaining_links = [link for link in all_links if link not in joined_links]
        
        print(f"📊 Total links: {len(all_links)}")
        print(f"📊 Already joined: {len(joined_links)}")
        print(f"📊 Remaining to join: {len(remaining_links)}")
        
        if not remaining_links:
            print("✅ All links already processed!")
            return
        
        # Process links
        successful_joins = 0
        failed_joins = 0
        
        for i, link in enumerate(remaining_links, 1):
            if self.should_exit:
                print("\n🛑 Interrupted by user. Saving progress...")
                break
            
            print(f"\n[{i}/{len(remaining_links)}] Processing: {link}")
            
            success, reason = await self.try_join_link(link)
            
            if success:
                successful_joins += 1
                print(f"🎉 Success: {reason}")
            else:
                failed_joins += 1
                print(f"💥 Failed: {reason}")
            
            # Random delay between attempts
            if i < len(remaining_links):  # Don't delay after last link
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                print(f"⏳ Waiting {delay:.1f}s...")
                await asyncio.sleep(delay)
        
        # Save remaining unprocessed links
        if self.should_exit and i < len(remaining_links):
            unprocessed_links = remaining_links[i:]
            self.write_remaining_links(unprocessed_links)
            print(f"💾 Saved {len(unprocessed_links)} unprocessed links")
        
        # Summary
        print(f"\n📈 SUMMARY:")
        print(f"✅ Successful joins: {successful_joins}")
        print(f"❌ Failed attempts: {failed_joins}")
        print(f"📋 Discussion groups found this session: {self.discussion_groups_found}")
        print(f"📁 Progress saved to: {os.path.abspath(JOINED_LINKS_FILE)}")
        
        # Check if any discussion groups were discovered
        discussion_file = os.path.join(os.path.dirname(JOINED_LINKS_FILE), 'discovered_discussion_groups.txt')
        if os.path.exists(discussion_file):
            try:
                with open(discussion_file, 'r', encoding='utf-8') as f:
                    discussion_count = len([line for line in f if line.strip() and not line.strip().startswith('#')])
                print(f"💬 Total discussion groups discovered: {discussion_count}")
                print(f"💬 Discussion groups saved to: {os.path.abspath(discussion_file)}")
            except:
                pass
        
        # Disconnect clients
        # Disconnect clients (validation pool encompasses all)
        for validator in self.validation_pool:
            try:
                await validator['client'].disconnect()
            except:
                pass
        print(f"🔌 Disconnected all clients")

async def main():
    joiner = GroupJoiner()
    await joiner.join_groups()

if __name__ == "__main__":
    asyncio.run(main())
