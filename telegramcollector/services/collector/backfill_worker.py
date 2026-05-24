"""BackfillWorker — cursor-based historical message fetcher with progress tracking.

Design constraints:
- Polls collector.backfill_jobs for status='pending' on a configurable interval
- Uses min_id cursor for descending-order pagination (newest → oldest)
- Calls rate_limiter.acquire(account_id) before every Telegram API call
- Writes raw_messages with media_path=NULL, then enqueues media download
- Tracks progress in backfill_state (last_processed_message_id = min batch ID)
- Handles FloodWaitError: wait error.seconds + 10, coordinate with rate_limiter
- Error isolation: one job failure does not stop other jobs
- NO imports from face_processor, identity_matcher, processing_queue
"""

import asyncio
import json
import logging
from datetime import datetime, date, timezone


def _tg_json(obj):
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)
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


class BackfillWorker:
    """Cursor-based historical message fetcher with progress tracking."""

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

        self._poll_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Begin polling backfill_jobs table and processing jobs."""
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("BackfillWorker started")

    async def stop(self) -> None:
        """Cancel all tasks and cleanup."""
        self._running = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("BackfillWorker stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll collector.backfill_jobs for pending jobs on a fixed interval."""
        while self._running:
            try:
                jobs = await self._fetch_pending_jobs()
                for job in jobs:
                    if not self._running:
                        break
                    try:
                        await self._process_job(job)
                    except Exception as exc:
                        chat_id = job.get("chat_id", "unknown")
                        job_id = job.get("id", "unknown")
                        logger.error(
                            f"BackfillWorker: job id={job_id} chat_id={chat_id} "
                            f"failed with unhandled error: {exc}",
                            exc_info=True,
                        )
                        # Mark job failed so it doesn't loop forever
                        try:
                            await self._mark_job_failed(job, str(exc))
                        except Exception:
                            pass
                    # Delay between chats
                    await asyncio.sleep(settings.COLLECTOR_BACKFILL_CHAT_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"BackfillWorker _poll_loop error: {exc}", exc_info=True)

            try:
                await asyncio.sleep(settings.COLLECTOR_BACKFILL_POLL_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _fetch_pending_jobs(self) -> list[dict]:
        """Return up to 5 pending backfill_jobs ordered by created_at.

        Processing a small batch per poll cycle keeps the backfill rate
        controllable and prevents all 800+ jobs from racing at startup.
        """
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, chat_id, account_id, status, created_at, updated_at
                    FROM collector.backfill_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 5
                    """
                )
                rows = await cur.fetchall()
                if not rows:
                    return []
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    async def _process_job(self, job: dict) -> None:
        """Execute a single backfill job end-to-end."""
        chat_id: int = job["chat_id"]
        account_id: int = job["account_id"]
        job_id: int = job["id"]

        # Fetch the Telegram client for this account
        client_manager = self._get_client(account_id)
        if client_manager is None:
            logger.warning(
                f"BackfillWorker: no client for account_id={account_id}, "
                f"requeueing job id={job_id} as pending"
            )
            await self._mark_job_pending(job)
            return

        # Mark job in_progress
        await self._mark_job_in_progress(job)

        # Upsert backfill_state row and read resume cursor
        min_id = await self._get_or_create_backfill_state(chat_id, account_id)

        logger.info(
            f"BackfillWorker: starting job id={job_id} chat_id={chat_id} "
            f"account_id={account_id} resume_min_id={min_id}"
        )

        try:
            batch_size = settings.COLLECTOR_BACKFILL_BATCH_SIZE
            while True:
                # Fetch a batch of messages
                try:
                    messages = await self._fetch_messages_batch(
                        client_manager, chat_id, min_id, batch_size
                    )
                except Exception as exc:
                    # Check for FloodWaitError
                    if _is_flood_wait(exc):
                        await self._handle_flood_wait(exc, account_id)
                        # Retry same batch
                        messages = await self._fetch_messages_batch(
                            client_manager, chat_id, min_id, batch_size
                        )
                    else:
                        raise

                if not messages:
                    # No more messages — backfill complete
                    break

                # Write each message
                for message in messages:
                    try:
                        await self._write_message(message, chat_id)
                        # Upsert sender
                        sender = getattr(message, "sender", None)
                        if sender is None:
                            # Try to get sender from message attributes
                            sender_id = getattr(message, "sender_id", None)
                            if sender_id is not None:
                                # We only have the ID; create a minimal user-like object
                                sender = _MinimalUser(sender_id)
                        if sender is not None:
                            try:
                                await self._upsert_user(sender, chat_id)
                                await self._write_user_sighting(
                                    sender.id,
                                    chat_id,
                                    sender.to_dict() if hasattr(sender, "to_dict") else {},
                                )
                            except Exception as user_exc:
                                logger.warning(
                                    f"BackfillWorker: user upsert failed for "
                                    f"chat_id={chat_id} message_id={message.id}: {user_exc}"
                                )
                        # Enqueue media AFTER DB write
                        if getattr(message, "media", None) is not None:
                            await self._enqueue_media(message, chat_id)
                    except Exception as msg_exc:
                        logger.warning(
                            f"BackfillWorker: failed to write message "
                            f"chat_id={chat_id} message_id={getattr(message, 'id', '?')}: {msg_exc}"
                        )

                # Update progress: min ID in this batch
                batch_ids = [m.id for m in messages if hasattr(m, "id")]
                if batch_ids:
                    new_min_id = min(batch_ids)
                    await self._update_backfill_state(
                        chat_id, account_id, last_processed_message_id=new_min_id
                    )
                    min_id = new_min_id

                # If we got fewer messages than batch_size, we've reached the end
                if len(messages) < batch_size:
                    break

            # Mark completed
            await self._mark_job_completed(job)
            await self._update_backfill_state(
                chat_id, account_id, status="completed", completed_at="NOW()"
            )
            logger.info(
                f"BackfillWorker: job id={job_id} chat_id={chat_id} completed"
            )

        except Exception as exc:
            logger.error(
                f"BackfillWorker: job id={job_id} chat_id={chat_id} failed: {exc}",
                exc_info=True,
            )
            await self._mark_job_failed(job, str(exc))
            await self._update_backfill_state(
                chat_id, account_id, status="failed", error=str(exc)
            )

    # ------------------------------------------------------------------
    # Telegram API
    # ------------------------------------------------------------------

    async def _fetch_messages_batch(
        self,
        client: "TelegramClientManager",
        chat_id: int,
        min_id: int,
        limit: int,
    ) -> list:
        """Fetch up to `limit` messages from chat_id with id > min_id.

        Uses iter_messages with reverse=False (newest first) and offset_id=min_id
        so we get messages just below the current cursor, descending.
        """
        await self.rate_limiter.acquire(
            getattr(client, "account_id", None)
        )
        messages = []
        async for msg in client.client.iter_messages(
            chat_id,
            limit=limit,
            max_id=min_id if min_id > 0 else 0,
            reverse=False,
        ):
            messages.append(msg)
        return messages

    # ------------------------------------------------------------------
    # DB write helpers
    # ------------------------------------------------------------------

    async def _write_message(self, message, chat_id: int) -> None:
        """INSERT into collector.raw_messages with media_path=NULL."""
        message_type = _detect_message_type(message)
        file_unique_id, file_id, _ext = _extract_file_info(message)
        has_media = getattr(message, "media", None) is not None

        fwd = getattr(message, "fwd_from", None)
        forward_from_chat_id = None
        forward_from_message_id = None
        if fwd is not None:
            forward_from_chat_id = getattr(
                getattr(fwd, "from_id", None), "channel_id", None
            )
            forward_from_message_id = getattr(fwd, "channel_post", None)

        reply = getattr(message, "reply_to", None)
        reply_to_message_id = None
        if reply is not None:
            reply_to_message_id = getattr(reply, "reply_to_msg_id", None)

        views = getattr(message, "views", None)
        sender_id = getattr(message, "sender_id", None)
        payload = (
            json.dumps(message.to_dict(), default=_tg_json) if hasattr(message, "to_dict") else "{}"
        )

        async def _do_write():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.raw_messages (
                            chat_id, message_id, sender_id, message_type,
                            has_media, media_path, file_unique_id, file_id,
                            is_edit, is_deleted,
                            forward_from_chat_id, forward_from_message_id,
                            reply_to_message_id, views,
                            collected_at, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, FALSE, FALSE,
                                %s, %s, %s, %s, NOW(), %s)
                        ON CONFLICT (chat_id, message_id) DO NOTHING
                        """,
                        (
                            chat_id,
                            message.id,
                            sender_id,
                            message_type,
                            has_media,
                            file_unique_id,
                            file_id,
                            forward_from_chat_id,
                            forward_from_message_id,
                            reply_to_message_id,
                            views,
                            payload,
                        ),
                    )

        await _retry_with_backoff(_do_write)

    async def _enqueue_media(self, message, chat_id: int) -> None:
        """LPUSH a media download task to collector:media_download_queue.

        Must only be called AFTER _write_message completes.
        """
        if self._redis is None:
            logger.warning(
                f"MediaStore unavailable; skipping media enqueue for "
                f"chat_id={chat_id} message_id={getattr(message, 'id', '?')}"
            )
            return

        file_unique_id, _file_id, ext = _extract_file_info(message)
        if file_unique_id is None:
            return  # No downloadable media

        file_size = 0
        media = getattr(message, "media", None)
        if media is not None:
            if hasattr(media, "document") and media.document:
                file_size = getattr(media.document, "size", 0) or 0
            elif hasattr(media, "photo") and media.photo:
                sizes = getattr(media.photo, "sizes", [])
                for sz in reversed(sizes):
                    if hasattr(sz, "size"):
                        file_size = sz.size
                        break

        task = {
            "task_type": "media_download",
            "chat_id": chat_id,
            "message_id": message.id,
            "file_unique_id": file_unique_id,
            "ext": ext or "bin",
            "file_size": file_size,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
            "_retry_count": 0,
        }

        try:
            await self._redis.lpush(MEDIA_QUEUE_KEY, json.dumps(task))
        except Exception as exc:
            logger.warning(
                f"BackfillWorker: failed to enqueue media for "
                f"chat_id={chat_id} message_id={message.id}: {exc}"
            )

    async def _upsert_user(self, user, chat_id: int) -> None:
        """INSERT/UPDATE collector.users."""
        payload = (
            json.dumps(user.to_dict(), default=_tg_json) if hasattr(user, "to_dict") else "{}"
        )

        async def _do_upsert():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.users (
                            id, username, first_name, last_name, phone, bio,
                            is_bot, is_verified, is_premium, is_scam, is_fake,
                            first_seen, last_seen, payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                NOW(), NOW(), %s)
                        ON CONFLICT (id) DO UPDATE SET
                            username    = EXCLUDED.username,
                            first_name  = EXCLUDED.first_name,
                            last_name   = EXCLUDED.last_name,
                            phone       = COALESCE(EXCLUDED.phone,
                                                   collector.users.phone),
                            bio         = COALESCE(EXCLUDED.bio,
                                                   collector.users.bio),
                            is_verified = EXCLUDED.is_verified,
                            is_premium  = EXCLUDED.is_premium,
                            is_scam     = EXCLUDED.is_scam,
                            is_fake     = EXCLUDED.is_fake,
                            last_seen   = NOW(),
                            payload     = EXCLUDED.payload
                        """,
                        (
                            user.id,
                            getattr(user, "username", None),
                            getattr(user, "first_name", None),
                            getattr(user, "last_name", None),
                            getattr(user, "phone", None),
                            getattr(user, "bio", None),
                            bool(getattr(user, "bot", False)),
                            bool(getattr(user, "verified", False)),
                            bool(getattr(user, "premium", False)),
                            bool(getattr(user, "scam", False)),
                            bool(getattr(user, "fake", False)),
                            payload,
                        ),
                    )

        await _retry_with_backoff(_do_upsert)

    async def _write_user_sighting(
        self, user_id: int, chat_id: int, payload: dict
    ) -> None:
        """INSERT into collector.user_sightings."""
        payload_json = (
            json.dumps(payload, default=_tg_json) if isinstance(payload, dict) else payload
        )

        async def _do_write():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.user_sightings
                            (user_id, seen_in_chat_id, seen_at, payload)
                        VALUES (%s, %s, NOW(), %s)
                        """,
                        (user_id, chat_id, payload_json),
                    )

        await _retry_with_backoff(_do_write)

    async def _update_backfill_state(
        self, chat_id: int, account_id: int, **fields
    ) -> None:
        """UPSERT collector.backfill_state with the given fields."""
        if not fields:
            return

        set_clauses = []
        values = []

        for key, value in fields.items():
            if value == "NOW()":
                set_clauses.append(f"{key} = NOW()")
            else:
                set_clauses.append(f"{key} = %s")
                values.append(value)

        set_clauses.append("updated_at = NOW()")
        set_sql = ", ".join(set_clauses)

        async def _do_update():
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        INSERT INTO collector.backfill_state
                            (chat_id, account_id, poll_type, updated_at)
                        VALUES (%s, %s, 'backfill', NOW())
                        ON CONFLICT (chat_id, account_id, poll_type) DO UPDATE SET
                            {set_sql}
                        """,
                        [chat_id, account_id] + values,
                    )

        await _retry_with_backoff(_do_update)

    async def _get_or_create_backfill_state(
        self, chat_id: int, account_id: int
    ) -> int:
        """Return last_processed_message_id for resume, or 0 if new.

        Also sets started_at if this is a fresh start (no existing row).
        """
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT last_processed_message_id, started_at
                    FROM collector.backfill_state
                    WHERE chat_id = %s AND account_id = %s AND poll_type = 'backfill'
                    """,
                    (chat_id, account_id),
                )
                row = await cur.fetchone()

        if row is None:
            # Create new state row
            await self._update_backfill_state(
                chat_id,
                account_id,
                status="in_progress",
                started_at="NOW()",
            )
            return 0

        last_id = row[0] or 0
        started_at = row[1]

        # Update status to in_progress; set started_at only if not already set
        if started_at is None:
            await self._update_backfill_state(
                chat_id,
                account_id,
                status="in_progress",
                started_at="NOW()",
            )
        else:
            await self._update_backfill_state(
                chat_id, account_id, status="in_progress"
            )

        return last_id

    # ------------------------------------------------------------------
    # Job status helpers
    # ------------------------------------------------------------------

    async def _mark_job_pending(self, job: dict) -> None:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE collector.backfill_jobs SET status = 'pending', error = NULL, updated_at = NOW() WHERE id = %s",
                    (job["id"],),
                )

    async def _mark_job_in_progress(self, job: dict) -> None:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE collector.backfill_jobs
                    SET status = 'in_progress', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (job["id"],),
                )

    async def _mark_job_completed(self, job: dict) -> None:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE collector.backfill_jobs
                    SET status = 'completed', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (job["id"],),
                )

    async def _mark_job_failed(self, job: dict, error: str) -> None:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE collector.backfill_jobs
                    SET status = 'failed', error = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (error, job["id"]),
                )

    # ------------------------------------------------------------------
    # FloodWait handling
    # ------------------------------------------------------------------

    async def _handle_flood_wait(self, exc: Exception, account_id: int) -> None:
        """Handle FloodWaitError: wait seconds + 10, coordinate with rate_limiter."""
        seconds = getattr(exc, "seconds", 0)
        wait_seconds = seconds + 10
        logger.warning(
            f"BackfillWorker: FloodWait for account_id={account_id}: "
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

class _MinimalUser:
    """Minimal user-like object when only sender_id is available."""

    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.username = None
        self.first_name = None
        self.last_name = None
        self.phone = None
        self.bio = None
        self.bot = False
        self.verified = False
        self.premium = False
        self.scam = False
        self.fake = False

    def to_dict(self) -> dict:
        return {"id": self.id}


def _is_flood_wait(exc: Exception) -> bool:
    """Return True if exc is a FloodWaitError (checked by class name to avoid import)."""
    return type(exc).__name__ == "FloodWaitError" or hasattr(exc, "seconds") and "flood" in type(exc).__name__.lower()


def _detect_message_type(message) -> str:
    """Return the message type string based on message media attributes."""
    if getattr(message, "photo", None) is not None:
        return "photo"
    video = getattr(message, "video", None)
    if video is not None:
        if getattr(video, "round_message", False):
            return "circle_video"
        return "video"
    if getattr(message, "audio", None) is not None:
        return "audio"
    if getattr(message, "voice", None) is not None:
        return "voice"
    if getattr(message, "document", None) is not None:
        return "document"
    if getattr(message, "sticker", None) is not None:
        return "sticker"
    if getattr(message, "poll", None) is not None:
        return "poll"
    if (
        getattr(message, "geo", None) is not None
        or getattr(message, "geo_live", None) is not None
    ):
        return "location"
    if getattr(message, "contact", None) is not None:
        return "contact"
    if getattr(message, "action", None) is not None:
        return "service"
    return "text"


def _extract_file_info(message) -> tuple:
    """Return (file_unique_id, None, ext) from message media.

    Uses Telethon-native object IDs (photo.id / document.id) as the
    file_unique_id dedup key.  file_id is always None — MediaStore re-fetches
    the message for the actual download to avoid file-reference expiry.
    """
    photo = getattr(message, "photo", None)
    if photo is not None:
        fuid = getattr(photo, "id", None)
        return (str(fuid) if fuid is not None else None, None, "jpg")

    video = getattr(message, "video", None)
    if video is not None:
        ext = _ext_from_mime(getattr(video, "mime_type", None)) or "mp4"
        fuid = getattr(video, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)

    audio = getattr(message, "audio", None)
    if audio is not None:
        ext = _ext_from_mime(getattr(audio, "mime_type", None)) or "mp3"
        fuid = getattr(audio, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)

    voice = getattr(message, "voice", None)
    if voice is not None:
        ext = _ext_from_mime(getattr(voice, "mime_type", None)) or "ogg"
        fuid = getattr(voice, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)

    sticker = getattr(message, "sticker", None)
    if sticker is not None:
        mime = getattr(sticker, "mime_type", "") or ""
        ext = "tgs" if "tgsticker" in mime else "webp"
        fuid = getattr(sticker, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)

    document = getattr(message, "document", None)
    if document is not None:
        mime = getattr(document, "mime_type", None)
        ext = _ext_from_mime(mime)
        if not ext:
            for attr in getattr(document, "attributes", []):
                fname = getattr(attr, "file_name", None)
                if fname and "." in fname:
                    ext = fname.rsplit(".", 1)[-1].lower()
                    break
        ext = ext or "bin"
        fuid = getattr(document, "id", None)
        return (str(fuid) if fuid is not None else None, None, ext)

    return (None, None, None)


def _ext_from_mime(mime_type: str | None) -> str | None:
    """Map common MIME types to file extensions."""
    if not mime_type:
        return None
    _map = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/mpeg": "mpeg",
        "video/quicktime": "mov",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "application/pdf": "pdf",
    }
    return _map.get(mime_type)


async def _retry_with_backoff(func, max_attempts: int = 3, base_delay: float = 1.0):
    """Retry an async callable with exponential backoff."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await func()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    f"DB retry {attempt + 1}/{max_attempts} after {delay}s: {exc}"
                )
                await asyncio.sleep(delay)
    raise last_exc
