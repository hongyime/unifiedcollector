import logging
import asyncio
import time
import sqlite3
from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError, 
    SessionPasswordNeededError, 
    SessionRevokedError, 
    AuthKeyError, 
    UserDeactivatedError,
    SecurityError
)
try:
    from telethon.tl.functions.auth import ResetAuthorizationRequest
except ImportError:
    ResetAuthorizationRequest = None
import os
from enum import Enum
from shared.database import get_db_connection

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """States for Telegram client connection state machine."""
    DISCONNECTED = 'disconnected'      # Not connected to Telegram
    CONNECTING = 'connecting'          # Attempting to connect
    CONNECTED = 'connected'            # Connected and authorized
    RECONNECTING = 'reconnecting'      # Lost connection, trying to recover
    FLOOD_WAIT = 'flood_wait'          # Being rate limited
    INVALID_SESSION = 'invalid_session'  # Session is revoked/invalid
    PAUSED = 'paused'                  # Manually paused by user


class TelegramClientManager:
    """
    Manages the Telethon client connection with state machine pattern.
    Handles initialization, authorization, health monitoring, and cleanup.
    
    State Transitions:
        DISCONNECTED -> CONNECTING -> CONNECTED
        CONNECTED -> DISCONNECTED (network loss)
        CONNECTED -> RECONNECTING -> CONNECTED (auto-recovery)
        CONNECTED -> FLOOD_WAIT -> CONNECTED (after wait)
        * -> INVALID_SESSION (session revoked)
    """
    
    def __init__(self, session_name: str = 'user_session', api_id: int = None, api_hash: str = None, enable_mtproto_reset: bool = False):
        from shared.config import settings
        self.api_id = api_id or settings.TG_API_ID
        self.api_hash = api_hash or settings.TG_API_HASH
        self.session_name = session_name
        self.enable_mtproto_reset = enable_mtproto_reset
        self._health_task = None
        self._is_healthy = False
        self._state = ConnectionState.DISCONNECTED
        self._state_change_callbacks = []
        self._rotation_callbacks: list = []
        self._flood_wait_until = None
        self._session_lock = None
        self.manual_pause: bool = False
        
        # Ensure session directory exists
        sessions_dir = os.path.join(os.getcwd(), 'sessions')
        os.makedirs(sessions_dir, exist_ok=True)
        
        # Prevent duplication if session_name already contains 'sessions/'
        if 'sessions' in session_name.split(os.sep):
            session_name = os.path.basename(session_name)
            
        session_path = os.path.join(sessions_dir, session_name)
        
        logger.debug(f"Initializing client with session path: {os.path.abspath(session_path)}")
        
        # Check if session is legacy (created before lock implementation)
        self._is_legacy_session = self._check_legacy_session(session_path)
        
        # Use SQLiteSession with increased timeout
        from telethon.sessions import SQLiteSession
        sqlite_session = SQLiteSession(session_path)
        
        import sqlite3
        if hasattr(sqlite_session, '_conn') and sqlite_session._conn:
            sqlite_session._conn.execute("PRAGMA busy_timeout = 30000")
        
        self.client = TelegramClient(sqlite_session, self.api_id, self.api_hash)
        self._session_path = session_path
    
    @property
    def state(self) -> ConnectionState:
        """Returns the current connection state."""
        return self._state
    
    async def _set_state(self, new_state: ConnectionState, reason: str = None):
        """Transitions to a new state with logging and notifications."""
        if self._state == new_state:
            return
        
        old_state = self._state
        self._state = new_state
        
        state_emoji = {
            ConnectionState.DISCONNECTED: '🔴',
            ConnectionState.CONNECTING: '🟡',
            ConnectionState.CONNECTED: '🟢',
            ConnectionState.RECONNECTING: '🟠',
            ConnectionState.FLOOD_WAIT: '⏳',
            ConnectionState.INVALID_SESSION: '❌',
            ConnectionState.PAUSED: '⏸️'
        }
        
        emoji = state_emoji.get(new_state, '❓')
        logger.info(f"{emoji} Session {self.session_name}: {old_state.value} -> {new_state.value}" + 
                   (f" ({reason})" if reason else ""))
        
        # Notify Hub for important state changes
        critical_states = [ConnectionState.INVALID_SESSION, ConnectionState.FLOOD_WAIT, ConnectionState.DISCONNECTED]
        if new_state in critical_states or old_state in critical_states:
            try:
                from shared.hub_notifier import notify
                message = f"{emoji} **{self.session_name}**: {new_state.value}"
                if reason:
                    message += f" - {reason}"
                priority = 2 if new_state == ConnectionState.INVALID_SESSION else 1
                await notify('system', message, priority=priority)
            except Exception as e:
                logger.debug(f"Could not notify Hub of state change: {e}")
        
        # Call registered callbacks
        for callback in self._state_change_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(old_state, new_state)
                else:
                    callback(old_state, new_state)
            except Exception as e:
                logger.error(f"State change callback error: {e}")
    
    def on_state_change(self, callback):
        """Registers a callback for state changes."""
        self._state_change_callbacks.append(callback)

    def on_session_rotation(self, callback):
        """
        Registers a callback invoked with the next session_name when rotation occurs.
        Signature: async def callback(next_session_name: str) -> None
        """
        self._rotation_callbacks.append(callback)
    
    def _check_legacy_session(self, session_path: str) -> bool:
        """Check if session was created before lock implementation."""
        candidates = [session_path]
        if not session_path.endswith('.session'):
            candidates.append(f"{session_path}.session")

        existing = next((path for path in candidates if os.path.exists(path)), None)
        if existing:
            file_age = time.time() - os.path.getmtime(existing)
            # If session older than 1 hour, it's legacy
            return file_age > 3600
        return False

    def _get_session_db_path(self) -> str:
        """Returns the actual SQLite session file path used by Telethon."""
        return self._session_path if self._session_path.endswith('.session') else f"{self._session_path}.session"

    def _clear_stale_session_state(self) -> bool:
        """
        Clears stale update state from the session DB without deleting auth_key.

        Returns:
            True if cleanup executed successfully (or table absent), else False.
        """
        session_db = self._get_session_db_path()
        if not os.path.exists(session_db):
            logger.debug(f"Session DB not found for cleanup: {session_db}")
            return False

        try:
            conn = sqlite3.connect(session_db)
            cur = conn.cursor()
            try:
                cur.execute("DELETE FROM update_state")
                conn.commit()
                logger.info(f"Cleared stale update_state for session {self.session_name}")
            except sqlite3.OperationalError:
                # Older/newer Telethon schema may not have this table.
                logger.debug(f"No update_state table for {self.session_name}; skipping cleanup")
            finally:
                conn.close()
            return True
        except Exception as e:
            logger.warning(f"Failed to clear stale session state for {self.session_name}: {e}")
            return False
    
    async def _acquire_session_lock(self):
        """Acquires exclusive lock on session file."""
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        
        await self._session_lock.acquire()
    
    def _release_session_lock(self):
        """Releases session file lock."""
        if self._session_lock and self._session_lock.locked():
            self._session_lock.release()
    
    async def start(self, phone: str = None, force_reset: bool = False):
        """
        Starts the Telegram client with optional MTProto state validation.
        
        Args:
            phone: Phone number for first-time authorization (optional if session exists)
            force_reset: Force MTProto reset (use only on new sessions)
        """
        await self._set_state(ConnectionState.CONNECTING)
        
        try:
            # Soft session lock with timeout fallback for backward compatibility
            # Increased timeout from 10s to 30s for slow systems or heavy I/O
            lock_acquired = False
            try:
                await asyncio.wait_for(self._acquire_session_lock(), timeout=30)
                lock_acquired = True
            except asyncio.TimeoutError:
                if self._is_legacy_session:
                    # Legacy sessions can proceed without lock (backward compatible)
                    logger.warning(f"⚠️ Could not acquire lock for {self.session_name}, proceeding (legacy session)")
                    lock_acquired = False
                else:
                    await self._set_state(ConnectionState.DISCONNECTED, "Session locked by another client")
                    raise RuntimeError("Session file locked. Wait and retry.")
            
            # Timeout on connect — WSL2 clock drift can cause Telethon to hang
            try:
                await asyncio.wait_for(self.client.connect(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Connection timed out for {self.session_name}. "
                    f"Attempting stale session-state cleanup and one retry..."
                )

                self._clear_stale_session_state()

                try:
                    await asyncio.wait_for(self.client.connect(), timeout=30)
                except asyncio.TimeoutError:
                    await self._set_state(ConnectionState.DISCONNECTED, "Connection timed out (clock drift?)")
                    raise RuntimeError(
                        f"Connection timed out for {self.session_name} (30s x2). "
                        f"Likely host/WSL clock drift or stale MTProto session state. "
                        f"Run scripts/sync_wsl_clock.bat on the Windows host, then restart containers."
                    )
            
            # --- Clock drift detection (warning only) ---
            # WSL2's clock can drift when the host sleeps/hibernates.
            # Telethon's time_offset is the CORRECT compensation — do NOT
            # reset it to 0 or server messages will be rejected as "very new".
            if hasattr(self.client, '_sender') and self.client._sender:
                state = getattr(self.client._sender, '_state', None)
                if state and hasattr(state, 'time_offset'):
                    offset = state.time_offset
                    if abs(offset) > 30:  # More than 30 seconds off
                        logger.warning(
                            f"⚠️ WSL2 clock drift detected for {self.session_name}: "
                            f"time_offset={offset}s (Telethon is compensating). "
                            f"Run 'wsl --shutdown' on the Windows host to fix the system clock."
                        )
            # --- End clock drift detection ---
            
            # MTProto reset only if enabled AND not legacy
            if (self.enable_mtproto_reset or force_reset) and not self._is_legacy_session and ResetAuthorizationRequest:
                try:
                    logger.info(f"Resetting MTProto authorization state for {self.session_name}...")
                    await self.client(ResetAuthorizationRequest())
                    logger.info(f"MTProto state reset successfully")
                except Exception as e:
                    logger.warning(f"MTProto reset failed (non-critical): {e}")
            elif self._is_legacy_session and not force_reset:
                logger.info(f"Skipping MTProto reset for legacy session {self.session_name} (backward compatible)")
            
            # Check authorization status
            if not await self.client.is_user_authorized():
                if phone:
                    logger.info(f"Authorizing with phone: {phone}")
                    await asyncio.wait_for(self.client.start(phone=phone), timeout=30)
                else:
                    await asyncio.wait_for(self.client.start(), timeout=30)
            
            # Verify we're logged in
            me = await self.client.get_me()
            if me:
                logger.info(f"✅ Logged in: {me.first_name} (@{me.username or 'N/A'})")
                self._is_healthy = True
                await self._set_state(ConnectionState.CONNECTED)
            else:
                raise RuntimeError("Failed to get user info after authorization")
            
            # Start health monitoring
            self._health_task = asyncio.create_task(self._health_monitor())
            
            # Schedule auto-cleanup of login messages
            asyncio.create_task(self._cleanup_login_messages())
            
            return me

        except (SessionRevokedError, AuthKeyError, UserDeactivatedError) as e:
            await self._set_state(ConnectionState.INVALID_SESSION, str(e))
            await self._handle_invalid_session()
            raise
        
        except FloodWaitError as e:
            await self._set_state(ConnectionState.FLOOD_WAIT, f"Waiting {e.seconds}s")
            self._flood_wait_until = asyncio.get_event_loop().time() + e.seconds
            await asyncio.sleep(e.seconds)
            return await self.start(phone, force_reset)
            
        except Exception as e:
            await self._set_state(ConnectionState.DISCONNECTED, str(e))
            raise
        finally:
            # Always release session lock
            if lock_acquired:
                self._release_session_lock()

    async def _handle_invalid_session(self):
        """
        Handles cleanup for invalid sessions:
        1. Updates DB status to 'paused' (not 'invalid') to preserve checkpoints
        2. Does NOT delete session file - user can re-login via Login Bot
        3. If SESSION_ROTATION_ENABLED and not manual_pause, queries DB for the
           next active account and fires all registered rotation callbacks.
        """
        try:
            session_file_path = os.path.join('sessions', f"{self.session_name}.session")

            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        UPDATE collector.telegram_accounts
                        SET status = 'paused',
                            last_error = 'Session logged out - use Login Bot to re-authenticate'
                        WHERE session_file_path = %s
                           OR session_file_path = %s
                    """, (session_file_path, os.path.join('sessions', self.session_name)))
                    await conn.commit()

            logger.warning(f"⚠️ Session {self.session_name} paused. Checkpoints preserved.")

            # Notify Hub about invalid session
            try:
                from shared.hub_notifier import notify
                await notify('error', f"❌ Session **{self.session_name}** logged out - requires re-authentication", priority=2)
            except Exception:
                pass

            # Session rotation — only when enabled and not manually paused
            from shared.config import settings
            if settings.SESSION_ROTATION_ENABLED and not self.manual_pause:
                try:
                    async with get_db_connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("""
                                SELECT session_file_path FROM collector.telegram_accounts
                                WHERE status = 'active'
                                  AND session_file_path NOT LIKE %s
                                ORDER BY last_active ASC
                                LIMIT 1
                            """, (f"%{self.session_name}%",))
                            row = await cur.fetchone()

                    if row:
                        next_session_path = row[0]
                        # Extract bare session name from path
                        next_session_name = os.path.splitext(os.path.basename(next_session_path))[0]
                        logger.info(
                            f"🔄 Rotating session: {self.session_name} → {next_session_name}"
                        )
                        for cb in self._rotation_callbacks:
                            try:
                                await cb(next_session_name)
                            except Exception as cb_err:
                                logger.error(f"Rotation callback error: {cb_err}")
                    else:
                        logger.critical(
                            f"🚨 Session {self.session_name} is invalid and NO other active "
                            f"accounts are available. Scanning has stopped."
                        )
                except Exception as rot_err:
                    logger.error(f"Error during session rotation query: {rot_err}")

        except Exception as e:
            logger.error(f"Error during session pause handling: {e}")
    
    async def _health_monitor(self):
        """
        Periodically checks connection health and reconnects if needed.
        Uses exponential backoff for reconnection attempts.
        
        Also detects MTProto message ID conflicts (caused by another app
        using the same Telegram account) and performs full reconnection
        with session time resync.
        """
        MAX_RECONNECT_ATTEMPTS = 10  # Increased for shared-account resilience
        BASE_RECONNECT_DELAY = 5
        reconnect_attempts = 0
        self._consecutive_mtproto_errors = 0
        
        while True:
            try:
                await asyncio.sleep(60)
                
                if not self.client.is_connected():
                    self._is_healthy = False
                    await self._set_state(ConnectionState.RECONNECTING, f"Attempt {reconnect_attempts + 1}/{MAX_RECONNECT_ATTEMPTS}")
                    
                    # Exponential backoff
                    delay = BASE_RECONNECT_DELAY * (2 ** min(reconnect_attempts, 6))  # Cap at ~320s
                    if reconnect_attempts > 0:
                        await asyncio.sleep(delay)
                    
                    try:
                        # Full disconnect + reconnect to force fresh MTProto state
                        try:
                            await self.client.disconnect()
                        except Exception:
                            pass
                        await asyncio.sleep(2)  # Brief pause before reconnect
                        await self.client.connect()
                        
                        if await self.client.is_user_authorized():
                            # Reset MTProto state on reconnect if enabled
                            if self.enable_mtproto_reset and not self._is_legacy_session and ResetAuthorizationRequest:
                                try:
                                    await self.client(ResetAuthorizationRequest())
                                except Exception:
                                    pass
                            
                            self._is_healthy = True
                            reconnect_attempts = 0
                            self._consecutive_mtproto_errors = 0
                            await self._set_state(ConnectionState.CONNECTED, "Reconnected")
                        else:
                            reconnect_attempts += 1
                            await self._set_state(ConnectionState.INVALID_SESSION, "Not authorized after reconnect")
                    except FloodWaitError as e:
                        await self._set_state(ConnectionState.FLOOD_WAIT, f"Waiting {e.seconds}s")
                        await asyncio.sleep(e.seconds)
                    except Exception as conn_err:
                        reconnect_attempts += 1
                        
                        if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                            await self._set_state(ConnectionState.DISCONNECTED, f"Max attempts reached: {conn_err}")
                            try:
                                from shared.database import log_processing_error
                                await log_processing_error(
                                    error_type='ClientDisconnected',
                                    error_message=f"Max reconnect attempts reached: {conn_err}",
                                    error_context={'session': self.session_name}
                                )
                            except Exception:
                                pass
                            break
                else:
                    # Connected - ensure state is correct
                    if self._state != ConnectionState.CONNECTED:
                        await self._set_state(ConnectionState.CONNECTED)
                    self._is_healthy = True
                    reconnect_attempts = 0
                    
            except asyncio.CancelledError:
                break
            except (SecurityError, ConnectionError) as e:
                # MTProto conflict detection:
                # "Too many messages had to be ignored consecutively" or
                # "Server sent a very new message with ID" → another app is
                # using this account simultaneously.
                self._consecutive_mtproto_errors += 1
                error_msg = str(e)
                
                if self._consecutive_mtproto_errors >= 3:
                    logger.warning(
                        f"⚠️ MTProto conflict detected for {self.session_name} "
                        f"({self._consecutive_mtproto_errors} consecutive errors). "
                        f"Another app may be using this account. "
                        f"Backing off for 120s before reconnecting..."
                    )
                    self._is_healthy = False
                    await self._set_state(ConnectionState.RECONNECTING, "MTProto conflict - backing off")
                    
                    # Disconnect fully, wait, then reconnect
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                    # Back off longer to let the other app stabilize
                    await asyncio.sleep(120)
                    
                    try:
                        await self.client.connect()
                        if await self.client.is_user_authorized():
                            self._is_healthy = True
                            self._consecutive_mtproto_errors = 0
                            reconnect_attempts = 0
                            await self._set_state(ConnectionState.CONNECTED, "Recovered from MTProto conflict")
                            logger.info(f"✅ {self.session_name} recovered from MTProto conflict")
                        else:
                            await self._set_state(ConnectionState.INVALID_SESSION, "Not authorized after MTProto recovery")
                    except Exception as recovery_err:
                        logger.error(f"MTProto recovery failed for {self.session_name}: {recovery_err}")
                        reconnect_attempts += 1
                else:
                    logger.warning(f"MTProto error #{self._consecutive_mtproto_errors} for {self.session_name}: {error_msg}")
                    self._is_healthy = False
            except Exception as e:
                logger.error(f"Health check error: {e}")
                self._is_healthy = False
    
    @property
    def is_healthy(self) -> bool:
        """Returns True if the client is connected and authorized."""
        return self._is_healthy and self.client.is_connected()
    
    async def is_authorized(self) -> bool:
        """Check if the client is authorized."""
        return await self.client.is_user_authorized()

    async def _cleanup_login_messages(self):
        """
        Waits 2 minutes after startup, then deletes messages exchanged with
        the user-defined login bot (e.g., a bot used to facilitate the login process).
        
        The bot ID/username is configured via the LOGIN_BOT_ID environment variable.
        This clears sensitive OTP/login codes from the chat history.
        """
        from shared.config import settings
        login_bot = settings.LOGIN_BOT_ID
        if not login_bot:
            logger.debug("LOGIN_BOT_ID not set. Skipping login message cleanup.")
            return
        
        logger.info(f"Scheduling login message cleanup with bot '{login_bot}' in 2 minutes...")
        await asyncio.sleep(120)  # Wait 2 minutes
        
        try:
            # Get the bot entity (can be username like @MyBot or numeric ID)
            bot_entity = await self.client.get_entity(login_bot)
            
            # Fetch recent messages with this bot
            messages = await self.client.get_messages(bot_entity, limit=20)
            
            if messages:
                # Delete our messages to the bot (we can only delete our own side)
                our_messages = [m for m in messages if m.out]  # m.out = sent by us
                if our_messages:
                    await self.client.delete_messages(bot_entity, our_messages)
                    logger.info(f"Deleted {len(our_messages)} of our messages from chat with '{login_bot}'.")
                
                # Attempt to delete bot's messages (may fail if we lack permissions)
                try:
                    bot_messages = [m for m in messages if not m.out]
                    if bot_messages:
                        await self.client.delete_messages(bot_entity, bot_messages)
                        logger.info(f"Deleted {len(bot_messages)} bot messages from chat with '{login_bot}'.")
                except Exception:
                    logger.debug("Could not delete bot's messages (permission denied or not allowed).")
            else:
                logger.debug(f"No messages found with bot '{login_bot}' to delete.")
                
        except Exception as e:
            logger.warning(f"Failed to cleanup login messages with bot '{login_bot}': {e}")

    async def stop(self):
        """Gracefully disconnect the client."""
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        
        await self.client.disconnect()
        self._is_healthy = False
        self._release_session_lock()
        logger.info("Telegram client disconnected.")


# Convenience function for simple initialization
async def initialize_client(session_name: str = 'user_session') -> TelegramClient:
    """
    Convenience function to initialize and return a connected Telegram client.
    
    Returns:
        A connected and authorized TelegramClient instance.
    """
    manager = TelegramClientManager(session_name=session_name)
    await manager.start()
    return manager.client
