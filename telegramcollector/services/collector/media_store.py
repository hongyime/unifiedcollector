"""Media store — deduplicating media download and symlink management.

Workers consume tasks from the Redis list at ``collector:media_download_queue``
and write files to a two-level directory layout::

    {base_path}/by_id/{file_unique_id}.{ext}          ← canonical file
    {base_path}/by_message/{chat_id}/{message_id}.{ext} ← symlink → by_id

The ``by_id`` path is the deduplication key: if the file already exists on
disk the worker skips the download and only creates the symlink.
"""

import asyncio
import json
import logging
import os
import sys
from typing import TYPE_CHECKING

from shared.database import get_db_connection
from shared.dlq import DLQProcessor

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from shared.telegram_client import TelegramClientManager

logger = logging.getLogger(__name__)

QUEUE_KEY = "collector:media_download_queue"
DLQ_KEY = "collector:media_download_dlq"


class MediaStore:
    """Deduplicating media downloader backed by a Redis work queue.

    Files are stored once under ``by_id/`` and referenced from
    ``by_message/`` via symlinks (POSIX) or hardlinks (Windows).
    """

    QUEUE_KEY = QUEUE_KEY
    DLQ_KEY = DLQ_KEY

    def __init__(
        self,
        redis_client,
        tg_clients: list,
        dlq_processor: "DLQProcessor",
        base_path: str,
        max_size_mb: int,
        num_workers: int = 4,
    ) -> None:
        self.redis_client = redis_client
        self.tg_clients = tg_clients
        self.dlq_processor = dlq_processor
        self.base_path = base_path
        self.max_size_mb = max_size_mb
        self.num_workers = num_workers
        self._worker_tasks: list[asyncio.Task] = []
        self._client_index: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch ``num_workers`` asyncio tasks running ``_worker_loop``."""
        for _ in range(self.num_workers):
            task = asyncio.create_task(self._worker_loop())
            self._worker_tasks.append(task)
        logger.info(f"MediaStore started with {self.num_workers} workers")

    async def stop(self) -> None:
        """Cancel all worker tasks and await their completion."""
        for task in self._worker_tasks:
            task.cancel()
        results = await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                logger.warning(f"Worker task raised on stop: {result}")
        self._worker_tasks.clear()
        logger.info("MediaStore stopped")

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _build_by_id_path(self, file_unique_id: str, ext: str) -> str:
        """Return ``{base_path}/by_id/{file_unique_id}.{ext}``.

        ``ext`` is normalised to lowercase with no leading dot.
        """
        ext = ext.lstrip(".").lower()
        return os.path.join(self.base_path, "by_id", f"{file_unique_id}.{ext}")

    def _build_by_message_path(self, chat_id: int, message_id: int, ext: str) -> str:
        """Return ``{base_path}/by_message/{chat_id}/{message_id}.{ext}``.

        Creates the parent directory if it does not exist.
        """
        ext = ext.lstrip(".").lower()
        parent = os.path.join(self.base_path, "by_message", str(chat_id))
        os.makedirs(parent, exist_ok=True)
        return os.path.join(parent, f"{message_id}.{ext}")

    # ------------------------------------------------------------------
    # Symlink / hardlink
    # ------------------------------------------------------------------

    def _create_symlink(self, target: str, link_path: str) -> None:
        """Create a symlink (POSIX) or hardlink (Windows) from ``link_path`` → ``target``.

        Silently skips if ``link_path`` already exists.
        """
        if os.path.exists(link_path):
            return

        if sys.platform != "win32":
            # Use a relative path so the store is portable across mount points.
            rel_target = os.path.relpath(target, os.path.dirname(link_path))
            os.symlink(rel_target, link_path)
        else:
            # Windows: fall back to a hardlink (no symlink privilege required).
            os.link(target, link_path)

    # ------------------------------------------------------------------
    # Client selection
    # ------------------------------------------------------------------

    def _pick_client(self):
        """Return the next healthy ``TelegramClientManager`` in round-robin order."""
        if not self.tg_clients:
            raise RuntimeError("No Telegram clients available")

        num = len(self.tg_clients)
        for _ in range(num):
            client = self.tg_clients[self._client_index % num]
            self._client_index = (self._client_index + 1) % num
            if client.is_healthy:
                return client

        raise RuntimeError("No healthy Telegram clients available")

    # ------------------------------------------------------------------
    # Database update
    # ------------------------------------------------------------------

    async def _update_media_path(self, chat_id: int, message_id: int, media_path: str) -> None:
        """Update ``collector.raw_messages.media_path`` for the given message."""
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE collector.raw_messages SET media_path = %s "
                    "WHERE chat_id = %s AND message_id = %s",
                    (media_path, chat_id, message_id),
                )

    # ------------------------------------------------------------------
    # Task processing
    # ------------------------------------------------------------------

    async def _process_task(self, task: dict) -> None:
        """Download a single media file and update the database.

        Steps:
        1. Extract fields from the task dict.
        2. Skip oversized files (no DLQ entry).
        3. If the ``by_id`` file already exists, create symlink + update DB.
        4. Otherwise re-fetch the message from Telegram, download, symlink, DB.

        We re-fetch the message (get_messages) instead of using a stored file_id
        because Telethon doesn't expose Bot-API file_ids.  Re-fetching also
        avoids FILE_REFERENCE_EXPIRED errors on older tasks.
        """
        file_unique_id: str = task["file_unique_id"]
        ext: str = task.get("ext", "bin")
        chat_id: int = task["chat_id"]
        message_id: int = task["message_id"]
        file_size: int = task.get("file_size", 0)

        # Guard: skip tasks that somehow arrived without a dedup key
        if not file_unique_id:
            logger.warning(f"Dropping task with no file_unique_id: chat_id={chat_id} message_id={message_id}")
            return

        # 2. Size guard — skip silently, no DLQ
        max_bytes = self.max_size_mb * 1024 * 1024
        if file_size > max_bytes:
            logger.info(
                f"Skipping oversized media: file_unique_id={file_unique_id} "
                f"size={file_size} max={max_bytes}"
            )
            return

        # 3. Build paths
        by_id_path = self._build_by_id_path(file_unique_id, ext)

        try:
            # 4. Dedup check — file already on disk
            if os.path.exists(by_id_path):
                await self._update_media_path(chat_id, message_id, by_id_path)
                return

            # 5. Pick a healthy client
            client = self._pick_client()

            # 6. Re-fetch the live message so we have a fresh file reference.
            #    This avoids FILE_REFERENCE_EXPIRED for backfilled/old tasks.
            message = await client.client.get_messages(chat_id, ids=message_id)
            if message is None or not getattr(message, "media", None):
                logger.warning(
                    f"Message has no media (deleted?): chat_id={chat_id} message_id={message_id}"
                )
                return

            await client.client.download_media(message, file=by_id_path)

            if not os.path.exists(by_id_path):
                logger.warning(
                    f"download_media produced no file: chat_id={chat_id} message_id={message_id}"
                )
                return

            # 7. Update media_path to by_id path (by_message symlinks not used — NTFS bind mounts
            #    on Docker/WSL2 do not support symlink or hardlink creation from Linux containers)
            await self._update_media_path(chat_id, message_id, by_id_path)

        except Exception as e:
            error_str = str(e)
            error_type_name = type(e).__name__

            # FloodWaitError — sleep and re-raise so the worker retries
            if "FloodWaitError" in error_type_name or hasattr(e, "seconds"):
                seconds = getattr(e, "seconds", 60)
                logger.warning(
                    f"FloodWaitError for file_unique_id={file_unique_id}: "
                    f"sleeping {seconds + 10}s"
                )
                await asyncio.sleep(seconds + 10)
                raise

            # Permanent errors — file reference expired or similar
            if any(
                pat in error_type_name
                for pat in ("FileReferenceExpiredError", "FileReferenceInvalidError")
            ) or any(
                pat in error_str.lower()
                for pat in ("file reference expired", "file reference invalid", "not found", "invalid")
            ):
                logger.error(
                    f"Permanent error for file_unique_id={file_unique_id}: {e}"
                )
                await self.dlq_processor._add_to_dlq(task, error_reason=error_str)
                return

            # Disk / permission errors → RESOURCE
            if isinstance(e, (OSError, PermissionError)):
                logger.error(
                    f"Disk/permission error for file_unique_id={file_unique_id}: {e}"
                )
                await self.dlq_processor._add_to_dlq(task, error_reason=error_str)
                return

            # Network / timeout → TRANSIENT
            logger.error(
                f"Transient error for file_unique_id={file_unique_id}: {e}"
            )
            await self.dlq_processor._add_to_dlq(task, error_reason=error_str)

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        """Consume tasks from Redis and process them indefinitely."""
        while True:
            try:
                # Pause if media drive is not accessible
                if not os.path.isdir(self.base_path):
                    logger.warning(
                        f"Media path not accessible: {self.base_path} — pausing 30s"
                    )
                    await asyncio.sleep(30)
                    continue

                raw = await self.redis_client.brpop(QUEUE_KEY, timeout=5)
                if raw:
                    task = json.loads(raw[1])
                    try:
                        await self._process_task(task)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"MediaStore task failed: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"MediaStore worker loop error: {e}")
