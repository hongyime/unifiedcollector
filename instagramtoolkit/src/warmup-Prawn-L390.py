"""
Warm-up utilities for Instagram operations (PHASE 2).

Performs light operations before heavy automation to mimic human behavior
and build trust with Instagram's detection systems.
"""
import random
import time
from typing import Optional

from src.config import (
    WARMUP_ENABLED,
    WARMUP_OPERATIONS,
    WARMUP_DELAY_MIN,
    WARMUP_DELAY_MAX,
)
from src.resilience import _interruptible_sleep


def warmup_session(loader, account_username: str) -> bool:
    """
    Perform warm-up operations before heavy automation.
    
    PHASE 2: Light activity to mimic human behavior:
    - View own profile
    - Check a few random profiles
    - Random delays between operations
    
    Args:
        loader: Authenticated Instaloader instance
        account_username: Username of the logged-in account
    
    Returns:
        True if warm-up succeeded, False if failed
    """
    if not WARMUP_ENABLED:
        return True
    
    print(f"[WARMUP] Starting warm-up period ({WARMUP_OPERATIONS} operations)...")
    
    try:
        import instaloader
        
        # Operation 1: View own profile
        try:
            print("[WARMUP] 1/3 - Viewing own profile...")
            profile = instaloader.Profile.from_username(loader.context, account_username)
            _ = profile.biography  # Access a property to trigger API call
            delay = random.uniform(WARMUP_DELAY_MIN, WARMUP_DELAY_MAX)
            _interruptible_sleep(delay)
        except Exception as e:
            print(f"[WARMUP] Warning: Could not view own profile: {e}")
        
        # Operation 2: View a popular account (less likely to trigger suspicion)
        try:
            print("[WARMUP] 2/3 - Viewing a public profile...")
            popular_accounts = ['instagram', 'natgeo', 'nasa', 'nike', 'spotify']
            random_account = random.choice(popular_accounts)
            profile = instaloader.Profile.from_username(loader.context, random_account)
            _ = profile.biography
            delay = random.uniform(WARMUP_DELAY_MIN, WARMUP_DELAY_MAX)
            _interruptible_sleep(delay)
        except Exception as e:
            print(f"[WARMUP] Warning: Could not view public profile: {e}")
        
        # Operation 3: Small delay to simulate browsing
        try:
            print("[WARMUP] 3/3 - Simulating browsing...")
            delay = random.uniform(WARMUP_DELAY_MIN, WARMUP_DELAY_MAX)
            _interruptible_sleep(delay)
        except Exception:
            pass
        
        print("[WARMUP] ✅ Warm-up complete")
        return True
        
    except Exception as e:
        print(f"[WARMUP] ❌ Warm-up failed: {e}")
        return False


def should_warmup(operation_type: str) -> bool:
    """
    Determine if warm-up is needed for this operation type.
    
    Args:
        operation_type: Type of operation (spider, download, etc.)
    
    Returns:
        True if warm-up should be performed
    """
    if not WARMUP_ENABLED:
        return False
    
    # Warm-up for heavy operations only
    heavy_operations = ['spider', 'seed', 'batch_spider', 'batch_download']
    return operation_type in heavy_operations
