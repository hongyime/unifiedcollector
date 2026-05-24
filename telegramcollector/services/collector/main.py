"""Collector entrypoint — wires all components together and manages lifecycle.

Startup sequence:
1. _init_db_with_retry(60)
2. _load_accounts()
3. TelegramClientManager per account → start()
4. RateLimiter(rate=30)
5. Redis client + DLQProcessor
6. MediaStore → start()
7. RealtimeWorker → start()
8. GroupManager → start()
9. AccountScheduler → start()
10. ClockDriftMonitor → start()
11. UpdateHandler → start()
12. _register_signals()

Shutdown (reverse order):
UpdateHandler → ClockDriftMonitor → AccountScheduler → GroupManager →
RealtimeWorker → MediaStore → disconnect all clients → set _shutdown_event

Constraints:
- MUST NOT import from face_processor, identity_matcher, processing_queue
- All DB writes use collector.* schema tables
- Uses existing database.py DatabaseManager and get_db_connection
- Uses existing telegram_client.py TelegramClientManager without modification
"""

import asyncio
import logging
import signal
import sys
import time

import redis as sync_redis
import redis.asyncio as aioredis

from shared.database import db_manager, get_db_connection
from shared.config import settings
from shared.dlq import DLQProcessor
from shared.hub_notifier import HubNotifier
from shared.telegram_client import TelegramClientManager

from services.collector.account_manager import bot_client_manager
from services.collector.admin_log_poller import AdminLogPoller
from services.collector.backfill_worker import BackfillWorker
from services.collector.clock_monitor import ClockDriftMonitor
from services.collector.group_manager import GroupManager
from services.collector.media_store import MediaStore
from services.collector.rate_limiter import RateLimiter
from services.collector.realtime_worker import RealtimeWorker
from services.collector.scheduler import AccountScheduler
from services.collector.story_scanner import StoryScanner
from services.collector.update_handler import UpdateHandler

logger = logging.getLogger(__name__)

class CollectorMain:
    def __init__(self) -> None:
        self.clients: list[TelegramClientManager] = []
        self.realtime_worker: RealtimeWorker | None = None
        self.media_store: MediaStore | None = None
        self.rate_limiter: RateLimiter | None = None
        self.group_manager: GroupManager | None = None
        self.scheduler: AccountScheduler | None = None
        self.clock_monitor: ClockDriftMonitor | None = None
        self.update_handler: UpdateHandler | None = None
        # Phase 4 components
        self.backfill_worker: BackfillWorker | None = None
        self.admin_log_poller: AdminLogPoller | None = None
        self.story_scanner: StoryScanner | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize all components in dependency order."""
        # 1. Database with retry
        await self._init_db_with_retry(60)

        # 2. Load active accounts
        accounts = await self._load_accounts()
        logger.info(f"Loaded {len(accounts)} active account(s)")

        # 3. One TelegramClientManager per account
        for account in accounts:
            phone = account["phone_number"]
            # Derive session name from the stored file path (stem only, no '+').
            # login_bot writes: /data/sessions/collector/<digits>.session
            # TelegramClientManager resolves relative to /app/sessions/
            import os as _os
            session_file = account.get("session_file_path", "")
            if session_file:
                session_name = _os.path.splitext(_os.path.basename(session_file))[0]
            else:
                session_name = phone.lstrip("+")
            manager = TelegramClientManager(session_name=session_name)
            manager.account_id = account["id"]
            try:
                await manager.start()
                self.clients.append(manager)
                logger.info(f"Started client for account {phone} (session={session_name})")
            except Exception as exc:
                logger.error(f"Failed to start client for {phone}: {exc}")

        # 4. Bot client pool + HubNotifier (must start before any hub sends)
        await bot_client_manager.start()
        bot_client_manager.register_worker(self)
        await HubNotifier.get_instance().start()

        # 5. RateLimiter — single shared instance
        self.rate_limiter = RateLimiter(rate=30)

        # 6. Redis clients
        password_part = (
            f":{settings.REDIS_PASSWORD}@" if settings.REDIS_PASSWORD else ""
        )
        redis_url = (
            f"redis://{password_part}{settings.REDIS_HOST}"
            f":{settings.REDIS_PORT}/{settings.REDIS_DB}"
        )
        redis_client = aioredis.from_url(redis_url, decode_responses=False)

        sync_redis_client = sync_redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD,
            decode_responses=True,
        )
        dlq_processor = DLQProcessor(redis_client=sync_redis_client)

        # 6. MediaStore
        self.media_store = MediaStore(
            redis_client=redis_client,
            tg_clients=self.clients,
            dlq_processor=dlq_processor,
            base_path=settings.MEDIA_STORE_PATH,
            max_size_mb=settings.COLLECTOR_MAX_MEDIA_SIZE_MB,
            num_workers=settings.COLLECTOR_MEDIA_WORKER_COUNT,
        )
        await self.media_store.start()

        # 7. RealtimeWorker
        self.realtime_worker = RealtimeWorker(
            clients=self.clients,
            rate_limiter=self.rate_limiter,
        )
        await self.realtime_worker.start()

        # 8. GroupManager
        self.group_manager = GroupManager(
            rate_limiter=self.rate_limiter,
            clients=self.clients,
        )
        await self.group_manager.start()

        # 9. AccountScheduler
        self.scheduler = AccountScheduler(
            enabled=settings.ACCOUNT_SCHEDULE_ENABLED,
            active_start=settings.ACCOUNT_ACTIVE_START,
            active_end=settings.ACCOUNT_ACTIVE_END,
        )
        await self.scheduler.start()

        # 10. ClockDriftMonitor
        self.clock_monitor = ClockDriftMonitor()
        await self.clock_monitor.start()

        # 11. UpdateHandler
        self.update_handler = UpdateHandler(shutdown_callback=self.shutdown)
        await self.update_handler.start()

        # 12. Signal handlers
        self._register_signals()

        # 13. Phase 4: BackfillWorker (conditional)
        if settings.COLLECTOR_BACKFILL_ENABLED:
            self.backfill_worker = BackfillWorker(
                clients=self.clients,
                rate_limiter=self.rate_limiter,
                media_store=self.media_store,
                redis_client=redis_client,
            )
            await self.backfill_worker.start()
            logger.info("BackfillWorker started")

        # 14. Phase 4: AdminLogPoller (always)
        self.admin_log_poller = AdminLogPoller(
            clients=self.clients,
            rate_limiter=self.rate_limiter,
        )
        await self.admin_log_poller.start()
        logger.info("AdminLogPoller started")

        # 15. Phase 4: StoryScanner (always)
        self.story_scanner = StoryScanner(
            clients=self.clients,
            rate_limiter=self.rate_limiter,
            media_store=self.media_store,
            redis_client=redis_client,
        )
        await self.story_scanner.start()
        logger.info("StoryScanner started")

        logger.info("CollectorMain initialized — all components running")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Wait until shutdown is signalled, running account discovery in background."""
        import asyncio as _asyncio
        discovery = _asyncio.create_task(self._account_discovery_loop())
        await self._shutdown_event.wait()
        discovery.cancel()
        try:
            await discovery
        except _asyncio.CancelledError:
            pass

    async def _account_discovery_loop(self) -> None:
        """Poll DB every 30s for new/removed accounts and hot-wire them in."""
        import asyncio as _asyncio
        import os as _os
        known_ids: set[int] = {id(m) for m in self.clients}
        known_account_ids: set[int] = set()

        # Seed with already-loaded accounts
        try:
            accounts = await self._load_accounts()
            known_account_ids = {a["id"] for a in accounts}
        except Exception:
            pass

        while not self._shutdown_event.is_set():
            try:
                await _asyncio.sleep(30)
                if self._shutdown_event.is_set():
                    break

                accounts = await self._load_accounts()
                current_ids = {a["id"] for a in accounts}

                # Connect new accounts
                new = [a for a in accounts if a["id"] not in known_account_ids]
                for account in new:
                    phone = account["phone_number"]
                    session_file = account.get("session_file_path", "")
                    session_name = (
                        _os.path.splitext(_os.path.basename(session_file))[0]
                        if session_file else phone.lstrip("+")
                    )
                    manager = TelegramClientManager(session_name=session_name)
                    manager.account_id = account["id"]
                    try:
                        await manager.start()
                        self.clients.append(manager)
                        if self.realtime_worker:
                            self.realtime_worker._register_handlers(manager)
                        known_account_ids.add(account["id"])
                        logger.info(f"Hot-added account {phone} (session={session_name})")
                    except Exception as exc:
                        logger.error(f"Failed to hot-add account {phone}: {exc}")

                # Disconnect removed/paused accounts
                removed_ids = known_account_ids - current_ids
                if removed_ids:
                    known_account_ids = current_ids
                    # Clients list is pruned on next restart; just log for now
                    logger.info(f"Account(s) removed from DB: {removed_ids}")

            except Exception as exc:
                logger.error(f"Account discovery loop error: {exc}")

    async def _run_main(self) -> None:
        """Top-level coroutine: initialize then run."""
        await self.initialize()
        await self.run()

    # ------------------------------------------------------------------
    # Shutdown (reverse start order)
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop all components in reverse start order, then signal completion."""
        if self._shutdown_event.is_set():
            return  # Already shutting down
        logger.info("CollectorMain: initiating graceful shutdown...")

        # Phase 4 shutdown (reverse start order: StoryScanner → AdminLogPoller → BackfillWorker)
        if self.story_scanner is not None:
            try:
                await self.story_scanner.stop()
            except Exception as exc:
                logger.error(f"Error stopping StoryScanner: {exc}")

        if self.admin_log_poller is not None:
            try:
                await self.admin_log_poller.stop()
            except Exception as exc:
                logger.error(f"Error stopping AdminLogPoller: {exc}")

        if self.backfill_worker is not None:
            try:
                await self.backfill_worker.stop()
            except Exception as exc:
                logger.error(f"Error stopping BackfillWorker: {exc}")

        # 1. UpdateHandler
        if self.update_handler is not None:
            try:
                await self.update_handler.stop()
            except Exception as exc:
                logger.error(f"Error stopping UpdateHandler: {exc}")

        # 2. ClockDriftMonitor
        if self.clock_monitor is not None:
            try:
                await self.clock_monitor.stop()
            except Exception as exc:
                logger.error(f"Error stopping ClockDriftMonitor: {exc}")

        # 3. AccountScheduler
        if self.scheduler is not None:
            try:
                await self.scheduler.stop()
            except Exception as exc:
                logger.error(f"Error stopping AccountScheduler: {exc}")

        # 4. GroupManager
        if self.group_manager is not None:
            try:
                await self.group_manager.stop()
            except Exception as exc:
                logger.error(f"Error stopping GroupManager: {exc}")

        # 5. RealtimeWorker
        if self.realtime_worker is not None:
            try:
                await self.realtime_worker.stop()
            except Exception as exc:
                logger.error(f"Error stopping RealtimeWorker: {exc}")

        # 6. MediaStore
        if self.media_store is not None:
            try:
                await self.media_store.stop()
            except Exception as exc:
                logger.error(f"Error stopping MediaStore: {exc}")

        # 7. Disconnect all Telegram clients
        for manager in self.clients:
            try:
                await manager.stop()
            except Exception as exc:
                logger.error(f"Error disconnecting client {manager.session_name}: {exc}")

        # 8. HubNotifier + bot client pool
        try:
            await HubNotifier.get_instance().stop()
        except Exception as exc:
            logger.error(f"Error stopping HubNotifier: {exc}")
        try:
            await bot_client_manager.disconnect()
        except Exception as exc:
            logger.error(f"Error disconnecting bot client manager: {exc}")

        # Signal completion
        self._shutdown_event.set()
        logger.info("CollectorMain: shutdown complete")

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _register_signals(self) -> None:
        """Register SIGTERM and SIGINT to trigger graceful shutdown."""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(self.shutdown()),
            )
        logger.debug("Signal handlers registered (SIGTERM, SIGINT)")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _init_db_with_retry(self, timeout_seconds: int = 60) -> None:
        """Retry DatabaseManager.initialize() with exponential backoff.

        Delays: 1s, 2s, 4s, 8s, 16s — exits with code 1 if 60s elapsed.
        """
        delays = [1, 2, 4, 8, 16]
        start = time.monotonic()
        for delay in delays:
            try:
                await db_manager.initialize()
                logger.info("Database initialized successfully")
                return
            except Exception as exc:
                if time.monotonic() - start >= timeout_seconds:
                    logger.error("DB init failed after timeout — exiting")
                    sys.exit(1)
                logger.warning(f"DB init failed, retrying in {delay}s: {exc}")
                await asyncio.sleep(delay)
        # All delays exhausted
        sys.exit(1)

    async def _load_accounts(self) -> list[dict]:
        """Return active accounts from collector.telegram_accounts."""
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, phone_number, display_name, status, session_file_path
                    FROM collector.telegram_accounts
                    WHERE status = 'active'
                    """
                )
                cols = [desc[0] for desc in cur.description]
                rows = await cur.fetchall()
                return [dict(zip(cols, row)) for row in rows]


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    collector = CollectorMain()
    asyncio.run(collector._run_main())


if __name__ == "__main__":
    main()
