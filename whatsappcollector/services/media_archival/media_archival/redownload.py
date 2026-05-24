from __future__ import annotations

import asyncio

from .config import settings
from .database import database
from .downloader import media_downloader
from .observability import get_logger

logger = get_logger(__name__)


class MediaRedownloadManager:
    def __init__(self) -> None:
        self.running = False

    async def run_once(self) -> int:
        if not settings.MEDIA_REDOWNLOAD_ENABLED:
            return 0

        candidates = await database.list_expiring_media(settings.MEDIA_REDOWNLOAD_LOOKAHEAD_HOURS)
        attempted = 0
        for row in candidates:
            attempted += 1
            await media_downloader.download_message(row)
        return attempted

    async def run_forever(self) -> None:
        self.running = True
        while self.running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("media_redownload_failed", error=str(exc))
            await asyncio.sleep(settings.MEDIA_REDOWNLOAD_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self.running = False


redownload_manager = MediaRedownloadManager()
