"""
LinkDiscoveryService — cursor-based consumer of collector.raw_messages.

Extracts Telegram links from message text, optionally resolves metadata via
the Telegram API, applies configurable queue rules, and auto-queues matching
links into collector.group_join_queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

import asyncpg

logger = logging.getLogger(__name__)


class LinkDiscoveryService:
    def __init__(self, db_pool, extractor, resolver, queue_rules) -> None:
        """
        Wires all components together. Does not start the loop.

        db_pool:     asyncpg connection pool (link_disc_user credentials).
        extractor:   Extractor instance (stateless, no DB access).
        resolver:    Resolver instance (Telegram API, rate-limited).
        queue_rules: QueueRules instance (reads link_discovery.queue_rules).
        """
        self._pool = db_pool
        self._extractor = extractor
        self._resolver = resolver
        self._queue_rules = queue_rules
        self._running: bool = False
        self._cursor: int | None = None

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    async def _init_cursor(self) -> int:
        await self._pool.execute(
            """
            INSERT INTO collector.service_cursors (service_name, last_message_id, updated_at)
            VALUES ('link_discovery', 0, NOW())
            ON CONFLICT (service_name) DO NOTHING;
            """
        )
        row = await self._pool.fetchrow(
            "SELECT last_message_id FROM collector.service_cursors "
            "WHERE service_name = 'link_discovery';"
        )
        self._cursor = int(row["last_message_id"])
        return self._cursor

    async def _advance_cursor(self, new_value: int) -> None:
        await self._pool.execute(
            """
            INSERT INTO collector.service_cursors (service_name, last_message_id, updated_at)
            VALUES ('link_discovery', $1, NOW())
            ON CONFLICT (service_name)
            DO UPDATE SET last_message_id = EXCLUDED.last_message_id,
                          updated_at      = EXCLUDED.updated_at;
            """,
            new_value,
        )
        self._cursor = new_value

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    async def _process_batch(self, messages: list[dict]) -> None:
        from shared.config import settings

        for message in messages:
            try:
                payload = message.get('payload') or {}
                if isinstance(payload, str):
                    payload = json.loads(payload)
                text = payload.get('message', '') or ''
                links = self._extractor.extract_links(text)

                for extracted in links:
                    if extracted.is_bot_link:
                        continue  # skip bot links

                    extracted.raw_message_id = message['id']

                    # INSERT with ON CONFLICT DO NOTHING, detect new row via RETURNING id
                    row = await self._pool.fetchrow(
                        """
                        INSERT INTO link_discovery.discovered_links
                            (raw_message_id, link, link_type, is_bot_link, status, discovered_at)
                        VALUES ($1, $2, $3, $4, 'new', NOW())
                        ON CONFLICT (link) DO NOTHING
                        RETURNING id;
                        """,
                        extracted.raw_message_id,
                        extracted.link,
                        extracted.link_type,
                        extracted.is_bot_link,
                    )

                    newly_inserted = row is not None

                    if newly_inserted and settings.LINK_DISCOVERY_RESOLVE_METADATA:
                        metadata = await self._resolver.resolve(extracted.link)
                        if metadata is not None:
                            # Detect language from chat_title
                            language = None
                            if metadata.chat_title:
                                try:
                                    from langdetect import detect, DetectorFactory
                                    DetectorFactory.seed = 0
                                    language = detect(metadata.chat_title)
                                except Exception:
                                    language = None

                            await self._pool.execute(
                                """
                                UPDATE link_discovery.discovered_links
                                   SET chat_title   = $1,
                                       member_count = $2,
                                       link_type    = $3,
                                       is_bot_link  = $4,
                                       language     = $5
                                 WHERE link = $6;
                                """,
                                metadata.chat_title,
                                metadata.member_count,
                                metadata.link_type,
                                metadata.is_bot,
                                language,
                                extracted.link,
                            )

                            if metadata.is_bot:
                                continue  # skip queue rules for bots

                            # Build link_row for queue rules evaluation
                            link_row = {
                                'link': extracted.link,
                                'link_type': metadata.link_type,
                                'chat_title': metadata.chat_title,
                                'language': language,
                                'member_count': metadata.member_count,
                                'is_bot_link': metadata.is_bot,
                            }
                        else:
                            # Metadata resolution failed, use what we have
                            link_row = {
                                'link': extracted.link,
                                'link_type': extracted.link_type,
                                'chat_title': None,
                                'language': None,
                                'member_count': None,
                                'is_bot_link': False,
                            }
                    elif newly_inserted:
                        link_row = {
                            'link': extracted.link,
                            'link_type': extracted.link_type,
                            'chat_title': None,
                            'language': None,
                            'member_count': None,
                            'is_bot_link': False,
                        }
                    else:
                        continue  # duplicate, skip

                    # Evaluate queue rules
                    decision = await self._queue_rules.evaluate(link_row)
                    if decision.should_queue:
                        try:
                            await self._pool.execute(
                                """
                                INSERT INTO collector.group_join_queue
                                    (link, account_id, status, source, language_filter, added_at)
                                VALUES ($1, NULL, 'pending', 'link_discovery', TRUE, NOW());
                                """,
                                extracted.link,
                            )
                            await self._pool.execute(
                                "UPDATE link_discovery.discovered_links SET status = 'queued' WHERE link = $1;",
                                extracted.link,
                            )
                        except Exception as e:
                            logger.error(
                                f"Error queuing link {extracted.link}: {e}", exc_info=True
                            )

            except Exception as e:
                logger.error(
                    f"Error processing message {message.get('id')}: {e}", exc_info=True
                )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        from shared.config import get_dynamic_setting, settings

        self._running = True
        loop = asyncio.get_running_loop()

        def _handle_signal(sig):
            logger.info(f"Received signal {sig.name}, shutting down gracefully…")
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal, sig)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda s, f: self.stop())

        logger.info("LinkDiscoveryService started.")
        cursor_initialised = False

        while self._running:
            processing_enabled = bool(
                get_dynamic_setting(
                    "LINK_DISCOVERY_PROCESSING_ENABLED",
                    settings.LINK_DISCOVERY_PROCESSING_ENABLED,
                )
            )
            if not processing_enabled:
                await asyncio.sleep(5)
                continue

            if not cursor_initialised:
                await self._init_cursor()
                cursor_initialised = True
                logger.info(f"Cursor initialised at {self._cursor}.")

            batch = await self._pool.fetch(
                "SELECT id, chat_id, message_id, payload "
                "FROM collector.raw_messages "
                "WHERE message_type IN ('text', 'service') AND id > $1 "
                "ORDER BY id ASC LIMIT $2",
                self._cursor,
                settings.LINK_DISCOVERY_BATCH_SIZE,
            )

            if not batch:
                await asyncio.sleep(settings.LINK_DISCOVERY_POLL_INTERVAL)
                continue

            await self._process_batch(batch)
            new_cursor = max(msg['id'] for msg in batch)
            await self._advance_cursor(new_cursor)

        logger.info("LinkDiscoveryService stopped.")

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from shared.config import settings
    from services.link_discovery.extractor import Extractor
    from services.link_discovery.resolver import Resolver
    from services.link_discovery.queue_rules import QueueRules

    async def _main() -> None:
        dsn = (
            f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}"
            f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
        )
        pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)

        try:
            extractor = Extractor()
            resolver = Resolver(
                db_pool=pool,
                tg_api_id=settings.TG_API_ID,
                tg_api_hash=settings.TG_API_HASH,
                rate_limit_per_minute=settings.LINK_DISCOVERY_RESOLVE_RATE_LIMIT,
            )
            queue_rules = QueueRules(db_pool=pool)
            service = LinkDiscoveryService(
                db_pool=pool,
                extractor=extractor,
                resolver=resolver,
                queue_rules=queue_rules,
            )
            await service.start()
        finally:
            await pool.close()

    asyncio.run(_main())
