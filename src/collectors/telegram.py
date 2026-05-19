import asyncio
import json
import logging
import os
import time
from collections import deque
from enum import Enum
from pathlib import Path

from src.core.base_collector import BaseCollector
from src.core.account_pool import AccountPool
from src.core.bot_pool import BotPool
from src.core.hub_notifier import HubNotifier, NotifyCategory

logger = logging.getLogger(__name__)


class SessionState(Enum):
    INIT = "init"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FLOOD_WAIT = "flood_wait"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class TelegramCollector(BaseCollector):
    SOURCE_NAME = "telegram"

    def __init__(self):
        super().__init__()
        self._api_id = os.getenv("TELEGRAM_API_ID", "")
        self._api_hash = os.getenv("TELEGRAM_API_HASH", "")
        self._session_name = os.getenv("TELEGRAM_SESSION", "collector")
        self._client = None
        self._state = SessionState.INIT
        self._sem = asyncio.Semaphore(3)
        self._batch_size = int(os.getenv("TELEGRAM_BATCH_SIZE", "100"))
        self._max_media_size = int(os.getenv("TELEGRAM_MAX_MEDIA_SIZE_MB", "50")) * 1024 * 1024
        self._backfill_enabled = os.getenv("TELEGRAM_BACKFILL_ENABLED", "true").lower() == "true"
        self._backfill_msg_per_sec = float(os.getenv("TELEGRAM_BACKFILL_MSG_PER_SEC", "20.0"))
        self._story_enabled = os.getenv("TELEGRAM_STORY_SCAN_ENABLED", "true").lower() == "true"
        self._story_interval = int(os.getenv("TELEGRAM_STORY_SCAN_INTERVAL", "300"))

        self.account_pool = AccountPool(
            default_cooldown=600.0,
            error_cooldown=1800.0,
            max_consecutive_errors=3,
        )
        self._load_accounts()

        self._bot_pool = BotPool()
        bot_tokens = os.getenv("TELEGRAM_BOT_TOKENS", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_tokens:
            self._bot_pool.load_from_env(bot_tokens)

        self._hub_notifier = HubNotifier(
            hub_group=os.getenv("TELEGRAM_HUB_GROUP", ""),
        )

        self._admin_log_enabled = os.getenv("TELEGRAM_ADMIN_LOG_ENABLED", "true").lower() == "true"
        self._admin_log_interval = int(os.getenv("TELEGRAM_ADMIN_LOG_INTERVAL", "300"))
        self._group_join_enabled = os.getenv("TELEGRAM_GROUP_JOIN_ENABLED", "false").lower() == "true"
        self._max_joins_per_hour = int(os.getenv("TELEGRAM_MAX_JOINS_PER_HOUR", "5"))
        self._join_min_delay = float(os.getenv("TELEGRAM_JOIN_MIN_DELAY", "30"))
        self._join_timestamps: deque[float] = deque()

    def _load_accounts(self):
        i = 1
        while True:
            name = os.getenv(f"TELEGRAM_ACCOUNT_{i}_NAME", "")
            if not name:
                break
            api_id = os.getenv(f"TELEGRAM_ACCOUNT_{i}_API_ID", self._api_id)
            api_hash = os.getenv(f"TELEGRAM_ACCOUNT_{i}_API_HASH", self._api_hash)
            phone = os.getenv(f"TELEGRAM_ACCOUNT_{i}_PHONE", "")
            session = os.getenv(f"TELEGRAM_ACCOUNT_{i}_SESSION", "")
            self.account_pool.add_account(name, {
                "api_id": api_id,
                "api_hash": api_hash,
                "phone": phone,
                "session": session,
            })
            i += 1

    async def _ensure_client(self):
        if self._client is not None and self._state == SessionState.CONNECTED:
            return

        self._state = SessionState.CONNECTING
        from telethon import TelegramClient

        session_dir = self.media_dir / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)

        account = self.account_pool.get_next()
        if account:
            api_id = int(account.credentials.get("api_id", self._api_id))
            api_hash = account.credentials.get("api_hash", self._api_hash)
            session_path = account.credentials.get("session", "")
            if session_path and Path(session_path).exists():
                session_file = str(session_path)
            else:
                session_file = str(session_dir / account.name)
            logger.info("Connecting Telegram as %s", account.name)
        else:
            api_id = int(self._api_id)
            api_hash = self._api_hash
            session_file = str(session_dir / self._session_name)

        try:
            self._client = TelegramClient(session_file, api_id, api_hash)
            await self._client.start()
            self._state = SessionState.CONNECTED
            logger.info("Telegram client connected (state=%s)", self._state.value)
        except Exception as e:
            self._state = SessionState.ERROR
            if account:
                self.account_pool.record_error(account.name)
            raise

    async def collect(self, targets: list[str]):
        if not self._api_id or not self._api_hash:
            logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
            return

        await self._ensure_client()
        self._hub_notifier.set_client(self._client)
        await self._hub_notifier.start()
        await self._bot_pool.start_health_monitor()

        self._hub_notifier.notify(
            NotifyCategory.COLLECTION_START,
            f"Starting collection of {len(targets)} targets",
        )

        collected = 0
        for target in targets:
            if self._stop.is_set():
                break
            logger.info("Collecting telegram/%s", target)
            try:
                await self._collect_chat(target)
                await self.checkpoint.save_progress(target)
                collected += 1

                if self._admin_log_enabled:
                    try:
                        entity = await self._client.get_entity(int(target))
                    except ValueError:
                        entity = await self._client.get_entity(target)
                    await self._poll_admin_logs(entity)

            except Exception as e:
                if "FloodWaitError" in type(e).__name__:
                    self._hub_notifier.notify(
                        NotifyCategory.RATE_LIMIT,
                        f"FloodWait on {target}: {e}",
                    )
                    await self._handle_flood_wait(e)
                else:
                    logger.error("Failed telegram/%s: %s", target, e)
                    self._hub_notifier.notify(NotifyCategory.ERROR, f"{target}: {e}")
                    await self.send_to_dlq(target, target, str(e))

        if self._story_enabled:
            await self._scan_stories(targets)

        if self._group_join_enabled:
            await self._process_join_queue()

        self._hub_notifier.notify(
            NotifyCategory.COLLECTION_COMPLETE,
            f"Completed {collected}/{len(targets)} targets",
            immediate=True,
        )

    async def _handle_flood_wait(self, error):
        wait_seconds = getattr(error, "seconds", 60)
        self._state = SessionState.FLOOD_WAIT
        logger.warning("FloodWait: sleeping %ds (state=%s)", wait_seconds, self._state.value)

        account = self.account_pool.get_next()
        if account:
            self.account_pool.cooldown(account.name, float(wait_seconds))

        await asyncio.sleep(min(wait_seconds, 300))
        self._state = SessionState.CONNECTED

    async def _collect_chat(self, target: str):
        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

        try:
            entity = await self._client.get_entity(int(target))
        except ValueError:
            entity = await self._client.get_entity(target)

        chat_id = str(entity.id)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id

        await self._collect_profile_photo(entity, chat_id, chat_name)

        last_id = self.checkpoint.get_last_id()
        min_id = int(last_id) if last_id else 0

        count = 0
        delay = 1.0 / self._backfill_msg_per_sec if self._backfill_enabled and min_id == 0 else 0

        async for message in self._client.iter_messages(entity, min_id=min_id, limit=None):
            if self._stop.is_set():
                break
            if not message.media:
                continue

            await self.wait_rate_limit("telegram.org")

            if isinstance(message.media, MessageMediaPhoto):
                await self._handle_photo(message, chat_id, chat_name)
                count += 1
            elif isinstance(message.media, MessageMediaDocument):
                doc = message.media.document
                if not doc:
                    continue
                mime = getattr(doc, "mime_type", "")
                size = getattr(doc, "size", 0) or 0
                if size > self._max_media_size:
                    logger.debug("Skipping large media %d bytes (max %d)", size, self._max_media_size)
                    continue
                if mime.startswith(("image/", "video/")):
                    await self._handle_document(message, chat_id, chat_name, mime)
                    count += 1

            if delay > 0:
                await asyncio.sleep(delay)

            if count % self._batch_size == 0 and count > 0:
                await self.checkpoint.save_progress(str(message.id))
                logger.info("telegram/%s: processed %d media messages", chat_name, count)

        if count > 0:
            logger.info("telegram/%s: total %d media items", chat_name, count)

    async def _collect_profile_photo(self, entity, chat_id: str, chat_name: str):
        cid = f"profile_{chat_id}"
        if self.is_known(cid):
            return
        try:
            photo = await self._client.download_profile_photo(entity, bytes)
            if photo:
                await self.download_media({
                    "entity_id": chat_id,
                    "entity_name": chat_name,
                    "content_type": "profile_photo",
                    "content_id": cid,
                    "data": photo,
                    "extension": "jpg",
                })
        except Exception as e:
            logger.debug("Profile photo download failed for %s: %s", chat_name, e)

    async def _scan_stories(self, targets: list[str]):
        try:
            from telethon.tl.functions.stories import GetPeerStoriesRequest
            for target in targets:
                if self._stop.is_set():
                    break
                try:
                    entity = await self._client.get_entity(int(target))
                except ValueError:
                    entity = await self._client.get_entity(target)

                chat_id = str(entity.id)
                chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id

                try:
                    result = await self._client(GetPeerStoriesRequest(peer=entity))
                    stories = getattr(result, "stories", None)
                    if not stories:
                        continue
                    story_items = getattr(stories, "stories", [])
                    for story in story_items:
                        if self._stop.is_set():
                            break
                        story_id = getattr(story, "id", None)
                        if not story_id:
                            continue
                        cid = f"story_{chat_id}_{story_id}"
                        if self.is_known(cid):
                            continue
                        media = getattr(story, "media", None)
                        if media:
                            data = await self._client.download_media(media, bytes)
                            if data:
                                is_video = hasattr(media, "document")
                                await self.download_media({
                                    "entity_id": chat_id,
                                    "entity_name": chat_name,
                                    "content_type": "story_video" if is_video else "story",
                                    "content_id": cid,
                                    "data": data,
                                    "extension": "mp4" if is_video else "jpg",
                                })
                except Exception as e:
                    logger.debug("Story scan failed for %s: %s", chat_name, e)
        except ImportError:
            logger.debug("Telethon version does not support stories API")

    async def _handle_photo(self, message, chat_id: str, chat_name: str):
        cid = str(message.id)
        if self.is_known(cid):
            return
        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": "photo",
            "content_id": cid,
            "message": message,
            "extension": "jpg",
        })

    async def _handle_document(self, message, chat_id: str, chat_name: str, mime: str):
        cid = str(message.id)
        if self.is_known(cid):
            return
        if mime.startswith("video/"):
            ext = "mp4"
            content_type = "video"
        else:
            ext = mime.split("/")[-1].replace("jpeg", "jpg")
            content_type = "photo"

        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": content_type,
            "content_id": cid,
            "message": message,
            "extension": ext,
        })

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            entity_id=item["entity_id"],
            entity_name=item["entity_name"],
            content_type=item["content_type"],
            content_id=cid,
            extension=item.get("extension", "jpg"),
        )

        dest = self.media_dir / filename
        if dest.exists():
            return

        try:
            if "data" in item:
                data = item["data"]
            elif "message" in item:
                async with self._sem:
                    data = await self._client.download_media(item["message"], bytes)
                if data is None:
                    return
            else:
                return

            sha = self.sha256_bytes(data)
            self.save_file(data, filename)
            self.rate_limiter.record_success("telegram.org")
            self.circuit_breaker.record_success()

            await self.insert_media_item(
                entity_id=item["entity_id"],
                entity_name=item["entity_name"],
                content_type=item["content_type"],
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                sha256=sha,
            )
        except Exception as e:
            if "FloodWaitError" in type(e).__name__:
                raise
            self.rate_limiter.record_failure("telegram.org")
            self.circuit_breaker.record_failure()
            logger.error("Download failed msg %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _download_via_bot(self, message, chat_id: str, chat_name: str):
        bot = self._bot_pool.get_healthy_bot()
        if not bot or not bot.client:
            return None
        try:
            data = await bot.client.download_media(message, bytes)
            if data:
                self._bot_pool.record_success(bot.name)
            return data
        except Exception as e:
            self._bot_pool.record_error(bot.name)
            logger.debug("Bot %s download failed: %s", bot.name, e)
            return None

    async def _poll_admin_logs(self, entity):
        if not self._pool:
            return
        try:
            from telethon.tl.functions.channels import GetAdminLogRequest
            from telethon.tl.types import ChannelAdminLogEventsFilter

            result = await self._client(GetAdminLogRequest(
                channel=entity,
                q="",
                min_id=0,
                max_id=0,
                limit=100,
                events_filter=None,
                admins=None,
            ))

            chat_id = str(entity.id)
            for event in result.events:
                event_type = type(event.action).__name__
                actor_id = str(event.user_id) if event.user_id else ""

                detail = {}
                if hasattr(event.action, "prev_message"):
                    detail["prev_message"] = getattr(event.action.prev_message, "message", "")
                if hasattr(event.action, "new_message"):
                    detail["new_message"] = getattr(event.action.new_message, "message", "")
                if hasattr(event.action, "prev_participant"):
                    pp = event.action.prev_participant
                    detail["target_id"] = str(getattr(pp, "user_id", ""))

                try:
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO telegram_admin_events
                                (chat_id, event_type, actor_id, detail)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT DO NOTHING
                            """,
                            chat_id, event_type, actor_id, json.dumps(detail),
                        )
                except Exception as e:
                    logger.debug("Admin event store failed: %s", e)

        except ImportError:
            logger.debug("Admin log API not available in this Telethon version")
        except Exception as e:
            logger.debug("Admin log poll failed: %s", e)

    async def _process_join_queue(self):
        if not self._pool or not self._client:
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, link, link_type FROM telegram_group_joins
                    WHERE status = 'pending'
                    ORDER BY queued_at ASC LIMIT 20
                    """
                )

            now = time.time()
            while self._join_timestamps and (now - self._join_timestamps[0]) > 3600:
                self._join_timestamps.popleft()

            for row in rows:
                if self._stop.is_set():
                    break
                if len(self._join_timestamps) >= self._max_joins_per_hour:
                    logger.info("Join hourly limit reached (%d/%d)",
                                len(self._join_timestamps), self._max_joins_per_hour)
                    break

                link = row["link"]
                link_type = row["link_type"]
                row_id = row["id"]

                try:
                    await asyncio.sleep(self._join_min_delay)

                    if link_type == "invite":
                        from telethon.tl.functions.messages import ImportChatInviteRequest
                        hash_part = link.split("/")[-1].replace("+", "")
                        await self._client(ImportChatInviteRequest(hash_part))
                    else:
                        from telethon.tl.functions.channels import JoinChannelRequest
                        entity = await self._client.get_entity(link)
                        await self._client(JoinChannelRequest(entity))

                    self._join_timestamps.append(time.time())
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE telegram_group_joins SET status = 'joined' WHERE id = $1",
                            row_id,
                        )
                    self._hub_notifier.notify(
                        NotifyCategory.DISCOVERY, f"Joined group: {link}"
                    )
                    logger.info("Joined group: %s", link)

                except Exception as e:
                    logger.error("Failed to join %s: %s", link, e)
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE telegram_group_joins SET status = 'error' WHERE id = $1",
                            row_id,
                        )
                    if "FloodWaitError" in type(e).__name__:
                        break

        except Exception as e:
            logger.debug("Join queue processing failed: %s", e)

    async def cleanup(self):
        await self._hub_notifier.stop()
        await self._bot_pool.stop_health_monitor()
        if self._client:
            self._state = SessionState.DISCONNECTED
            await self._client.disconnect()
            self._client = None
