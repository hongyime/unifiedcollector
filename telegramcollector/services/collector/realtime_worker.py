"""Realtime ingestion worker — registers Telethon event handlers on all connected
TelegramClientManager instances and writes raw data to the collector schema.

Design constraints:
- _write_raw_message is called BEFORE _enqueue_media_download — always
- media_path = NULL on every INSERT to raw_messages
- ON CONFLICT (chat_id, message_id) DO NOTHING — silent dedup
- Hub Group messages are discarded (event.chat_id == hub_group_id)
- Each event handler is wrapped in try/except Exception
- Uses database.get_db_connection() for all DB writes
- Uses redis.asyncio for LPUSH to collector:media_download_queue
"""

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING


def _tg_json(obj):
    """JSON default handler for Telethon to_dict() output.
    Handles bytes (access hashes), datetime, and any other non-serializable types.
    """
    from datetime import datetime, date
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)  # fallback — never raise, just stringify

import redis.asyncio as aioredis
from telethon import events

from shared.database import get_db_connection
from shared.config import settings, get_hub_group_id, resolve_hub_group_id
from shared.hub_notifier import increment_stat

if TYPE_CHECKING:
    from services.collector.rate_limiter import RateLimiter
    from shared.telegram_client import TelegramClientManager

logger = logging.getLogger(__name__)

MEDIA_QUEUE_KEY = "collector:media_download_queue"


class RealtimeWorker:
    """Event-driven ingestion worker.

    Registers Telethon event handlers on every TelegramClientManager and
    writes raw data to the collector.* schema.  Media download tasks are
    enqueued to Redis *after* the DB write completes.
    """

    def __init__(self, clients: list, rate_limiter) -> None:
        self.clients = clients
        self.rate_limiter = rate_limiter
        self._redis: aioredis.Redis | None = None
        self._hub_group_id: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Redis, resolve hub_group_id, register handlers on all clients."""
        # Build Redis URL from settings
        password_part = (
            f":{settings.REDIS_PASSWORD}@" if settings.REDIS_PASSWORD else ""
        )
        redis_url = (
            f"redis://{password_part}{settings.REDIS_HOST}:{settings.REDIS_PORT}"
            f"/{settings.REDIS_DB}"
        )
        self._redis = aioredis.from_url(redis_url, decode_responses=False)

        # Resolve hub group id — try static first, then async resolution
        self._hub_group_id = get_hub_group_id()
        if self._hub_group_id is None and self.clients:
            try:
                self._hub_group_id = await resolve_hub_group_id(
                    self.clients[0].client
                )
            except Exception as exc:
                logger.warning(f"Could not resolve hub_group_id: {exc}")

        # Register handlers on every client
        for manager in self.clients:
            self._register_handlers(manager)

        logger.info(
            f"RealtimeWorker started: {len(self.clients)} client(s), "
            f"hub_group_id={self._hub_group_id}"
        )

    async def stop(self) -> None:
        """Close Redis connection.

        Note: Telethon does not expose a simple 'remove all handlers' API.
        CollectorMain disconnects the clients, which implicitly stops event
        delivery.
        """
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.warning(f"Error closing Redis connection: {exc}")
            self._redis = None
        logger.info("RealtimeWorker stopped")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self, manager: "TelegramClientManager") -> None:
        """Register all Telethon event handlers on manager.client."""
        client = manager.client

        client.add_event_handler(self._on_new_message, events.NewMessage())
        client.add_event_handler(self._on_message_edited, events.MessageEdited())
        client.add_event_handler(self._on_message_deleted, events.MessageDeleted())
        client.add_event_handler(self._on_chat_action, events.ChatAction())
        client.add_event_handler(self._on_user_update, events.UserUpdate())

        logger.debug(f"Registered event handlers on client {manager.session_name}")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Handle incoming new messages."""
        try:
            chat_id = event.chat_id

            # Discard Hub Group messages
            if self._hub_group_id is not None and chat_id == self._hub_group_id:
                return

            message = event.message

            # 1. Write raw message row first (media_path = NULL)
            await self._write_raw_message(message, chat_id)
            await increment_stat('messages_scanned')

            # 2. Upsert sender + sighting
            sender = await event.get_sender()
            if sender is not None:
                await self._upsert_user(sender, chat_id)
                await self._write_user_sighting(
                    sender.id,
                    chat_id,
                    sender.to_dict() if hasattr(sender, "to_dict") else {},
                )

            # 3. Enqueue media download AFTER DB write
            if message.media is not None:
                await self._enqueue_media_download(message, chat_id)

        except Exception as exc:
            chat_id_str = getattr(event, "chat_id", "unknown")
            msg_id_str = getattr(getattr(event, "message", None), "id", "unknown")
            logger.error(
                f"_on_new_message error chat_id={chat_id_str} "
                f"message_id={msg_id_str}: {exc}",
                exc_info=True,
            )

    async def _on_message_edited(self, event: events.MessageEdited.Event) -> None:
        """Handle edited messages."""
        try:
            chat_id = event.chat_id
            message = event.message

            # Also update raw_messages with the edit (upsert via DO NOTHING keeps
            # original row; we record the edit separately)
            await self._write_raw_message(message, chat_id)

            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    payload = (
                        json.dumps(message.to_dict(), default=_tg_json)
                        if hasattr(message, "to_dict")
                        else "{}"
                    )
                    await cur.execute(
                        """
                        INSERT INTO collector.message_edits
                            (chat_id, message_id, edited_at, payload)
                        VALUES (%s, %s, NOW(), %s)
                        ON CONFLICT (chat_id, message_id) DO NOTHING
                        """,
                        (chat_id, message.id, payload),
                    )

        except Exception as exc:
            chat_id_str = getattr(event, "chat_id", "unknown")
            msg_id_str = getattr(getattr(event, "message", None), "id", "unknown")
            logger.error(
                f"_on_message_edited error chat_id={chat_id_str} "
                f"message_id={msg_id_str}: {exc}",
                exc_info=True,
            )

    async def _on_message_deleted(self, event: events.MessageDeleted.Event) -> None:
        """Handle deleted messages — one row per deleted message_id."""
        try:
            chat_id = event.chat_id
            deleted_ids = event.deleted_ids or []

            if not deleted_ids:
                return

            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    for msg_id in deleted_ids:
                        await cur.execute(
                            """
                            INSERT INTO collector.message_deletions
                                (chat_id, message_id, deleted_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (chat_id, message_id) DO NOTHING
                            """,
                            (chat_id, msg_id),
                        )

        except Exception as exc:
            chat_id_str = getattr(event, "chat_id", "unknown")
            logger.error(
                f"_on_message_deleted error chat_id={chat_id_str}: {exc}",
                exc_info=True,
            )

    async def _on_chat_action(self, event: events.ChatAction.Event) -> None:
        """Handle chat actions — upsert chats and chat_members."""
        try:
            chat_id = event.chat_id

            # Determine role from action type
            role = "member"
            if event.user_kicked:
                role = "banned"
            elif event.user_left:
                role = "left"
            elif event.user_joined or event.user_added:
                role = "member"

            # Get the affected user(s)
            user_ids: list[int] = []
            try:
                if event.user_id:
                    user_ids.append(event.user_id)
            except Exception:
                pass

            if not user_ids:
                return

            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    for user_id in user_ids:
                        await cur.execute(
                            """
                            INSERT INTO collector.chat_members
                                (chat_id, user_id, role, joined_at, seen_at)
                            VALUES (%s, %s, %s, NOW(), NOW())
                            ON CONFLICT (chat_id, user_id) DO UPDATE SET
                                role = EXCLUDED.role,
                                left_at = CASE
                                    WHEN EXCLUDED.role IN ('banned', 'left')
                                    THEN NOW()
                                    ELSE collector.chat_members.left_at
                                END,
                                seen_at = NOW()
                            """,
                            (chat_id, user_id, role),
                        )

        except Exception as exc:
            chat_id_str = getattr(event, "chat_id", "unknown")
            logger.error(
                f"_on_chat_action error chat_id={chat_id_str}: {exc}",
                exc_info=True,
            )

    async def _on_user_update(self, event: events.UserUpdate.Event) -> None:
        """Handle user updates — upsert users and write sightings."""
        try:
            user = await event.get_user()
            if user is None:
                return

            chat_id = getattr(event, "chat_id", None)
            await self._upsert_user(user, chat_id)

            if chat_id is not None:
                await self._write_user_sighting(
                    user.id,
                    chat_id,
                    user.to_dict() if hasattr(user, "to_dict") else {},
                )

        except Exception as exc:
            logger.error(f"_on_user_update error: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # DB write helpers
    # ------------------------------------------------------------------

    async def _write_raw_message(self, message, chat_id: int) -> None:
        """INSERT into collector.raw_messages with media_path = NULL.

        ON CONFLICT (chat_id, message_id) DO NOTHING — silent dedup.
        """
        message_type = self._detect_message_type(message)
        file_unique_id, file_id, _ext = self._extract_file_info(message)
        has_media = message.media is not None

        # Forward info
        forward_from_chat_id = None
        forward_from_message_id = None
        fwd = getattr(message, "fwd_from", None)
        if fwd is not None:
            forward_from_chat_id = getattr(
                getattr(fwd, "from_id", None), "channel_id", None
            )
            forward_from_message_id = getattr(fwd, "channel_post", None)

        # Reply info
        reply_to_message_id = None
        reply = getattr(message, "reply_to", None)
        if reply is not None:
            reply_to_message_id = getattr(reply, "reply_to_msg_id", None)

        views = getattr(message, "views", None)
        sender_id = getattr(message, "sender_id", None)

        payload = (
            json.dumps(message.to_dict(), default=_tg_json) if hasattr(message, "to_dict") else "{}"
        )

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

    async def _upsert_user(self, user, chat_id: int | None = None) -> None:
        """INSERT/UPDATE collector.users."""
        payload = (
            json.dumps(user.to_dict(), default=_tg_json) if hasattr(user, "to_dict") else "{}"
        )

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

    async def _write_user_sighting(
        self, user_id: int, chat_id: int, payload: dict
    ) -> None:
        """INSERT into collector.user_sightings (no conflict handling)."""
        payload_json = (
            json.dumps(payload, default=_tg_json) if isinstance(payload, dict) else payload
        )

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

    # ------------------------------------------------------------------
    # Media enqueue
    # ------------------------------------------------------------------

    async def _enqueue_media_download(self, message, chat_id: int) -> None:
        """LPUSH a media download task to collector:media_download_queue.

        Must only be called AFTER _write_raw_message completes.
        """
        file_unique_id, _file_id, ext = self._extract_file_info(message)
        if file_unique_id is None:
            return  # No downloadable media (webpage preview, poll, etc.)

        # Determine file size for the oversized-skip guard in MediaStore
        file_size = 0
        media = message.media
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

        if self._redis is not None:
            await self._redis.lpush(MEDIA_QUEUE_KEY, json.dumps(task))
        else:
            logger.warning(
                f"Redis not connected; dropping media task for "
                f"chat_id={chat_id} message_id={message.id}"
            )

    # ------------------------------------------------------------------
    # Message type detection
    # ------------------------------------------------------------------

    def _detect_message_type(self, message) -> str:
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

    # ------------------------------------------------------------------
    # File info extraction
    # ------------------------------------------------------------------

    def _extract_file_info(self, message) -> tuple:
        """Return (file_unique_id, None, ext) from message media.

        file_unique_id is derived from the Telethon-native object ID (photo.id
        or document.id), which is stable and unique across Telegram.  We no
        longer store the Bot-API file_id string because Telethon objects don't
        expose it; the MediaStore re-fetches the message for actual download.

        Returns (None, None, None) if the message has no downloadable media.
        """
        # Photo — Telethon Photo object; use photo.id as stable dedup key
        photo = getattr(message, "photo", None)
        if photo is not None:
            fuid = getattr(photo, "id", None)
            return (str(fuid) if fuid is not None else None, None, "jpg")

        # Document-based types: video, audio, voice, sticker, document.
        # Telethon's shortcut properties (.video, .audio, etc.) all return the
        # underlying Document object, so document.id is the stable dedup key.
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


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _ext_from_mime(mime_type: str | None) -> str | None:
    """Return a file extension (no dot) for a MIME type, or None."""
    if not mime_type:
        return None
    _MIME_MAP = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "video/mp4": "mp4",
        "video/mpeg": "mpeg",
        "video/quicktime": "mov",
        "video/webm": "webm",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/ogg": "ogg",
        "audio/opus": "opus",
        "audio/aac": "aac",
        "audio/flac": "flac",
        "application/pdf": "pdf",
        "application/zip": "zip",
        "application/x-tgsticker": "tgs",
        "image/vnd.djvu": "djvu",
        "text/plain": "txt",
    }
    return _MIME_MAP.get(mime_type.lower())
