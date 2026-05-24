"""
FaceRecognitionService — services/face_recognition/main.py

Cursor-based consumer that reads media messages from collector.raw_messages,
detects faces, matches identities, and publishes to the Telegram Hub Group.

Requirements: 1.1–1.6, 2.1–2.4, 8.5, 10.1, 12.1, 12.4, 18.3
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import redis

from shared.bot_pool import BotPool
from shared.config import get_dynamic_setting, settings
from services.face_recognition.processor import FaceProcessor
from services.face_recognition.matcher import IdentityMatcher
from services.face_recognition.publisher import Publisher

logger = logging.getLogger(__name__)

_DLQ_KEY = "face_recognition:dlq"

class FaceRecognitionService:
    """
    Orchestrates the face recognition pipeline:
      cursor loop → batch query → process → advance cursor.
    """

    def __init__(
        self,
        db_pool: asyncpg.Pool,
        redis_client: redis.Redis,
        bot_pool: BotPool,
        processor: FaceProcessor,
        matcher: IdentityMatcher,
        publisher: Publisher,
    ) -> None:
        """
        Wires all components together. Does not start the loop.

        db_pool:      asyncpg connection pool (face_recog_user credentials)
        redis_client: shared Redis instance (dynamic settings + DLQ); may be None
        bot_pool:     initialized BotPool with FACE_BOT_TOKENS
        processor:    FaceProcessor singleton
        matcher:      IdentityMatcher instance
        publisher:    Publisher instance
        """
        self._db_pool = db_pool
        self._redis = redis_client
        self._bot_pool = bot_pool
        self._processor = processor
        self._matcher = matcher
        self._publisher = publisher

        self._running: bool = False
        self._cursor: int = 0
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Task 9.2 — cursor helpers
    # ------------------------------------------------------------------

    async def _init_cursor(self) -> int:
        """
        Returns current cursor value from collector.service_cursors.
        Inserts a row with last_message_id=0 if missing.
        Requirements: 2.2, 2.4
        """
        async with self._db_pool.acquire() as conn:
            # Insert row if it doesn't exist yet (ON CONFLICT DO NOTHING)
            await conn.execute(
                """
                INSERT INTO collector.service_cursors
                    (service_name, last_message_id, updated_at)
                VALUES ('face_recognition', 0, NOW())
                ON CONFLICT (service_name) DO NOTHING
                """
            )
            row = await conn.fetchrow(
                "SELECT last_message_id FROM collector.service_cursors "
                "WHERE service_name = 'face_recognition'"
            )
        value: int = int(row["last_message_id"])
        logger.info("Cursor initialised: last_message_id=%d", value)
        return value

    async def _advance_cursor(self, new_value: int) -> None:
        """
        UPSERTs collector.service_cursors with new_value.
        Only called after the entire batch is processed or DLQ'd.
        Requirements: 2.2, 2.3
        """
        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO collector.service_cursors
                    (service_name, last_message_id, updated_at)
                VALUES ('face_recognition', $1, NOW())
                ON CONFLICT (service_name)
                DO UPDATE SET last_message_id = EXCLUDED.last_message_id,
                              updated_at      = EXCLUDED.updated_at
                """,
                new_value,
            )
        self._cursor = new_value
        logger.debug("Cursor advanced to %d", new_value)

    # ------------------------------------------------------------------
    # Task 9.3 — batch processing
    # ------------------------------------------------------------------

    async def _process_batch(self, messages: list[dict]) -> None:
        """
        Processes one batch of raw_messages rows end-to-end.
        Never raises — all per-message errors are caught and pushed to DLQ.
        Requirements: 1.4, 2.3, 8.5, 12.1, 12.4
        """
        for msg in messages:
            msg_id: int = msg["id"]
            file_unique_id: str = msg.get("file_unique_id", "")

            try:
                # --- Dedup check: skip if already processed ---
                async with self._db_pool.acquire() as conn:
                    already = await conn.fetchrow(
                        "SELECT 1 FROM face_recognition.processed_media "
                        "WHERE file_unique_id = $1",
                        file_unique_id,
                    )
                if already:
                    logger.debug(
                        "Skipping already-processed file_unique_id=%s (msg_id=%d)",
                        file_unique_id,
                        msg_id,
                    )
                    continue

                # --- Face detection ---
                faces: list[dict] = await self._processor.process_message(msg)

                if not faces:
                    # No faces found — record processed_media with faces_found=0
                    async with self._db_pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO face_recognition.processed_media
                                (file_unique_id, media_type, faces_found, topics_matched, processed_at)
                            VALUES ($1, $2, 0, '{}', NOW())
                            ON CONFLICT (file_unique_id) DO NOTHING
                            """,
                            file_unique_id,
                            msg.get("message_type", ""),
                        )
                    continue

                # --- Identity matching ---
                topic_ids: list[int] = []
                for face in faces:
                    topic_id, _is_new = await self._matcher.find_or_create_identity(
                        embedding=face["embedding"],
                        quality_score=face["quality"],
                        source_chat_id=msg["source_chat_id"],
                        source_message_id=msg_id,
                        frame_index=face.get("frame_index", 0),
                    )
                    if topic_id != 0:
                        topic_ids.append(topic_id)

                # --- Publish to Hub ---
                if topic_ids:
                    await self._publisher.process_message_faces(msg, topic_ids)
                else:
                    # All faces below quality threshold — still record processed_media
                    async with self._db_pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO face_recognition.processed_media
                                (file_unique_id, media_type, faces_found, topics_matched, processed_at)
                            VALUES ($1, $2, $3, '{}', NOW())
                            ON CONFLICT (file_unique_id) DO NOTHING
                            """,
                            file_unique_id,
                            msg.get("message_type", ""),
                            len(faces),
                        )

            except Exception as exc:
                logger.exception(
                    "Failed to process msg_id=%d file_unique_id=%s: %s",
                    msg_id,
                    file_unique_id,
                    exc,
                )
                self._push_to_dlq(msg_id, file_unique_id, exc)
                # Requirement 12.4: advance past failed message (cursor advances after full batch)

    def _push_to_dlq(self, message_id: int, file_unique_id: str, exc: Exception) -> None:
        """
        Pushes a failed message to the Redis DLQ.
        Silently skips if Redis is unavailable.
        Requirements: 12.1
        """
        if self._redis is None:
            logger.warning(
                "Redis unavailable — cannot push msg_id=%d to DLQ", message_id
            )
            return

        entry = {
            "message_id": message_id,
            "file_unique_id": file_unique_id,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "retry_count": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._redis.rpush(_DLQ_KEY, json.dumps(entry))
            logger.info(
                "Pushed msg_id=%d to DLQ (error_type=%s)", message_id, entry["error_type"]
            )
        except Exception as redis_exc:
            logger.error("Failed to push to DLQ: %s", redis_exc)

    # ------------------------------------------------------------------
    # Task 9.4 — start / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Enters the cursor loop. Blocks until stop() is called or a signal is received.
        Registers SIGTERM/SIGINT handlers that set _running=False.
        Requirements: 1.2, 1.3, 1.4, 2.1, 10.1
        """
        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except (NotImplementedError, RuntimeError):
                # Windows / non-main-thread fallback
                signal.signal(sig, lambda s, f: self._handle_signal())

        self._running = True
        self._stop_event.clear()

        # Initialise cursor
        self._cursor = await self._init_cursor()

        logger.info("FaceRecognitionService started (cursor=%d)", self._cursor)

        batch_size: int = settings.FACE_BATCH_SIZE
        poll_interval: int = settings.FACE_POLL_INTERVAL

        while self._running:
            # --- Check if processing is enabled (dynamic) ---
            enabled: bool = get_dynamic_setting(
                "FACE_PROCESSING_ENABLED", settings.FACE_PROCESSING_ENABLED
            )
            if not enabled:
                logger.debug("FACE_PROCESSING_ENABLED=False — sleeping %ds", poll_interval)
                await asyncio.sleep(poll_interval)
                continue

            # --- Query next batch ---
            try:
                async with self._db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT *, chat_id AS source_chat_id
                          FROM collector.raw_messages
                         WHERE has_media = TRUE
                           AND message_type IN ('photo', 'video', 'circle_video')
                           AND media_path IS NOT NULL
                           AND id > $1
                         ORDER BY id ASC
                         LIMIT $2
                        """,
                        self._cursor,
                        batch_size,
                    )
            except Exception as exc:
                logger.error("Batch query failed: %s — retrying in %ds", exc, poll_interval)
                await asyncio.sleep(poll_interval)
                continue

            if not rows:
                logger.debug("No new messages — sleeping %ds", poll_interval)
                await asyncio.sleep(poll_interval)
                continue

            messages = [dict(r) for r in rows]
            logger.info(
                "Processing batch of %d messages (cursor=%d → %d)",
                len(messages),
                self._cursor,
                messages[-1]["id"],
            )

            # --- Process batch (never raises) ---
            await self._process_batch(messages)

            # --- Advance cursor after full batch ---
            max_id: int = max(m["id"] for m in messages)
            await self._advance_cursor(max_id)

        # Loop exited — persist cursor one final time
        logger.info("FaceRecognitionService loop exited (cursor=%d)", self._cursor)
        self._stop_event.set()

    def _handle_signal(self) -> None:
        """Signal handler: sets _running=False so the loop exits after the current batch."""
        logger.info("Shutdown signal received — finishing current batch then stopping")
        self._running = False

    async def stop(self) -> None:
        """
        Signals the loop to stop after the current batch. Persists cursor. Idempotent.
        Requirements: 1.4
        """
        if not self._running:
            return  # Already stopped — idempotent
        self._running = False
        logger.info("stop() called — waiting for current batch to finish")
        # Wait for the loop to acknowledge the stop
        await self._stop_event.wait()
        logger.info("FaceRecognitionService stopped (cursor=%d)", self._cursor)


# ---------------------------------------------------------------------------
# Task 9.5 — Startup initialization
# ---------------------------------------------------------------------------

async def _create_db_pool() -> asyncpg.Pool:
    """
    Creates asyncpg pool with face_recog_user credentials.
    Retries with exponential backoff up to 60 seconds on failure.
    Requirements: 1.5, 18.3
    """
    # Prefer a dedicated face_recog_user if available via env, else fall back to DB_USER
    db_user = getattr(settings, "FACE_DB_USER", None) or settings.DB_USER

    dsn = (
        f"postgresql://{db_user}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )

    max_wait = 60.0
    delay = 1.0
    elapsed = 0.0

    while True:
        try:
            pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
            logger.info("Database pool created (user=%s)", db_user)
            return pool
        except Exception as exc:
            if elapsed >= max_wait:
                logger.critical(
                    "Cannot connect to database after %.0fs: %s", max_wait, exc
                )
                sys.exit(1)
            logger.warning(
                "DB connection failed (%.0fs elapsed): %s — retrying in %.0fs",
                elapsed,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            elapsed += delay
            delay = min(delay * 2, 30.0)  # exponential backoff, cap at 30s


def _connect_redis() -> Optional[redis.Redis]:
    """
    Connects to Redis. Logs a warning and returns None if unavailable.
    Requirements: 1.6
    """
    try:
        client = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        client.ping()
        logger.info("Redis connected (%s:%d)", settings.REDIS_HOST, settings.REDIS_PORT)
        return client
    except Exception as exc:
        logger.warning(
            "Redis unavailable (%s) — continuing with static config only", exc
        )
        return None


async def _main() -> None:
    """
    Full startup sequence for the Face Recognition Service.
    Requirements: 1.1, 1.5, 1.6, 9.2, 18.3
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,  # override any handlers set by onnxruntime/insightface at import time
    )

    logger.info("Starting Face Recognition Service…")

    # 1. Database pool (with retry)
    db_pool = await _create_db_pool()

    # 2. Redis (optional)
    redis_client = _connect_redis()

    # 3. Bot pool
    bot_tokens = settings.parsed_face_bot_tokens
    if not bot_tokens:
        logger.warning("FACE_BOT_TOKENS is empty — publisher will not be able to upload media")

    BotPool.reset_instance()
    pool_instance = BotPool()
    if bot_tokens:
        await pool_instance.initialize(bot_tokens)

    # 4. Components
    processor = FaceProcessor.get_instance()
    matcher = IdentityMatcher(db_pool)
    publisher = Publisher(db_pool, pool_instance)

    # 5. Service
    service = FaceRecognitionService(
        db_pool=db_pool,
        redis_client=redis_client,
        bot_pool=pool_instance,
        processor=processor,
        matcher=matcher,
        publisher=publisher,
    )

    try:
        await service.start()
    finally:
        await pool_instance.shutdown()
        await db_pool.close()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(_main())
