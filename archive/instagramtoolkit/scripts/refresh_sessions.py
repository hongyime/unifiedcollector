#!/usr/bin/env python3
"""
Fresh login test script - force re-authentication for all accounts
"""
import os
import sys

# Add parent directory to path so we can import src package
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from src.account_manager import InstagramAccountManager
from src.config import INSTAGRAM_ACCOUNTS

def refresh_all_sessions():
    print("🔄 Refreshing all account sessions...")
    print("=" * 50)
    
    manager = InstagramAccountManager()
    
    successful = []
    failed = []
    
    for account in INSTAGRAM_ACCOUNTS:
        print(f"\n🔄 Refreshing session for {account['name']} ({account['username']})...")
        
        # Remove old session file
        session_file = manager.get_session_file(account['username'])
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                print(f"🗑️  Removed old session file")
            except Exception as e:
                print(f"⚠️  Could not remove session file: {e}")
        
        # Force fresh login
        loader = manager.get_authenticated_loader(account['name'], force_fresh_login=True)
        
        if loader:
            print(f"✅ Fresh login successful for {account['name']}")
            print(f"   - Context logged in: {loader.context.is_logged_in}")
            print(f"   - Context username: {getattr(loader.context, 'username', 'UNAVAILABLE')}")
            successful.append(account['name'])
            manager.logout()
        else:
            print(f"❌ Fresh login failed for {account['name']}")
            failed.append(account['name'])
    
    print(f"\n📊 Fresh Login Summary:")
    print(f"✅ Successful: {len(successful)} accounts")
    for name in successful:
        print(f"   - {name}")
    
    if failed:
        print(f"❌ Failed: {len(failed)} accounts")
        for name in failed:
            print(f"   - {name}")
    
    print(f"\n📈 Success rate: {len(successful)}/{len(INSTAGRAM_ACCOUNTS)} ({len(successful)/len(INSTAGRAM_ACCOUNTS)*100:.1f}%)")

if __name__ == "__main__":
    refresh_all_sessions()
