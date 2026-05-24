#!/usr/bin/env python3
"""
Login Verifier for Telegram Toolkit
Provides account login verification and suppresses verbose Telethon logging
"""
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError
)

from src.core.progress_logger import log_info, log_success, log_error, log_warning


def setup_logging():
    """Suppress verbose Telethon logs while keeping important ones"""
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
    
    telethon_logger = logging.getLogger('telethon')
    telethon_logger.setLevel(logging.WARNING)
    
    telegraph_logger = logging.getLogger('telegraph')
    telegraph_logger.setLevel(logging.WARNING)
    
    asyncio_logger = logging.getLogger('asyncio')
    asyncio_logger.setLevel(logging.WARNING)


def verify_session_exists(session_file: str) -> Tuple[bool, str]:
    """Check if session file exists and is valid"""
    if not session_file:
        return False, "Session file path is empty"
    
    session_path = Path(session_file)
    if not session_path.exists():
        return False, f"Session file not found: {session_file}"
    
    if session_path.stat().st_size < 100:
        return False, f"Session file is too small (likely corrupted): {session_file}"
    
    return True, "Session file exists"


async def verify_account_login(
    account_dict: Dict[str, Any],
    timeout: int = 30
) -> Tuple[bool, Dict[str, Any]]:
    """
    Verify account can login using existing session
    Returns (success, user_info_or_error)
    """
    name = account_dict.get('name', 'unknown')
    session_file = account_dict.get('session_file', '')
    api_id = account_dict.get('api_id')
    api_hash = account_dict.get('api_hash')
    phone = account_dict.get('phone', '')
    
    log_info(f"🔐 Verifying login for account: {name}...")
    
    session_exists, session_msg = verify_session_exists(session_file)
    if not session_exists:
        log_error(f"❌ [{name}] Session check failed: {session_msg}")
        return False, {'error': session_msg, 'account': name}
    
    log_info(f"📁 [{name}] Session file found, testing connection...")
    
    client = TelegramClient(session_file, api_id, api_hash)
    
    try:
        await asyncio.wait_for(client.start(), timeout=timeout)
        
        me = await asyncio.wait_for(client.get_me(), timeout=timeout)
        
        user_info = {
            'account': name,
            'user_id': me.id,
            'username': getattr(me, 'username', None),
            'first_name': getattr(me, 'first_name', ''),
            'last_name': getattr(me, 'last_name', ''),
            'phone': getattr(me, 'phone', phone),
            'is_bot': getattr(me, 'bot', False),
            'is_premium': getattr(me, 'premium', False)
        }
        
        await client.disconnect()
        
        username_str = f"@{me.username}" if me.username else "no username"
        log_success(f"✅ [{name}] Login verified: {me.first_name} {username_str}")
        
        return True, user_info
        
    except asyncio.TimeoutError:
        await client.disconnect()
        log_error(f"❌ [{name}] Login timeout after {timeout}s — session may need regeneration via Account Manager")
        return False, {'error': f'Connection timeout after {timeout}s. The session file may be expired or require interactive re-authentication. Run Account Manager to regenerate.', 'account': name}
        
    except SessionPasswordNeededError:
        await client.disconnect()
        log_error(f"❌ [{name}] Two-factor authentication enabled")
        return False, {'error': 'Two-factor authentication is enabled. Please disable 2FA temporarily.', 'account': name}
        
    except PhoneCodeInvalidError:
        await client.disconnect()
        log_error(f"❌ [{name}] Invalid phone code")
        return False, {'error': 'Invalid phone code', 'account': name}
        
    except PhoneNumberInvalidError:
        await client.disconnect()
        log_error(f"❌ [{name}] Invalid phone number")
        return False, {'error': 'Invalid phone number format', 'account': name}
        
    except asyncio.CancelledError:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise

    except Exception as e:
        await client.disconnect()
        error_msg = str(e)

        if 'database' in error_msg.lower() or 'corrupt' in error_msg.lower():
            log_error(f"❌ [{name}] Session database corrupted: {error_msg}")
            return False, {'error': f'Session corrupted: {error_msg}', 'account': name, 'corrupted': True}

        log_error(f"❌ [{name}] Login failed: {error_msg}")
        return False, {'error': error_msg, 'account': name}


async def verify_all_accounts() -> Dict[str, Dict[str, Any]]:
    """Verify all configured accounts and return status"""
    from src.core.dynamic_config import get_accounts
    
    accounts = get_accounts()
    results = {}
    
    print("\n" + "="*60)
    print("🔍 VERIFYING ALL ACCOUNT LOGINS")
    print("="*60 + "\n")
    
    for account in accounts:
        name = account.get('name', 'unknown')
        success, info = await verify_account_login(account)
        
        results[name] = {
            'success': success,
            'info': info if success else None,
            'error': info.get('error') if not success else None,
            'corrupted': info.get('corrupted', False) if not success else False,
            'session_exists': True
        }
    
    print("\n" + "="*60)
    print("📊 ACCOUNT VERIFICATION SUMMARY")
    print("="*60)
    
    working = sum(1 for r in results.values() if r['success'])
    total = len(results)
    
    for name, status in results.items():
        if status['success']:
            info = status['info']
            print(f"  ✅ {name}: {info['first_name']} (@{info['username'] or 'no username'})")
        else:
            if status.get('corrupted'):
                print(f"  ❌ {name}: Session corrupted - {status['error']}")
            else:
                print(f"  ❌ {name}: {status['error']}")
    
    print(f"\n📈 Results: {working}/{total} accounts working\n")
    
    return results


async def verify_accounts_for_feature(feature_name: str, account_names: Optional[List[str]] = None) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Verify specific accounts before running a feature
    Returns (all_valid, valid_accounts)
    """
    from src.core.dynamic_config import get_accounts
    
    all_accounts = get_accounts()
    
    if account_names:
        accounts = [acc for acc in all_accounts if acc['name'] in account_names]
    else:
        accounts = all_accounts
    
    log_info(f"🔐 Verifying accounts for: {feature_name}")
    log_info(f"   Accounts to verify: {[a['name'] for a in accounts]}")
    
    valid_accounts = []
    failed_accounts = []
    
    for account in accounts:
        success, info = await verify_account_login(account)
        if success:
            valid_accounts.append(account)
        else:
            failed_accounts.append(account)
            log_warning(f"Account {account['name']} failed login verification and will be skipped")
    
    for failed_acc in failed_accounts:
        await offer_account_removal(failed_acc['name'])
    
    if not valid_accounts:
        log_error(f"No valid accounts available for {feature_name}")
        return False, []
    
    log_success(f"{len(valid_accounts)}/{len(accounts)} accounts verified for {feature_name}")
    
    return len(valid_accounts) == len(accounts), valid_accounts


async def offer_account_removal(account_name: str) -> bool:
    """
    Prompt user to remove a failed account.
    Returns True if account was removed, False otherwise.
    """
    log_warning(f"Account '{account_name}' failed login verification.")
    try:
        choice = input(f"   ❓ Would you like to remove account '{account_name}' from the toolkit? (y/N): ").strip().lower()
        if choice == 'y':
            return remove_account_by_name(account_name)
        else:
            log_info(f"Keeping account '{account_name}' (skipped for this operation)")
            return False
    except (EOFError, KeyboardInterrupt):
        log_info(f"Keeping account '{account_name}' (no input received)")
        return False


def remove_account_by_name(account_name: str) -> bool:
    """
    Programmatically remove an account by name using AccountManager logic.
    Does NOT change any existing AccountManager functionality.
    """
    try:
        from src.managers.account_manager import AccountManager
        manager = AccountManager()
        accounts = manager.load_current_accounts()
        
        target_account = None
        target_index = -1
        for i, acc in enumerate(accounts):
            if acc['name'].lower() == account_name.lower():
                target_account = acc
                target_index = i
                break
        
        if target_account is None:
            log_error(f"Account '{account_name}' not found in configuration")
            return False
        
        session_file = target_account.get('session_file', '')
        if session_file and os.path.exists(session_file):
            os.remove(session_file)
            log_info(f"Deleted session file: {session_file}")
        
        accounts.pop(target_index)
        if manager.save_accounts_to_config(accounts):
            manager.reload_config_in_modules()
            log_success(f"Account '{account_name}' removed successfully!")
            return True
        else:
            log_error(f"Failed to save config after removing '{account_name}'")
            return False
        
    except Exception as e:
        log_error(f"Error removing account '{account_name}': {e}")
        return False


def print_login_status():
    """Quick status check for all accounts"""
    from src.core.dynamic_config import get_accounts
    
    accounts = get_accounts()
    
    print("\n" + "="*60)
    print("📱 ACCOUNT LOGIN STATUS")
    print("="*60)
    
    for account in accounts:
        name = account.get('name')
        session_file = account.get('session_file', '')
        
        exists, _ = verify_session_exists(session_file)
        
        status = "✅ Session exists" if exists else "❌ No session"
        print(f"  {name}: {status}")
    
    print("")


if __name__ == "__main__":
    setup_logging()
    asyncio.run(verify_all_accounts())
