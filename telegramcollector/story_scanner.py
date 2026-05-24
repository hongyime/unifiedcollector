"""
story_scanner.py — Per-account story scanner for the worker.py local runtime.

Provides the per-account interface (client, processing_queue, media_manager)
expected by worker.py. Wraps the story scanning logic from services/collector/.
"""
import asyncio
import logging
from typing import Optional

from shared.database import get_db_connection
from shared.config import settings

logger = logging.getLogger(__name__)


class StoryScanner:
    """Per-account story scanner used by worker.py single-process runtime."""

    def __init__(self, client, processing_queue, media_manager):
        self.client = client
        self.processing_queue = processing_queue
        self.media_manager = media_manager
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._account_id: Optional[int] = None
        self._interval: int = settings.STORY_SCAN_INTERVAL

    async def start_polling(self, account_id: int, interval: int) -> None:
        """Start background story polling for this account."""
        if self._running:
            return
        self._account_id = account_id
        self._interval = interval
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(), name=f"story_scanner_{account_id}"
        )
        logger.info("StoryScanner started for account %d (interval=%ds)", account_id, interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.debug("StoryScanner stopped for account %s", self._account_id)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._scan_stories()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("StoryScanner poll error (account %s): %s", self._account_id, exc)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _scan_stories(self) -> None:
        if self._account_id is None:
            return
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT peer_id FROM collector.monitored_peers
                        WHERE account_id = %s
                    """, (self._account_id,))
                    peers = await cur.fetchall()
        except Exception as exc:
            logger.warning("StoryScanner: failed to get monitored peers: %s", exc)
            return

        for (peer_id,) in peers:
            if not self._running:
                break
            try:
                from telethon.tl.functions.stories import GetPeerStoriesRequest
                result = await self.client(GetPeerStoriesRequest(peer=peer_id))
                stories = getattr(result, 'stories', None)
                if stories is None:
                    continue
                story_list = getattr(stories, 'stories', [])
                for story in story_list:
                    await self._store_story(peer_id, story)
            except Exception as exc:
                logger.debug("StoryScanner: peer %d failed: %s", peer_id, exc)

    async def _store_story(self, peer_id: int, story) -> None:
        from datetime import timezone, datetime
        try:
            story_id = getattr(story, 'id', None)
            if story_id is None:
                return
            expire_date = getattr(story, 'expire_date', None)
            expire_ts = (
                datetime.fromtimestamp(expire_date, tz=timezone.utc) if expire_date else None
            )
            has_media = getattr(story, 'media', None) is not None
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO collector.stories
                            (story_id, peer_id, account_id, expire_date, collected_at)
                        VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (story_id, peer_id, account_id) DO NOTHING
                    """, (story_id, peer_id, self._account_id, expire_ts))
        except Exception as exc:
            logger.debug("StoryScanner: failed to store story %s: %s", story_id, exc)
