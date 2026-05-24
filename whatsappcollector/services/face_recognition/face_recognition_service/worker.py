from __future__ import annotations

import asyncio
import os
from pathlib import Path

from .broker import RabbitMQBroker
from .config import settings
from .database import database
from .matcher import identity_matcher
from .observability import get_logger, publisher_queue_depth, start_metrics_server
from .processor import face_processor
from .publisher import findings_publisher
from shared.live_config import ConfigOverlay

overlay = ConfigOverlay(settings, "face_recognition", settings.REDIS_URL)

logger = get_logger(__name__)


class FaceRecognitionWorker:
    def __init__(self) -> None:
        self.running = False
        self.is_ready: bool = False
        self._tasks: list[asyncio.Task] = []
        self._semaphore = asyncio.Semaphore(settings.FACE_BIOMETRIC_SEMAPHORE)
        self._broker = RabbitMQBroker(settings.RABBITMQ_URL)

    def try_load_models(self) -> bool:
        try:
            face_processor._verify_models()
            if face_processor.models_ready:
                self.is_ready = True
                logger.info("face_models_loaded")
            else:
                logger.error("face_models_load_failed", path=settings.FACE_MODELS_PATH, error="model files not found")
                self.is_ready = False
        except Exception as exc:
            logger.error("face_models_load_failed", path=settings.FACE_MODELS_PATH, error=str(exc))
            self.is_ready = False
        return self.is_ready

    async def _model_reload_loop(self) -> None:
        while not self.is_ready:
            await asyncio.sleep(60)
            if self.try_load_models():
                logger.info("face_biometric_pipeline_activated")
                return

    async def start(self) -> None:
        self.running = True
        await database.connect()
        await database.seed_cursor()
        await self._broker.connect()
        await self._broker.declare_topology()
        findings_publisher.start(self._broker)
        start_metrics_server(settings.METRICS_PORT)
        self.try_load_models()
        await overlay.start_poll_loop()
        if not self.is_ready:
            logger.warning("face_biometric_pipeline_deferred")
            self._tasks = [
                asyncio.create_task(self._process_loop()),
                asyncio.create_task(self._dashboard_probe_loop()),
                asyncio.create_task(self._model_reload_loop()),
            ]
        else:
            self._tasks = [
                asyncio.create_task(self._process_loop()),
                asyncio.create_task(self._dashboard_probe_loop()),
            ]
        logger.info("face_recognition_worker_started")

    async def stop(self) -> None:
        self.running = False
        await findings_publisher.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await database.close()
        await self._broker.close()
        await overlay.stop_poll_loop()
        logger.info("face_recognition_worker_stopped")

    def _resolve_media_path(self, row) -> str:
        for key in ("by_message_path", "by_id_path"):
            value = row[key] if key in row else None
            if value:
                return str(Path(value))
        return ""

    async def _process_one(self, row) -> None:
        source_message_id = str(row["message_id"])
        source_chat_jid = str(row["chat_jid"])
        raw_message_id = int(row["raw_message_id"])
        mime_type = str(row.get("mime_type") or "")
        message_type = str(row.get("message_type") or "")
        media_path = self._resolve_media_path(row)
        if not media_path:
            logger.warning("face_media_path_missing", message_id=source_message_id, chat_jid=source_chat_jid)
            return

        embeddings = []
        async with self._semaphore:
            embeddings = face_processor.process_media_file(media_path, mime_type, message_type=message_type)

        async with database.pool.acquire() as conn:  # type: ignore[union-attr]
            async with conn.transaction():
                if await database.has_processed_media(source_message_id, source_chat_jid, conn=conn):
                    return

                face_count = 0
                for embedding in embeddings:
                    identity_id, is_new = await identity_matcher.match_embedding(
                        embedding=embedding.embedding,
                        source_message_id=source_message_id,
                        source_chat_jid=source_chat_jid,
                        frame_index=embedding.frame_index,
                        confidence=embedding.confidence,
                        conn=conn,
                    )
                    await database.insert_face_embedding(
                        identity_id=str(identity_id),
                        embedding=embedding.embedding,
                        source_message_id=source_message_id,
                        source_chat_jid=source_chat_jid,
                        frame_index=embedding.frame_index,
                        is_valid=True,
                        conn=conn,
                    )
                    await findings_publisher.publish_sighting(
                        identity_id=str(identity_id),
                        original_image_path=embedding.source_path or media_path,
                        event_type="new_identity" if is_new else "identity_match",
                        confidence=embedding.confidence,
                    )
                    face_count += 1

                await database.mark_processed_media(
                    source_message_id,
                    source_chat_jid,
                    face_count,
                    conn=conn,
                )
                await database.advance_cursor(raw_message_id, conn=conn)

    def _is_storage_accessible(self) -> bool:
        try:
            p = Path(settings.MEDIA_ROOT)
            return p.exists() and os.access(p, os.R_OK)
        except Exception:
            return False

    async def _process_loop(self) -> None:
        _storage_warned = False
        while self.running:
            if not self.is_ready:
                await asyncio.sleep(overlay.get("FACE_POLL_SECONDS"))
                continue
            if not self._is_storage_accessible():
                if not _storage_warned:
                    logger.warning("face_recognition_storage_unavailable", path=settings.MEDIA_ROOT)
                    _storage_warned = True
                await asyncio.sleep(30)
                continue
            if _storage_warned:
                logger.info("face_recognition_storage_resumed", path=settings.MEDIA_ROOT)
                _storage_warned = False
            try:
                cursor = await database.get_cursor()
                rows = await database.list_pending_media(cursor, overlay.get("FACE_PROCESSING_BATCH_SIZE"))
                if not rows:
                    await asyncio.sleep(overlay.get("FACE_POLL_SECONDS"))
                    continue

                for row in rows:
                    await self._process_one(row)
            except asyncio.CancelledError:
                raise
            except (Exception, SystemExit) as exc:
                logger.warning("face_recognition_loop_failed", error=str(exc))
            await asyncio.sleep(overlay.get("FACE_POLL_SECONDS"))

    async def _dashboard_probe_loop(self) -> None:
        while self.running:
            try:
                publisher_queue_depth.set(0 if not findings_publisher else len(findings_publisher._queue))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(30)


worker = FaceRecognitionWorker()
