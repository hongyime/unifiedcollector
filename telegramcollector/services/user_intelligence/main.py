"""
UserIntelligenceService: cursor-based consumer of collector.user_sightings.

Wires ChangeTracker, MembershipTracker, and NetworkBuilder together in a
polling loop that advances a persistent cursor stored in
collector.service_cursors.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


class UserIntelligenceService:
    def __init__(
        self,
        db_pool: asyncpg.Pool,
        change_tracker,
        membership_tracker,
        network_builder,
    ) -> None:
        """
        Wires all components together. Does not start the loop.

        db_pool:            asyncpg connection pool (user_intel_user credentials)
        change_tracker:     ChangeTracker instance
        membership_tracker: MembershipTracker instance
        network_builder:    NetworkBuilder instance
        """
        self._pool = db_pool
        self._change_tracker = change_tracker
        self._membership_tracker = membership_tracker
        self._network_builder = network_builder
        self._running: bool = False
        self._cursor: Optional[int] = None

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    async def _init_cursor(self) -> int:
        """
        Reads last_message_id from collector.service_cursors
        WHERE service_name = 'user_intelligence'.
        If no row exists, INSERTs one with last_message_id = 0 (ON CONFLICT DO NOTHING)
        and returns 0.
        """
        await self._pool.execute(
            """
            INSERT INTO collector.service_cursors (service_name, last_message_id, updated_at)
            VALUES ('user_intelligence', 0, NOW())
            ON CONFLICT (service_name) DO NOTHING;
            """
        )
        row = await self._pool.fetchrow(
            "SELECT last_message_id FROM collector.service_cursors "
            "WHERE service_name = 'user_intelligence';"
        )
        return int(row["last_message_id"])

    async def _advance_cursor(self, new_value: int) -> None:
        """
        UPSERTs collector.service_cursors with the new cursor value.
        new_value must be >= the current cursor value (monotonicity invariant).
        """
        await self._pool.execute(
            """
            INSERT INTO collector.service_cursors (service_name, last_message_id, updated_at)
            VALUES ('user_intelligence', $1, NOW())
            ON CONFLICT (service_name)
            DO UPDATE SET last_message_id = EXCLUDED.last_message_id,
                          updated_at      = EXCLUDED.updated_at;
            """,
            new_value,
        )

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    async def _process_batch(self, sightings: list) -> None:
        """
        Processes one batch of user_sightings rows end-to-end in ascending id order.

        Per-sighting errors are caught and logged without aborting the batch.
        """
        from shared.config import get_dynamic_setting, settings

        network_enabled = bool(
            get_dynamic_setting("USER_INTEL_NETWORK_ENABLED", settings.USER_INTEL_NETWORK_ENABLED)
        )

        for sighting in sorted(sightings, key=lambda s: s["id"]):
            try:
                await self._change_tracker.process_sighting(sighting)
                is_new = await self._membership_tracker.process_sighting(sighting)
                if is_new and network_enabled:
                    await self._network_builder.process_new_membership(
                        sighting["user_id"], sighting["seen_in_chat_id"]
                    )
            except Exception as e:
                logger.error(
                    f"Error processing sighting {sighting['id']}: {e}", exc_info=True
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Enters the cursor loop. Blocks until stop() is called or SIGTERM/SIGINT received.

        Registers signal handlers that set self._running = False and wait for the
        current batch to complete before returning.
        """
        from shared.config import get_dynamic_setting, settings

        self._running = True

        # Register graceful shutdown on SIGTERM / SIGINT
        loop = asyncio.get_running_loop()

        def _handle_signal(sig):
            logger.info(f"Received signal {sig.name}, shutting down gracefully…")
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal, sig)
            except (NotImplementedError, RuntimeError):
                # Windows does not support add_signal_handler for all signals
                signal.signal(sig, lambda s, f: self.stop())

        logger.info("UserIntelligenceService started.")

        cursor: Optional[int] = None  # lazily initialised on first enabled iteration

        while self._running:
            processing_enabled = bool(
                get_dynamic_setting("USER_INTEL_PROCESSING_ENABLED", settings.USER_INTEL_PROCESSING_ENABLED)
            )
            if not processing_enabled:
                await asyncio.sleep(5)
                continue

            # Initialise cursor once (first enabled iteration)
            if cursor is None:
                cursor = await self._init_cursor()
                logger.info(f"Cursor initialised at {cursor}.")

            batch = await self._pool.fetch(
                "SELECT id, user_id, seen_in_chat_id, seen_at, payload "
                "FROM collector.user_sightings "
                "WHERE id > $1 ORDER BY id ASC LIMIT $2",
                cursor,
                settings.USER_INTEL_BATCH_SIZE,
            )

            if not batch:
                await asyncio.sleep(settings.USER_INTEL_POLL_INTERVAL)
                continue

            await self._process_batch(batch)
            cursor = max(s["id"] for s in batch)
            await self._advance_cursor(cursor)

        logger.info("UserIntelligenceService stopped.")

    def stop(self) -> None:
        """
        Signals the loop to stop after the current batch completes. Idempotent.
        """
        self._running = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from shared.config import settings

    from services.user_intelligence.change_tracker import ChangeTracker
    from services.user_intelligence.membership_tracker import MembershipTracker
    from services.user_intelligence.network_builder import NetworkBuilder

    async def _main() -> None:
        dsn = (
            f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}"
            f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
        )

        pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
        try:
            change_tracker = ChangeTracker(pool)
            membership_tracker = MembershipTracker(pool)
            network_builder = NetworkBuilder(pool)
            service = UserIntelligenceService(
                db_pool=pool,
                change_tracker=change_tracker,
                membership_tracker=membership_tracker,
                network_builder=network_builder,
            )
            await service.start()
        finally:
            await pool.close()

    asyncio.run(_main())
