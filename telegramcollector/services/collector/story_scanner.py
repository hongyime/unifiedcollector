"""StoryScanner — ephemeral story content scanner with expiry-aware prioritization.

Design constraints:
- Polls stories from monitored peers on a configurable interval
- Calls rate_limiter.acquire(account_id) before every Telegram API call
- Skips stories where expire_date < NOW()
- Prioritizes stories within COLLECTOR_STORY_EXPIRY_BUFFER minutes of expiry
- Writes to collector.stories with UNIQUE(story_id, peer_id, account_id) ON CONFLICT DO NOTHING
- Sets media_path = NULL initially; MediaStore updates it after download
- Enqueues media download to collector:media_download_queue (Redis LPUSH) after DB write
- Handles FloodWaitError: wait error.seconds + 10, coordinate with rate_limiter
- Per-peer error isolation: catch exceptions, log, continue to next peer
- NO imports from face_processor, identity_matcher, processing_queue
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from shared.database import get_db_connection
from shared.config import settings

if TYPE_CHECKING:
    from services.collector.media_store import MediaStore
    from services.collector.rate_limiter import RateLimiter
    from shared.telegram_client import TelegramClientManager

logger = logging.getLogger(__name__)

MEDIA_QUEUE_KEY = "collector:media_download_queue"


async def _retry_with_backoff(func, max_attempts: int = 3, base_delay: float = 1.0):
    """Retry an async callable with exponential backoff.
    
    Only retries on transient DB connection errors (psycopg.OperationalError).
    Other exceptions are raised immediately without retry.
    """
    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as exc:
            # Only retry on psycopg OperationalError (transient DB errors)
            # Check by class name to handle mocked psycopg in tests
            exc_type_name = type(exc).__name__
            is_transient = exc_type_name in ("OperationalError",)
            if not is_transient or attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"Retry {attempt + 1}/{max_attempts} after {delay}s: {exc}")
            await asyncio.sleep(delay)


def _is_flood_wait(exc: Exception) -> bool:
    """Return True if exc is a FloodWaitError."""
    name = type(exc).__name__
    return name == "FloodWaitError" or (
        hasattr(exc, "seconds") and "flood" in name.lower()
    )


class StoryScanner:
    """Ephemeral story content scanner with expiry-aware prioritization."""

    def __init__(
        self,
        clients: list,
        rate_limiter: "RateLimiter",
        media_store: "MediaStore | None",
        redis_client: "aioredis.Redis",
    ) -> None:
        """
        Args:
            clients: Shared TelegramClientManager pool
            rate_limiter: Shared RateLimiter instance
            media_store: Shared MediaStore (may be None if disabled)
            redis_client: Redis client for media queue LPUSH
        """
        self.clients = clients
        self.rate_limiter = rate_limiter
        self.media_store = media_store
        self._redis = redis_client

        self._scan_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin scanning stories for all monitored peers."""
        self._running = True
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("StoryScanner started")

    async def stop(self) -> None:
        """Cancel all tasks and cleanup."""
        self._running = False
        if self._scan_task is not None:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None
        logger.info("StoryScanner stopped")

    # ------------------------------------------------------------------
    # Scan loop
    # ------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        """Scan stories for each monitored peer on a fixed interval."""
        while self._running:
            try:
                peers = await self._get_monitored_peers()
                for peer in peers:
                    if not self._running:
                        break
                    peer_id: int = peer["peer_id"]
                    account_id: int = peer["account_id"]
                    client = self._get_client(account_id)
                    if client is None:
                        logger.warning(
                            f"StoryScanner: no client for account_id={account_id}, "
                            f"skipping peer_id={peer_id}"
                        )
                        continue
                    try:
                        await self._scan_peer_stories(client, peer_id)
                    except Exception as exc:
                        logger.error(
                            f"StoryScanner: peer_id={peer_id} account_id={account_id} "
                            f"failed: {exc}",
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"StoryScanner _scan_loop error: {exc}", exc_info=True)

            try:
                await asyncio.sleep(settings.COLLECTOR_STORY_SCAN_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _get_monitored_peers(self) -> list[dict]:
        """Return monitored peers from collector.monitored_peers.

        Falls back to distinct peer_ids from collector.stories if the
        monitored_peers table does not exist. Returns [] if neither exists.
        """
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT peer_id, account_id
                        FROM collector.monitored_peers
                        WHERE is_active = TRUE
                        ORDER BY peer_id ASC
                        """
                    )
                    rows = await cur.fetchall()
                    if not rows:
                        return []
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, row)) for row in rows]
        except Exception as exc:
            # monitored_peers table may not exist yet; fall back to stories
            logger.debug(
                f"StoryScanner: monitored_peers query failed ({exc}), "
                "falling back to collector.stories distinct peers"
            )
            try:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            SELECT DISTINCT peer_id,
                                   account_id
                            FROM collector.stories
                            ORDER BY peer_id ASC
                            """
                        )
                        rows = await cur.fetchall()
                        if not rows:
                            return []
                        cols = [d[0] for d in cur.description]
                        return [dict(zip(cols, row)) for row in rows]
            except Exception as exc2:
                logger.warning(
                    f"StoryScanner: could not fetch monitored peers: {exc2}"
                )
                return []

    # ------------------------------------------------------------------
    # Per-peer story scanning
    # ------------------------------------------------------------------

    async def _scan_peer_stories(
        self, client: "TelegramClientManager", peer_id: int
    ) -> None:
        """Fetch and process stories for a single peer.

        Calls rate_limiter.acquire() before the API call, sorts stories by
        expiry priority (soonest-expiring first), skips expired stories, then
        writes each story and enqueues its media.
        """
        account_id = getattr(client, "account_id", None)

        try:
            await self.rate_limiter.acquire(account_id)
            stories = await client.client.get_stories(peer_id)
        except Exception as exc:
            if _is_flood_wait(exc):
                await self._handle_flood_wait(exc, account_id)
                # Retry once after flood wait
                await self.rate_limiter.acquire(account_id)
                stories = await client.client.get_stories(peer_id)
            else:
                raise

        if not stories:
            return

        # Sort by expiry priority (lower value = higher priority = sooner expiry)
        sorted_stories = sorted(stories, key=self._get_expiry_priority)

        for story in sorted_stories:
            # Skip expired stories
            if self._is_story_expired(story):
                continue
            try:
                await self._write_story(story, peer_id, account_id)
                await self._enqueue_story_media(story, peer_id, account_id)
            except Exception as exc:
                logger.warning(
                    f"StoryScanner: failed to write story "
                    f"peer_id={peer_id} story_id={getattr(story, 'id', '?')}: {exc}"
                )

    # ------------------------------------------------------------------
    # Story persistence
    # ------------------------------------------------------------------

    async def _write_story(
        self, story, peer_id: int, account_id: int
    ) -> None:
        """INSERT into collector.stories with ON CONFLICT DO NOTHING."""
        story_id = getattr(story, "id", None)
        expire_date = getattr(story, "expire_date", None)

        # Detect media type and extract file info
        media_type, file_unique_id, file_id = _extract_story_file_info(story)

        payload = (
            json.dumps(story.to_dict()) if hasattr(story, "to_dict") else "{}"
        )

        async def _do_write():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.stories (
                            story_id, peer_id, account_id,
                            media_type, media_path,
                            file_unique_id, file_id,
                            expire_date, collected_at, payload
                        )
                        VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, NOW(), %s)
                        ON CONFLICT (story_id, peer_id, account_id) DO NOTHING
                        """,
                        (
                            story_id,
                            peer_id,
                            account_id,
                            media_type,
                            file_unique_id,
                            file_id,
                            expire_date,
                            payload,
                        ),
                    )

        await _retry_with_backoff(_do_write)

    # ------------------------------------------------------------------
    # Media enqueue
    # ------------------------------------------------------------------

    async def _enqueue_story_media(
        self, story, peer_id: int, account_id: int
    ) -> None:
        """LPUSH a media download task to collector:media_download_queue.

        Must only be called AFTER _write_story completes.
        If Redis is unavailable, logs a warning and returns gracefully.
        """
        if self._redis is None:
            logger.warning(
                f"StoryScanner: MediaStore unavailable; skipping media enqueue "
                f"for peer_id={peer_id} story_id={getattr(story, 'id', '?')}"
            )
            return

        _media_type, file_unique_id, file_id = _extract_story_file_info(story)
        if file_id is None:
            return

        file_size = _get_story_file_size(story)
        ext = _get_story_ext(story)

        task = {
            "task_type": "media_download",
            "chat_id": None,
            "message_id": None,
            "story_id": getattr(story, "id", None),
            "peer_id": peer_id,
            "file_unique_id": file_unique_id,
            "file_id": file_id,
            "ext": ext or "bin",
            "file_size": file_size,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "_retry_count": 0,
        }

        try:
            await self._redis.lpush(MEDIA_QUEUE_KEY, json.dumps(task))
        except Exception as exc:
            logger.warning(
                f"StoryScanner: failed to enqueue media for "
                f"peer_id={peer_id} story_id={getattr(story, 'id', '?')}: {exc}"
            )

    # ------------------------------------------------------------------
    # Expiry helpers
    # ------------------------------------------------------------------

    def _is_story_expired(self, story) -> bool:
        """Return True if story.expire_date has already passed."""
        expire_date = getattr(story, "expire_date", None)
        if expire_date is None:
            return False
        now = datetime.now(timezone.utc)
        # Ensure expire_date is timezone-aware for comparison
        if isinstance(expire_date, datetime):
            if expire_date.tzinfo is None:
                expire_date = expire_date.replace(tzinfo=timezone.utc)
            return expire_date < now
        return False

    def _get_expiry_priority(self, story) -> float:
        """Return a sort key for expiry prioritization.

        Stories within COLLECTOR_STORY_EXPIRY_BUFFER minutes of expiry get
        priority (lower return value = processed first).
        Stories that are already expired return float('inf') so they sort last
        (they will be skipped anyway).
        """
        expire_date = getattr(story, "expire_date", None)
        if expire_date is None:
            return float("inf")

        now = datetime.now(timezone.utc)
        if isinstance(expire_date, datetime):
            if expire_date.tzinfo is None:
                expire_date = expire_date.replace(tzinfo=timezone.utc)
        else:
            return float("inf")

        if expire_date < now:
            # Already expired — sort to end (will be skipped)
            return float("inf")

        seconds_until_expiry = (expire_date - now).total_seconds()
        buffer_seconds = settings.COLLECTOR_STORY_EXPIRY_BUFFER * 60

        if seconds_until_expiry <= buffer_seconds:
            # Within buffer — high priority (negative offset so they sort first)
            return -buffer_seconds + seconds_until_expiry
        else:
            return seconds_until_expiry

    # ------------------------------------------------------------------
    # FloodWait handling
    # ------------------------------------------------------------------

    async def _handle_flood_wait(self, exc: Exception, account_id: int) -> None:
        """Handle FloodWaitError: wait seconds + 10, coordinate with rate_limiter."""
        seconds = getattr(exc, "seconds", 0)
        wait_seconds = seconds + 10
        logger.warning(
            f"StoryScanner: FloodWait for account_id={account_id}: "
            f"waiting {wait_seconds}s"
        )
        self.rate_limiter.set_flood_wait(account_id, seconds)
        await asyncio.sleep(wait_seconds)

    # ------------------------------------------------------------------
    # Client lookup
    # ------------------------------------------------------------------

    def _get_client(self, account_id: int) -> "TelegramClientManager | None":
        """Return the TelegramClientManager for account_id, or None."""
        for manager in self.clients:
            mgr_account_id = getattr(manager, "account_id", None)
            if mgr_account_id == account_id:
                return manager
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _extract_story_file_info(story) -> tuple:
    """Return (media_type, file_unique_id, file_id) from a story object."""
    # Try photo
    photo = getattr(story, "photo", None)
    if photo is not None:
        return (
            "photo",
            getattr(photo, "file_unique_id", None),
            getattr(photo, "file_id", None),
        )
    # Try media attribute (may contain MessageMediaPhoto or MessageMediaDocument)
    media = getattr(story, "media", None)
    if media is not None:
        inner_photo = getattr(media, "photo", None)
        if inner_photo is not None:
            return (
                "photo",
                getattr(inner_photo, "file_unique_id", None),
                getattr(inner_photo, "file_id", None),
            )
        inner_doc = getattr(media, "document", None)
        if inner_doc is not None:
            mime = getattr(inner_doc, "mime_type", "") or ""
            media_type = "video" if "video" in mime else "document"
            return (
                media_type,
                getattr(inner_doc, "file_unique_id", None),
                getattr(inner_doc, "file_id", None),
            )
    # Try document directly
    document = getattr(story, "document", None)
    if document is not None:
        mime = getattr(document, "mime_type", "") or ""
        media_type = "video" if "video" in mime else "document"
        return (
            media_type,
            getattr(document, "file_unique_id", None),
            getattr(document, "file_id", None),
        )
    return (None, None, None)


def _get_story_file_size(story) -> int:
    """Return file size in bytes from a story object, or 0."""
    media = getattr(story, "media", None)
    if media is not None:
        doc = getattr(media, "document", None)
        if doc is not None:
            return getattr(doc, "size", 0) or 0
        photo = getattr(media, "photo", None)
        if photo is not None:
            sizes = getattr(photo, "sizes", [])
            for sz in reversed(sizes):
                if hasattr(sz, "size"):
                    return sz.size
    return 0


def _get_story_ext(story) -> str:
    """Return file extension for a story's media."""
    media = getattr(story, "media", None)
    if media is not None:
        doc = getattr(media, "document", None)
        if doc is not None:
            mime = getattr(doc, "mime_type", "") or ""
            if "mp4" in mime or "video" in mime:
                return "mp4"
            if "webm" in mime:
                return "webm"
            return "bin"
        photo = getattr(media, "photo", None)
        if photo is not None:
            return "jpg"
    photo = getattr(story, "photo", None)
    if photo is not None:
        return "jpg"
    return "bin"
