"""
Main Worker - Application entry point that integrates all components.

Initializes and coordinates:
- Phase 1: Database connection
- Phase 2: Telegram client, scanners, topic manager, media uploader
- Phase 3: Face processor, video extractor, identity matcher, processing queue
"""
import logging
import asyncio
import os
import re
import signal
import time
from typing import Dict, List
from shared.config import get_dynamic_setting, settings
from shared.hub_notifier import HubNotifier
from shared.observability import start_metrics_server
from services.collector.clock_monitor import start_clock_monitoring, stop_clock_monitoring

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Pre-compile session filename pattern for auto-discovery (F-014)
try:
    _SESSION_PATTERN = re.compile(r'account_(\d+)_\d+\.session$')
except re.error as e:
    logger.error(f"Invalid regex pattern for session discovery: {e}")
    _SESSION_PATTERN = None


class MainWorker:
    """
    Main application worker that coordinates all components.
    Supports multiple Telegram accounts running in parallel.
    """
    
    def __init__(self):
        self.clients: Dict[int, 'TelegramClientManager'] = {}  # account_id -> client_manager
        self.processing_queue = None
        self._shutdown_event = asyncio.Event()
        self._running = False
        self.scanners: Dict[int, tuple] = {}  # account_id -> (MessageScanner, RealtimeScanner)
        self.story_scanners: Dict[int, 'StoryScanner'] = {}  # account_id -> StoryScanner
        self.scheduler = None  # AccountScheduler instance

    async def initialize(self):
        """Initializes all components in proper order."""
        self._running = True
        logger.info("Initializing application components...")
        
        # Start metrics server early for health checks
        start_metrics_server(8000)
        
        # Phase 1: Database
        from shared.database import init_db, db_manager, get_db_connection
        await db_manager.initialize()
        await init_db()
        logger.info("✓ Database initialized (Phase 1)")
        
        # Phase 2: Bot Client (for topics and publishing)
        from services.collector.account_manager import bot_client_manager
        await bot_client_manager.start()
        bot_client_manager.register_worker(self)
        logger.info("✓ Bot client connected (for topic management)")
        
        # Resolve Hub Group ID (supports @username)
        from shared.config import resolve_hub_group_id
        try:
            hub_id = await resolve_hub_group_id(bot_client_manager.client)
            logger.info(f"✓ Hub Group resolved: {settings.HUB_GROUP_ID} → {hub_id}")
        except Exception as e:
            logger.error(f"Failed to resolve Hub Group ID: {e}")
            raise
        
        # Phase 2: Topic Manager (uses bot client)
        from shared.topic_manager import TopicManager
        self.topic_manager = TopicManager()  # Uses bot_client singleton internally
        logger.info("✓ Topic manager ready (using bot)")
        
        # Phase 2: Media Uploader (uses bot client)
        from shared.media_uploader import MediaUploader
        self.media_uploader = MediaUploader(topic_manager=self.topic_manager)  # Uses bot_client singleton
        logger.info("✓ Media uploader ready (using bot)")

        
        # Phase 3: Face Processor
        from services.face_recognition.processor import FaceProcessor
        self.face_processor = FaceProcessor.get_instance()
        logger.info("✓ Face processor initialized (Phase 3)")

        # Phase 3: Video Extractor
        from services.collector.video_extractor import VideoFrameExtractor
        self.video_extractor = VideoFrameExtractor()
        logger.info("✓ Video extractor ready")

        # Phase 3: Identity Matcher (needs asyncpg pool — separate from psycopg pool)
        import asyncpg
        from services.face_recognition.matcher import IdentityMatcher
        _face_dsn = (
            f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}"
            f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
        )
        self._face_db_pool = await asyncpg.create_pool(dsn=_face_dsn, min_size=2, max_size=5)
        self.identity_matcher = IdentityMatcher(self._face_db_pool)
        logger.info("✓ Identity matcher ready")
        
        # Phase 3: Processing Queue
        from shared.processing_queue import ProcessingQueue
        num_workers = settings.NUM_WORKERS
        high_watermark = settings.QUEUE_MAX_SIZE
        low_watermark = max(1, int(high_watermark * 0.2)) # 20% of max
        
        self.processing_queue = ProcessingQueue(
            face_processor=self.face_processor,
            video_extractor=self.video_extractor,
            identity_matcher=self.identity_matcher,
            media_uploader=self.media_uploader,
            topic_manager=self.topic_manager,
            num_workers=num_workers,
            high_watermark=high_watermark,
            low_watermark=low_watermark
        )
        await self.processing_queue.start()
        logger.info(f"✓ Processing queue started with {num_workers} workers")
        
        # Phase 4: Hub Notifier (for status updates)
        self.hub_notifier = HubNotifier.get_instance()
        await self.hub_notifier.start()
        logger.info("✓ Hub notifier started")

        # Phase 5: Health Checker with Self-Healing
        from health_checker import HealthChecker  # noqa: local runtime module
        # client will be updated after accounts are loaded (P0.1 fix)
        self.health_checker = HealthChecker(
            client=None,
            face_processor=self.face_processor,
            processing_queue=self.processing_queue,
            check_interval=settings.HEALTH_CHECK_INTERVAL
        )
        
        # Register recovery actions
        self.health_checker.register_recovery('telegram', self._recover_telegram)
        self.health_checker.register_recovery('hub_access', self._recover_hub_access)
        
        await self.health_checker.start()
        logger.info("✓ Health checker started with self-healing enabled")

        # Phase 2: Load User Accounts (Multiple)
        from shared.telegram_client import TelegramClientManager
        from shared.media_downloader import MediaDownloadManager
        from message_scanner import MessageScanner, RealtimeScanner  # noqa: local runtime module

        # AUTO-DISCOVERY: Scan sessions directory and register any existing sessions
        await self._auto_discover_sessions()

        # SELF-HEALING: Purge Hub Group from checkpoints to prevent infinite loops
        try:
            from shared.config import get_hub_group_id
            hub_group_id = get_hub_group_id()
            if hub_group_id:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM collector.scan_checkpoints WHERE chat_id IN (%s, %s)",
                            (hub_group_id, -hub_group_id)
                        )
            logger.info("✓ Self-healing: Hub Group removed from scan checkpoints")
        except Exception as e:
            logger.warning(f"Failed to remove Hub Group from checkpoints: {e}")

        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id, phone_number, session_file_path FROM collector.telegram_accounts WHERE status = 'active'")
                rows = await cur.fetchall()

                # Debug: check total accounts
                await cur.execute("SELECT COUNT(*), status FROM collector.telegram_accounts GROUP BY status")
                stats = await cur.fetchall()
                if stats:
                    logger.info(f"Account stats in DB: {stats}")
                else:
                    logger.info("Account stats in DB: No accounts found.")

        if not rows:
            logger.warning("⚠️  No active Telegram accounts found in database! Please register with the Login Bot.")
        
        self.scanners = {} # account_id -> (MessageScanner, RealtimeScanner)

        for account_id, phone, session_path in rows:
            try:
                # Extract session name from path (e.g., "sessions/account_123.session" -> "account_123")
                session_name = os.path.splitext(os.path.basename(session_path))[0]
                
                logger.info(f"Connecting account {account_id} ({phone})...")
                
                # Determine if MTProto reset should be enabled for this session
                enable_reset = settings.ENABLE_MTPROTO_RESET
                
                # Only reset new sessions if configured
                if settings.MTPROTO_RESET_NEW_SESSIONS_ONLY:
                    is_new = not os.path.exists(session_path) or \
                            (time.time() - os.path.getmtime(session_path) < 3600)
                    enable_reset = enable_reset and is_new
                
                manager = TelegramClientManager(session_name=session_name, enable_mtproto_reset=enable_reset)
                await manager.start()
                
                self.clients[account_id] = manager
                
                # Create Scanners for this account
                # Note: MediaDownloadManager needs a client. We create one per account.
                media_downloader = MediaDownloadManager(manager.client)
                
                scanner = MessageScanner(
                    client=manager.client,
                    media_manager=media_downloader,
                    processing_queue=self.processing_queue
                )
                
                rt_scanner = RealtimeScanner(
                    client=manager.client,
                    media_manager=media_downloader,
                    processing_queue=self.processing_queue
                )
                
                self.scanners[account_id] = (scanner, rt_scanner)
                
                # Story Scanner (only user accounts can access stories)
                if get_dynamic_setting("STORY_SCAN_ENABLED", settings.STORY_SCAN_ENABLED):
                    from story_scanner import StoryScanner  # noqa: per-account local runtime adapter
                    story_scanner = StoryScanner(
                        client=manager.client,
                        processing_queue=self.processing_queue,
                        media_manager=media_downloader
                    )
                    self.story_scanners[account_id] = story_scanner
                
                logger.info(f"✓ Account {account_id} connected and scanners ready")
                
            except Exception as e:
                logger.error(f"Failed to connect account {account_id} ({phone}): {e}")

        logger.info(f"✓ All {len(self.clients)} accounts initialized successfully!")

        # P0.1 fix: Update HealthChecker with first connected client
        if self.clients:
            first_client = next(iter(self.clients.values())).client
            self.health_checker.client = first_client
            logger.info("✓ HealthChecker updated with connected client")
        
        # Start background clock drift monitor (critical for Telegram auth)
        await start_clock_monitoring()
        logger.info("✓ Clock drift monitor started (background)")
        
        # Failsafe: Ensure bots are in the Hub group
        await self._ensure_bots_in_hub()
        
        # Log status to Hub Group
        await self.log_startup_status()

    async def _ensure_bots_in_hub(self):
        """
        Failsafe: Checks if bots are members of the Hub group.
        If not, uses a logged-in user account (that is an admin in the Hub)
        to invite them automatically.
        """
        from telethon.tl.functions.channels import InviteToChannelRequest, GetParticipantRequest
        from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
        from telethon.errors import (
            UserAlreadyParticipantError, ChatAdminRequiredError,
            UserPrivacyRestrictedError, FloodWaitError,
            UserNotMutualContactError, UserBotError
        )
        from shared.bot_pool import bot_pool
        
        from shared.config import get_hub_group_id, resolve_hub_group_id
        hub_id = get_hub_group_id() or settings.HUB_GROUP_ID
        if not hub_id:
            return
        
        if not self.clients:
            logger.debug("No user accounts connected, skipping bot invite check")
            return
        
        logger.info("🔍 Checking if bots are in the Hub group...")
        
        # Step 1: Find a user account that is an admin in the Hub
        admin_client = None
        admin_account_id = None
        
        for account_id, manager in self.clients.items():
            try:
                client = manager.client
                if not isinstance(hub_id, int):
                    try:
                        hub_id = await resolve_hub_group_id(client)
                    except Exception:
                        hub_id = settings.HUB_GROUP_ID
                hub = await client.get_entity(hub_id)
                
                # Check if this user is an admin
                me = await client.get_me()
                participant = await client(GetParticipantRequest(hub, me.id))
                p = participant.participant
                
                if isinstance(p, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                    admin_client = client
                    admin_account_id = account_id
                    logger.info(f"✓ Account {account_id} is admin in Hub")
                    break
                else:
                    logger.debug(f"Account {account_id} is in Hub but not admin")
            except Exception as e:
                logger.debug(f"Account {account_id} can't access Hub: {e}")
                continue
        
        if not admin_client:
            logger.warning("⚠️ No user accounts are admins in the Hub group. Bots must be added manually.")
            return
        
        # Step 2: Check each bot and invite if missing
        bot_tokens = settings.parsed_bot_tokens
        invited_count = 0
        
        for bot_info in bot_tokens:
            bot_name = bot_info['name']
            bot_token = bot_info['token']
            
            try:
                # Extract bot ID from token (format: "bot_id:secret")
                bot_id = int(bot_token.split(':')[0])
                
                # Check if bot is already in the Hub
                try:
                    if not isinstance(hub_id, int):
                        try:
                            hub_id = await resolve_hub_group_id(admin_client)
                        except Exception:
                            hub_id = settings.HUB_GROUP_ID
                    hub = await admin_client.get_entity(hub_id)
                    await admin_client(GetParticipantRequest(hub, bot_id))
                    logger.debug(f"Bot {bot_name} ({bot_id}) already in Hub ✓")
                    continue
                except Exception:
                    # Bot not found in Hub — needs invite
                    pass
                
                # Invite the bot
                logger.info(f"📨 Inviting bot {bot_name} ({bot_id}) to Hub...")
                try:
                    bot_entity = await admin_client.get_entity(bot_id)
                    if not isinstance(hub_id, int):
                        try:
                            hub_id = await resolve_hub_group_id(admin_client)
                        except Exception:
                            hub_id = settings.HUB_GROUP_ID
                    hub = await admin_client.get_entity(hub_id)
                    await admin_client(InviteToChannelRequest(hub, [bot_entity]))
                    logger.info(f"✅ Bot {bot_name} invited to Hub successfully!")
                    invited_count += 1
                except UserAlreadyParticipantError:
                    logger.debug(f"Bot {bot_name} already in Hub")
                except (ChatAdminRequiredError, UserPrivacyRestrictedError) as e:
                    logger.warning(f"⚠️ Can't invite {bot_name}: {e}")
                except UserNotMutualContactError:
                    logger.warning(f"⚠️ Can't invite {bot_name}: privacy settings prevent it")
                except FloodWaitError as e:
                    wait_time = min(e.seconds, 300)  # Cap at 5 minutes
                    logger.warning(f"⏳ FloodWait inviting {bot_name}: waiting {wait_time}s")
                    await asyncio.sleep(wait_time)
                except (ConnectionError, OSError, TimeoutError) as e:
                    logger.warning(f"⚠️ Network error inviting {bot_name}: {e}")
                except Exception as e:
                    logger.warning(f"⚠️ Unexpected error inviting {bot_name}: {e}")
                    
            except ValueError as e:
                logger.error(f"⚠️ Invalid bot token format for {bot_name}: {e}")
            except Exception as e:
                logger.warning(f"⚠️ Could not check/invite bot {bot_name}: {e}")
        
        if invited_count > 0:
            logger.info(f"✅ Invited {invited_count} bot(s) to Hub group")
            # Brief pause for Telegram to process the invites
            await asyncio.sleep(2)
        else:
            logger.info("✓ All bots already in Hub group")

    async def log_startup_status(self):
        """Logs system startup status to the Hub Group."""
        try:
            from services.collector.account_manager import bot_client_manager
            client = bot_client_manager.client
            from shared.config import get_hub_group_id, resolve_hub_group_id
            hub_id = get_hub_group_id()
            if hub_id is None and client:
                try:
                    hub_id = await resolve_hub_group_id(client)
                except Exception:
                    hub_id = settings.HUB_GROUP_ID
            
            if not hub_id:
                return

            active_accounts = len(self.clients)
            mode = settings.RUN_MODE.upper()
            workers = settings.NUM_WORKERS
            version = "1.0.0" # Could be dynamic
            
            message = (
                f"🚀 **Face Archiver System Online**\n\n"
                f"📊 **Status Report:**\n"
                f"• **Active Accounts:** `{active_accounts}`\n"
                f"• **Run Mode:** `{mode}`\n"
                f"• **Workers:** `{workers}`\n"
                f"• **System:** `Operational`\n"
                f"\n"
                f"🔍 *Monitoring started for all connected accounts.*"
            )
            
            # Send to Hub Group (General Topic by default if no thread_id specified)
            await client.send_message(hub_id, message)
            logger.info(f"Sent startup status to Hub Group {hub_id}")
            
        except Exception as e:
            logger.warning(f"Failed to send startup status: {e}")
    
    async def _send_shutdown_notification(self):
        """Sends shutdown notification to Hub."""
        try:
            from services.collector.account_manager import bot_client_manager
            client = bot_client_manager.client
            from shared.config import get_hub_group_id, resolve_hub_group_id
            hub_id = get_hub_group_id()
            if hub_id is None and client:
                try:
                    hub_id = await resolve_hub_group_id(client)
                except Exception:
                    hub_id = settings.HUB_GROUP_ID
            
            if client and hub_id:
                await client.send_message(
                    hub_id, 
                    "🛑 **System Shutting Down**\n\n_Graceful shutdown initiated..._"
                )
        except Exception as e:
            logger.warning(f"Failed to send shutdown notification: {e}")
    
    async def _auto_discover_sessions(self):
        """
        Auto-discovers and registers existing session files.
        This enables self-healing after database wipes - existing sessions
        are automatically registered without needing the Login Bot.
        """
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError, FloodWaitError
        from shared.telegram_client import TelegramClientManager
        from shared.database import get_db_connection
        
        sessions_dir = settings.SESSIONS_DIR
        if not os.path.exists(sessions_dir):
            logger.info(f"Sessions directory does not exist: {sessions_dir}")
            return
        
        # Find all .session files
        session_files = [f for f in os.listdir(sessions_dir) if f.endswith('.session')]
        
        if not session_files:
            logger.info("No session files found for auto-discovery.")
            return
        
        # Deduplicate: if multiple sessions exist for the same phone number
        # (e.g. account_123_ts1.session and account_123_ts2.session),
        # keep only the newest and delete the rest to avoid MTProto conflicts.
        if _SESSION_PATTERN is None:
            logger.error("Session pattern failed to compile; skipping deduplication.")
            return

        phone_sessions = {}  # phone -> [(mtime, filename), ...]
        for sf in session_files:
            match = _SESSION_PATTERN.match(sf)
            if match:
                phone = match.group(1)
                fpath = os.path.join(sessions_dir, sf)
                mtime = os.path.getmtime(fpath)
                phone_sessions.setdefault(phone, []).append((mtime, sf))
        
        for phone, entries in phone_sessions.items():
            if len(entries) > 1:
                entries.sort(reverse=True)  # newest first
                for _, old_file in entries[1:]:
                    old_path = os.path.join(sessions_dir, old_file)
                    try:
                        os.remove(old_path)
                        logger.warning(f"🗑️ Removed duplicate session for phone {phone}: {old_file}")
                        session_files.remove(old_file)
                    except OSError as e:
                        logger.warning(f"Failed to remove duplicate session {old_file}: {e}")
                    # Also remove journal
                    journal = old_path + '-journal'
                    if os.path.exists(journal):
                        try:
                            os.remove(journal)
                        except OSError:
                            pass
        
        logger.info(f"🔍 Auto-discovery: Found {len(session_files)} session file(s)")
        
        for session_file in session_files:
            session_name = session_file.replace('.session', '')
            session_path = os.path.join(sessions_dir, session_file)
            
            try:
                # Check if already registered in database
                async with get_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT id FROM collector.telegram_accounts WHERE session_file_path = %s",
                            (session_path,)
                        )
                        existing = await cur.fetchone()
                        
                        if existing:
                            logger.debug(f"Session {session_name} already registered (ID: {existing[0]})")
                            continue
                
                # Try to connect and validate the session
                logger.info(f"🔄 Validating session: {session_name}")
                
                # Determine if MTProto reset should be enabled
                enable_reset = settings.ENABLE_MTPROTO_RESET
                
                if settings.MTPROTO_RESET_NEW_SESSIONS_ONLY:
                    is_new = not os.path.exists(session_path) or \
                            (time.time() - os.path.getmtime(session_path) < 3600)
                    enable_reset = enable_reset and is_new
                
                manager = TelegramClientManager(session_name=session_name, enable_mtproto_reset=enable_reset)
                await manager.start()
                
                client = manager.client
                
                if not await client.is_user_authorized():
                    logger.warning(f"⚠️ Session {session_name} is not authorized (needs re-login)")
                    await manager.stop()
                    continue
                
                # Get user info for the phone number
                me = await client.get_me()
                phone = me.phone or f"unknown_{session_name}"
                
                await manager.stop()
                
                # Register in database
                async with get_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("""
                            INSERT INTO collector.telegram_accounts (phone_number, session_file_path, status)
                            VALUES (%s, %s, 'active')
                            ON CONFLICT (phone_number) DO UPDATE SET 
                                session_file_path = EXCLUDED.session_file_path,
                                status = 'active',
                                last_active = NOW()
                            RETURNING id
                        """, (phone, session_path))
                        result = await cur.fetchone()
                        account_id = result[0] if result else None
                
                logger.info(f"✅ Auto-registered session: {phone} (ID: {account_id})")
                
            except SessionPasswordNeededError:
                logger.warning(f"⚠️ Session {session_name} requires 2FA password - use Login Bot")
            except FloodWaitError as e:
                wait_time = min(e.seconds, 600)  # Cap at 10 minutes
                logger.warning(f"⏳ FloodWait validating {session_name}. Waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
                # Will retry on next discovery loop iteration
            except Exception as e:
                logger.error(f"❌ Failed to validate session {session_name}: {e}")
    
    # ── Account Scheduler Callbacks ──────────────────────────────────────
    
    async def _on_schedule_activate(self):
        """
        Called by AccountScheduler when entering the active time window.
        Reconnects all user account clients and resumes scanners.
        """
        logger.info("🟢 Schedule: Reconnecting user accounts...")
        
        from shared.database import get_db_connection
        from shared.telegram_client import TelegramClientManager
        from shared.media_downloader import MediaDownloadManager
        from message_scanner import MessageScanner, RealtimeScanner  # noqa: local runtime module
        
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, phone_number, session_file_path FROM collector.telegram_accounts WHERE status = 'active'"
                )
                rows = await cur.fetchall()
        
        reconnected = 0
        for account_id, phone, session_path in rows:
            if account_id in self.clients:
                # Already connected — just check health
                manager = self.clients[account_id]
                if not manager.is_healthy:
                    try:
                        await manager.client.connect()
                        logger.info(f"  ✓ Reconnected account {account_id} ({phone})")
                        reconnected += 1
                    except Exception as e:
                        logger.error(f"  ✗ Failed to reconnect account {account_id}: {e}")
            else:
                # New account or was fully disconnected — re-initialize
                try:
                    session_name = os.path.splitext(os.path.basename(session_path))[0]
                    manager = TelegramClientManager(session_name=session_name)
                    await manager.start()
                    self.clients[account_id] = manager
                    
                    # Re-create scanners
                    media_downloader = MediaDownloadManager(manager.client)
                    scanner = MessageScanner(
                        client=manager.client,
                        media_manager=media_downloader,
                        processing_queue=self.processing_queue
                    )
                    rt_scanner = RealtimeScanner(
                        client=manager.client,
                        media_manager=media_downloader,
                        processing_queue=self.processing_queue
                    )
                    self.scanners[account_id] = (scanner, rt_scanner)
                    
                    # Re-create story scanner
                    if get_dynamic_setting("STORY_SCAN_ENABLED", settings.STORY_SCAN_ENABLED):
                        from story_scanner import StoryScanner  # noqa: per-account local runtime adapter
                        story_scanner = StoryScanner(
                            client=manager.client,
                            processing_queue=self.processing_queue,
                            media_manager=media_downloader
                        )
                        self.story_scanners[account_id] = story_scanner
                        await story_scanner.start_polling(
                            account_id=account_id,
                            interval=settings.STORY_SCAN_INTERVAL
                        )
                    
                    logger.info(f"  ✓ Connected account {account_id} ({phone})")
                    reconnected += 1
                except Exception as e:
                    logger.error(f"  ✗ Failed to connect account {account_id}: {e}")
        
        logger.info(f"🟢 Schedule activation complete: {reconnected} account(s) connected")
        
        try:
            from shared.hub_notifier import notify
            await notify('system', f"🟢 Schedule: {reconnected} account(s) activated", priority=1)
        except Exception:
            pass

    async def _on_schedule_deactivate(self):
        """
        Called by AccountScheduler when leaving the active time window.
        Gracefully disconnects user account clients so the other project can use them.
        Bot clients remain connected for command handling.
        """
        logger.info("🔴 Schedule: Disconnecting user accounts (other project's turn)...")
        
        disconnected = 0
        
        # Stop story scanners first
        for account_id, story_scanner in list(self.story_scanners.items()):
            try:
                await story_scanner.stop()
            except Exception as e:
                logger.debug(f"Error stopping story scanner {account_id}: {e}")
        
        # Stop realtime scanners
        for account_id, (scanner, rt_scanner) in list(self.scanners.items()):
            try:
                await rt_scanner.stop()
            except Exception as e:
                logger.debug(f"Error stopping realtime scanner {account_id}: {e}")
        
        # Disconnect user account clients
        for account_id, manager in list(self.clients.items()):
            try:
                await manager.stop()
                disconnected += 1
                logger.info(f"  ✓ Disconnected account {account_id}")
            except Exception as e:
                logger.error(f"  ✗ Error disconnecting account {account_id}: {e}")
        
        # Clear client references (they'll be re-created on activation)
        self.clients.clear()
        self.scanners.clear()
        self.story_scanners.clear()
        
        logger.info(f"🔴 Schedule deactivation complete: {disconnected} account(s) disconnected")
        
        try:
            from shared.hub_notifier import notify
            await notify('system', f"🔴 Schedule: {disconnected} account(s) deactivated (other project's turn)", priority=1)
        except Exception:
            pass

    # ── Scanning ─────────────────────────────────────────────────────────
    
    async def _run_backfill_with_logging(self):
        """Wrapper around run_backfill that logs start/end and catches exceptions."""
        try:
            logger.info("📜 Background backfill starting...")
            await self.run_backfill()
            logger.info("📜 Background backfill completed successfully!")
        except Exception as e:
            logger.error(f"📜 Background backfill failed: {e}", exc_info=True)

    async def run_backfill(self):
        """Runs backfill scanning for ALL connected accounts."""
        if not self.clients:
            logger.warning("No accounts connected. Skipping backfill.")
            return

        logger.info("Starting backfill scan for all accounts...")
        
        tasks = []
        for account_id in self.clients:
            tasks.append(self._run_single_backfill(account_id))
        
        await asyncio.gather(*tasks)
        logger.info("Backfill scan complete for all accounts!")

    async def _run_single_backfill(self, account_id: int):
        """Runs backfill for a single account."""
        scanner, _ = self.scanners[account_id]
        logger.info(f"Running backfill for Account {account_id}...")
        
        # Discover all chats
        await scanner.discover_and_scan_all_chats(account_id)
        
        # Resume incomplete chats - ORDERED BY PRIORITY (personal → group → channel)
        from shared.database import get_db_connection
        from shared.config import get_hub_group_id
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                hub_group_id = get_hub_group_id()
                if hub_group_id is not None:
                    await cur.execute("""
                        SELECT chat_id, chat_type FROM collector.scan_checkpoints
                        WHERE account_id = %s AND scan_mode = 'backfill'
                          AND chat_id NOT IN (%s, %s)
                        ORDER BY
                            CASE chat_type
                                WHEN 'personal' THEN 1
                                WHEN 'group' THEN 2
                                WHEN 'channel' THEN 3
                                ELSE 4
                            END
                    """, (account_id, hub_group_id, -hub_group_id))
                else:
                    await cur.execute("""
                        SELECT chat_id, chat_type FROM collector.scan_checkpoints
                        WHERE account_id = %s AND scan_mode = 'backfill'
                        ORDER BY
                            CASE chat_type
                                WHEN 'personal' THEN 1
                                WHEN 'group' THEN 2
                                WHEN 'channel' THEN 3
                                ELSE 4
                            END
                    """, (account_id,))
                chats = await cur.fetchall()
        
        logger.info(f"Account {account_id}: Scanning {len(chats)} chats in priority order (personal → group → channel)")
        
        for chat_id, chat_type in chats:
            try:
                logger.info(f"Scanning {chat_type} chat {chat_id}...")
                await scanner.scan_chat_backfill(account_id, chat_id)
            except Exception as e:
                logger.error(f"Error scanning chat {chat_id} (Account {account_id}): {e}")
    
    async def run_realtime(self):
        """Runs real-time monitoring for ALL connected accounts."""
        if not self.clients:
            logger.warning("No accounts connected. Waiting for shutdown...")
            await self._shutdown_event.wait()
            return

        logger.info("Starting real-time monitoring for all accounts...")
        
        tasks = []
        for account_id in self.clients:
            tasks.append(self._run_single_realtime(account_id))
        
        # Also wait for shutdown event
        tasks.append(self._shutdown_event.wait())
        
        # Run until shutdown
        await asyncio.gather(*tasks)

    async def _run_single_realtime(self, account_id: int):
        """Runs realtime monitor for a single account."""
        _, rt_scanner = self.scanners[account_id]
        
        # Get chats to monitor — if backfill hasn't discovered chats yet, 
        # wait briefly and retry so realtime doesn't start with zero chats
        from shared.database import get_db_connection
        from shared.config import get_hub_group_id
        chat_ids = []
        
        for attempt in range(6):  # Try up to 6 times (30s total)
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    hub_group_id = get_hub_group_id()
                    if hub_group_id is not None:
                        await cur.execute(
                            "SELECT chat_id FROM collector.scan_checkpoints WHERE account_id = %s AND chat_id NOT IN (%s, %s)",
                            (account_id, hub_group_id, -hub_group_id)
                        )
                    else:
                        await cur.execute(
                            "SELECT chat_id FROM collector.scan_checkpoints WHERE account_id = %s",
                            (account_id,)
                        )
                    rows = await cur.fetchall()
                    chat_ids = [row[0] for row in rows]
            
            if chat_ids:
                break
            
            if attempt < 5:
                logger.info(f"Account {account_id}: No chats discovered yet, waiting for backfill to find them... (attempt {attempt + 1}/6)")
                await asyncio.sleep(5)
        
        if chat_ids:
            logger.info(f"Account {account_id}: Monitoring {len(chat_ids)} chats")
            await rt_scanner.start_monitoring(chat_ids, account_id)
        else:
            logger.warning(f"Account {account_id}: No chats found to monitor.")
        
        # Start story scanner polling (runs alongside realtime)
        if account_id in self.story_scanners:
            await self.story_scanners[account_id].start_polling(
                account_id=account_id,
                interval=settings.STORY_SCAN_INTERVAL
            )
            logger.info(f"✓ Story scanner started for account {account_id}")
    
    async def run(self, mode: str = 'both'):
        """Main run method."""
        try:
            await self.initialize()
            
            # Helper function to handle task exceptions
            def task_exception_handler(task: asyncio.Task):
                try:
                    exc = task.exception()
                    if exc:
                        logger.error(f"Background task {task.get_name()} failed: {exc}", exc_info=exc)
                except asyncio.CancelledError:
                    logger.debug(f"Background task {task.get_name()} was cancelled")
                except Exception as e:
                    logger.error(f"Error handling task exception: {e}")
            
            # Start account scheduler (for sharing accounts across projects)
            from services.collector.scheduler import AccountScheduler
            self.scheduler = AccountScheduler(
                enabled=settings.ACCOUNT_SCHEDULE_ENABLED,
                active_start=settings.ACCOUNT_ACTIVE_START,
                active_end=settings.ACCOUNT_ACTIVE_END,
                on_activate=self._on_schedule_activate,
                on_deactivate=self._on_schedule_deactivate,
            )
            await self.scheduler.start()
            
            # Start health check scheduler as background task
            health_task = asyncio.create_task(self._health_check_scheduler(), name="health_check")
            health_task.add_done_callback(task_exception_handler)

            # Start account discovery background task
            discovery_task = asyncio.create_task(self._account_discovery_loop(mode), name="account_discovery")
            discovery_task.add_done_callback(task_exception_handler)

            # Start update handler
            from services.collector.update_handler import setup_update_handler
            self.update_handler = setup_update_handler(self.shutdown)
            await self.update_handler.start()
            
            # Start topic cleanup scheduler
            cleanup_task = asyncio.create_task(self._cleanup_scheduler(), name="cleanup")
            cleanup_task.add_done_callback(task_exception_handler)
            
            # Run one-time topic label migration
            await self.topic_manager.migrate_labels_to_ids()
            
            # Start realtime monitoring FIRST (it's event-driven and non-blocking for new messages)
            # Then run backfill in the background to catch up on history
            backfill_task = None
            if mode in ('backfill', 'both'):
                # Run backfill as a background task so it doesn't block realtime
                backfill_task = asyncio.create_task(self._run_backfill_with_logging(), name="backfill")
                backfill_task.add_done_callback(task_exception_handler)
                logger.info("Backfill started as background task (realtime monitoring will start immediately)")

            if mode in ('realtime', 'both'):
                await self.run_realtime()
            elif mode == 'backfill':
                # In backfill-only mode, wait for backfill to finish
                if backfill_task:
                    await backfill_task
                
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            raise
        finally:
            await self.shutdown()
            
    async def _account_discovery_loop(self, mode: str):
        """
        Periodically checks the database for newly added Telegram accounts 
        and initializes scanners for them without requiring a restart.
        Also detects removed accounts and cleans them up.
        """
        logger.info("Account discovery loop started.")
        
        # Track known accounts to detect removals
        known_accounts = set(self.clients.keys())
        
        while not self._shutdown_event.is_set():
            try:
                # Wait 60 seconds between checks, break if shutdown
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=60.0)
                    break
                except asyncio.TimeoutError:
                    pass

                # Skip discovery when scheduler has deactivated accounts
                if self.scheduler and self.scheduler.enabled and not self.scheduler.is_active:
                    continue

                # Pre-check database for active accounts
                from shared.database import get_db_connection
                async with get_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT id, phone_number, session_file_path FROM collector.telegram_accounts WHERE status = 'active'")
                        rows = await cur.fetchall()

                # Build set of current active accounts
                current_accounts = {row[0] for row in rows}
                
                # Detect removed accounts
                removed_accounts = known_accounts - current_accounts
                for account_id in removed_accounts:
                    logger.info(f"Account {account_id} removed from database or marked inactive, cleaning up...")
                    try:
                        if account_id in self.clients:
                            await self.clients[account_id].stop()
                            del self.clients[account_id]
                        if account_id in self.scanners:
                            del self.scanners[account_id]
                        if account_id in self.story_scanners:
                            del self.story_scanners[account_id]
                        known_accounts.remove(account_id)
                        logger.info(f"✓ Account {account_id} cleaned up successfully")
                    except Exception as e:
                        logger.error(f"Error cleaning up account {account_id}: {e}")
                
                # Find accounts that are not currently in self.clients (new accounts)
                new_accounts = []
                for account_id, phone, session_path in rows:
                    if account_id not in self.clients:
                        new_accounts.append((account_id, phone, session_path))

                for account_id, phone, session_path in new_accounts:
                    if self._shutdown_event.is_set():
                        break
                        
                    logger.info(f"🔍 Discovery: Found new active account {account_id} ({phone}). Connecting...")
                    session_name = os.path.splitext(os.path.basename(session_path))[0]
                    
                    from shared.telegram_client import TelegramClientManager
                    from shared.media_downloader import MediaDownloadManager
                    from message_scanner import MessageScanner, RealtimeScanner  # noqa: local runtime module
                    
                    manager = TelegramClientManager(session_name=session_name)
                    try:
                        await manager.start()
                        self.clients[account_id] = manager
                        
                        media_downloader = MediaDownloadManager(manager.client)
                        scanner = MessageScanner(
                            client=manager.client,
                            media_manager=media_downloader,
                            processing_queue=self.processing_queue
                        )
                        rt_scanner = RealtimeScanner(
                            client=manager.client,
                            media_manager=media_downloader,
                            processing_queue=self.processing_queue
                        )
                        self.scanners[account_id] = (scanner, rt_scanner)
                        logger.info(f"✓ Account {account_id} dynamically connected and scanners ready")
                        
                        # Track this account as known
                        known_accounts.add(account_id)
                        
                        # Story Scanner for dynamically discovered account
                        if get_dynamic_setting("STORY_SCAN_ENABLED", settings.STORY_SCAN_ENABLED):
                            from story_scanner import StoryScanner  # noqa: per-account local runtime adapter
                            story_scanner = StoryScanner(
                                client=manager.client,
                                processing_queue=self.processing_queue,
                                media_manager=media_downloader
                            )
                            self.story_scanners[account_id] = story_scanner
                        
                        # Start backfill if applicable
                        if mode in ('backfill', 'both'):
                            asyncio.create_task(self._run_single_backfill(account_id))
                        
                        # Start realtime if applicable
                        if mode in ('realtime', 'both'):
                            asyncio.create_task(self._run_single_realtime(account_id))
                            
                    except Exception as e:
                        logger.error(f"Failed to connect new account {account_id} ({phone}): {e}")

            except Exception as e:
                logger.error(f"Account discovery loop error: {e}")
                await asyncio.sleep(60)
    
    async def _health_check_scheduler(self):
        """
        Periodically performs comprehensive health checks and logs to Hub's general topic.
        Runs every HEALTH_CHECK_INTERVAL seconds (default: 30 min).
        """
        interval = settings.HEALTH_CHECK_INTERVAL
        logger.info(f"Health check scheduler started (interval: {interval}s)")
        
        while not self._shutdown_event.is_set():
            try:
                # Wait for the interval or shutdown
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Time to run health check
                
                await self._run_health_checks()
                
            except Exception as e:
                logger.error(f"Health check scheduler error: {e}")
                await asyncio.sleep(60)  # Wait a bit before retrying
    
    async def _run_health_checks(self):
        """Runs all health checks and posts report to Hub."""
        from datetime import datetime
        from shared.database import get_db_connection, db_manager
        from services.collector.account_manager import bot_client_manager
        import redis
        
        checks = {}
        warnings = []
        redis_client = None  # Initialize to None
        
        # 1. Database Health
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await asyncio.wait_for(cur.execute("SELECT 1"), timeout=5.0)
            checks['Database'] = '✅ Connected'
        except Exception as e:
            checks['Database'] = f'❌ Error: {str(e)[:50]}'
            warnings.append('Database')
        
        # 2. Redis Health
        try:
            redis_client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                socket_timeout=5  # Add timeout
            )
            redis_client.ping()
            checks['Redis'] = '✅ Connected'
        except Exception as e:
            checks['Redis'] = f'❌ Error: {str(e)[:50]}'
            warnings.append('Redis')
            redis_client = None # Ensure it is None if failed
        
        # 3. Queue Health
        if self.processing_queue:
            try:
                queue_size = self.processing_queue.get_queue_size()
                queue_stats = self.processing_queue.get_stats()
                bp_state = self.processing_queue.get_backpressure_state().value
                
                if queue_size >= settings.QUEUE_MAX_SIZE * 0.8:
                    checks['Queue'] = f'⚠️ High load ({queue_size}/{settings.QUEUE_MAX_SIZE})'
                    warnings.append('Queue (High Load)')
                else:
                    checks['Queue'] = f'✅ {queue_size} items ({bp_state})'
                
                checks['Processed'] = f'📊 {queue_stats.get("total_processed", 0)} total, {queue_stats.get("faces_found", 0)} faces'
            except Exception as e:
                checks['Queue'] = f'❌ Error: {str(e)[:50]}'
                warnings.append('Queue')
        else:
            checks['Queue'] = '⏳ Not initialized'
        
        # 4. Telegram Accounts Health
        active_accounts = 0
        paused_accounts = 0
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT status, COUNT(*) FROM collector.telegram_accounts GROUP BY status")
                    rows = await cur.fetchall()
                    status_counts = {row[0]: row[1] for row in rows}
                    active_accounts = status_counts.get('active', 0)
                    paused_accounts = status_counts.get('paused', 0)
            
            if paused_accounts > 0:
                checks['Accounts'] = f'⚠️ {active_accounts} active, {paused_accounts} paused'
                warnings.append('Paused Accounts')
            else:
                checks['Accounts'] = f'✅ {active_accounts} active'
        except Exception as e:
            checks['Accounts'] = f'❌ Error: {str(e)[:50]}'
        
        # 5. Scan Progress
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT 
                            COUNT(*) as total,
                            SUM(CASE WHEN is_complete = true THEN 1 ELSE 0 END) as complete
                        FROM collector.scan_checkpoints
                    """)
                    row = await cur.fetchone()
                    total_chats = row[0] or 0
                    complete_chats = row[1] or 0
                    if total_chats > 0:
                        progress = (complete_chats / total_chats) * 100
                        checks['Scan Progress'] = f'📈 {complete_chats}/{total_chats} chats ({progress:.1f}%)'
                    else:
                        checks['Scan Progress'] = '⏳ No chats scanned yet'
        except Exception as e:
            checks['Scan Progress'] = f'❌ Error: {str(e)[:50]}'
        
        # 6. Topics/Identities
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT COUNT(*) FROM face_recognition.telegram_topics")
                    topic_count = (await cur.fetchone())[0]
                    await cur.execute("SELECT COUNT(*) FROM face_recognition.face_embeddings")
                    face_count = (await cur.fetchone())[0]
            checks['Identities'] = f'👥 {topic_count} topics, {face_count} embeddings'
        except Exception as e:
            checks['Identities'] = f'❌ Error: {str(e)[:50]}'
        
        # 7. Dead Letter Queue (failed tasks)
        try:
            if redis_client and self.processing_queue:
                dlq_size = redis_client.llen(self.processing_queue.dead_letter_key)
                if dlq_size > 100:
                    checks['Dead Letter Queue'] = f'⚠️ {dlq_size} failed tasks'
                    warnings.append('DLQ High')
                elif dlq_size > 0:
                    checks['Dead Letter Queue'] = f'📋 {dlq_size} failed tasks'
                else:
                    checks['Dead Letter Queue'] = '✅ Empty'
            else:
                 checks['Dead Letter Queue'] = '⚠️ Redis/Queue unavailable'
        except Exception as e:
            checks['Dead Letter Queue'] = f'❌ Error: {str(e)[:50]}'
        
        # 8. Memory Usage (if psutil available)
        try:
            import psutil
            process = psutil.Process()
            mem_mb = process.memory_info().rss / 1024 / 1024
            if mem_mb > 3000:  # > 3GB
                checks['Memory'] = f'⚠️ {mem_mb:.0f} MB'
                warnings.append('High Memory')
            else:
                checks['Memory'] = f'✅ {mem_mb:.0f} MB'
        except ImportError:
            pass  # psutil not available, skip memory check
        except Exception:
            pass
        
        # 9. Bot Pool Health
        try:
            from shared.bot_pool import bot_pool
            healthy = len(bot_pool.get_healthy_bots())
            total = bot_pool.bot_count
            if healthy < total:
                locked = total - healthy
                checks['Bot Pool'] = f'⚠️ {healthy}/{total} healthy ({locked} locked)'
                warnings.append('Bot Pool')
            else:
                checks['Bot Pool'] = f'✅ {healthy}/{total} bots healthy'
        except Exception as e:
            checks['Bot Pool'] = f'❌ Error: {str(e)[:50]}'
        
        # Build report
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if warnings:
            status_emoji = '⚠️'
            status_text = f'Issues: {", ".join(warnings)}'
        else:
            status_emoji = '✅'
            status_text = 'All systems operational'
        
        report = f"{status_emoji} **Health Check Report** - `{now}`\n\n"
        report += f"**Status:** {status_text}\n\n"
        
        for key, value in checks.items():
            report += f"• **{key}:** {value}\n"
        
        # Send to Hub's general topic (reply_to=None means general thread)
        try:
            client = bot_client_manager.client
            from shared.config import get_hub_group_id, resolve_hub_group_id
            hub_id = get_hub_group_id()
            if hub_id is None and client:
                try:
                    hub_id = await resolve_hub_group_id(client)
                except Exception:
                    hub_id = settings.HUB_GROUP_ID
            if client and hub_id:
                await client.send_message(hub_id, report)
                logger.info(f"Health check report sent to Hub Group")
        except Exception as e:
            logger.error(f"Failed to send health check report: {e}")

        # SELF HEALING: Check HubNotifier
        try:
            if hasattr(self, 'hub_notifier'):
                # Check all hub notifier tasks
                is_healthy = (
                    self.hub_notifier._running and
                    (not self.hub_notifier._flush_task or not self.hub_notifier._flush_task.done()) and
                    (not hasattr(self.hub_notifier, '_supervisor_task') or 
                     not self.hub_notifier._supervisor_task or 
                     not self.hub_notifier._supervisor_task.done())
                )
                
                if not is_healthy:
                    logger.warning("⚠️ Hub Notifier stopped unexpectedly. Restarting...")
                    try:
                        await self.hub_notifier.stop()  # Ensure clean
                    except Exception as e:
                        logger.warning(f"Error stopping hub notifier: {e}")
                    
                    self.hub_notifier = HubNotifier.get_instance()
                    await self.hub_notifier.start()
                    logger.info("✅ Hub Notifier self-healed")
                else:
                    logger.debug("Hub Notifier is healthy")
        except Exception as e:
            logger.error(f"Failed to self-heal HubNotifier: {e}")

    async def _cleanup_scheduler(self):
        """
        Periodically cleans up old messages in the General topic.
        Runs every CLEANUP_INTERVAL seconds.
        """
        interval = settings.CLEANUP_INTERVAL
        retention = settings.GENERAL_TOPIC_RETENTION_HOURS
        
        logger.info(f"Cleanup scheduler started (interval: {interval}s, retention: {retention}h)")
        
        # Initial wait to let system stabilize
        await asyncio.sleep(60)
        
        while not self._shutdown_event.is_set():
            try:
                # 1. Pick a user client for cleanup (bots are restricted for GetReplies)
                cleanup_client = None
                if self.clients:
                    # Get the first available healthy user client
                    for manager in self.clients.values():
                        if manager.client and await manager.client.is_user_authorized():
                            cleanup_client = manager.client
                            break
                
                if not cleanup_client:
                    logger.warning("No authorized user client available for topic cleanup. Falling back to bot (may fail).")
                
                # 2. Cleanup General Topic (Thread ID 1)
                await self.topic_manager.cleanup_topic(
                    telegram_topic_id=1, 
                    retention_hours=retention,
                    client=cleanup_client
                )
                
                # 3. Topic Repair (Self-healing)
                await self.topic_manager.ensure_all_topics_exist(client=cleanup_client)
                
                # Wait for the interval or shutdown
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Time to run again
                
            except Exception as e:
                logger.error(f"Cleanup scheduler error: {e}")
                await asyncio.sleep(60)

    async def _recover_telegram(self):
        """Attempts to recover Telegram connections."""
        logger.warning("Recovery: Checking Telegram clients...")
        for account_id, manager in self.clients.items():
            if not manager.client.is_connected():
                logger.info(f"Reconnecting account {account_id}...")
                await manager.start()
    
    async def _recover_hub_access(self):
        """Attempts to recover Hub access using bot pool rotation."""
        logger.warning("Recovery: Checking Bot pool...")
        from services.collector.account_manager import bot_client_manager
        try:
            # This will automatically pick a healthy bot from the pool
            client = bot_client_manager.client
            if not client or not client.is_connected():
                logger.info("Reconnecting bot pool...")
                await bot_client_manager.start()
        except RuntimeError as e:
            logger.error(f"No healthy bots available: {e}")
            # Try to restart the bot pool
            await bot_client_manager.start()
    
    async def shutdown(self):
        """Gracefully shuts down all components and cleans up tasks."""
        self._running = False
        logger.info("Graceful shutdown initiated...")
        
        # 0. Stop clock monitor
        try:
            await stop_clock_monitoring()
        except Exception as e:
            logger.debug(f"Error stopping clock monitor: {e}")
        
        # 0. Stop account scheduler
        if self.scheduler:
            try:
                await self.scheduler.stop()
            except Exception as e:
                logger.debug(f"Error stopping scheduler: {e}")
        
        # 1. Send shutdown notification (before services stop)
        await self._send_shutdown_notification()
        
        # 2. Stop Scanners (to stop receiving new events)
        for account_id, (scanner, rt_scanner) in self.scanners.items():
            try:
                await rt_scanner.stop()
                logger.info(f"Stopped realtime scanner for account {account_id}")
            except Exception as e:
                logger.debug(f"Error stopping scanner: {e}")
        
        # 2b. Stop Story Scanners
        for account_id, story_scanner in self.story_scanners.items():
            try:
                await story_scanner.stop()
                logger.info(f"Stopped story scanner for account {account_id}")
            except Exception as e:
                logger.debug(f"Error stopping story scanner: {e}")

        # 3. Stop hub notifier (flushes pending events)
        if hasattr(self, 'hub_notifier'):
            try:
                await self.hub_notifier.stop()
            except Exception as e:
                logger.debug(f"Error stopping hub notifier: {e}")
        
        # 4. Stop health checker
        if hasattr(self, 'health_checker'):
            try:
                await self.health_checker.stop()
            except Exception as e:
                logger.debug(f"Error stopping health checker: {e}")

        # 5. Stop update handler
        if hasattr(self, 'update_handler'):
            try:
                await self.update_handler.stop()
            except Exception as e:
                logger.debug(f"Error stopping update handler: {e}")

        # 6. Stop processing queue
        if self.processing_queue:
            try:
                await self.processing_queue.stop(drain_timeout=settings.SIGTERM_DRAIN_TIMEOUT)
                logger.info("Stopped processing queue")
            except Exception as e:
                logger.debug(f"Error stopping queue: {e}")
        
        # 7. Disconnect all Telegram clients
        for account_id, manager in self.clients.items():
            try:
                await manager.stop()
                logger.info(f"Disconnected account {account_id}")
            except Exception as e:
                logger.debug(f"Error disconnecting account {account_id}: {e}")
        
        # 8. Disconnect Bot Client
        try:
            from services.collector.account_manager import bot_client_manager
            if bot_client_manager.is_ready():
                await bot_client_manager.disconnect()
                logger.info("Disconnected bot client")
        except Exception as e:
            logger.debug(f"Failed to disconnect bot client: {e}")

        # 9. Close asyncpg pool (face recognition identity matcher)
        if hasattr(self, '_face_db_pool') and self._face_db_pool:
            try:
                await self._face_db_pool.close()
                logger.info("Closed face recognition DB pool")
            except Exception as e:
                logger.debug(f"Error closing face DB pool: {e}")

        # 10. Close main Database pool
        try:
            from shared.database import db_manager
            await db_manager.close()
            logger.info("Closed database connections")
        except Exception as e:
            logger.debug(f"Error closing database: {e}")
        
        # 10. Final Task Cleanup (The most critical part for "Event loop is closed" errors)
        await self._cancel_all_tasks()
        
        logger.info("Shutdown complete")

    async def _cancel_all_tasks(self):
        """Cancels all remaining background tasks on the current loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        current_task = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks(loop) if t is not current_task]
        
        if not tasks:
            return

        logger.info(f"Cancelling {len(tasks)} remaining background tasks...")
        for task in tasks:
            task.cancel()

        # Wait for all tasks to acknowledge cancellation (with timeout)
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for tasks to cancel")
        except Exception as e:
            logger.debug(f"Error during final task cleanup: {e}")

def main():
    """Entry point with crash recovery and auto-restart."""
    MAX_RESTART_ATTEMPTS = 5
    BASE_RESTART_DELAY = 10  # seconds
    restart_count = 0
    
    while restart_count < MAX_RESTART_ATTEMPTS:
        worker = MainWorker()
        
        # Handle signals for graceful shutdown
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        def signal_handler():
            logger.info("Received shutdown signal")
            worker._shutdown_event.set()
            asyncio.create_task(worker.shutdown())
        
        # Register signal handlers where supported
        if os.name != 'nt':  # Unix: SIGTERM + SIGINT via asyncio
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, signal_handler)
        else:  # Windows: use signal.signal (add_signal_handler not supported)
            def _win_signal_handler(signum, frame):
                loop.call_soon_threadsafe(signal_handler)
            signal.signal(signal.SIGINT, _win_signal_handler)
            signal.signal(signal.SIGBREAK, _win_signal_handler)  # Ctrl+Break
        
        try:
            mode = settings.RUN_MODE  # 'backfill', 'realtime', or 'both'
            
            # Reset HubNotifier singleton to ensure fresh event loop connection
            try:
                from shared.hub_notifier import HubNotifier
                HubNotifier.reset_instance()
            except Exception as e:
                logger.warning(f"Failed to reset HubNotifier: {e}")
                
            logger.info(f"Starting worker (attempt {restart_count + 1}/{MAX_RESTART_ATTEMPTS})")
            loop.run_until_complete(worker.run(mode))
            
            # If we get here normally (not exception), don't restart
            logger.info("Worker completed normally")
            break
            
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
            loop.run_until_complete(worker.shutdown())
            break  # Don't restart on user interrupt
            
        except asyncio.CancelledError:
            logger.info("Worker task cancelled (likely during shutdown). Exiting cleanly.")
            try:
                loop.run_until_complete(worker.shutdown())
            except Exception:
                pass
            break
            
        except Exception as e:
            restart_count += 1
            delay = BASE_RESTART_DELAY * (2 ** (restart_count - 1))  # Exponential backoff
            
            logger.error(f"Worker crashed (attempt {restart_count}/{MAX_RESTART_ATTEMPTS}): {e}")
            
            # Log error to database (async in sync context)
            try:
                from shared.database import log_processing_error
                loop.run_until_complete(log_processing_error(
                    error_type='WorkerCrash',
                    error_message=str(e),
                    error_context={'restart_count': restart_count, 'mode': settings.RUN_MODE}
                ))
            except Exception as log_err:
                logger.warning(f"Failed to log crash to DB: {log_err}")
            
            # Cleanup
            try:
                loop.run_until_complete(worker.shutdown())
            except Exception:
                pass
            
            if restart_count < MAX_RESTART_ATTEMPTS:
                logger.info(f"Restarting in {delay} seconds...")
                import time
                time.sleep(delay)
            else:
                logger.error("Max restart attempts reached. Exiting.")
                
        finally:
            try:
                # 1. Ensure worker is shut down if it hasn't been
                if 'worker' in locals() and worker._running:
                    loop.run_until_complete(worker.shutdown())
                
                # 2. Reset singletons to prevent stale loop references
                from shared.hub_notifier import HubNotifier
                from services.face_recognition.processor import FaceProcessor
                from services.collector.account_manager import bot_client_manager, BotClientManager
                from shared.bot_pool import BotPool
                
                HubNotifier.reset_instance()
                FaceProcessor.reset_instance()
                BotClientManager.reset_instance()
                BotPool.reset_instance()
                
                # 3. Final loop drain to let library tasks (like Telethon loops) finish
                tasks = asyncio.all_tasks(loop)
                if tasks:
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                
                loop.close()
            except Exception as e:
                logger.debug(f"Final cleanup error: {e}")
    
    logger.info("Worker process exiting")


if __name__ == '__main__':
    main()

