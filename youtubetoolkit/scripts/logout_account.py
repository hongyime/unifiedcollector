#!/usr/bin/env python3
"""
Simple script to clear cached credentials so the user can re-authenticate
with a different YouTube/Google account.
"""

import os
import sys

from auth_cache import clear_cached_credentials
from app_paths import SUBSCRIPTIONS_FILE as APP_SUBSCRIPTIONS_FILE

def clear_credentials():
    print("\n🔐 YOUTUBE ACCOUNT LOGOUT UTILITY 🔐")
    print("=" * 45)
    
    cache_file = str(APP_SUBSCRIPTIONS_FILE)
    
    deleted_anything = False
    
    # Delete auth credential cache files
    try:
        deleted_paths = clear_cached_credentials()
        if deleted_paths:
            removed_names = ", ".join(path.name for path in deleted_paths)
            print(f"✅ Successfully cleared cached login credentials. ({removed_names} removed)")
            deleted_anything = True
    except Exception as e:
        print(f"❌ Failed to delete cached credentials: {e}")
            
    # Also delete the subscription cache so the new account's subs load freshly
    if os.path.exists(cache_file):
        try:
            os.remove(cache_file)
            print("✅ Successfully cleared old subscription cache. (subscriptions.json removed)")
            deleted_anything = True
        except Exception as e:
            print(f"❌ Failed to delete subscription cache: {e}")
            
    if deleted_anything:
        print("\n🎉 Logout complete! The next time you run a Scrape or Workflow command,")
        print("your browser will open and ask you to log in to a Google Account again.")
    else:
        print("\nℹ️ No cached credentials were found. You are already logged out!")

if __name__ == '__main__':
    clear_credentials()
