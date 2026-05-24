import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import httpx

from src.core.base_collector import BaseCollector
from src.core.change_tracker import ChangeTracker
from src.core.link_extractor import extract_whatsapp_links
from src.core.face_processor import FaceProcessor
from src.core.face_matcher import FaceMatcher
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)

MEDIA_EXTS = {"jpg", "jpeg", "png", "mp4", "opus", "webp", "gif", "pdf", "3gp", "m4a"}


class WhatsappCollector(BaseCollector):
    SOURCE_NAME = "whatsapp"

    def __init__(self):
        super().__init__()
        self._export_dir = os.getenv("WHATSAPP_EXPORT_DIR", "")

        self._session_bridges: dict[str, str] = {}
        bridges_json = os.getenv("WHATSAPP_SESSION_BRIDGES_JSON", "") or os.getenv("SESSION_BRIDGES_JSON", "")
        if bridges_json:
            try:
                self._session_bridges = json.loads(bridges_json)
            except json.JSONDecodeError:
                logger.warning("Invalid SESSION_BRIDGES_JSON")

        self._bridge_secret = os.getenv("WHATSAPP_MEDIA_BRIDGE_SECRET", "") or os.getenv("MEDIA_BRIDGE_SECRET", "")
        self._session_names = [
            s.strip() for s in
            (os.getenv("WHATSAPP_SESSION_NAMES", "") or os.getenv("SESSION_NAMES", "")).split(",")
            if s.strip()
        ]

        self._broker_type = os.getenv("WHATSAPP_BROKER_TYPE", "") or os.getenv("BROKER_TYPE", "rabbitmq")
        self._rabbitmq_url = os.getenv("WHATSAPP_RABBITMQ_URL", "") or os.getenv("RABBITMQ_URL", "")
        self._redis_url = os.getenv("WHATSAPP_REDIS_URL", "") or os.getenv("REDIS_URL", "")
        self._dedup_ttl = int(os.getenv("WHATSAPP_DEDUP_TTL_SECONDS", "") or os.getenv("COLLECTOR_DEDUP_TTL_SECONDS", "86400"))

        self._backfill_batch = int(os.getenv("WHATSAPP_BACKFILL_BATCH_SIZE", "") or os.getenv("COLLECTOR_BACKFILL_BATCH_SIZE", "50"))
        self._backfill_poll = int(os.getenv("WHATSAPP_BACKFILL_POLL_SECONDS", "") or os.getenv("COLLECTOR_BACKFILL_POLL_SECONDS", "30"))
        self._backfill_rpm = int(os.getenv("WHATSAPP_BACKFILL_REQ_PER_MIN", "") or os.getenv("COLLECTOR_BACKFILL_REQ_PER_MIN", "5"))
        self._max_backfill_days = int(os.getenv("WHATSAPP_MAX_BACKFILL_AGE_DAYS", "") or os.getenv("COLLECTOR_MAX_BACKFILL_AGE_DAYS", "90"))

        self._media_batch = int(os.getenv("WHATSAPP_MEDIA_ARCHIVAL_BATCH_SIZE", "") or os.getenv("MEDIA_ARCHIVAL_BATCH_SIZE", "50"))
        self._media_poll = int(os.getenv("WHATSAPP_MEDIA_ARCHIVAL_POLL_SECONDS", "") or os.getenv("MEDIA_ARCHIVAL_POLL_SECONDS", "5"))
        self._media_max_retries = int(os.getenv("WHATSAPP_MEDIA_MAX_RETRIES", "") or os.getenv("MEDIA_ARCHIVAL_MAX_DOWNLOAD_RETRIES", "3"))
        self._media_retention_days = int(os.getenv("WHATSAPP_MEDIA_RETENTION_DAYS", "") or os.getenv("MEDIA_RETENTION_DAYS", "90"))

        self._session_risk_threshold = float(os.getenv("WHATSAPP_SESSION_RISK_THRESHOLD", "") or os.getenv("SESSION_RISK_THRESHOLD", "0.8"))
        self._session_cooldown = int(os.getenv("WHATSAPP_SESSION_COOLDOWN_SECONDS", "") or os.getenv("SESSION_COOLDOWN_SECONDS", "300"))

        self._sem = asyncio.Semaphore(3)
        self._redis = None
        self._broker_conn = None
        self._broker_channel = None
        self._session_health: dict[str, dict] = {}
        self._use_realtime = bool(self._session_bridges and self._bridge_secret)
        self._use_export = bool(self._export_dir and os.path.isdir(self._export_dir))

        self._face_enabled = os.getenv("WHATSAPP_FACE_RECOGNITION_ENABLED", "false").lower() == "true"
        self._face_processor = FaceProcessor() if self._face_enabled else None
        self._face_matcher = FaceMatcher()
        self._change_tracker = ChangeTracker()
        self._link_discovery_enabled = os.getenv("WHATSAPP_LINK_DISCOVERY_ENABLED", "true").lower() == "true"
        self._bulk_send_enabled = os.getenv("WHATSAPP_BULK_SEND_ENABLED", "false").lower() == "true"
        self._bulk_hourly_cap = int(os.getenv("WHATSAPP_BULK_HOURLY_CAP", "30"))
        self._bulk_daily_cap = int(os.getenv("WHATSAPP_BULK_DAILY_CAP", "200"))
        self._bulk_min_membership_hours = int(os.getenv("WHATSAPP_BULK_MIN_MEMBERSHIP_HOURS", "48"))
        self._bulk_send_counts: dict[str, list[float]] = {}

    @property
    def account_media_dir(self) -> Path:
        # isolation by session name (first one if multiple)
        session = self._session_names[0] if self._session_names else "default"
        path = self.media_dir / f"session_{sanitize_name(session)}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def collect(self, targets: list[str]):
        if self._use_realtime:
            await self._collect_realtime(targets)
        elif self._use_export:
            await self._collect_from_exports(targets)
        else:
            logger.warning(
                "WhatsApp: no collection mode available. "
                "Set SESSION_BRIDGES_JSON + MEDIA_BRIDGE_SECRET for real-time, "
                "or WHATSAPP_EXPORT_DIR for offline import."
            )

    # ── Real-time collection (wa-client-ts bridge) ──

    async def _collect_realtime(self, targets: list[str]):
        await self._init_redis()
        await self._init_broker()

        tasks = []
        if self._broker_channel:
            tasks.append(asyncio.create_task(self._consume_broker(targets)))
        else:
            tasks.append(asyncio.create_task(self._poll_sessions(targets)))

        tasks.append(asyncio.create_task(self._media_archival_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup_connections()

    async def _init_redis(self):
        if not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("WhatsApp Redis connected")
        except Exception as e:
            logger.warning("Redis unavailable, dedup will use local set: %s", e)
            self._redis = None

    async def _init_broker(self):
        if not self._rabbitmq_url or self._broker_type != "rabbitmq":
            return
        try:
            import aio_pika
            self._broker_conn = await aio_pika.connect_robust(self._rabbitmq_url)
            self._broker_channel = await self._broker_conn.channel()
            await self._broker_channel.set_qos(prefetch_count=10)
            logger.info("WhatsApp RabbitMQ connected")
        except Exception as e:
            logger.warning("RabbitMQ unavailable, falling back to HTTP polling: %s", e)
            self._broker_conn = None
            self._broker_channel = None

    async def _consume_broker(self, targets: list[str]):
        import aio_pika

        exchange = await self._broker_channel.declare_exchange(
            "whatsapp.events", aio_pika.ExchangeType.TOPIC, durable=True,
        )

        queue = await self._broker_channel.declare_queue(
            "unifiedcollector.messages", durable=True,
        )
        await queue.bind(exchange, routing_key="messages.#")

        async with queue.iterator() as qi:
            async for message in qi:
                if self._stop.is_set():
                    break
                async with message.process():
                    try:
                        body = json.loads(message.body.decode())
                        await self._handle_message_event(body, targets)
                    except Exception as e:
                        logger.error("Broker message processing failed: %s", e)

    async def _handle_message_event(self, event: dict, targets: list[str]):
        msg_id = event.get("message_id") or event.get("key", {}).get("id", "")
        chat_jid = event.get("chat_jid") or event.get("key", {}).get("remoteJid", "")

        if not msg_id or not chat_jid:
            return

        if targets and not any(t.lower() in chat_jid.lower() for t in targets):
            return

        if await self._is_duplicate(msg_id, chat_jid):
            return

        chat_name = event.get("chat_name") or event.get("pushName") or chat_jid.split("@")[0]
        session = event.get("session_name", self._session_names[0] if self._session_names else "default")

        # 1. Upsert Chat & User Info
        await self._upsert_chat(chat_jid, chat_name, event)
        sender_uuid = await self._track_user_profile(event)

        # 2. Save Message (ALL messages)
        await self._upsert_message(event, chat_jid, sender_uuid)

        # 3. Handle Media if exists
        media_type = event.get("media_type") or event.get("messageType", "")
        has_media = media_type in ("imageMessage", "videoMessage", "audioMessage", "documentMessage", "stickerMessage")

        if not has_media:
            # Check if it has a media_url or directPath even if type is not explicit
            if not (event.get("media_url") or event.get("directPath")):
                return

        ext = self._media_type_to_ext(media_type)
        content_type = self._media_type_to_content_type(media_type)
        cid = f"wa_{msg_id}"

        if self.is_known(cid):
            return

        media_key = event.get("mediaKey")
        direct_path = event.get("directPath")

        if media_key and direct_path and session:
            data = await self._download_via_bridge(session, msg_id, media_key, direct_path)
        else:
            media_url = event.get("media_url", "")
            data = await self._download_direct(media_url) if media_url else None

        if data:
            await self._save_media(data, cid, chat_jid, chat_name, content_type, ext, event)

            if self._face_enabled and content_type in ("photo", "video"):
                await self._process_faces(data, cid, chat_jid.split("@")[0], content_type)

        if self._link_discovery_enabled:
            text = event.get("body", "") or event.get("text", "") or event.get("caption", "")
            if text:
                await self._discover_links(text, chat_jid)

    async def _upsert_chat(self, jid: str, name: str, event: dict):
        is_group = "@g.us" in jid
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO whatsapp_chats (platform_chat_id, name, is_group, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (platform_chat_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    updated_at = NOW()
            """, jid, name, is_group)

    async def _upsert_message(self, event: dict, chat_jid: str, sender_uuid: str | None):
        msg_id = event.get("message_id") or event.get("key", {}).get("id", "")
        text = event.get("body") or event.get("text") or event.get("caption")
        timestamp = event.get("timestamp")
        dt = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else datetime.now(timezone.utc)
        
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow("SELECT id FROM whatsapp_chats WHERE platform_chat_id = $1", chat_jid)
            chat_uuid = chat_row['id'] if chat_row else None
            
            await conn.execute("""
                INSERT INTO whatsapp_messages (
                    platform_message_id, chat_id, sender_id, from_me,
                    text, media_mime_type, timestamp, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (platform_message_id) DO NOTHING
            """,
            msg_id, chat_uuid, sender_uuid, event.get("key", {}).get("fromMe", False),
            text, event.get("mimetype"), dt, json.dumps(event)
            )

    async def _save_media(self, data: bytes, cid: str, chat_jid: str,
                           chat_name: str, content_type: str, ext: str,
                           event: dict | None = None):
        entity_id = chat_jid.split("@")[0] if chat_jid else "unknown"

        filename = self.build_filename(
            entity_id=entity_id,
            entity_name=chat_name,
            content_type=content_type,
            content_id=cid,
            extension=ext,
        )

        dest_dir = self.account_media_dir / content_type
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        if dest.exists():
            return

        try:
            sha = self.sha256_bytes(data)
            
            # Atomic write
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            
            self.circuit_breaker.record_success()

            metadata = {
                "entity_id": entity_id,
                "entity_name": chat_name,
                "content_type": content_type,
                "content_id": cid,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": event or {}
            }
            self.save_json(metadata, dest_dir / f"{Path(filename).stem}_metadata.json")

            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=chat_name,
                content_type=content_type,
                content_id=cid,
                filename=filename,
                file_path=str(dest),
                file_size=len(data),
                sha256=sha,
                metadata=metadata,
            )
            self._known_ids.add(cid)
        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.error("Save failed %s: %s", cid, e)
            await self.send_to_dlq(entity_id, cid, str(e))

    async def _track_user_profile(self, event: dict) -> str | None:
        if not self._pool:
            return None
        sender_jid = event.get("sender_jid") or event.get("key", {}).get("participant", "")
        if not sender_jid:
            sender_jid = event.get("chat_jid", "")
            if not sender_jid or "@g.us" in sender_jid:
                return None

        payload = {
            "push_name": event.get("pushName", ""),
            "display_name": event.get("verifiedBizName", "") or event.get("notify", ""),
            "phone_number": sender_jid.split("@")[0] if "@" in sender_jid else "",
            "is_business": event.get("isBusinessMessage", False),
        }

        try:
            # Custom upsert to return the internal ID
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO whatsapp_users (platform_user_id, name, pushname, collected_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        pushname = EXCLUDED.pushname,
                        collected_at = NOW()
                    RETURNING id
                """, sender_jid, payload["display_name"], payload["push_name"])
                return row['id']
        except Exception as e:
            logger.debug("User profile tracking failed: %s", e)
            return None

    async def _poll_sessions(self, targets: list[str]):
        while not self._stop.is_set():
            for session_name, bridge_url in self._session_bridges.items():
                if self._stop.is_set():
                    break

                if self._is_session_cooled_down(session_name):
                    continue

                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(f"{bridge_url}/health")
                        if resp.status_code != 200:
                            self._record_session_failure(session_name)
                            continue

                        health = resp.json()
                        if health.get("status") != "connected":
                            logger.debug("Session %s not connected: %s", session_name, health.get("status"))
                            continue

                        resp = await client.get(
                            f"{bridge_url}/messages/recent",
                            params={"limit": self._backfill_batch},
                            headers=self._bridge_headers(),
                        )
                        if resp.status_code != 200:
                            continue

                        messages = resp.json()
                        for msg in messages:
                            if self._stop.is_set():
                                break
                            msg["session_name"] = session_name
                            await self._handle_message_event(msg, targets)

                        self._record_session_success(session_name)

                except Exception as e:
                    logger.error("Session %s poll failed: %s", session_name, e)
                    self._record_session_failure(session_name)

            await asyncio.sleep(self._backfill_poll)

    async def _media_archival_loop(self):
        while not self._stop.is_set():
            await asyncio.sleep(self._media_poll)

    async def _download_via_bridge(self, session: str, msg_id: str,
                                    media_key: str, direct_path: str) -> bytes | None:
        bridge_url = self._session_bridges.get(session)
        if not bridge_url:
            return None

        timestamp = str(int(time.time()))
        payload = f"{msg_id}:{timestamp}"
        sig = hmac.new(
            self._bridge_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        try:
            async with self._sem:
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    resp = await client.post(
                        f"{bridge_url}/media/decrypt",
                        json={
                            "messageId": msg_id,
                            "mediaKey": media_key,
                            "directPath": direct_path,
                        },
                        headers={
                            **self._bridge_headers(),
                            "X-Timestamp": timestamp,
                            "X-Signature": sig,
                        },
                    )
                    if resp.status_code == 200:
                        return resp.content
                    logger.warning("Bridge decrypt failed %s: %d", msg_id, resp.status_code)
        except Exception as e:
            logger.error("Bridge download failed %s: %s", msg_id, e)
        return None

    async def _download_direct(self, url: str) -> bytes | None:
        try:
            async with self._sem:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.content
        except Exception as e:
            logger.error("Direct download failed: %s", e)
        return None

    def _bridge_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bridge_secret}",
            "Content-Type": "application/json",
        }

    async def _is_duplicate(self, msg_id: str, chat_jid: str) -> bool:
        key = f"collector:dedup:{msg_id}:{chat_jid}"
        if self._redis:
            try:
                result = await self._redis.set(key, "1", ex=self._dedup_ttl, nx=True)
                return result is None
            except Exception:
                pass
        return self.is_known(f"wa_{msg_id}")

    def _is_session_cooled_down(self, session_name: str) -> bool:
        health = self._session_health.get(session_name, {})
        cooldown_until = health.get("cooldown_until", 0)
        return time.time() < cooldown_until

    def _record_session_success(self, session_name: str):
        h = self._session_health.setdefault(session_name, {"errors": 0, "risk": 0.0, "cooldown_until": 0})
        h["errors"] = max(0, h["errors"] - 1)
        h["risk"] = max(0.0, h["risk"] - 0.1)

    def _record_session_failure(self, session_name: str):
        h = self._session_health.setdefault(session_name, {"errors": 0, "risk": 0.0, "cooldown_until": 0})
        h["errors"] += 1
        h["risk"] = min(1.0, h["risk"] + 0.2)
        if h["risk"] >= self._session_risk_threshold:
            h["cooldown_until"] = time.time() + self._session_cooldown
            logger.warning("Session %s cooled down for %ds (risk=%.1f)", session_name, self._session_cooldown, h["risk"])

    async def _cleanup_connections(self):
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
        if self._broker_conn:
            try:
                await self._broker_conn.close()
            except Exception:
                pass

    @staticmethod
    def _media_type_to_ext(media_type: str) -> str:
        return {
            "imageMessage": "jpg",
            "videoMessage": "mp4",
            "audioMessage": "opus",
            "documentMessage": "pdf",
            "stickerMessage": "webp",
        }.get(media_type, "bin")

    @staticmethod
    def _media_type_to_content_type(media_type: str) -> str:
        return {
            "imageMessage": "photo",
            "videoMessage": "video",
            "audioMessage": "audio",
            "documentMessage": "document",
            "stickerMessage": "sticker",
        }.get(media_type, "media")

    # ── Export reader (offline import mode) ──

    async def _collect_from_exports(self, targets: list[str]):
        export_path = Path(self._export_dir)

        for entry in sorted(export_path.iterdir()):
            if self._stop.is_set():
                break
            if entry.suffix == ".zip":
                await self._process_zip(entry, targets)
            elif entry.is_dir():
                await self._process_chat_dir(entry, targets)

    async def _process_zip(self, zip_path: Path, targets: list[str]):
        chat_name = zip_path.stem.replace("WhatsApp Chat - ", "").strip()

        if targets and not any(t.lower() in chat_name.lower() for t in targets):
            return

        logger.info("Processing WhatsApp export: %s", chat_name)

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    if self._stop.is_set():
                        break
                    if info.is_dir():
                        continue

                    ext = Path(info.filename).suffix.lstrip(".").lower()
                    if ext not in MEDIA_EXTS:
                        continue

                    cid = Path(info.filename).stem
                    if self.is_known(cid):
                        continue

                    data = zf.read(info.filename)
                    content_type = self._classify_type(ext)

                    await self._save_media(
                        data, cid, chat_name, chat_name, content_type,
                        ext if ext != "jpeg" else "jpg",
                    )
        except Exception as e:
            logger.error("Failed to process %s: %s", zip_path.name, e)
            await self.send_to_dlq(chat_name, zip_path.name, str(e))

        await self.checkpoint.save_progress(chat_name)

    async def _process_chat_dir(self, chat_dir: Path, targets: list[str]):
        chat_name = chat_dir.name.replace("WhatsApp Chat - ", "").strip()

        if targets and not any(t.lower() in chat_name.lower() for t in targets):
            return

        logger.info("Processing WhatsApp chat dir: %s", chat_name)

        for f in sorted(chat_dir.rglob("*")):
            if self._stop.is_set():
                break
            if not f.is_file():
                continue

            ext = f.suffix.lstrip(".").lower()
            if ext not in MEDIA_EXTS:
                continue

            cid = f.stem
            if self.is_known(cid):
                continue

            data = f.read_bytes()
            content_type = self._classify_type(ext)

            await self._save_media(
                data, cid, chat_name, chat_name, content_type,
                ext if ext != "jpeg" else "jpg",
            )

        await self.checkpoint.save_progress(chat_name)

    @staticmethod
    def _classify_type(ext: str) -> str:
        if ext in ("mp4", "3gp"):
            return "video"
        if ext in ("opus", "m4a"):
            return "audio"
        if ext in ("pdf",):
            return "document"
        return "photo"

    async def download_media(self, item: dict):
        cid = item["content_id"]
        if self.is_known(cid):
            return
        data = item.get("data")
        if not data:
            return
        await self._save_media(
            data, cid, item["entity_id"], item["entity_name"],
            item["content_type"], item.get("extension", "jpg"),
        )
