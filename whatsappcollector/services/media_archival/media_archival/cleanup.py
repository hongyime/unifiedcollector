from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .database import database
from .observability import cleanup_deletions_total, cleanup_duration_seconds, get_logger

logger = get_logger(__name__)


def should_delete_media_file(raw_message_id: int, min_cursor: int) -> bool:
    return raw_message_id < min_cursor


class MediaCleanupManager:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

    async def run_once(self) -> dict[str, int]:
        start = asyncio.get_running_loop().time()
        min_cursor = await database.get_min_service_cursor()
        cutoff = datetime.utcnow() - timedelta(days=settings.MEDIA_RETENTION_DAYS)
        candidates = await database.get_cleanup_candidates(min_cursor=min_cursor, cutoff=cutoff)

        deleted_files = 0
        deleted_links = 0

        for row in candidates:
            by_message = Path(row["by_message_path"]) if row["by_message_path"] else None
            by_id = Path(row["by_id_path"]) if row["by_id_path"] else None
            if by_message and by_message.exists():
                try:
                    by_message.unlink()
                    deleted_links += 1
                    cleanup_deletions_total.labels(kind="symlink").inc()
                except FileNotFoundError:
                    pass

            if by_id and by_id.exists():
                ref_count = await database.count_file_references(row["file_unique_id"], row["sha256"])
                if ref_count <= 1 and should_delete_media_file(int(row["raw_message_id"]), min_cursor):
                    try:
                        by_id.unlink()
                        deleted_files += 1
                        cleanup_deletions_total.labels(kind="file").inc()
                    except FileNotFoundError:
                        pass

        # Purge permanent failures not retried in 30+ days
        purged = await database.delete_stale_failures(older_than_days=30)
        if purged:
            logger.info("media_cleanup_purged_failures", count=purged)

        cleanup_duration_seconds.observe(asyncio.get_running_loop().time() - start)
        logger.info("media_cleanup_run_complete", deleted_files=deleted_files,
                    deleted_links=deleted_links, purged_failures=purged)
        return {"deleted_files": deleted_files, "deleted_links": deleted_links, "purged_failures": purged}

    async def run_forever(self) -> None:
        self.running = True
        while self.running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("media_cleanup_failed", error=str(exc))
            await asyncio.sleep(settings.MEDIA_CLEANUP_INTERVAL_HOURS * 3600)

    async def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()


cleanup_manager = MediaCleanupManager()
