#!/usr/bin/env python3
"""
Check which accounts can access a specific private profile
"""
import os
import sys

# Add parent directory to path so we can import src package
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from src.account_manager import InstagramAccountManager
from src.config import INSTAGRAM_ACCOUNTS
import instaloader

def check_profile_access_by_account(username="_bonbonchocolateee_"):
    print(f"🔍 Checking profile access for: {username}")
    print("=" * 60)
    
    results = {}
    
    for account in INSTAGRAM_ACCOUNTS:
        print(f"\n🔐 Testing with account: {account['name']} ({account['username']})")
        
        try:
            manager = InstagramAccountManager()
            # Force fresh login
            loader = manager.get_authenticated_loader(account['name'], force_fresh_login=True)
            
            if not loader:
                print(f"❌ Failed to authenticate {account['name']}")
                results[account['name']] = {'authenticated': False, 'error': 'Authentication failed'}
                continue
            
            print(f"✅ Authenticated as {account['username']}")
            print(f"   - Context logged in: {loader.context.is_logged_in}")
            
            # Get profile
            profile = instaloader.Profile.from_username(loader.context, username)
            
            # Check access
            is_private = getattr(profile, 'is_private', None)
            followed_by_viewer = getattr(profile, 'followed_by_viewer', None)
            
            print(f"   - Target is private: {is_private}")
            print(f"   - Followed by this account: {followed_by_viewer}")
            
            # Test actual content access
            can_access_posts = False
            try:
                posts_iter = profile.get_posts()
                first_post = next(iter(posts_iter), None)
                can_access_posts = True
                print(f"   - Can access posts: ✅")
            except instaloader.exceptions.PrivateProfileNotFollowedException:
                print(f"   - Can access posts: ❌ (PrivateProfileNotFollowedException)")
            except Exception as e:
                print(f"   - Can access posts: ❌ ({e})")
            
            results[account['name']] = {
                'authenticated': True,
                'is_private': is_private,
                'followed_by_viewer': followed_by_viewer,
                'can_access_posts': can_access_posts,
                'username': account['username']
            }
            
            manager.logout()
            
        except Exception as e:
            print(f"❌ Error with account {account['name']}: {e}")
            results[account['name']] = {'authenticated': False, 'error': str(e)}
    
    # Summary
    print(f"\n📊 SUMMARY for {username}")
    print("=" * 60)
    
    following_accounts = []
    non_following_accounts = []
    
    for account_name, result in results.items():
        if result.get('authenticated'):
            if result.get('followed_by_viewer') and result.get('can_access_posts'):
                following_accounts.append(account_name)
                print(f"✅ {account_name} ({result['username']}): CAN ACCESS")
            else:
                non_following_accounts.append(account_name)
                print(f"❌ {account_name} ({result['username']}): CANNOT ACCESS")
                if result.get('is_private'):
                    print(f"   - Private profile, not followed")
        else:
            print(f"⚠️  {account_name}: AUTHENTICATION FAILED")
    
    print(f"\n🎯 RECOMMENDATION:")
    if following_accounts:
        best_account = following_accounts[0]
        print(f"Use account '{best_account}' for downloading from {username}")
        print(f"Command: python main.py download --account {best_account} --username {username}")
    else:
        print(f"❌ None of your accounts can access {username}")
        print("   - Either the profile doesn't exist")
        print("   - Or it's private and none of your accounts follow it")
        print("   - Or there are authentication issues")

if __name__ == "__main__":
    import sys
    username = sys.argv[1] if len(sys.argv) > 1 else "_bonbonchocolateee_"
    check_profile_access_by_account(username)
