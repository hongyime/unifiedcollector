#!/usr/bin/env python3
"""
Base Feature Class for Telegram Toolkit
Provides common patterns shared by all feature processors.
"""
import asyncio
import random
import signal
from typing import Any, Dict, List, Optional, Callable
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from src.core.account_health import AccountFailureError, AccountHealthPolicy, is_account_error
from src.core.state_manager import get_state_manager
from src.core.dynamic_config import get_config_value


class BaseFeature:
    """Common patterns shared by all feature processors"""

    _instances: List["BaseFeature"] = []
    _signal_handler_installed = False
    _shutdown_requested = False
    
    def __init__(self, name: str = "base"):
        self.name = name
        self.state = get_state_manager()
        self.config = self._load_config()
        self.should_exit = False
        BaseFeature._instances.append(self)
        
        # Rate limiting
        self.min_delay = self.config.get('MIN_DELAY', 0.5)
        self.max_delay = self.config.get('MAX_DELAY', 2.0)
        
        # Retry configuration
        self.max_retries = self.config.get('MAX_RETRIES', 3)
        self.retry_delay = self.config.get('RETRY_DELAY', 5)
        
        # Setup graceful shutdown
        self._setup_signal_handlers()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from dynamic config"""
        return {
            'MIN_DELAY': get_config_value('MIN_DELAY', 0.5),
            'MAX_DELAY': get_config_value('MAX_DELAY', 2.0),
            'MAX_RETRIES': get_config_value('MAX_RETRIES', 3),
            'RETRY_DELAY': get_config_value('RETRY_DELAY', 5),
        }
    
    def _setup_signal_handlers(self):
        """Setup handlers for graceful shutdown on Ctrl+C"""
        if BaseFeature._signal_handler_installed:
            return

        def graceful_shutdown(signum, frame):
            if BaseFeature._shutdown_requested:
                print("\n⚠️ Ctrl+C received again. Flushing buffered state, then exiting immediately.")
                try:
                    for instance in list(BaseFeature._instances):
                        instance.should_exit = True
                        if getattr(instance, 'state', None) is not None:
                            instance.state.flush_all_buffers()
                except Exception:
                    pass
                raise KeyboardInterrupt

            BaseFeature._shutdown_requested = True
            print("\n⚠️ Ctrl+C detected! Saving progress and stopping active features...")
            for instance in list(BaseFeature._instances):
                instance.should_exit = True
            print("✅ Shutdown requested. Waiting for active tasks to stop cleanly...")
        
        signal.signal(signal.SIGINT, graceful_shutdown)
        BaseFeature._signal_handler_installed = True

    def _abort_requested(self) -> bool:
        """Return True when a cooperative shutdown has been requested."""
        return self.should_exit or BaseFeature._shutdown_requested

    async def _sleep_with_abort(self, total_seconds: float, step: float = 0.2) -> bool:
        """
        Sleep in small increments so Ctrl+C can stop retries quickly.

        Returns:
            True if aborted during sleep, else False.
        """
        if total_seconds <= 0:
            return self._abort_requested()

        remaining = float(total_seconds)
        while remaining > 0:
            if self._abort_requested():
                return True
            tick = min(step, remaining)
            await asyncio.sleep(tick)
            remaining -= tick
        return self._abort_requested()
    
    async def verify_entity_access(self, client: TelegramClient, entity) -> bool:
        """Check if account can access entity (group/channel)"""
        try:
            await client.get_entity(entity)
            return True
        except Exception as e:
            error_msg = str(e)
            if any(x in error_msg for x in [
                'Could not find the input entity',
                'PEER_ID_INVALID',
                'USER_ID_INVALID',
                'CHANNEL_PRIVATE',
                'USER_NOT_PARTICIPANT'
            ]):
                return False
            raise
    
    async def retry_api_call(
        self,
        func: Callable,
        *args,
        _client: Optional[TelegramClient] = None,
        _account_name: Optional[str] = None,
        _clients_map: Optional[Dict[str, TelegramClient]] = None,
        _account_health: Optional[AccountHealthPolicy] = None,
        **kwargs
    ) -> Optional[Any]:
        """Retry API calls with exponential backoff and optional cross-account failover.

        When _clients_map and _account_health are provided, a FloodWait on the
        primary account will cause an automatic fallback to the best available
        alternate account instead of blocking for the full wait duration.
        """
        last_error = None

        for attempt in range(self.max_retries):
            if self._abort_requested():
                print(f"⚠️ [{self.name}] Shutdown requested, aborting retries")
                return None
            try:
                return await func(*args, **kwargs)
            except FloodWaitError as e:
                wait_time = e.seconds
                # Record flood-wait so other callers also avoid this account
                if _account_health and _account_name:
                    _account_health.record_flood_wait(_account_name, wait_time)

                # Try a different account before sleeping
                fallback_result = await self._try_fallback_clients(
                    func, *args,
                    _clients_map=_clients_map,
                    _account_health=_account_health,
                    _exclude_account=_account_name,
                    **kwargs,
                )
                if fallback_result is not None:
                    return fallback_result

                print(f"⏳ [{self.name}] Rate limit: Waiting {wait_time}s...")
                if await self._sleep_with_abort(wait_time):
                    print(f"⚠️ [{self.name}] Shutdown requested during FloodWait backoff")
                    return None
                last_error = e
            except RPCError as e:
                if is_account_error(e):
                    raise AccountFailureError(_account_name or self.name, e, phase=f"{self.name}.retry_api_call") from e
                error_msg = str(e)
                # Known unrecoverable errors
                if any(x in error_msg for x in [
                    'Could not find the input entity',
                    'PEER_ID_INVALID',
                    'USER_ID_INVALID',
                    'USERNAME_NOT_OCCUPIED'
                ]):
                    print(f"❌ [{self.name}] Unrecoverable error: {error_msg}")
                    return None

                # Retry on other errors
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    print(f"⚠️ [{self.name}] Retry {attempt+1}/{self.max_retries} in {delay}s...")
                    if await self._sleep_with_abort(delay):
                        print(f"⚠️ [{self.name}] Shutdown requested during retry backoff")
                        return None
                else:
                    print(f"❌ [{self.name}] Max retries reached: {error_msg}")
                    return None
                last_error = e
            except Exception as e:
                if is_account_error(e):
                    raise AccountFailureError(_account_name or self.name, e, phase=f"{self.name}.retry_api_call") from e
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay
                    print(f"⚠️ [{self.name}] Retry {attempt+1}/{self.max_retries} in {delay}s...")
                    if await self._sleep_with_abort(delay):
                        print(f"⚠️ [{self.name}] Shutdown requested during retry backoff")
                        return None
                else:
                    print(f"❌ [{self.name}] Max retries reached: {e}")
                    return None
                last_error = e

        return None

    async def _try_fallback_clients(
        self,
        func: Callable,
        *args,
        _clients_map: Optional[Dict[str, TelegramClient]] = None,
        _account_health: Optional[AccountHealthPolicy] = None,
        _exclude_account: Optional[str] = None,
        **kwargs,
    ) -> Optional[Any]:
        """Attempt the same API call on the best available alternate account."""
        if not _clients_map or not _account_health:
            return None

        candidate_names = list(_clients_map.keys())
        best = _account_health.get_best_account(candidate_names, exclude=_exclude_account)
        if best is None:
            return None

        alt_client = _clients_map[best]
        # Replace the client reference in the callable — the func is typically
        # client.get_entity or client.download_media, so we rebind it.
        try:
            method_name = getattr(func, '__name__', None) or getattr(func, '__func__', None)
            if method_name and hasattr(alt_client, str(method_name)):
                alt_func = getattr(alt_client, str(method_name))
                result = await alt_func(*args, **kwargs)
                print(f"🔄 [{self.name}] Fallback succeeded via account {best}")
                return result
        except FloodWaitError as e:
            _account_health.record_flood_wait(best, e.seconds)
        except Exception:
            pass
        return None
    
    async def apply_rate_limit(self):
        """Apply random delay to avoid rate limiting"""
        delay = random.uniform(self.min_delay, self.max_delay)
        await self._sleep_with_abort(delay)
    
    def handle_error(self, error: Exception, context: Dict[str, Any]) -> str:
        """
        Categorize and handle errors.
        Returns error category: 'unrecoverable', 'temporary', 'access_denied'
        """
        error_msg = str(error)
        
        # Unrecoverable errors
        if any(x in error_msg for x in [
            'Could not find the input entity',
            'PEER_ID_INVALID',
            'USER_ID_INVALID',
            'CHANNEL_PRIVATE'
        ]):
            print(f"❌ [{self.name}] Unrecoverable error in {context.get('action', 'unknown')}: {error_msg}")
            return 'unrecoverable'
        
        # Access denied errors
        if any(x in error_msg for x in [
            'USER_NOT_PARTICIPANT',
            'USER_PRIVACY_RESTRICTED',
            'USER_BOT_REQUIRED'
        ]):
            print(f"🚫 [{self.name}] Access denied in {context.get('action', 'unknown')}: {error_msg}")
            return 'access_denied'
        
        # Temporary errors (retryable)
        if isinstance(error, FloodWaitError):
            print(f"⏳ [{self.name}] Rate limited in {context.get('action', 'unknown')}: wait {error.seconds}s")
            return 'temporary'
        
        # Unknown errors
        print(f"⚠️ [{self.name}] Error in {context.get('action', 'unknown')}: {error_msg}")
        return 'unknown'
    
    def format_user_info(self, user) -> Dict[str, Any]:
        """Format user information for storage"""
        return {
            'id': user.id,
            'username': getattr(user, 'username', '') or '',
            'first_name': getattr(user, 'first_name', '') or '',
            'last_name': getattr(user, 'last_name', '') or '',
            'phone': getattr(user, 'phone', '') or '',
            'is_bot': getattr(user, 'bot', False),
            'is_verified': getattr(user, 'verified', False),
            'is_premium': getattr(user, 'premium', False)
        }
    
    def extract_links(self, text: str) -> List[str]:
        """Extract links from text"""
        import re
        # Telegram link patterns
        patterns = [
            r'https?://t\.me/([a-zA-Z0-9_]{5,32})',
            r't\.me/([a-zA-Z0-9_]{5,32})',
            r'@([a-zA-Z0-9_]{5,32})',
        ]
        
        links = []
        for pattern in patterns:
            matches = re.findall(pattern, text)
            for match in matches:
                if not self._is_bot_keyword(match):
                    links.append(match)
        
        return links
    
    def _is_bot_keyword(self, text: str) -> bool:
        """Check if text contains bot-related keywords"""
        lower = text.lower()
        return 'bot' in lower or 'robot' in lower
