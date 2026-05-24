"""
message_scanner.py — Per-account message scanner for the worker.py local runtime.

MessageScanner: cursor-based historical backfill (one client, one account).
RealtimeScanner: event-driven real-time monitor (one client, one account).

The Docker deployment uses service-level equivalents in services/collector/.
"""
import asyncio
import logging
from typing import List, Optional

from telethon import events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaWebPage

from shared.database import get_db_connection
from shared.config import get_hub_group_id

logger = logging.getLogger(__name__)


def _classify_media(message) -> tuple:
    """Return (has_media, message_type, file_unique_id)."""
    media = message.media
    if media is None:
        return False, 'text', None
    if isinstance(media, MessageMediaPhoto):
        photo = media.photo
        fuid = getattr(photo, 'file_unique_id', None) or str(getattr(photo, 'id', ''))
        return True, 'photo', fuid
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        attrs = getattr(doc, 'attributes', [])
        is_round = any(getattr(a, 'round_message', False) for a in attrs)
        is_video = any(hasattr(a, 'duration') for a in attrs)
        fuid = getattr(doc, 'file_unique_id', None) or str(getattr(doc, 'id', ''))
        if is_round:
            return True, 'circle_video', fuid
        if is_video:
            return True, 'video', fuid
        return True, 'document', fuid
    if isinstance(media, MessageMediaWebPage):
        return False, 'text', None
    return True, 'other', None


def _chat_type(entity) -> str:
    name = type(entity).__name__
    if 'Channel' in name:
        return 'group' if (getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False)) else 'channel'
    if 'Chat' in name:
        return 'group'
    return 'personal'


class MessageScanner:
    """Per-account historical backfill scanner."""

    def __init__(self, client, media_manager, processing_queue):
        self.client = client
        self.media_manager = media_manager
        self.processing_queue = processing_queue

    async def discover_and_scan_all_chats(self, account_id: int) -> None:
        hub_id = get_hub_group_id()
        discovered = 0
        try:
            async for dialog in self.client.iter_dialogs(limit=None):
                entity = dialog.entity
                chat_id = dialog.id
                if hub_id and abs(chat_id) == abs(hub_id):
                    continue
                ct = _chat_type(entity)
                title = (
                    getattr(entity, 'title', None)
                    or getattr(entity, 'first_name', None)
                    or str(chat_id)
                )
                try:
                    async with get_db_connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("""
                                INSERT INTO collector.chats (id, type, title, username, collected_at)
                                VALUES (%s, %s, %s, %s, NOW())
                                ON CONFLICT (id) DO UPDATE
                                    SET title = EXCLUDED.title,
                                        username = EXCLUDED.username,
                                        collected_at = NOW()
                            """, (chat_id, ct, title[:255], getattr(entity, 'username', None)))
                            await cur.execute("""
                                INSERT INTO collector.scan_checkpoints
                                    (account_id, chat_id, chat_type, scan_mode, is_complete, last_updated)
                                VALUES (%s, %s, %s, 'backfill', FALSE, NOW())
                                ON CONFLICT (account_id, chat_id) DO NOTHING
                            """, (account_id, chat_id, ct))
                    discovered += 1
                except Exception as exc:
                    logger.warning("Failed to register chat %d: %s", chat_id, exc)
        except Exception as exc:
            logger.error("Account %d: dialog discovery failed: %s", account_id, exc)
        logger.info("Account %d: discovered %d chats", account_id, discovered)

    async def scan_chat_backfill(self, account_id: int, chat_id: int) -> None:
        hub_id = get_hub_group_id()
        if hub_id and abs(chat_id) == abs(hub_id):
            return

        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT last_processed_message_id, is_complete
                    FROM collector.scan_checkpoints
                    WHERE account_id = %s AND chat_id = %s
                """, (account_id, chat_id))
                row = await cur.fetchone()

        if row and row[1]:
            logger.debug("Chat %d already fully scanned (account %d)", chat_id, account_id)
            return

        last_processed = (row[0] or 0) if row else 0
        count = 0
        min_seen: Optional[int] = None

        try:
            async for message in self.client.iter_messages(chat_id, limit=None, min_id=last_processed):
                if message is None or message.id == 0:
                    continue
                has_media, msg_type, fuid = _classify_media(message)
                sender_id = (
                    getattr(message.sender, 'id', None)
                    if message.sender
                    else message.sender_id
                )
                try:
                    async with get_db_connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("""
                                INSERT INTO collector.raw_messages
                                    (chat_id, message_id, sender_id, message_type,
                                     has_media, file_unique_id, collected_at)
                                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                                ON CONFLICT (chat_id, message_id) DO NOTHING
                            """, (chat_id, message.id, sender_id, msg_type, has_media, fuid))
                    count += 1
                    min_seen = min(min_seen, message.id) if min_seen is not None else message.id
                except Exception as exc:
                    logger.warning("Failed to store message %d in chat %d: %s", message.id, chat_id, exc)
        except Exception as exc:
            logger.error("Chat %d backfill failed (account %d): %s", chat_id, account_id, exc)

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO collector.scan_checkpoints
                            (account_id, chat_id, last_processed_message_id, is_complete, last_updated)
                        VALUES (%s, %s, %s, TRUE, NOW())
                        ON CONFLICT (account_id, chat_id) DO UPDATE
                            SET last_processed_message_id = EXCLUDED.last_processed_message_id,
                                is_complete = TRUE,
                                last_updated = NOW()
                    """, (account_id, chat_id, min_seen or last_processed))
        except Exception as exc:
            logger.warning("Failed to update checkpoint for chat %d: %s", chat_id, exc)

        logger.info("Chat %d: stored %d messages (account %d)", chat_id, count, account_id)


class RealtimeScanner:
    """Per-account real-time message monitor."""

    def __init__(self, client, media_manager, processing_queue):
        self.client = client
        self.media_manager = media_manager
        self.processing_queue = processing_queue
        self._handler = None
        self._running = False

    async def start_monitoring(self, chat_ids: List[int], account_id: int) -> None:
        if self._running:
            return
        self._running = True
        hub_id = get_hub_group_id()
        watched = set(chat_ids) - ({hub_id, -hub_id} if hub_id else set())
        if not watched:
            logger.warning("Account %d: no chats to monitor", account_id)
            return

        async def _on_new_message(event):
            if event.chat_id not in watched:
                return
            msg = event.message
            has_media, msg_type, fuid = _classify_media(msg)
            sender_id = (
                getattr(msg.sender, 'id', None) if msg.sender else msg.sender_id
            )
            try:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("""
                            INSERT INTO collector.raw_messages
                                (chat_id, message_id, sender_id, message_type,
                                 has_media, file_unique_id, collected_at)
                            VALUES (%s, %s, %s, %s, %s, %s, NOW())
                            ON CONFLICT (chat_id, message_id) DO NOTHING
                        """, (event.chat_id, msg.id, sender_id, msg_type, has_media, fuid))
            except Exception as exc:
                logger.warning("Failed to store realtime message %d: %s", msg.id, exc)

        self._handler = _on_new_message
        self.client.add_event_handler(_on_new_message, events.NewMessage(chats=list(watched)))
        logger.info("Account %d: real-time monitoring started (%d chats)", account_id, len(watched))

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        if self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:
                pass
            self._handler = None
