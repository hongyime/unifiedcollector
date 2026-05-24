from __future__ import annotations

import asyncio
import os

from .cleanup import cleanup_manager
from .config import settings
from .database import database
from .downloader import media_downloader
from .observability import get_logger, queue_depth_gauge, start_metrics_server
from .redownload import redownload_manager
from shared.task_supervisor import TaskSupervisor
from shared.live_config import ConfigOverlay

overlay = ConfigOverlay(settings, "media_archival", settings.REDIS_URL)

logger = get_logger(__name__)


class MediaArchivalWorker:
    def __init__(self) -> None:
        self.running = False
        self._supervisors: list[TaskSupervisor] = []
        self._download_failures: dict[int, int] = {}

    async def start(self) -> None:
        self.running = True
        await database.connect()
        await database.seed_cursor()
        start_metrics_server(9091, health_check_fn=lambda: {
            "status": "ok" if self.running else "degraded",
            "worker": "running" if self.running else "stopped",
            "broker": "n/a"
        })
        supervisors = [
            TaskSupervisor("download_loop", self._download_loop),
            TaskSupervisor("cleanup_manager", cleanup_manager.run_forever),
            TaskSupervisor("redownload_manager", redownload_manager.run_forever),
            TaskSupervisor("queue_depth_loop", self._queue_depth_loop),
        ]
        self._supervisors = supervisors
        for s in supervisors:
            await s.start()
        await overlay.start_poll_loop()
        logger.info("media_archival_worker_started")

    async def stop(self) -> None:
        self.running = False
        await cleanup_manager.stop()
        await redownload_manager.stop()
        for s in self._supervisors:
            await s.stop()
        await overlay.stop_poll_loop()
        await database.close()
        logger.info("media_archival_worker_stopped")

    def _is_storage_accessible(self) -> bool:
        try:
            p = settings.storage_path
            return p.exists() and os.access(p, os.W_OK)
        except Exception:
            return False

    async def _download_loop(self) -> None:
        _storage_warned = False
        while self.running:
            if not self._is_storage_accessible():
                if not _storage_warned:
                    logger.warning("media_archival_storage_unavailable", path=str(settings.storage_path))
                    _storage_warned = True
                await asyncio.sleep(30)
                continue
            if _storage_warned:
                logger.info("media_archival_storage_resumed", path=str(settings.storage_path))
                _storage_warned = False
            try:
                cursor = await database.get_media_cursor()
                rows = await database.get_pending_media_messages(cursor, overlay.get("MEDIA_ARCHIVAL_BATCH_SIZE"))
                if not rows:
                    await asyncio.sleep(overlay.get("MEDIA_ARCHIVAL_POLL_SECONDS"))
                    continue

                for row in rows:
                    raw_message_id = int(row["raw_message_id"])
                    result = await media_downloader.download_message(row)
                    if result.success:
                        self._download_failures.pop(raw_message_id, None)
                        await database.advance_cursor("media_archival", raw_message_id)
                    else:
                        self._download_failures[raw_message_id] = self._download_failures.get(raw_message_id, 0) + 1
                        if self._download_failures[raw_message_id] >= overlay.get("MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES"):
                            logger.error(
                                "media_archival_dead_letter",
                                raw_message_id=raw_message_id,
                                failures=self._download_failures[raw_message_id],
                            )
                            del self._download_failures[raw_message_id]
                            await database.advance_cursor("media_archival", raw_message_id)
                            continue
                        # Keep cursor at the failing row so retry logic can revisit it.
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("media_archival_download_loop_failed", error=str(exc))
            await asyncio.sleep(overlay.get("MEDIA_ARCHIVAL_POLL_SECONDS"))

    async def _queue_depth_loop(self) -> None:
        while self.running:
            try:
                cursor = await database.get_media_cursor()
                rows = await database.get_pending_media_messages(cursor, 100)
                queue_depth_gauge.set(len(rows))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("media_archival_depth_loop_failed", error=str(exc))
            await asyncio.sleep(30)


worker = MediaArchivalWorker()
