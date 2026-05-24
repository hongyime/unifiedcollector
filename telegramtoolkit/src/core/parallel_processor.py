#!/usr/bin/env python3
"""
Parallel Processing Utilities for Telegram Operations
Handles concurrent operations across multiple accounts and chats with rate limiting
"""
import asyncio
import sys
import time
import logging
from typing import List, Dict, Any, Callable, Optional, Set
from collections import defaultdict
from pathlib import Path

from src.core.account_health import AccountFailureError, AccountHealthPolicy, is_account_error

def safe_file_operations() -> None:
    """Set up safe file operations with proper encoding"""
    import sys
    
    # Force UTF-8 encoding for file operations
    if hasattr(sys.stdout, 'reconfigure') and hasattr(sys.stderr, 'reconfigure'):
        try:
            getattr(sys.stdout, 'reconfigure')(encoding='utf-8')
            getattr(sys.stderr, 'reconfigure')(encoding='utf-8')
        except Exception:
            pass


# Initialize safe file operations
safe_file_operations()


class TelegramParallelProcessor:
    """Manages parallel processing of Telegram operations with intelligent rate limiting"""
    
    def __init__(self, max_concurrent_per_account: int = 3, max_total_concurrent: int = 10, 
                 delay_between_batches: float = 1.0, min_delay_per_chat: float = 0.5):
        self.max_concurrent_per_account = max_concurrent_per_account
        self.max_total_concurrent = max_total_concurrent
        self.delay_between_batches = delay_between_batches
        self.min_delay_per_chat = min_delay_per_chat
        
        # Rate limiting tracking
        self.rate_limit_tracker: defaultdict[str, float] = defaultdict(float)
        self.account_request_count: defaultdict[str, int] = defaultdict(int)
        self.global_request_count: int = 0
        self.account_health = AccountHealthPolicy()
        
        # Statistics
        self.failed_operations: List[Dict[str, Any]] = []
        self.success_count: int = 0
        self.total_operations: int = 0
        self.start_time: Optional[float] = None
        self.account_stats: defaultdict[str, Dict[str, int]] = defaultdict(lambda: {'success': 0, 'failed': 0, 'rate_limited': 0})
        
        # Setup logging
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)
    
    async def process_with_multiple_accounts(self, accounts: List[Dict[str, Any]], targets: List[Dict[str, Any]], 
                                           operation_func: Callable[..., Any], **kwargs: Any) -> List[Dict[str, Any]]:
        """
        Process targets across multiple accounts in parallel
        
        Args:
            accounts: List of account dictionaries with credentials
            targets: List of chats/users/entities to process
            operation_func: Async function to execute on each target
            **kwargs: Additional arguments for operation_func
        """
        self.start_time = time.time()
        self.total_operations = len(targets)
        
        print(f"🚀 Starting parallel processing with {len(accounts)} accounts for {len(targets)} targets")
        print(f"⚙️ Max concurrent per account: {self.max_concurrent_per_account}")
        print(f"⚙️ Max total concurrent: {self.max_total_concurrent}")
        
        # Import here to avoid circular imports
        from telethon import TelegramClient  # type: ignore[import-untyped]
        
        # Initialize all clients
        clients: Dict[str, Any] = {}
        account_lookup = {account["name"]: account for account in accounts}
        try:
            print(f"🔧 Initializing {len(accounts)} client sessions...")
            for account in accounts:
                try:
                    client = TelegramClient(account['session_file'], account['api_id'], account['api_hash'])
                    await client.start()  # type: ignore[misc]
                    
                    # Verify client is working
                    me = await client.get_me()
                    clients[account['name']] = client
                    me_display = getattr(me, 'username', None) or getattr(me, 'id', 'unknown')
                    print(f"✅ Connected account: {account['name']} (@{me_display})")
                    
                except Exception as e:
                    recovered = await self.account_health.handle_account_failure(client, account, e, "parallel startup")
                    if recovered:
                        try:
                            me = await client.get_me()
                            clients[account['name']] = client
                            me_display = getattr(me, 'username', None) or getattr(me, 'id', 'unknown')
                            print(f"✅ Connected account after reconnect: {account['name']} (@{me_display})")
                            continue
                        except Exception as retry_error:
                            print(f"❌ Failed to initialize {account['name']} after reconnect: {retry_error}")
                    print(f"❌ Failed to initialize {account['name']}: {e}")
                    continue
            
            if not clients:
                print("❌ No clients could be initialized!")
                return []
            
            # Distribute targets across accounts for parallel processing
            results = await self._process_distributed_parallel(clients, account_lookup, targets, operation_func, **kwargs)
            
            # Print final statistics
            self._print_final_statistics()
            
            return results
            
        finally:
            # Clean up all clients
            print("🔄 Cleaning up client connections...")
            for account_name, client in clients.items():
                try:
                    await client.disconnect()
                    print(f"✅ Disconnected: {account_name}")
                except Exception as e:
                    print(f"⚠️ Error disconnecting {account_name}: {e}")
    
    async def _process_distributed_parallel(self, clients: Dict[str, Any], account_lookup: Dict[str, Dict[str, Any]], targets: List[Dict[str, Any]], 
                                          operation_func: Callable[..., Any], **kwargs: Any) -> List[Dict[str, Any]]:
        """Process targets using distributed parallel processing across all accounts"""
        
        # Distribute targets across clients.
        # If target contains an 'account' hint, pin it to that account.
        # Otherwise, fall back to round-robin distribution.
        client_items = list(clients.items())
        target_batches: List[List[Dict[str, Any]]] = [[] for _ in client_items]
        account_to_batch_index = {account_name: idx for idx, (account_name, _) in enumerate(client_items)}
        rr_index = 0
        
        for target in targets:
            preferred_account = target.get('account')
            if preferred_account in account_to_batch_index:
                batch_index = account_to_batch_index[preferred_account]
            else:
                batch_index = rr_index % len(client_items)
                rr_index += 1
            target_batches[batch_index].append(target)
        
        print("📊 Distribution across accounts:")
        for i, (account_name, _) in enumerate(client_items):
            batch_size = len(target_batches[i])
            print(f"  📁 {account_name}: {batch_size} targets")
        
        # Create semaphore for global concurrency control
        global_semaphore = asyncio.Semaphore(self.max_total_concurrent)
        
        # Process each account's batch in parallel
        tasks: List[asyncio.Task[List[Dict[str, Any]]]] = []
        task_account_names: List[str] = []
        for (account_name, client), target_batch in zip(client_items, target_batches):
            if target_batch:  # Only create task if there are targets to process
                task = asyncio.create_task(
                    self._process_account_batch(
                        client=client,
                        account=account_lookup.get(account_name, {"name": account_name}),
                        account_name=account_name,
                        targets=target_batch,
                        operation_func=operation_func,
                        global_semaphore=global_semaphore,
                        **kwargs
                    )
                )
                tasks.append(task)
                task_account_names.append(account_name)
        
        # Wait for all accounts to complete
        print(f"🔄 Processing {len(tasks)} account batches in parallel...")
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Combine all results
        all_results: List[Dict[str, Any]] = []
        for i, result in enumerate(batch_results):
            if isinstance(result, BaseException):
                account_name = task_account_names[i]
                print(f"❌ Account {account_name} batch failed: {result}")
                self.account_stats[account_name]['failed'] += 1
            else:
                all_results.extend(result)
        
        return all_results
    
    async def _process_account_batch(self, client: Any, account: Dict[str, Any], account_name: str, targets: List[Dict[str, Any]], 
                                   operation_func: Callable[..., Any], global_semaphore: asyncio.Semaphore, 
                                   **kwargs: Any) -> List[Dict[str, Any]]:
        """Process a batch of targets for a single account with concurrency control"""
        
        account_semaphore = asyncio.Semaphore(self.max_concurrent_per_account)
        results: List[Dict[str, Any]] = []
        
        print(f"🔄 {account_name}: Starting batch of {len(targets)} targets")
        
        # Process targets in smaller chunks to manage memory and rate limits
        chunk_size = min(self.max_concurrent_per_account * 2, 20)
        
        for i in range(0, len(targets), chunk_size):
            if self.account_health.is_retired(account_name):
                print(f"🚫 {account_name}: Retired for this run, skipping remaining targets")
                break
            chunk = targets[i:i + chunk_size]
            
            # Create tasks for this chunk
            chunk_tasks: List[asyncio.Task[Optional[Dict[str, Any]]]] = []
            for target in chunk:
                task = asyncio.create_task(
                    self._safe_operation_wrapper(
                        client=client,
                        account=account,
                        account_name=account_name,
                        target=target,
                        operation_func=operation_func,
                        account_semaphore=account_semaphore,
                        global_semaphore=global_semaphore,
                        **kwargs
                    )
                )
                chunk_tasks.append(task)
            
            # Wait for chunk to complete
            chunk_results = await asyncio.gather(*chunk_tasks, return_exceptions=True)
            
            # Process chunk results
            for target, result in zip(chunk, chunk_results):
                if isinstance(result, BaseException):
                    error_msg = str(result).lower()
                    if 'flood' in error_msg or 'too many requests' in error_msg:
                        self.account_stats[account_name]['rate_limited'] += 1
                        print(f"⚠️ {account_name}: Rate limited on {target.get('title', target.get('id', 'Unknown'))}")
                        # Add back to queue for retry with longer delay
                        await asyncio.sleep(5)
                    else:
                        self.account_stats[account_name]['failed'] += 1
                        print(f"❌ {account_name}: Failed {target.get('title', target.get('id', 'Unknown'))}: {result}")
                    
                    self.failed_operations.append({
                        'account': account_name,
                        'target': target,
                        'error': str(result)
                    })
                else:
                    if isinstance(result, dict):
                        results.append(result)
                    self.account_stats[account_name]['success'] += 1
                    self.success_count += 1
            
            # Progress update
            processed = min(i + chunk_size, len(targets))
            progress = (processed / len(targets)) * 100
            print(f"📊 {account_name}: {processed}/{len(targets)} targets processed ({progress:.1f}%)")
            sys.stdout.flush()

            # Delay between chunks to respect rate limits
            if i + chunk_size < len(targets):
                await asyncio.sleep(self.delay_between_batches)
        
        print(f"✅ {account_name}: Completed {len(results)} successful operations")
        return results
    
    async def _safe_operation_wrapper(self, client: Any, account: Dict[str, Any], account_name: str, target: Dict[str, Any], 
                                    operation_func: Callable[..., Any], account_semaphore: asyncio.Semaphore,
                                    global_semaphore: asyncio.Semaphore, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Wrapper that handles rate limiting and errors for individual operations"""
        
        async with global_semaphore:  # Global concurrency limit
            async with account_semaphore:  # Per-account concurrency limit
                try:
                    if self.account_health.is_retired(account_name):
                        return None
                    if not await self.account_health.ensure_connected(client, account):
                        return None
                    # Rate limiting per target
                    target_key = f"{account_name}_{operation_func.__name__}_{target.get('id', 'unknown')}"
                    last_request = self.rate_limit_tracker.get(target_key) or 0
                    current_time = time.time()
                    
                    # Ensure minimum delay between requests to same target
                    time_since_last = current_time - last_request
                    if time_since_last < self.min_delay_per_chat:
                        await asyncio.sleep(self.min_delay_per_chat - time_since_last)
                    
                    # Update rate limit tracker
                    self.rate_limit_tracker[target_key] = time.time()
                    self.account_request_count[account_name] += 1
                    self.global_request_count += 1
                    
                    # Execute the operation
                    result = await operation_func(client, target, account_name=account_name, **kwargs)
                    return result
                    
                except UnicodeDecodeError as ue:
                    # Handle encoding errors specifically
                    print(f"🔤 {account_name}: Encoding error on {target.get('title', target.get('id', 'Unknown'))}: {ue}")
                    # Try to handle gracefully
                    return None
                    
                except Exception as e:
                    # Handle different types of errors
                    if isinstance(e, AccountFailureError) or is_account_error(e):
                        original_error = e.original_error if isinstance(e, AccountFailureError) else e
                        recovered = await self.account_health.handle_account_failure(
                            client,
                            account,
                            original_error,
                            f"parallel target:{target.get('id', 'unknown')}",
                        )
                        if not recovered:
                            return None
                        raise e
                    error_msg = str(e).lower()
                    if 'charmap' in error_msg or 'codec' in error_msg:
                        # Encoding issues
                        print(f"🔤 {account_name}: Character encoding issue on {target.get('title', target.get('id', 'Unknown'))}")
                        return None
                    elif 'flood' in error_msg or 'too many requests' in error_msg:
                        # Extract wait time if available
                        wait_time = 5  # Default wait time
                        if 'wait' in error_msg:
                            import re
                            wait_match = re.search(r'(\d+)', error_msg)
                            if wait_match:
                                wait_time = min(int(wait_match.group(1)), 60)  # Cap at 60 seconds
                        
                        print(f"⏰ {account_name}: Rate limited, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        # Retry the operation once after waiting
                        try:
                            result = await operation_func(client, target, account_name=account_name, **kwargs)
                            return result
                        except Exception:
                            pass  # Fall through to raise original error
                    
                    elif 'chat_admin_required' in error_msg or 'forbidden' in error_msg:
                        # Access denied - skip silently
                        pass
                    
                    elif 'user_not_participant' in error_msg:
                        # Not a member - expected for some operations
                        pass
                    
                    else:
                        # Log unexpected errors
                        self.logger.warning(f"Unexpected error in {account_name}: {e}")
                    
                    raise e
    
    def _print_final_statistics(self) -> None:
        """Print comprehensive statistics after processing"""
        elapsed_time = time.time() - self.start_time if self.start_time else 0
        
        print("\n📊 PARALLEL PROCESSING COMPLETE")
        print("=" * 60)
        print(f"⏱️ Total time: {elapsed_time:.2f} seconds")
        print(f"✅ Successful operations: {self.success_count:,}")
        print(f"❌ Failed operations: {len(self.failed_operations):,}")
        print(f"📈 Success rate: {(self.success_count / max(self.total_operations, 1)) * 100:.1f}%")
        print(f"🔥 Operations per second: {self.success_count / max(elapsed_time, 1):.2f}")
        print(f"🌐 Total requests made: {self.global_request_count:,}")
        
        print("\n📋 PER-ACCOUNT STATISTICS:")
        for account, stats in self.account_stats.items():
            total_account_ops = stats['success'] + stats['failed'] + stats['rate_limited']
            if total_account_ops > 0:
                success_rate = (stats['success'] / total_account_ops) * 100
                print(f"  📁 {account}:")
                print(f"     ✅ Success: {stats['success']:,} ({success_rate:.1f}%)")
                print(f"     ❌ Failed: {stats['failed']:,}")
                print(f"     ⏰ Rate Limited: {stats['rate_limited']:,}")
                print(f"     📊 Requests: {self.account_request_count[account]:,}")
        
        if self.failed_operations:
            print("\n⚠️ FAILED OPERATIONS SUMMARY:")
            error_types: defaultdict[str, int] = defaultdict(int)
            for failure in self.failed_operations:
                error_type = failure['error'].split(':')[0] if ':' in failure['error'] else failure['error']
                error_types[error_type] += 1
            
            for error_type, count in error_types.items():
                print(f"  ❌ {error_type}: {count} occurrences")


class AccountManager:
    """Manages available Telegram accounts for parallel processing"""
    
    @staticmethod
    def get_available_accounts() -> List[str]:
        """Get list of configured account names with valid session files"""
        sessions_dir = Path("sessions")
        sessions_dir.mkdir(exist_ok=True)
        
        configured_accounts = AccountManager.get_account_dictionaries()
        valid_configured_names: List[str] = []
        
        for account in configured_accounts:
            name = str(account.get('name', '')).strip()
            if not name:
                continue
            
            session_file = str(account.get('session_file', '')).strip()
            session_path = Path(session_file) if session_file else (sessions_dir / f"{name}.session")
            if session_path.exists():
                valid_configured_names.append(name)
        
        if valid_configured_names:
            return sorted(set(valid_configured_names))
        
        return sorted([session_file.stem for session_file in sessions_dir.glob("*.session")])
    
    @staticmethod
    def get_account_dictionaries() -> List[Dict[str, Any]]:
        """Get full account dictionaries with API credentials"""
        try:
            from src.core.dynamic_config import get_accounts
            return get_accounts()
        except Exception as e:
            print(f"⚠️ Error loading account data: {e}")
            return []
    
    @staticmethod
    def get_accounts_by_names(account_names: List[str]) -> List[Dict[str, Any]]:
        """Convert account names to full account dictionaries"""
        try:
            from src.core.dynamic_config import get_accounts
            all_accounts = get_accounts()
            lookup: Dict[str, Dict[str, Any]] = {}
            
            for account in all_accounts:
                name = str(account.get('name', '')).strip()
                if name:
                    lookup[name] = account
                
                session_file = str(account.get('session_file', '')).strip()
                if session_file:
                    session_stem = Path(session_file).stem
                    if session_stem:
                        lookup[session_stem] = account
            
            resolved: List[Dict[str, Any]] = []
            seen_names: Set[str] = set()
            for identifier in account_names:
                account = lookup.get(identifier)
                if not account:
                    continue
                account_name = account.get('name')
                if not isinstance(account_name, str):
                    continue
                if account_name in seen_names:
                    continue
                resolved.append(account)
                seen_names.add(account_name)
            return resolved
        except Exception as e:
            print(f"⚠️ Error converting account names: {e}")
            return []
    
    @staticmethod
    def validate_accounts(accounts: List[str]) -> List[str]:
        """Validate that session files exist for given accounts"""
        valid_accounts: List[str] = []
        sessions_dir = Path("sessions")
        
        for account in accounts:
            session_path = sessions_dir / f"{account}.session"
            if session_path.exists():
                valid_accounts.append(account)
            else:
                print(f"⚠️ Session file not found for account: {account}")
        
        return valid_accounts
    
    @staticmethod
    def get_optimal_account_count(total_targets: int, max_accounts: int = 8) -> int:
        """Calculate optimal number of accounts to use based on target count"""
        available_accounts = AccountManager.get_available_accounts()
        
        if max_accounts:
            max_accounts = min(max_accounts, len(available_accounts))
        else:
            max_accounts = len(available_accounts)
        
        # Use more accounts for larger target sets
        if total_targets <= 50:
            return min(2, max_accounts)
        elif total_targets <= 200:
            return min(3, max_accounts)
        elif total_targets <= 500:
            return min(4, max_accounts)
        else:
            return max_accounts
