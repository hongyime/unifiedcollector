"""Unified Telegram collector — Wave 2 Batch E.

Ports `telegramcollector/services/collector/{backfill_worker,realtime_worker,...}.py`
+ cherry-picks `telegramtoolkit/src/{core/scan_targets,managers/download_profile_photos,
managers/processors/user_analyzer_processor}.py` into a single BaseCollector subclass.

Public surface (called by scheduler / cron):
    - run(targets)          BaseCollector lifecycle entry
    - collect(targets)      per-cycle parallel collect across worker pool
    - collect_realtime()    spawn @client.on(NewMessage) handlers + run forever
    - backfill_chat(chat_id, target_depth=N, max_iterations=M)
                            cursor-based historical pagination (newest -> oldest)
    - collect_dialogs()     iter_dialogs across all workers; upsert telegram_chats
    - collect_chat_members(chat_id)
                            iter_participants → telegram_chat_members upsert
                            (called by daily 03:00 SGT cron — see PRD memory)
    - collect_user_profile(user_id)
                            user metadata + profile photos
    - download_message_media(message_id)
                            single-message media download via Telethon → core.media_download

Anything outbound (send/reply/edit/delete/forward, bot commands, web UI,
bulk_sender) is DROPPED per Wave 0 spec.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from datetime import datetime, date, timezone
from enum import Enum
from pathlib import Path

from src.core.base_collector import BaseCollector
from src.core.account_pool import AccountPool
from src.core.bot_pool import BotPool
from src.core.hub_notifier import HubNotifier, NotifyCategory
from src.core.file_naming import sanitize_name
from src.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.core.user_change_tracker import (
    UserChangeTracker,
    TELEGRAM_TRACKED_FIELDS,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# JSON helpers — Telethon to_dict() emits bytes (access hashes) + datetime.
# ──────────────────────────────────────────────────────────────────────────


def _tg_json(obj):
    """JSON default for Telethon objects.

    Handles bytes (access hashes), datetime, and any other non-serializable
    types — never raises so message ingest never fails on serialization.
    """
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


_MIME_EXT_MAP = {
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


def _ext_from_mime(mime_type):
    if not mime_type:
        return None
    return _MIME_EXT_MAP.get(mime_type.lower())


def _is_flood_wait(exc):
    """Detect FloodWaitError without importing telethon at module scope."""
    name = type(exc).__name__
    if name == "FloodWaitError":
        return True
    return hasattr(exc, "seconds") and "flood" in name.lower()


# ──────────────────────────────────────────────────────────────────────────
# Worker
# ──────────────────────────────────────────────────────────────────────────


class SessionState(Enum):
    INIT = "init"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FLOOD_WAIT = "flood_wait"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class TelegramWorker:
    """Per-account Telegram worker. Owns its own TelegramClient + session."""

    def __init__(self, parent: "TelegramCollector", account, worker_id: int):
        self.parent = parent
        self.account = account
        self.worker_id = worker_id
        self.client = None
        self.account_id = getattr(account, "id", None)  # for backfill rate-limit
        self.state = SessionState.INIT
        self._claimed_chats: set[str] = set()  # chats this worker is assigned
        # Per-account circuit breaker: trips after 5 consecutive Telethon
        # errors and stays open for 60s before allowing a single probe.
        # FloodWaitError is handled separately by record_flood_wait, so
        # excluding it from the breaker prevents legitimate rate-limits
        # from tripping the circuit.
        self.breaker = CircuitBreaker(
            name=f"telegram[{account.name}]",
            failure_threshold=5,
            recovery_timeout=60.0,
        )

    async def connect(self):
        from telethon.sync import TelegramClient
        session_dir = Path("sessions")
        session_dir.mkdir(parents=True, exist_ok=True)

        api_id = int(self.account.credentials.get("api_id") or os.getenv("TELEGRAM_API_ID", "0"))
        api_hash = self.account.credentials.get("api_hash") or os.getenv("TELEGRAM_API_HASH", "")
        session_path = self.account.credentials.get("session", "")
        if session_path and Path(session_path).exists():
            session_file = str(session_path)
        else:
            session_file = str(session_dir / self.account.name)

        logger.info(
            "[worker=%d account=%s] Connecting Telegram (session=%s)",
            self.worker_id, self.account.name, session_file,
        )
        try:
            self.state = SessionState.CONNECTING
            self.client = TelegramClient(session_file, api_id, api_hash)
            await self.client.start()
            self.state = SessionState.CONNECTED
            try:
                me = await self.client.get_me()
                me_label = f"id={getattr(me, 'id', '?')} user={getattr(me, 'username', None)} phone={getattr(me, 'phone', None)}"
            except Exception:
                me_label = "<unknown>"
            logger.info(
                "[worker=%d account=%s] Telegram client CONNECTED (%s)",
                self.worker_id, self.account.name, me_label,
            )
        except Exception as e:
            self.state = SessionState.ERROR
            err_text = str(e).lower()
            if "auth" in err_text or "session" in err_text or "phone" in err_text or "key" in err_text:
                kind = "auth_failure"
            elif "timeout" in err_text:
                kind = "timeout"
            else:
                kind = "network_error"
            self.parent.account_pool.record_error_classified(self.account.name, kind)
            logger.error(
                "[worker=%d account=%s] Connect failed (%s): %s",
                self.worker_id, self.account.name, kind, e,
            )
            raise

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.state = SessionState.DISCONNECTED
            self.client = None

    async def run_targets(self, targets: list[str]):
        """Process the list of targets assigned to this worker."""
        for target in targets:
            if self.parent._stop.is_set():
                break
            logger.info(
                "[worker=%d account=%s] Collecting telegram/%s",
                self.worker_id, self.account.name, target,
            )
            try:
                await self.breaker.call(
                    lambda t=target: self.parent._collect_chat(self, t)
                )
                await self.parent.checkpoint.save_progress(target)
                self.parent.account_pool.record_success(self.account.name)
            except CircuitOpenError as e:
                logger.warning(
                    "[worker=%d account=%s] circuit open, skipping %s: %s",
                    self.worker_id, self.account.name, target, e,
                )
                try:
                    await self.parent.send_to_dlq(target, target, f"circuit_open: {e}")
                except Exception:
                    pass
            except Exception as e:
                if _is_flood_wait(e):
                    await self.parent._handle_flood_wait(self, e)
                else:
                    err_text = str(e).lower()
                    if "auth" in err_text or "unauthorized" in err_text or "session" in err_text:
                        kind = "auth_failure"
                    elif "timeout" in err_text:
                        kind = "timeout"
                    elif "privat" in err_text or "forbidden" in err_text or "channel_private" in err_text:
                        kind = "privacy_error"
                    else:
                        kind = "network_error"
                    self.parent.account_pool.record_error_classified(self.account.name, kind)
                    logger.error(
                        "[worker=%d account=%s] Failed telegram/%s (%s): %s",
                        self.worker_id, self.account.name, target, kind, e,
                    )
                    try:
                        await self.parent.send_to_dlq(target, target, str(e))
                    except Exception:
                        pass


# ──────────────────────────────────────────────────────────────────────────
# Collector
# ──────────────────────────────────────────────────────────────────────────


class TelegramCollector(BaseCollector):
    SOURCE_NAME = "telegram"

    def __init__(self):
        super().__init__()
        self._api_id = os.getenv("TELEGRAM_API_ID", "")
        self._api_hash = os.getenv("TELEGRAM_API_HASH", "")
        self._session_name = os.getenv("TELEGRAM_SESSION", "collector")
        self._workers: list[TelegramWorker] = []
        self._primary_client = None  # for HubNotifier/BotPool/etc that expect a single client
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
        self._hub_notifier = HubNotifier()
        self._join_timestamps: deque = deque()
        self._max_joins_per_hour = int(os.getenv("TELEGRAM_MAX_JOINS_PER_HOUR", "5"))
        self._join_min_delay = int(os.getenv("TELEGRAM_JOIN_MIN_DELAY", "30"))
        self._admin_log_enabled = os.getenv("TELEGRAM_POLL_ADMIN_LOGS", "true").lower() == "true"
        self._group_join_enabled = os.getenv("TELEGRAM_GROUP_JOIN_ENABLED", "true").lower() == "true"

        # Realtime listener state — populated by collect_realtime()
        self._realtime_running = False
        self._hub_group_id: int | None = None

    def _load_accounts(self):
        self.account_pool.load_from_env("TELEGRAM", ["NAME", "API_ID", "API_HASH", "SESSION", "PHONE"])

    @property
    def account_media_dir(self) -> Path:
        # Use session name for isolation (kept for backward compat).
        acc_name = sanitize_name(self._session_name)
        path = self.media_dir / f"session_{acc_name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _account_media_dir_for(self, worker: "TelegramWorker") -> Path:
        acc_name = sanitize_name(worker.account.name)
        path = self.media_dir / f"session_{acc_name}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    async def _spawn_workers(self) -> list[TelegramWorker]:
        """Connect one TelegramClient per loaded account, in parallel."""
        accounts = list(self.account_pool._accounts)  # snapshot
        if not accounts:
            logger.error("No Telegram accounts in pool — cannot start workers")
            return []

        workers = [TelegramWorker(self, acc, idx) for idx, acc in enumerate(accounts)]
        results = await asyncio.gather(
            *(w.connect() for w in workers),
            return_exceptions=True,
        )
        live: list[TelegramWorker] = []
        for w, r in zip(workers, results):
            if isinstance(r, Exception):
                logger.error(
                    "[worker=%d account=%s] failed to connect: %s",
                    w.worker_id, w.account.name, r,
                )
                continue
            live.append(w)

        if live:
            self._primary_client = live[0].client
        logger.info(
            "Telegram parallel mode: %d/%d worker(s) connected",
            len(live), len(workers),
        )
        return live

    def _dispatch(self, targets: list[str], num_workers: int) -> list[list[str]]:
        """Hash-bucket targets so each chat is owned by exactly one worker.

        Even if multiple accounts are members of the same chat, only one
        worker (one account) will scrape it per cycle — shared-channel dedup.
        """
        buckets: list[list[str]] = [[] for _ in range(num_workers)]
        for t in targets:
            idx = (hash(t) & 0x7FFFFFFF) % num_workers
            buckets[idx].append(t)
        for i, b in enumerate(buckets):
            logger.info("[dispatch] worker=%d -> %d target(s)", i, len(b))
        return buckets

    # ------------------------------------------------------------------
    # Top-level collect (parallel)
    # ------------------------------------------------------------------

    async def collect(self, targets: list[str]):
        if not self._api_id or not self._api_hash:
            logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
            return

        self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("No Telegram workers connected — aborting cycle")
            return

        # HubNotifier + BotPool keyed off the primary client (first worker).
        self._hub_notifier.set_client(self._primary_client)
        await self._hub_notifier.start()
        await self._bot_pool.start_health_monitor()

        self._hub_notifier.notify(
            NotifyCategory.COLLECTION_START,
            f"Starting collection of {len(targets)} targets across {len(self._workers)} accounts",
        )

        # Dispatch targets to workers (hash-based, shared-chat aware).
        buckets = self._dispatch(targets, len(self._workers))
        for w, bucket in zip(self._workers, buckets):
            w._claimed_chats = set(bucket)

        # Run all workers concurrently — true parallelism.
        results = await asyncio.gather(
            *(w.run_targets(bucket) for w, bucket in zip(self._workers, buckets)),
            return_exceptions=True,
        )
        for w, r in zip(self._workers, results):
            if isinstance(r, Exception):
                logger.error(
                    "[worker=%d account=%s] worker crashed: %s",
                    w.worker_id, w.account.name, r,
                )

        # Spider queue: process under primary worker only (avoids cross-worker contention).
        if os.getenv("TELEGRAM_SPIDER_ENABLED", "true").lower() == "true":
            try:
                await self._process_spider_queue(self._workers[0])
            except Exception as e:
                logger.error("Spider queue processing failed: %s", e)

        if self._story_enabled:
            try:
                await self._scan_stories(self._workers[0], targets)
            except Exception as e:
                logger.error("Story scan failed: %s", e)

        if self._group_join_enabled:
            try:
                await self._process_join_queue()
            except Exception as e:
                logger.error("Join queue failed: %s", e)

    async def _process_spider_queue(self, worker: "TelegramWorker"):
        """Process telegram_spider_queue jobs using the given worker's client."""
        while not self._stop.is_set():
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    UPDATE telegram_spider_queue
                    SET status = 'processing'
                    WHERE id = (
                        SELECT id FROM telegram_spider_queue
                        WHERE status = 'pending'
                        ORDER BY priority ASC, collected_at ASC
                        LIMIT 1
                    )
                    RETURNING platform_chat_id, title
                """)
            if not row:
                break
            try:
                await self._collect_chat(worker, row['platform_chat_id'])
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET status = 'completed' WHERE platform_chat_id = $1",
                        row['platform_chat_id'],
                    )
            except Exception:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE telegram_spider_queue SET status = 'failed' WHERE platform_chat_id = $1",
                        row['platform_chat_id'],
                    )

    async def _handle_flood_wait(self, worker: "TelegramWorker", error):
        wait_seconds = getattr(error, "seconds", 60)
        worker.state = SessionState.FLOOD_WAIT
        logger.warning(
            "[worker=%d account=%s] FloodWait: sleeping %ds",
            worker.worker_id, worker.account.name, wait_seconds,
        )
        # record_flood_wait classifies the error AND sets cooldown.
        # Use the actual flood-wait seconds so the pool doesn't release the
        # account until Telegram lets us back in.
        self.account_pool.record_flood_wait(worker.account.name, float(wait_seconds))
        # Sleep at least until the flood-wait elapses (capped to 5min so we
        # don't block the worker on truly long bans — those are surfaced by
        # is_available() and the next cycle skips this acct).
        await asyncio.sleep(min(wait_seconds, 300))
        worker.state = SessionState.CONNECTED

    # ------------------------------------------------------------------
    # Per-chat collection (now takes a worker arg)
    # ------------------------------------------------------------------

    async def _collect_chat(self, worker: "TelegramWorker", target: str):
        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

        client = worker.client
        try:
            entity = await client.get_entity(int(target))
        except ValueError:
            entity = await client.get_entity(target)

        chat_id = str(entity.id)
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id

        await self._upsert_chat(entity)
        await self._collect_profile_photo(worker, entity, chat_id, chat_name)

        last_id = self.checkpoint.last_processed_id
        try:
            min_id = int(last_id) if last_id else 0
        except (TypeError, ValueError):
            min_id = 0
        count = 0

        async for message in client.iter_messages(entity, min_id=min_id, limit=None):
            if self._stop.is_set():
                break

            await self.wait_rate_limit("telegram.org")

            sender_uuid = None
            if message.sender_id:
                sender_uuid = await self._upsert_sender(worker, message.sender_id)

            await self._upsert_message(message, chat_id, sender_uuid)

            if message.media:
                if isinstance(message.media, MessageMediaPhoto):
                    await self._handle_photo(worker, message, chat_id, chat_name)
                    count += 1
                elif isinstance(message.media, MessageMediaDocument):
                    doc = message.media.document
                    if doc and (getattr(doc, "size", 0) or 0) <= self._max_media_size:
                        mime = getattr(doc, "mime_type", "")
                        if mime.startswith(("image/", "video/")):
                            await self._handle_document(worker, message, chat_id, chat_name, mime)
                            count += 1

            if count % self._batch_size == 0 and count > 0:
                await self.checkpoint.save_progress(str(message.id))

        if count > 0:
            logger.info(
                "[worker=%d account=%s] telegram/%s: finished processing %d media items",
                worker.worker_id, worker.account.name, chat_name, count,
            )

    async def _upsert_chat(self, entity):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO telegram_chats (platform_chat_id, title, username, type, description, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (platform_chat_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    username = EXCLUDED.username,
                    type = EXCLUDED.type,
                    description = EXCLUDED.description,
                    updated_at = NOW()
            """,
            str(entity.id),
            getattr(entity, 'title', None),
            getattr(entity, 'username', None),
            'channel' if getattr(entity, 'broadcast', False) else 'group',
            getattr(entity, 'about', None)
            )

    async def _upsert_sender(self, worker: "TelegramWorker", platform_user_id) -> str | None:
        try:
            user = await worker.client.get_entity(platform_user_id)
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO telegram_users (platform_user_id, username, first_name, last_name, updated_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        updated_at = NOW()
                    RETURNING id
                """,
                str(user.id), user.username, user.first_name, user.last_name
                )
                return row['id']
        except Exception:
            return None

    async def _upsert_message(self, message, chat_id, sender_uuid):
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow("SELECT id FROM telegram_chats WHERE platform_chat_id = $1", str(chat_id))
            chat_uuid = chat_row['id'] if chat_row else None

            media_type = None
            if message.photo: media_type = 'photo'
            elif message.video: media_type = 'video'
            elif message.voice: media_type = 'voice'

            # Namespace message ID by chat to avoid global unique-constraint collisions
            # (different Telegram chats reuse low message IDs starting from 1).
            platform_msg_id = f"{chat_id}:{message.id}"
            await conn.execute("""
                INSERT INTO telegram_messages (
                    platform_message_id, chat_id, sender_id, text, caption,
                    media_type, platform_created_at, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (platform_message_id) DO NOTHING
            """,
            platform_msg_id, chat_uuid, sender_uuid, message.message, getattr(message, 'caption', None),
            media_type, message.date, json.dumps(message.to_dict(), default=_tg_json)
            )

    async def _collect_profile_photo(self, worker: "TelegramWorker", entity, chat_id: str, chat_name: str):
        cid = f"profile_{chat_id}"
        if self.is_known(cid):
            return
        try:
            photo = await worker.client.download_profile_photo(entity, bytes)
            if photo:
                await self.download_media({
                    "entity_id": chat_id,
                    "entity_name": chat_name,
                    "content_type": "profile_photo",
                    "content_id": cid,
                    "data": photo,
                    "extension": "jpg",
                }, worker=worker)
        except Exception as e:
            logger.debug("Profile photo download failed for %s: %s", chat_name, e)

    async def download_media(self, item: dict, worker: "TelegramWorker | None" = None):
        cid = item["content_id"]
        if self.is_known(cid):
            return

        filename = self.build_filename(
            item["entity_id"], item["entity_name"],
            item["content_type"], cid, extension=item.get("extension", "jpg")
        )

        # Use worker's per-account dir if provided, else fall back to legacy session dir.
        if worker is not None:
            base_dir = self._account_media_dir_for(worker)
        else:
            base_dir = self.account_media_dir
        dest_dir = base_dir / item["content_type"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        try:
            if "data" in item:
                data = item["data"]
            else:
                client = worker.client if worker else self._primary_client
                data = await client.download_media(item["media"], bytes)

            if not data:
                return

            sha = self.sha256_bytes(data)

            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)

            metadata = {
                "entity_id": item["entity_id"],
                "entity_name": item["entity_name"],
                "content_type": item["content_type"],
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": item.get("raw", {})
            }
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")

            await self.insert_media_item(
                entity_id=item["entity_id"], entity_name=item["entity_name"],
                content_type=item["content_type"], content_id=cid,
                filename=filename, file_path=str(dest),
                file_size=len(data), sha256=sha, metadata=metadata
            )
            self._known_ids.add(cid)
        except Exception as e:
            if _is_flood_wait(e):
                raise
            logger.error("Download failed %s: %s", cid, e)
            await self.send_to_dlq(item["entity_id"], cid, str(e))

    async def _handle_photo(self, worker: "TelegramWorker", message, chat_id: str, chat_name: str):
        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": "photo",
            "content_id": str(message.id),
            "media": message.media.photo,
            "raw": message.to_dict()
        }, worker=worker)

    async def _handle_document(self, worker: "TelegramWorker", message, chat_id: str, chat_name: str, mime: str):
        ext = mime.split("/")[-1]
        content_type = "video" if mime.startswith("video/") else "document"
        await self.download_media({
            "entity_id": chat_id,
            "entity_name": chat_name,
            "content_type": content_type,
            "content_id": str(message.id),
            "media": message.media.document,
            "extension": ext,
            "raw": message.to_dict()
        }, worker=worker)

    async def _scan_stories(self, worker: "TelegramWorker", targets: list[str]):
        try:
            from telethon.tl.functions.stories import GetPeerStoriesRequest
            client = worker.client
            for target in targets:
                if self._stop.is_set():
                    break
                try:
                    entity = await client.get_entity(int(target))
                except ValueError:
                    entity = await client.get_entity(target)

                chat_id = str(entity.id)
                chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id

                try:
                    result = await client(GetPeerStoriesRequest(peer=entity))
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
                            is_video = hasattr(media, "video")
                            await self.download_media({
                                "entity_id": chat_id,
                                "entity_name": chat_name,
                                "content_type": "story_video" if is_video else "story",
                                "content_id": cid,
                                "media": media,
                                "extension": "mp4" if is_video else "jpg",
                                "raw": story.to_dict()
                            }, worker=worker)
                except Exception as e:
                    logger.debug("Story fetch failed for %s: %s", chat_name, e)
        except ImportError:
            pass

    async def _poll_admin_logs(self, entity):
        # Placeholder — telegramcollector/services/collector/admin_log_poller.py
        # is not yet ported. Tracked in deferred plan.
        pass

    async def _process_join_queue(self):
        # Placeholder — telegramcollector/services/collector/group_manager.py
        # join queue is not yet ported. Tracked in deferred plan.
        pass

    # ==================================================================
    # Realtime ingestion — ported from
    # telegramcollector/services/collector/realtime_worker.py
    # ==================================================================

    async def collect_realtime(self):
        """Register Telethon event handlers on every connected worker and run forever.

        This is the @client.on(events.NewMessage) listener equivalent. New /
        edited / deleted messages and chat-action / user-update events are
        persisted to the unified telegram_* schema. Media is downloaded
        inline via download_message_media() rather than enqueued to Redis
        (the unified collector replaces the microservices' Redis queue).

        Runs until self._stop is set.
        """
        from telethon import events

        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("collect_realtime: no Telegram workers connected — bailing")
            return

        self._realtime_running = True
        for worker in self._workers:
            client = worker.client
            client.add_event_handler(
                lambda e, w=worker: self._on_new_message(w, e),
                events.NewMessage(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_message_edited(w, e),
                events.MessageEdited(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_message_deleted(w, e),
                events.MessageDeleted(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_chat_action(w, e),
                events.ChatAction(),
            )
            client.add_event_handler(
                lambda e, w=worker: self._on_user_update(w, e),
                events.UserUpdate(),
            )
            logger.info(
                "[worker=%d account=%s] realtime handlers registered",
                worker.worker_id, worker.account.name,
            )

        logger.info(
            "Realtime listener running across %d worker(s); awaiting events…",
            len(self._workers),
        )
        # Park until stop. Telethon delivers events under each client's own task.
        while self._realtime_running and not self._stop.is_set():
            await asyncio.sleep(1.0)

    async def _on_new_message(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            if self._hub_group_id is not None and chat_id == self._hub_group_id:
                return  # discard hub-group messages
            message = event.message
            await self._write_realtime_message(message, chat_id)
            sender = await event.get_sender()
            if sender is not None:
                await self._upsert_user_full(sender)
            if getattr(message, "media", None) is not None:
                # Download inline rather than queueing.
                try:
                    await self.download_message_media(message, worker=worker, chat_id=chat_id)
                except Exception as exc:
                    logger.debug("realtime media download failed: %s", exc)
        except Exception as exc:
            logger.error("_on_new_message error: %s", exc, exc_info=True)

    async def _on_message_edited(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            message = event.message
            await self._write_realtime_message(message, chat_id, is_edit=True)
        except Exception as exc:
            logger.error("_on_message_edited error: %s", exc, exc_info=True)

    async def _on_message_deleted(self, worker: "TelegramWorker", event):
        try:
            chat_id = event.chat_id
            for msg_id in (event.deleted_ids or []):
                async with self.pool.acquire() as conn:
                    # Mark the row deleted in metadata; ON CONFLICT NOTHING is
                    # fine because the row may not exist (deletion of a
                    # message we never saw).
                    await conn.execute("""
                        UPDATE telegram_messages
                        SET metadata = jsonb_set(
                                COALESCE(metadata, '{}'::jsonb),
                                '{deleted}', 'true'::jsonb, true
                            ),
                            updated_at = NOW()
                        WHERE platform_message_id = $1
                    """, f"{chat_id}:{msg_id}")
        except Exception as exc:
            logger.error("_on_message_deleted error: %s", exc, exc_info=True)

    async def _on_chat_action(self, worker: "TelegramWorker", event):
        """Translate Telethon chat actions into telegram_chat_members upserts."""
        try:
            chat_id = event.chat_id
            role = "member"
            if getattr(event, "user_kicked", False):
                role = "banned"
            elif getattr(event, "user_left", False):
                role = "left"
            user_ids: list[int] = []
            try:
                if getattr(event, "user_id", None):
                    user_ids.append(event.user_id)
            except Exception:
                pass
            if not user_ids:
                return
            async with self.pool.acquire() as conn:
                for user_id in user_ids:
                    await conn.execute("""
                        INSERT INTO telegram_chat_members
                            (chat_id, user_id, role, joined_at, last_seen_at, refreshed_at)
                        VALUES ($1, $2, $3, NOW(), NOW(), NOW())
                        ON CONFLICT (chat_id, user_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            last_seen_at = NOW(),
                            refreshed_at = NOW()
                    """, int(chat_id), int(user_id), role)
        except Exception as exc:
            logger.error("_on_chat_action error: %s", exc, exc_info=True)

    async def _on_user_update(self, worker: "TelegramWorker", event):
        try:
            user = await event.get_user()
            if user is not None:
                await self._upsert_user_full(user)
        except Exception as exc:
            logger.error("_on_user_update error: %s", exc, exc_info=True)

    async def _write_realtime_message(self, message, chat_id: int, is_edit: bool = False):
        """INSERT (or UPDATE-on-edit) the message into telegram_messages."""
        # Resolve UUIDs via the existing chat upsert chain. We don't have the
        # entity here so just key off platform_chat_id.
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow(
                "SELECT id FROM telegram_chats WHERE platform_chat_id = $1",
                str(chat_id),
            )
            chat_uuid = chat_row["id"] if chat_row else None

            sender_uuid = None
            sender_id = getattr(message, "sender_id", None)
            if sender_id is not None:
                user_row = await conn.fetchrow(
                    "SELECT id FROM telegram_users WHERE platform_user_id = $1",
                    str(sender_id),
                )
                sender_uuid = user_row["id"] if user_row else None

            media_type = self._detect_message_type(message)
            platform_msg_id = f"{chat_id}:{message.id}"
            payload_json = (
                json.dumps(message.to_dict(), default=_tg_json)
                if hasattr(message, "to_dict") else "{}"
            )

            if is_edit:
                # Update existing if present; else insert.
                await conn.execute("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (platform_message_id) DO UPDATE SET
                        text = EXCLUDED.text,
                        caption = EXCLUDED.caption,
                        metadata = EXCLUDED.metadata
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                )
            else:
                await conn.execute("""
                    INSERT INTO telegram_messages (
                        platform_message_id, chat_id, sender_id, text, caption,
                        media_type, platform_created_at, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (platform_message_id) DO NOTHING
                """,
                platform_msg_id, chat_uuid, sender_uuid,
                getattr(message, "message", None),
                getattr(message, "caption", None),
                media_type, message.date, payload_json,
                )

    async def _upsert_user_full(self, user):
        """Upsert with full Telethon user attributes (bot/verified/premium/etc)."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO telegram_users (
                        platform_user_id, username, first_name, last_name, updated_at
                    ) VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        updated_at = NOW()
                """,
                str(user.id),
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                )
        except Exception as exc:
            logger.debug("_upsert_user_full failed for %s: %s", getattr(user, "id", "?"), exc)

    def _detect_message_type(self, message) -> str:
        """Return message_type string — ported from realtime_worker."""
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

    def _extract_file_info(self, message) -> tuple:
        """Return (file_unique_id, None, ext) — ported from realtime_worker.

        file_unique_id derives from the Telethon-native object ID
        (photo.id or document.id), which is stable + unique across
        Telegram. Returns (None, None, None) if the message has no
        downloadable media.
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

    # ==================================================================
    # Backfill — ported from
    # telegramcollector/services/collector/backfill_worker.py
    # ==================================================================

    async def backfill_chat(
        self,
        chat_id,
        target_depth: int | None = None,
        max_iterations: int = 10000,
        worker: "TelegramWorker | None" = None,
    ):
        """Cursor-based historical backfill of messages in a chat.

        Walks newest -> oldest via Telethon ``iter_messages`` with ``max_id``
        pagination. Each batch of <=batch_size is persisted before advancing
        the cursor; FloodWait is absorbed via _handle_flood_wait. Bounded by
        ``target_depth`` (stop after N messages persisted) and
        ``max_iterations`` (safety: stop after M batches even if Telegram
        keeps streaming).

        Returns the count of messages written.
        """
        # Auto-spawn a worker if not given one.
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("backfill_chat: no Telegram workers — bailing")
                return 0
            worker = self._workers[0]

        client = worker.client
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            entity = await client.get_entity(chat_id)
        await self._upsert_chat(entity)

        chat_id_int = int(getattr(entity, "id"))
        chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id_int)
        batch_size = self._batch_size
        max_id = 0  # 0 means "from newest"
        written = 0
        iterations = 0

        logger.info(
            "backfill_chat: chat_id=%s name=%s target_depth=%s max_iter=%d",
            chat_id_int, chat_name, target_depth, max_iterations,
        )

        while iterations < max_iterations:
            if self._stop.is_set():
                break
            iterations += 1

            try:
                messages = []
                async for msg in client.iter_messages(
                    entity, limit=batch_size,
                    max_id=max_id if max_id > 0 else 0,
                    reverse=False,
                ):
                    messages.append(msg)
            except Exception as exc:
                if _is_flood_wait(exc):
                    await self._handle_flood_wait(worker, exc)
                    continue
                logger.error("backfill_chat: fetch failed: %s", exc)
                break

            if not messages:
                logger.info("backfill_chat: chat=%s reached end (no more messages)", chat_id_int)
                break

            for message in messages:
                try:
                    sender_uuid = None
                    if getattr(message, "sender_id", None):
                        sender_uuid = await self._upsert_sender(worker, message.sender_id)
                    await self._upsert_message(message, str(chat_id_int), sender_uuid)
                    written += 1
                except Exception as exc:
                    logger.warning(
                        "backfill_chat: failed write chat=%s msg=%s: %s",
                        chat_id_int, getattr(message, "id", "?"), exc,
                    )

            # Advance cursor — min ID in this batch is the next max_id.
            batch_ids = [m.id for m in messages if hasattr(m, "id")]
            if batch_ids:
                max_id = min(batch_ids)

            if target_depth is not None and written >= target_depth:
                logger.info(
                    "backfill_chat: chat=%s hit target_depth=%d (written=%d)",
                    chat_id_int, target_depth, written,
                )
                break

            if len(messages) < batch_size:
                # Partial batch → end of channel.
                break

        logger.info(
            "backfill_chat: chat=%s complete written=%d iterations=%d",
            chat_id_int, written, iterations,
        )
        return written

    # ==================================================================
    # Dialog enumeration — cherry-picked from
    # telegramtoolkit/src/core/scan_targets.py (iter_dialogs pattern)
    # ==================================================================

    async def collect_dialogs(self) -> list[dict]:
        """Enumerate joined dialogs across all workers and upsert telegram_chats.

        Returns a deduplicated list of {platform_chat_id, title, type} dicts.
        Workers running in parallel will see the same shared chats; we
        dedupe by platform_chat_id so we only INSERT each one once.
        """
        if not self._workers:
            self._workers = await self._spawn_workers()
        if not self._workers:
            logger.error("collect_dialogs: no Telegram workers — bailing")
            return []

        seen: dict[str, dict] = {}
        for worker in self._workers:
            if self._stop.is_set():
                break
            try:
                async for dialog in worker.client.iter_dialogs():
                    entity = getattr(dialog, "entity", None)
                    if entity is None:
                        continue
                    cid = str(getattr(entity, "id", ""))
                    if not cid or cid in seen:
                        continue
                    # Upsert into telegram_chats.
                    try:
                        await self._upsert_chat(entity)
                    except Exception as exc:
                        logger.debug("upsert_chat failed for %s: %s", cid, exc)
                    if getattr(entity, "broadcast", False):
                        chat_type = "channel"
                    elif getattr(entity, "megagroup", False):
                        chat_type = "supergroup"
                    elif hasattr(entity, "title"):
                        chat_type = "group"
                    else:
                        chat_type = "private"
                    seen[cid] = {
                        "platform_chat_id": cid,
                        "title": getattr(entity, "title", None)
                                 or getattr(entity, "username", None)
                                 or cid,
                        "type": chat_type,
                    }
            except Exception as exc:
                logger.error(
                    "[worker=%d account=%s] collect_dialogs failed: %s",
                    worker.worker_id, worker.account.name, exc,
                )

        logger.info("collect_dialogs: %d unique dialog(s)", len(seen))
        return list(seen.values())

    # ==================================================================
    # Common-chat / chat-members refresh — daily 03:00 SGT cron
    # ==================================================================

    async def collect_chat_members(self, chat_id, worker: "TelegramWorker | None" = None) -> int:
        """Iterate participants of chat_id and upsert into telegram_chat_members.

        Per-memory PRD: refreshed daily at 03:00 SGT for common-chat-membership
        analytics. Sets refreshed_at = NOW() so stale rows can be pruned.

        Returns the number of upserted member rows.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("collect_chat_members: no workers — bailing")
                return 0
            worker = self._workers[0]

        client = worker.client
        try:
            entity = await client.get_entity(int(chat_id))
        except (ValueError, TypeError):
            entity = await client.get_entity(chat_id)

        chat_id_int = int(getattr(entity, "id"))
        await self._upsert_chat(entity)

        seen: set[int] = set()
        upserted = 0
        try:
            async for participant in client.iter_participants(entity):
                if self._stop.is_set():
                    break
                pid = getattr(participant, "id", None)
                if pid is None or pid in seen:
                    continue
                seen.add(pid)

                # Best-effort upsert into telegram_users so the FK target exists.
                try:
                    await self._upsert_user_full(participant)
                except Exception:
                    pass

                # Determine role from participant.participant.* attributes.
                role = "member"
                p = getattr(participant, "participant", None)
                if p is not None:
                    pname = type(p).__name__
                    if "Creator" in pname:
                        role = "creator"
                    elif "Admin" in pname:
                        role = "admin"
                    elif "Banned" in pname:
                        role = "banned"
                    elif "Left" in pname:
                        role = "left"

                joined_at = None
                if p is not None:
                    joined_at = getattr(p, "date", None)

                async with self.pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO telegram_chat_members
                            (chat_id, user_id, role, joined_at, last_seen_at, refreshed_at)
                        VALUES ($1, $2, $3, $4, NOW(), NOW())
                        ON CONFLICT (chat_id, user_id) DO UPDATE SET
                            role = EXCLUDED.role,
                            joined_at = COALESCE(EXCLUDED.joined_at, telegram_chat_members.joined_at),
                            last_seen_at = NOW(),
                            refreshed_at = NOW()
                    """, chat_id_int, int(pid), role, joined_at)
                upserted += 1
        except Exception as exc:
            if _is_flood_wait(exc):
                await self._handle_flood_wait(worker, exc)
            else:
                logger.error(
                    "collect_chat_members chat=%s failed: %s",
                    chat_id_int, exc,
                )

        logger.info(
            "collect_chat_members: chat=%s upserted=%d",
            chat_id_int, upserted,
        )
        return upserted

    # ==================================================================
    # User profile + photos — cherry-picked from
    # telegramtoolkit/src/managers/download_profile_photos.py
    # ==================================================================

    async def collect_user_profile(
        self, user_id, worker: "TelegramWorker | None" = None,
    ) -> dict | None:
        """Fetch user metadata + profile photos.

        Returns a dict of the persisted fields, or None if the user can't
        be resolved by any worker.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("collect_user_profile: no workers — bailing")
                return None
            worker = self._workers[0]

        client = worker.client
        try:
            user = await client.get_entity(int(user_id))
        except (ValueError, TypeError):
            try:
                user = await client.get_entity(user_id)
            except Exception as exc:
                logger.warning("collect_user_profile resolve failed: %s", exc)
                return None
        except Exception as exc:
            logger.warning("collect_user_profile resolve failed: %s", exc)
            return None

        # ── User-intelligence diff: snapshot the row BEFORE upserting so the
        # change tracker can compare old → new and emit one row per changed
        # field into telegram_user_changes. Wrapped in try/except so any
        # failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT username, first_name, last_name "
                    "FROM telegram_users WHERE platform_user_id = $1",
                    str(getattr(user, "id", user_id)),
                )
        except Exception as exc:
            logger.debug("user_change_tracker: prev-row fetch failed: %s", exc)

        await self._upsert_user_full(user)

        try:
            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "username":   getattr(user, "username", None),
                "first_name": getattr(user, "first_name", None),
                "last_name":  getattr(user, "last_name", None),
                "bio":        getattr(user, "about", None) or getattr(user, "bio", None),
                "premium":    getattr(user, "premium", None),
                "verified":   getattr(user, "verified", None),
                "phone":      getattr(user, "phone", None),
            }
            photo = getattr(user, "photo", None)
            if photo is not None:
                new_snapshot["profile_photo_id"] = getattr(photo, "photo_id", None)
            await tracker.detect_and_log(
                table="telegram_user_changes",
                pk_col="user_id",
                pk_val=int(getattr(user, "id", 0) or 0),
                current_row=dict(prev_row) if prev_row else None,
                new_row=new_snapshot,
                fields=TELEGRAM_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker: detect_and_log failed: %s", exc)

        uid = str(getattr(user, "id", user_id))
        uname = (getattr(user, "username", None)
                 or getattr(user, "first_name", None) or uid)

        # Profile photo (first/largest).
        try:
            cid = f"profile_user_{uid}"
            if not self.is_known(cid):
                photo_bytes = await client.download_profile_photo(user, bytes)
                if photo_bytes:
                    await self.download_media({
                        "entity_id": uid,
                        "entity_name": uname,
                        "content_type": "user_profile_photo",
                        "content_id": cid,
                        "data": photo_bytes,
                        "extension": "jpg",
                    }, worker=worker)
        except Exception as exc:
            logger.debug("user profile photo failed for %s: %s", uid, exc)

        # Older photos via get_profile_photos (cherry-pick from toolkit).
        try:
            photos = await client.get_profile_photos(user)
            for idx, photo in enumerate(photos or []):
                pid = getattr(photo, "id", None)
                if pid is None:
                    continue
                cid_p = f"profile_user_{uid}_{pid}"
                if self.is_known(cid_p):
                    continue
                try:
                    photo_bytes = await client.download_media(photo, bytes)
                    if photo_bytes:
                        await self.download_media({
                            "entity_id": uid,
                            "entity_name": uname,
                            "content_type": "user_profile_photo",
                            "content_id": cid_p,
                            "data": photo_bytes,
                            "extension": "jpg",
                        }, worker=worker)
                except Exception as exc:
                    logger.debug("photo %s for %s failed: %s", pid, uid, exc)
        except Exception as exc:
            logger.debug("get_profile_photos failed for %s: %s", uid, exc)

        return {
            "platform_user_id": uid,
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone": getattr(user, "phone", None),
            "is_bot": bool(getattr(user, "bot", False)),
            "is_verified": bool(getattr(user, "verified", False)),
            "is_premium": bool(getattr(user, "premium", False)),
        }

    # ==================================================================
    # Single-message media download — routed through src.core.media_download
    # ==================================================================

    async def download_message_media(
        self,
        message_or_id,
        worker: "TelegramWorker | None" = None,
        chat_id=None,
    ):
        """Download the media attached to a Telethon message.

        Accepts either a Telethon Message object directly, or
        (message_id, chat_id) so callers without an event handle can
        re-fetch. Routes through self.download_media (which performs
        atomic write + sha256 + insert_media_item) — that is the unified
        delegated-backend equivalent of src/core/media_download.py for
        Telethon's library-level download_media() API.
        """
        if worker is None:
            if not self._workers:
                self._workers = await self._spawn_workers()
            if not self._workers:
                logger.error("download_message_media: no workers — bailing")
                return None
            worker = self._workers[0]

        client = worker.client

        # Resolve message object if only an ID was passed.
        message = message_or_id
        if not hasattr(message, "media"):
            if chat_id is None:
                logger.error("download_message_media: chat_id required when given an ID")
                return None
            try:
                msgs = await client.get_messages(int(chat_id), ids=int(message_or_id))
                message = msgs if hasattr(msgs, "media") else (msgs[0] if msgs else None)
            except Exception as exc:
                logger.error("download_message_media: get_messages failed: %s", exc)
                return None

        if message is None or getattr(message, "media", None) is None:
            return None

        chat_id_str = str(chat_id) if chat_id is not None else str(getattr(message, "chat_id", "unknown"))
        # Try to resolve a name; fall back to chat_id.
        chat_name = chat_id_str
        try:
            entity = await client.get_entity(int(chat_id_str))
            chat_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id_str
        except Exception:
            pass

        from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
        if isinstance(message.media, MessageMediaPhoto):
            await self._handle_photo(worker, message, chat_id_str, chat_name)
            return True
        if isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            await self._handle_document(worker, message, chat_id_str, chat_name, mime)
            return True

        # Unknown media type — fall through to a generic Telethon download.
        try:
            data = await client.download_media(message.media, bytes)
            if not data:
                return None
            _, _, ext = self._extract_file_info(message)
            await self.download_media({
                "entity_id": chat_id_str,
                "entity_name": chat_name,
                "content_type": "media",
                "content_id": str(message.id),
                "data": data,
                "extension": ext or "bin",
                "raw": message.to_dict(),
            }, worker=worker)
            return True
        except Exception as exc:
            logger.error("download_message_media generic path failed: %s", exc)
            return None

    # ==================================================================
    # Cleanup
    # ==================================================================

    async def cleanup(self):
        self._realtime_running = False
        try:
            await self._hub_notifier.stop()
        except Exception:
            pass
        try:
            await self._bot_pool.stop_health_monitor()
        except Exception:
            pass
        for w in self._workers:
            await w.disconnect()
        self._workers = []
        self._primary_client = None
