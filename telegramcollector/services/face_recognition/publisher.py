"""
Publisher — uploads matched media to the Telegram Hub Group forum topics.

Responsibilities:
- Dedup check before upload (uploaded_media table)
- Ensure Telegram forum topic exists for each identity (telegram_topics table)
- Upload media files to Hub Group via BotPool
- Record processed_media after all topics are handled

No imports from collector/. All Telegram I/O via bot_pool.get_bot().
"""
import asyncio
import logging
import os
from typing import List

import asyncpg

from shared.bot_pool import BotPool
from shared.config import get_hub_group_id, settings

logger = logging.getLogger(__name__)

# Retry backoff delays in seconds (3 attempts: 5s, 15s, 45s)
_RETRY_DELAYS = [5, 15, 45]


class Publisher:
    def __init__(self, db_pool: asyncpg.Pool, bot_pool: BotPool) -> None:
        """
        db_pool:  asyncpg pool (face_recog_user)
        bot_pool: initialized BotPool with FACE_BOT_TOKENS
        """
        self._db_pool = db_pool
        self._bot_pool = bot_pool

    # ------------------------------------------------------------------
    # Task 6.2 — dedup helpers
    # ------------------------------------------------------------------

    async def _is_already_uploaded(
        self,
        source_chat_id: int,
        source_message_id: int,
        topic_id: int,
    ) -> bool:
        """
        Returns True if a row already exists in uploaded_media for this
        (source_chat_id, source_message_id, topic_id) triple.
        Requirements: 8.2
        """
        async with self._db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 1
                  FROM face_recognition.uploaded_media
                 WHERE source_chat_id    = $1
                   AND source_message_id = $2
                   AND topic_id          = $3
                """,
                source_chat_id,
                source_message_id,
                topic_id,
            )
        return row is not None

    async def _record_processed_media(
        self,
        file_unique_id: str,
        media_type: str,
        faces_found: int,
        topics_matched: List[int],
    ) -> None:
        """
        INSERT into processed_media ON CONFLICT DO NOTHING.
        topics_matched is stored as a TEXT[] array of topic db ids.
        Requirements: 8.4, 8.5
        """
        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO face_recognition.processed_media
                    (file_unique_id, media_type, faces_found, topics_matched, processed_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (file_unique_id) DO NOTHING
                """,
                file_unique_id,
                media_type,
                faces_found,
                [str(t) for t in topics_matched],
            )

    # ------------------------------------------------------------------
    # Task 6.3 — ensure Telegram forum topic exists
    # ------------------------------------------------------------------

    async def _ensure_topic_exists(self, db_topic_id: int) -> int:
        """
        Checks telegram_topics for db_topic_id.
        If topic_id (Telegram forum topic ID) is NULL or 0, creates the forum
        topic in the Hub Group and updates the DB row.
        Retries up to 3 times with 5s / 15s / 45s backoff.
        Returns the Telegram forum topic ID.
        Requirements: 7.2, 7.3
        """
        async with self._db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT topic_id, label FROM face_recognition.telegram_topics WHERE id = $1",
                db_topic_id,
            )

        if row is None:
            raise ValueError(f"telegram_topics row not found for id={db_topic_id}")

        telegram_topic_id = row["topic_id"]
        if telegram_topic_id and telegram_topic_id != 0:
            return telegram_topic_id

        # Need to create the Telegram forum topic
        label = row["label"] or "Unknown Person"
        hub_id = get_hub_group_id()
        if hub_id is None:
            raise RuntimeError("HUB_GROUP_ID is not configured or not yet resolved")

        last_exc = None
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            try:
                bot = self._bot_pool.get_bot()
                input_channel = await bot.client.get_input_entity(hub_id)
                result = await bot.client(
                    _CreateForumTopicRequest(input_channel, label)
                )
                # CreateForumTopicRequest returns an Updates object.
                # The new topic ID is the message ID of the service message
                # in result.updates (UpdateNewChannelMessage) or result.messages.
                new_telegram_topic_id = None
                for upd in getattr(result, "updates", []):
                    msg = getattr(upd, "message", None)
                    if msg is not None and getattr(msg, "id", None):
                        new_telegram_topic_id = msg.id
                        break
                if new_telegram_topic_id is None and getattr(result, "messages", None):
                    new_telegram_topic_id = result.messages[0].id
                if new_telegram_topic_id is None:
                    raise RuntimeError(
                        f"Could not extract topic_id from CreateForumTopic result: {result}"
                    )

                async with self._db_pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE face_recognition.telegram_topics
                           SET topic_id   = $1,
                               updated_at = NOW()
                         WHERE id = $2
                        """,
                        new_telegram_topic_id,
                        db_topic_id,
                    )

                logger.info(
                    "Created Telegram forum topic %d for db_topic_id=%d (label=%r)",
                    new_telegram_topic_id,
                    db_topic_id,
                    label,
                )
                return new_telegram_topic_id

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Attempt %d/%d to create forum topic for db_topic_id=%d failed: %s",
                    attempt,
                    len(_RETRY_DELAYS),
                    db_topic_id,
                    exc,
                )
                if attempt < len(_RETRY_DELAYS):
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"Failed to create Telegram forum topic for db_topic_id={db_topic_id} "
            f"after {len(_RETRY_DELAYS)} attempts"
        ) from last_exc

    # ------------------------------------------------------------------
    # Task 6.4 — upload media to a Hub Group forum topic
    # ------------------------------------------------------------------

    async def _upload_to_topic(self, media_path: str, topic_id: int) -> int:
        """
        Uploads the file at media_path to the Hub Group forum topic identified
        by topic_id (Telegram forum topic ID).

        FloodWait handling:
          - Marks the offending bot locked via bot_pool.mark_locked
          - Immediately retries with the next available bot

        All-bots-locked handling:
          - Waits with exponential backoff (5s, 15s, 45s, …) until a bot is free

        Non-FloodWait errors:
          - Retried up to 3 times with 5s / 15s / 45s backoff; then re-raised

        Returns hub_message_id (Telegram message ID of the uploaded message).
        Requirements: 9.2, 9.3, 9.4
        """
        from telethon.errors import FloodWaitError

        hub_id = get_hub_group_id()
        if hub_id is None:
            raise RuntimeError("HUB_GROUP_ID is not configured or not yet resolved")

        non_flood_attempts = 0
        backoff_index = 0
        all_bots_locked_delays = [5, 15, 45, 90, 180]

        while True:
            # --- Try to get a healthy bot ---
            try:
                bot = self._bot_pool.get_bot()
            except RuntimeError:
                # All bots locked — wait with exponential backoff
                wait = all_bots_locked_delays[
                    min(backoff_index, len(all_bots_locked_delays) - 1)
                ]
                logger.warning(
                    "All bots locked; waiting %ds before retry (topic_id=%d)",
                    wait,
                    topic_id,
                )
                await asyncio.sleep(wait)
                backoff_index += 1
                continue

            # --- Attempt the upload ---
            try:
                message = await bot.client.send_file(
                    hub_id,
                    media_path,
                    reply_to=topic_id,
                )
                hub_message_id: int = message.id
                logger.debug(
                    "Uploaded %s to topic %d → hub_message_id=%d",
                    os.path.basename(media_path),
                    topic_id,
                    hub_message_id,
                )
                return hub_message_id

            except FloodWaitError as fwe:
                duration = fwe.seconds
                logger.warning(
                    "FloodWait %ds on bot %r; marking locked and retrying with next bot",
                    duration,
                    bot.name,
                )
                self._bot_pool.mark_locked(bot.name, duration)
                # Immediately retry with next bot (no sleep here)
                continue

            except Exception as exc:
                non_flood_attempts += 1
                if non_flood_attempts > len(_RETRY_DELAYS):
                    raise

                delay = _RETRY_DELAYS[non_flood_attempts - 1]
                logger.warning(
                    "Upload attempt %d/%d failed for topic_id=%d: %s — retrying in %ds",
                    non_flood_attempts,
                    len(_RETRY_DELAYS),
                    topic_id,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Task 6.5 — orchestrate per-message face publishing
    # ------------------------------------------------------------------

    async def process_message_faces(
        self,
        message: dict,
        topic_ids: List[int],
    ) -> None:
        """
        For each distinct topic_id in topic_ids:
          1. _is_already_uploaded → skip if True
          2. _ensure_topic_exists → creates Telegram forum topic if needed
          3. _upload_to_topic → upload media file
          4. _record_uploaded_media → INSERT into uploaded_media

        After all topics: _record_processed_media.

        Requirements: 8.1, 8.3, 8.6
        """
        source_chat_id: int = message["source_chat_id"]
        source_message_id: int = message["id"]
        media_path: str = message["media_path"]
        file_unique_id: str = message["file_unique_id"]
        media_type: str = message["message_type"]

        distinct_topic_ids = list(dict.fromkeys(topic_ids))  # preserve order, deduplicate
        matched_topics: List[int] = []

        for db_topic_id in distinct_topic_ids:
            # 1. Dedup check
            already = await self._is_already_uploaded(
                source_chat_id, source_message_id, db_topic_id
            )
            if already:
                logger.debug(
                    "Skipping upload: already uploaded (chat=%d, msg=%d, topic=%d)",
                    source_chat_id,
                    source_message_id,
                    db_topic_id,
                )
                matched_topics.append(db_topic_id)
                continue

            # 2. Ensure Telegram forum topic exists
            telegram_topic_id = await self._ensure_topic_exists(db_topic_id)

            # 3. Upload media
            hub_message_id = await self._upload_to_topic(media_path, telegram_topic_id)

            # 4. Record the upload
            await self._record_uploaded_media(
                source_chat_id,
                source_message_id,
                db_topic_id,
                hub_message_id,
            )
            matched_topics.append(db_topic_id)

        # After all topics: record processed_media
        await self._record_processed_media(
            file_unique_id,
            media_type,
            faces_found=len(distinct_topic_ids),
            topics_matched=matched_topics,
        )

    # ------------------------------------------------------------------
    # Internal helper — record a single upload
    # ------------------------------------------------------------------

    async def _record_uploaded_media(
        self,
        source_chat_id: int,
        source_message_id: int,
        topic_id: int,
        hub_message_id: int,
    ) -> None:
        """
        INSERT into uploaded_media ON CONFLICT DO NOTHING.
        Requirements: 8.3
        """
        async with self._db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO face_recognition.uploaded_media
                    (source_chat_id, source_message_id, topic_id, hub_message_id, created_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (source_chat_id, source_message_id, topic_id) DO NOTHING
                """,
                source_chat_id,
                source_message_id,
                topic_id,
                hub_message_id,
            )


# ---------------------------------------------------------------------------
# Telethon helper — CreateForumTopic request
# We import lazily to avoid hard dependency at module load time.
# ---------------------------------------------------------------------------

def _CreateForumTopicRequest(peer, title: str):
    """
    Returns a Telethon CreateForumTopicRequest for the given peer and title.
    Imported lazily so the module can be imported without Telethon installed
    (e.g. in unit tests that mock the bot client).
    """
    from telethon.tl.functions.channels import CreateForumTopicRequest
    return CreateForumTopicRequest(channel=peer, title=title)
