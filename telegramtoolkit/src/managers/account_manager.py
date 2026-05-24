"""
Account Management System
Handles adding, removing, and managing Telegram accounts with proper session handling

Features:
- Add new accounts with proper validation
- Session file management with correct naming
- Automatic session copying and organization
- Account configuration persistence
- Integration with all toolkit features
"""
import os
import json
import shutil
import asyncio
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError
from src.core.console import configure_console_output


configure_console_output()

class AccountManager:
    def __init__(self):
        self.config_file = "src/core/config.py"
        self.sessions_dir = "sessions"
        self.data_dir = "data"
        
        # Ensure directories exist
        os.makedirs(self.sessions_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)
    
    def _get_env_path(self) -> Path:
        """Return the path to the .env file in the project root."""
        return Path(__file__).resolve().parent.parent.parent / '.env'

    def load_current_accounts(self):
        """Load current accounts from .env file."""
        try:
            env_path = self._get_env_path()
            if not env_path.exists():
                return []

            env_vars: dict = {}
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        env_vars[key.strip()] = value.strip()

            accounts = []
            for i in range(1, 20):
                name = env_vars.get(f'ACCOUNT_{i}_NAME')
                if not name:
                    break
                accounts.append({
                    'name': name,
                    'api_id': int(env_vars.get(f'ACCOUNT_{i}_API_ID', '0')),
                    'api_hash': env_vars.get(f'ACCOUNT_{i}_API_HASH', ''),
                    'phone': env_vars.get(f'ACCOUNT_{i}_PHONE', ''),
                    'session_file': env_vars.get(f'ACCOUNT_{i}_SESSION', f'sessions/{name}.session'),
                    'prefix': env_vars.get(f'ACCOUNT_{i}_PREFIX', name[:4]),
                })
            return accounts
        except Exception as e:
            print(f"❌ Error loading accounts: {e}")
            return []

    def save_accounts_to_config(self, accounts):
        """Save accounts list back to .env file, preserving non-account lines."""
        try:
            env_path = self._get_env_path()

            # Read existing .env, keep non-account lines
            preserved_lines: list = []
            if env_path.exists():
                with open(env_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                skip = False
                for line in lines:
                    stripped = line.strip()
                    # Drop old ACCOUNT_N_* blocks
                    import re
                    if re.match(r'^ACCOUNT_\d+_', stripped.split('=')[0]):
                        skip = True
                        continue
                    # Drop comment headers that immediately precede account blocks
                    if skip and stripped.startswith('#') and 'Account' in stripped:
                        continue
                    skip = False
                    preserved_lines.append(line)

            # Build new account block
            account_lines: list = []
            for i, account in enumerate(accounts, 1):
                account_lines.append(f'\n# Account {i}: {account["name"]}\n')
                account_lines.append(f'ACCOUNT_{i}_NAME={account["name"]}\n')
                account_lines.append(f'ACCOUNT_{i}_API_ID={account["api_id"]}\n')
                account_lines.append(f'ACCOUNT_{i}_API_HASH={account["api_hash"]}\n')
                account_lines.append(f'ACCOUNT_{i}_PHONE={account["phone"]}\n')
                account_lines.append(f'ACCOUNT_{i}_SESSION={account["session_file"]}\n')
                account_lines.append(f'ACCOUNT_{i}_PREFIX={account["prefix"]}\n')

            # Backup original .env
            backup_file = str(env_path) + '.backup'
            if env_path.exists():
                shutil.copy2(env_path, backup_file)

            with open(env_path, 'w', encoding='utf-8') as f:
                f.writelines(preserved_lines)
                f.writelines(account_lines)

            print(f"✅ .env updated successfully (backup: {backup_file})")
            return True

        except Exception as e:
            print(f"❌ Error saving .env: {e}")
            return False
    
    def reload_config_in_modules(self):
        """Force reload config in all Python modules"""
        try:
            import importlib
            import sys
            
            # Reload config module if it's already imported
            if 'src.core.config' in sys.modules:
                importlib.reload(sys.modules['src.core.config'])
                print("🔄 Config module reloaded")
            
            # Reload dynamic_config module to refresh accounts cache
            if 'src.core.dynamic_config' in sys.modules:
                importlib.reload(sys.modules['src.core.dynamic_config'])
                print("🔄 Dynamic config module reloaded")
            
            # Force refresh accounts in dynamic config
            try:
                from src.core.dynamic_config import dynamic_config
                accounts = dynamic_config.force_reload()
                print(f"📊 Loaded {len(accounts)} accounts in dynamic configuration")
            except Exception as e:
                print(f"⚠️ Dynamic config reload warning: {e}")
            
            print("✅ All modules should now see the updated account configuration!")
                
        except Exception as e:
            print(f"⚠️ Config reload warning: {e}")
            print("💡 Consider restarting the toolkit to ensure all features use new accounts")
    
    def generate_session_filename(self, identifier):
        safe_name = "".join(filter(str.isdigit, identifier))
        if not safe_name:
            safe_name = "".join(c for c in identifier if c.isalnum() or c in ('_', '-')).lower()
        return f"sessions/{safe_name}.session"
    
    def generate_prefix(self, account_name):
        """Generate a short prefix from account name"""
        # Remove common words and get initials
        words = account_name.lower().replace('_', ' ').replace('-', ' ').split()
        filtered_words = [w for w in words if w not in ['the', 'and', 'or', 'of', 'to', 'in', 'for']]
        
        if len(filtered_words) == 1:
            # Single word: take first 4 characters
            return filtered_words[0][:4]
        else:
            # Multiple words: take first letter of each
            prefix = ''.join(w[0] for w in filtered_words[:4])
            return prefix
    
    async def test_account_login(self, api_id, api_hash, phone):
        """Test account login without saving session"""
        print(f"🔐 Testing login for {phone}...")
        
        # Create temporary session
        temp_session = f"temp_test_{phone.replace('+', '')}"
        client = TelegramClient(temp_session, api_id, api_hash)
        
        try:
            await client.start(phone)
            
            # Get account info
            me = await client.get_me()
            account_info = {
                'id': me.id,
                'username': me.username or 'No username',
                'first_name': me.first_name or '',
                'last_name': me.last_name or '',
                'phone': me.phone or phone
            }
            
            await client.disconnect()
            
            # Clean up temp session
            try:
                os.remove(f"{temp_session}.session")
            except:
                pass
            
            return True, account_info
            
        except SessionPasswordNeededError:
            await client.disconnect()
            return False, "Two-factor authentication is enabled. Please disable it temporarily or handle 2FA."
        except PhoneNumberInvalidError:
            await client.disconnect()
            return False, "Invalid phone number format."
        except Exception as e:
            await client.disconnect()
            return False, f"Login failed: {str(e)}"
    
    async def create_account_session(self, account_data):
        """Create and save session for new account"""
        session_file = account_data['session_file']
        session_path = session_file.replace('.session', '')
        
        print(f"🔑 Creating session for {account_data['name']}...")
        
        # Ensure sessions directory exists
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        
        client = TelegramClient(session_path, account_data['api_id'], account_data['api_hash'])
        
        try:
            await client.start(account_data['phone'])
            
            # Verify session works
            me = await client.get_me()
            print(f"✅ Session created for {me.first_name} (@{me.username or 'no username'})")
            print(f"📁 Session saved to: {session_file}")
            
            await client.disconnect()
            return True
            
        except Exception as e:
            print(f"❌ Failed to create session: {e}")
            try:
                await client.disconnect()
            except:
                pass
            return False
    
    async def add_new_account(self):
        """Interactive process to add a new account"""
        print("\n➕ Add New Telegram Account")
        print("="*50)
        
        # Get account details
        print("\n📝 Enter account details:")
        account_name = input("Account name (e.g., 'myaccount'): ").strip()
        if not account_name:
            print("❌ Account name cannot be empty")
            return False
        
        # Check if name already exists
        current_accounts = self.load_current_accounts()
        if any(acc['name'].lower() == account_name.lower() for acc in current_accounts):
            print("❌ Account name already exists")
            return False
        
        try:
            api_id = int(input("API ID: ").strip())
            api_hash = input("API Hash: ").strip()
            phone = input("Phone number (with country code, e.g., +1234567890): ").strip()
            
            if not api_hash or not phone:
                print("❌ API Hash and phone number cannot be empty")
                return False
            
        except ValueError:
            print("❌ Invalid API ID")
            return False
        
        # Test login first
        print(f"\n🔐 Testing account credentials...")
        success, result = await self.test_account_login(api_id, api_hash, phone)
        
        if not success:
            print(f"❌ Login test failed: {result}")
            return False
        
        print(f"✅ Login test successful!")
        print(f"   Account: {result['first_name']} {result['last_name']}")
        print(f"   Username: @{result['username']}")
        print(f"   ID: {result['id']}")
        
        # Confirm addition
        confirm = input(f"\n✅ Add this account to the toolkit? (y/N): ").strip().lower()
        if confirm != 'y':
            print("❌ Account addition cancelled")
            return False
        
        # Generate session filename and prefix
        session_file = self.generate_session_filename(account_name)
        prefix = self.generate_prefix(account_name)
        
        # Create account data
        new_account = {
            'name': account_name,
            'api_id': api_id,
            'api_hash': api_hash,
            'phone': phone,
            'session_file': session_file,
            'prefix': prefix
        }
        
        # Create session
        session_created = await self.create_account_session(new_account)
        if not session_created:
            print("❌ Failed to create session")
            return False
        
        # Add to accounts list
        current_accounts.append(new_account)
        
        # Save to config
        if self.save_accounts_to_config(current_accounts):
            # Inject new vars into the live process so no restart is needed
            idx = len(current_accounts)
            os.environ[f'ACCOUNT_{idx}_NAME'] = account_name
            os.environ[f'ACCOUNT_{idx}_API_ID'] = str(api_id)
            os.environ[f'ACCOUNT_{idx}_API_HASH'] = api_hash
            os.environ[f'ACCOUNT_{idx}_PHONE'] = phone
            os.environ[f'ACCOUNT_{idx}_SESSION'] = session_file
            os.environ[f'ACCOUNT_{idx}_PREFIX'] = prefix

            print(f"\n🎉 Account '{account_name}' added successfully!")
            print(f"   Session file: {session_file}")
            print(f"   Prefix: {prefix}")
            print(f"\n💡 Reloading configuration for all toolkit modules...")
            self.reload_config_in_modules()
            print(f"✅ All modules updated with new account configuration!")
            return True
        else:
            print("❌ Failed to save account to config")
            return False
    
    def list_accounts(self):
        """List all configured accounts"""
        accounts = self.load_current_accounts()
        
        print("\n👥 Configured Accounts:")
        print("="*60)
        
        if not accounts:
            print("   No accounts configured")
            return
        
        for i, account in enumerate(accounts, 1):
            session_exists = os.path.exists(account['session_file'])
            status = "✅" if session_exists else "❌"
            
            print(f"{i}. {account['name']}")
            print(f"   Phone: {account['phone']}")
            print(f"   Session: {account['session_file']} {status}")
            print(f"   Prefix: {account['prefix']}")
            print()
    
    def remove_account(self):
        """Remove an account from configuration"""
        accounts = self.load_current_accounts()
        
        if not accounts:
            print("❌ No accounts to remove")
            return False
        
        print("\n🗑️ Remove Account:")
        print("="*40)
        
        for i, account in enumerate(accounts, 1):
            print(f"{i}. {account['name']} ({account['phone']})")
        
        try:
            choice = int(input(f"\nSelect account to remove (1-{len(accounts)}): ").strip())
            if not (1 <= choice <= len(accounts)):
                print("❌ Invalid choice")
                return False
            
            account_to_remove = accounts[choice - 1]
            
            # Confirm removal
            confirm = input(f"\n⚠️ Remove account '{account_to_remove['name']}'? This will delete the session file. (y/N): ").strip().lower()
            if confirm != 'y':
                print("❌ Removal cancelled")
                return False
            
            # Remove session file
            if os.path.exists(account_to_remove['session_file']):
                os.remove(account_to_remove['session_file'])
                print(f"🗑️ Deleted session file: {account_to_remove['session_file']}")
            
            # Remove from accounts list
            accounts.pop(choice - 1)
            
            # Save config
            if self.save_accounts_to_config(accounts):
                # Re-write all ACCOUNT_N_* env vars in compact form (no gaps)
                # First clear all existing ACCOUNT_N_* keys
                keys_to_clear = [k for k in os.environ if k.startswith('ACCOUNT_') and '_' in k[8:]]
                for k in keys_to_clear:
                    os.environ.pop(k, None)
                # Re-inject remaining accounts at correct indices
                for idx, acc in enumerate(accounts, 1):
                    os.environ[f'ACCOUNT_{idx}_NAME'] = acc['name']
                    os.environ[f'ACCOUNT_{idx}_API_ID'] = str(acc['api_id'])
                    os.environ[f'ACCOUNT_{idx}_API_HASH'] = acc['api_hash']
                    os.environ[f'ACCOUNT_{idx}_PHONE'] = acc['phone']
                    os.environ[f'ACCOUNT_{idx}_SESSION'] = acc['session_file']
                    os.environ[f'ACCOUNT_{idx}_PREFIX'] = acc['prefix']

                print(f"✅ Account '{account_to_remove['name']}' removed successfully!")
                print(f"\n💡 Reloading configuration for all toolkit modules...")
                self.reload_config_in_modules()
                print(f"✅ All modules updated with account removal!")
                return True
            else:
                print("❌ Failed to save updated config")
                return False
            
        except ValueError:
            print("❌ Invalid input")
            return False
    
    async def manage_sessions(self):
        """Manage session files"""
        while True:
            print("\n🔧 Session Management:")
            print("="*50)
            print("1️⃣  Show Session Status")
            print("2️⃣  Copy Sessions from Individual Folders")
            print("3️⃣  Import Session File")
            print("4️⃣  Regenerate Missing Sessions")
            print("0️⃣  Back to Account Menu")
            print("="*50)
            
            choice = input("\n🔧 Enter your choice: ").strip()
            
            if choice == '1':
                self.show_session_status()
            elif choice == '2':
                self.copy_sessions_from_folders()
            elif choice == '3':
                self.import_session_file()
            elif choice == '4':
                await self.regenerate_missing_sessions()
            elif choice == '0':
                break
            else:
                print("❌ Invalid choice. Please try again.")
    
    def show_session_status(self):
        """Show detailed session status for all accounts"""
        print("\n📱 Session Status:")
        print("="*60)
        
        accounts = self.load_current_accounts()
        
        for account in accounts:
            session_path = account['session_file']
            session_exists = os.path.exists(session_path)
            
            print(f"\n👤 {account['name']}:")
            print(f"   📞 Phone: {account['phone']}")
            print(f"   📁 Session: {session_path}")
            print(f"   📊 Status: {'✅ Found' if session_exists else '❌ Missing'}")
            
            if session_exists:
                # Get file size and date
                try:
                    stat = os.stat(session_path)
                    size_kb = stat.st_size / 1024
                    import datetime
                    mod_time = datetime.datetime.fromtimestamp(stat.st_mtime)
                    print(f"   📏 Size: {size_kb:.1f} KB")
                    print(f"   📅 Modified: {mod_time.strftime('%Y-%m-%d %H:%M:%S')}")
                except:
                    pass
            else:
                print(f"   💡 You can copy from individual folders or regenerate")
    
    def copy_sessions_from_folders(self):
        """Copy session files from individual account folders"""
        print("\n📂 Copy Sessions from Individual Folders:")
        print("="*60)
        
        base_path = Path(__file__).parent.parent.parent
        accounts = self.load_current_accounts()
        copied_count = 0
        
        for account in accounts:
            account_name = account['name']
            target_session = account['session_file']
            
            # Look for session files in various locations
            potential_sources = [
                f"{base_path}/{account_name}/user_analysis/session.session",
                f"{base_path}/{account_name}/get_media/{account['prefix']}.session",
                f"{base_path}/{account_name}/get_links/session_name.session",
                f"{base_path}/{account_name}.session",
                f"{base_path}/{account_name}/session.session"
            ]
            
            session_found = False
            for source_path in potential_sources:
                if os.path.exists(source_path):
                    try:
                        # Ensure target directory exists
                        os.makedirs(os.path.dirname(target_session), exist_ok=True)
                        
                        # Copy session file
                        shutil.copy2(source_path, target_session)
                        print(f"✅ Copied {account_name}: {source_path} → {target_session}")
                        copied_count += 1
                        session_found = True
                        break
                    except Exception as e:
                        print(f"❌ Failed to copy {account_name}: {e}")
            
            if not session_found:
                print(f"❌ No session found for {account_name}")
        
        if copied_count > 0:
            print(f"\n🎉 Successfully copied {copied_count} session files!")
        else:
            print(f"\n❌ No session files were copied")
    
    def import_session_file(self):
        """Import a session file from a custom location"""
        accounts = self.load_current_accounts()
        
        if not accounts:
            print("❌ No accounts configured")
            return
        
        print("\n📥 Import Session File:")
        print("="*40)
        
        for i, account in enumerate(accounts, 1):
            session_exists = os.path.exists(account['session_file'])
            status = "✅" if session_exists else "❌"
            print(f"{i}. {account['name']} {status}")
        
        try:
            choice = int(input(f"\nSelect account (1-{len(accounts)}): ").strip())
            if not (1 <= choice <= len(accounts)):
                print("❌ Invalid choice")
                return
            
            account = accounts[choice - 1]
            source_path = input(f"\nEnter path to session file for {account['name']}: ").strip()
            
            if not os.path.exists(source_path):
                print("❌ Source file does not exist")
                return
            
            target_path = account['session_file']
            
            try:
                # Ensure target directory exists
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                
                # Copy session file
                shutil.copy2(source_path, target_path)
                print(f"✅ Session imported successfully!")
                print(f"   From: {source_path}")
                print(f"   To: {target_path}")
                
            except Exception as e:
                print(f"❌ Failed to import session: {e}")
                
        except ValueError:
            print("❌ Invalid input")
    
    async def regenerate_missing_sessions(self):
        """Regenerate sessions for accounts that are missing them"""
        accounts = self.load_current_accounts()
        missing_accounts = [acc for acc in accounts if not os.path.exists(acc['session_file'])]
        
        if not missing_accounts:
            print("✅ All accounts have valid session files!")
            return
        
        print(f"\n🔄 Found {len(missing_accounts)} accounts with missing sessions:")
        for account in missing_accounts:
            print(f"   ❌ {account['name']} ({account['phone']})")
        
        confirm = input(f"\n🔑 Regenerate sessions for these accounts? (y/N): ").strip().lower()
        if confirm != 'y':
            print("❌ Session regeneration cancelled")
            return
        
        success_count = 0
        for account in missing_accounts:
            print(f"\n🔑 Regenerating session for {account['name']}...")
            if await self.create_account_session(account):
                success_count += 1
        
        print(f"\n📊 Regeneration complete: {success_count}/{len(missing_accounts)} successful")
    
    async def show_menu(self):
        """Show account management menu"""
        while True:
            print("\n" + "="*60)
            print("👥 ACCOUNT MANAGEMENT")
            print("="*60)
            print("1️⃣  Add New Account")
            print("2️⃣  List All Accounts")
            print("3️⃣  Remove Account")
            print("4️⃣  Manage Sessions")
            print("5️⃣  Test Account Login")
            print("0️⃣  Back to Main Menu")
            print("="*60)
            
            choice = input("\n🔧 Enter your choice: ").strip()
            
            if choice == '1':
                await self.add_new_account()
            
            elif choice == '2':
                self.list_accounts()
            
            elif choice == '3':
                self.remove_account()
            
            elif choice == '4':
                await self.manage_sessions()
            
            elif choice == '5':
                await self.test_login_menu()
            
            elif choice == '0':
                break
            
            else:
                print("❌ Invalid choice. Please try again.")
    
    async def test_login_menu(self):
        """Test login for existing accounts"""
        accounts = self.load_current_accounts()
        
        if not accounts:
            print("❌ No accounts configured")
            return
        
        print("\n🔐 Test Account Login:")
        print("="*40)
        
        for i, account in enumerate(accounts, 1):
            print(f"{i}. {account['name']} ({account['phone']})")
        
        try:
            choice = int(input(f"\nSelect account to test (1-{len(accounts)}): ").strip())
            if not (1 <= choice <= len(accounts)):
                print("❌ Invalid choice")
                return
            
            account = accounts[choice - 1]
            success, result = await self.test_account_login(
                account['api_id'], 
                account['api_hash'], 
                account['phone']
            )
            
            if success:
                print(f"✅ Login successful!")
                print(f"   Account: {result['first_name']} {result['last_name']}")
                print(f"   Username: @{result['username']}")
            else:
                print(f"❌ Login failed: {result}")
                
        except ValueError:
            print("❌ Invalid input")

if __name__ == "__main__":
    manager = AccountManager()
    asyncio.run(manager.show_menu())
