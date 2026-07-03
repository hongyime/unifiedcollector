"""
WhatsApp collector — RECEIVES events from external Baileys bridges.

Bridge integration
------------------
Two docker containers (`wa_bridge_1`, `wa_bridge_2`) run a Baileys-based
WhatsApp Web client (the wa-client-ts service from whatsappcollector/).
Each bridge publishes message events; this collector consumes them in two modes:

  1. RabbitMQ topic exchange `whatsapp.events` (preferred). Bound queue
     `unifiedcollector.messages` with routing key `messages.#`. JSON event
     bodies match the `message_event` shape produced by
     services/wa-client-ts/src/event_handlers/messages.ts.

  2. HTTP polling fallback. For each session in WHATSAPP_SESSION_BRIDGES_JSON
     (mapping session_name -> bridge_url), GET `{bridge_url}/health` then
     `{bridge_url}/messages/recent?limit=N`. Auth via
     `Authorization: Bearer <WHATSAPP_MEDIA_BRIDGE_SECRET>`.

Encrypted media is decrypted by the bridge's `/media/decrypt` endpoint
(POST {messageId, mediaKey, directPath}). HMAC-SHA256 signed using
`WHATSAPP_MEDIA_BRIDGE_SECRET` and timestamp header.

Public entry points
-------------------
  - collect(targets)           : main loop — broker consume or HTTP poll
  - process_bridge_event(event): handle a single bridge event payload
  - backfill_chat(jid, ...)    : trigger on-demand backfill via bridge
  - download_media(item)       : persist queued media bytes

Send-side dropped (per task): no send/reply/react/edit/delete/typing/
mark-read, no bot-command-handler, no bulk_sender, no web UI, no CLI/setup.
This collector is RECEIVE-ONLY.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from src.core.base_collector import BaseCollector
from src.core.change_tracker import ChangeTracker
from src.core.link_extractor import extract_whatsapp_links
from src.core.file_naming import sanitize_name

logger = logging.getLogger(__name__)

MEDIA_EXTS = {"jpg", "jpeg", "png", "mp4", "opus", "webp", "gif", "pdf", "3gp", "m4a"}


class WhatsappCollector(BaseCollector):
    SOURCE_NAME = "whatsapp"
    INGEST_PATH = "messaging"  # realtime messaging path (P2 review §3 provenance)

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

        self._change_tracker = ChangeTracker()
        self._link_discovery_enabled = os.getenv("WHATSAPP_LINK_DISCOVERY_ENABLED", "true").lower() == "true"
        _spider_sessions = os.getenv("WHATSAPP_SPIDER_SESSIONS", "")
        self._spider_sessions: set[str] = (
            {s.strip().lower() for s in _spider_sessions.split(",") if s.strip()}
            if _spider_sessions else set()
        )
        # Send-side intentionally dropped: no bulk_send_enabled / hourly cap /
        # daily cap / membership gating. This collector is RECEIVE-ONLY.

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

        # Messages queue: live + history-sync events
        msg_queue = await self._broker_channel.declare_queue(
            "unifiedcollector.messages", durable=True,
        )
        await msg_queue.bind(exchange, routing_key="messages.#")

        # Contacts queue: lid→jid mapping events from bridge contacts handler
        contact_queue = await self._broker_channel.declare_queue(
            "unifiedcollector.contacts", durable=True,
        )
        await contact_queue.bind(exchange, routing_key="contacts.#")

        async def _consume_contacts():
            async with contact_queue.iterator() as qi:
                async for message in qi:
                    if self._stop.is_set():
                        break
                    async with message.process():
                        try:
                            body = json.loads(message.body.decode())
                            await self._handle_contact_event(body)
                        except Exception as e:
                            logger.debug("Contact event processing failed: %s", e)

        import asyncio as _asyncio
        _asyncio.create_task(_consume_contacts())

        async with msg_queue.iterator() as qi:
            async for message in qi:
                if self._stop.is_set():
                    break
                async with message.process():
                    try:
                        body = json.loads(message.body.decode())
                        # Two payload shapes arrive on this queue:
                        #  - single live message (flat: message_id/chat_jid/...)
                        #  - history-sync batch: {sync_type, session_name, messages:[...]}
                        # Unpack the batch so each historical message is ingested.
                        if isinstance(body, dict) and isinstance(body.get("messages"), list):
                            batch_session = body.get("session_name")
                            for m in body["messages"]:
                                if self._stop.is_set():
                                    break
                                if batch_session and "session_name" not in m:
                                    m["session_name"] = batch_session
                                await self._handle_message_event(m, targets)
                        else:
                            await self._handle_message_event(body, targets)
                    except Exception as e:
                        logger.error("Broker message processing failed: %s", e)

    async def _handle_contact_event(self, event: dict):
        """Maintain whatsapp_lid_map from contacts.update events.

        When Baileys syncs contacts it may provide both the phone-based JID
        (event['jid']) and the linked-device ID (event['lid']). We store this
        mapping so _track_user_profile can resolve @lid → phone JID for
        group message senders.
        """
        lid = event.get("lid")
        jid = event.get("jid")
        if not lid or not jid:
            return
        if "@lid" not in lid or "@s.whatsapp.net" not in jid:
            return
        display_name = event.get("display_name")
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO whatsapp_lid_map (lid, phone_jid, display_name, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (lid) DO UPDATE SET
                        phone_jid = EXCLUDED.phone_jid,
                        display_name = COALESCE(EXCLUDED.display_name, whatsapp_lid_map.display_name),
                        updated_at = NOW()
                """, lid, jid, display_name)
        except Exception as e:
            logger.debug("lid_map upsert failed: %s", e)

    async def _handle_message_event(self, event: dict, targets: list[str]):
        # WhatsApp "delete for everyone" (revoke) — flag the original message + when.
        if event.get("deletion"):
            rid = event.get("revoked_message_id")
            if rid and self.pool is not None:
                ts = event.get("timestamp")
                try:
                    dt = (datetime.fromtimestamp(float(ts), tz=timezone.utc)
                          if ts else datetime.now(timezone.utc))
                except (TypeError, ValueError, OSError):
                    dt = datetime.now(timezone.utc)
                try:
                    async with self.pool.acquire() as c:
                        await c.execute(
                            "UPDATE whatsapp_messages SET is_deleted=true, "
                            "deleted_at=COALESCE(deleted_at,$1), status='revoked' "
                            "WHERE platform_message_id=$2",
                            dt, rid,
                        )
                    logger.info("whatsapp: message %s revoked (delete-for-everyone)", rid)
                except Exception:
                    logger.debug("wa deletion update failed for %s", rid, exc_info=True)
            return

        msg_id = event.get("message_id") or event.get("key", {}).get("id", "")
        chat_jid = event.get("chat_jid") or event.get("key", {}).get("remoteJid", "")

        if not msg_id or not chat_jid:
            return

        if targets and "*" not in targets and "all" not in targets and not any(t.lower() in chat_jid.lower() for t in targets):
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
        self._progress_count += 1

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

        if self._link_discovery_enabled:
            text = event.get("body", "") or event.get("text", "") or event.get("caption", "")
            if text:
                await self._discover_links(text, chat_jid)

    async def _upsert_chat(self, jid: str, name: str, event: dict):
        # WhatsApp JID suffixes: @g.us = group, @newsletter = channel (one-to-many
        # broadcast), @broadcast = status/broadcast list, else = 1:1 DM. Bryan wants
        # ALL of these (all chats/groups/channels of connected accounts).
        if "@g.us" in jid:
            chat_type = "group"
        elif "@newsletter" in jid:
            chat_type = "channel"
        elif "@broadcast" in jid:
            chat_type = "broadcast"
        else:
            chat_type = "dm"
        is_group = chat_type == "group"
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO whatsapp_chats (platform_chat_id, name, is_group, chat_type, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (platform_chat_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    chat_type = EXCLUDED.chat_type,
                    updated_at = NOW()
            """, jid, name, is_group, chat_type)

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
            # Link the media back to its message so the row carries media_url/size
            # (was 0% — the analyzer couldn't join a message to its image). cid is
            # "wa_<platform_message_id>".
            if self.pool is not None and cid.startswith("wa_"):
                try:
                    async with self.pool.acquire() as _c:
                        await _c.execute(
                            "UPDATE whatsapp_messages SET media_url=$1, "
                            "media_size=COALESCE(media_size,$2) "
                            "WHERE platform_message_id=$3 AND media_url IS NULL",
                            str(dest), len(data), cid[3:],
                        )
                except Exception:
                    logger.debug("wa media-link update failed for %s", cid, exc_info=True)
            self._known_ids.add(cid)
        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.error("Save failed %s: %s", cid, e)
            await self.send_to_dlq(entity_id, cid, str(e))

    async def _track_user_profile(self, event: dict) -> str | None:
        if not self.pool:
            return None
        sender_jid = event.get("sender_jid") or event.get("key", {}).get("participant", "")
        if not sender_jid:
            sender_jid = event.get("chat_jid", "")
            if not sender_jid or "@g.us" in sender_jid:
                chat = event.get("chat_jid", "?")
                logger.debug("whatsapp: group message missing sender in chat %s", chat)
                return None

        # Resolve @lid → phone-based JID. Group messages in newer WhatsApp
        # use @lid (linked device ID) as the participant JID. The analyzer's
        # entity_platform_links lookup needs phone-based @s.whatsapp.net JIDs.
        # We look up whatsapp_lid_map (populated by contacts.update events).
        if "@lid" in sender_jid:
            try:
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT phone_jid FROM whatsapp_lid_map WHERE lid = $1", sender_jid
                    )
                if row:
                    sender_jid = row["phone_jid"]
            except Exception:
                pass  # fall through — store @lid JID as fallback

        # Only extract phone number from @s.whatsapp.net JIDs (not @lid which
        # has numeric LID prefix that happens to match \d{7,15} but is not a phone).
        phone_number: str | None = None
        if "@s.whatsapp.net" in sender_jid:
            prefix = sender_jid.split("@")[0]
            if re.fullmatch(r"\d{7,15}", prefix):
                phone_number = prefix

        payload = {
            "push_name": event.get("pushName", ""),
            # name was ~0% populated: verifiedBizName/notify are empty for personal
            # contacts. Fall back to pushName (the user's WhatsApp display name —
            # present on virtually every message) so `name` is actually filled.
            "display_name": event.get("verifiedBizName", "") or event.get("notify", "")
                            or event.get("pushName", "") or None,
            "phone_number": phone_number,
            "is_business": event.get("isBusinessMessage", False),
        }

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO whatsapp_users (platform_user_id, name, pushname, phone_number, is_business, collected_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (platform_user_id) DO UPDATE SET
                        pushname = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
                        name = COALESCE(EXCLUDED.name, whatsapp_users.name),
                        phone_number = COALESCE(EXCLUDED.phone_number, whatsapp_users.phone_number),
                        is_business = EXCLUDED.is_business,
                        collected_at = NOW()
                    RETURNING id
                """, sender_jid, payload["display_name"], payload["push_name"],
                    payload["phone_number"] or None, payload["is_business"])
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
                        # Bridge contract is {"status":"ok","whatsapp_ready":bool},
                        # NOT {"status":"connected"}. Accept either the ready flag or
                        # a connected/ok status so the HTTP-poll fallback isn't skipped
                        # forever on a correctly-running bridge.
                        if not health.get("whatsapp_ready") and health.get("status") not in ("connected", "ok"):
                            logger.debug("Session %s not ready: %s", session_name, health.get("status"))
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

    async def process_bridge_event(self, event: dict, targets: list[str] | None = None):
        """Public entry point: process a single bridge event payload.

        Use this when a host process feeds events directly (e.g. test harness,
        or a custom subscriber). Equivalent to internal _handle_message_event.
        """
        await self._handle_message_event(event, targets or [])

    async def backfill_chat(self, chat_jid: str, *, oldest_msg_key: str = "",
                             oldest_msg_ts: int = 0, count: int = 100,
                             session: str | None = None) -> str | None:
        """Request on-demand history backfill from a bridge for ``chat_jid``.

        Mirrors whatsappcollector BackfillManager.request_backfill_batch:
        POST {bridge_url}/backfill-request with chat_jid + oldest_msg_key/ts +
        correlation_id. Returns the correlation_id on success, None on failure.
        """
        session_name = session or (self._session_names[0] if self._session_names else "")
        bridge_url = self._session_bridges.get(session_name) if session_name else None
        if not bridge_url:
            # fall back to first available bridge
            if self._session_bridges:
                session_name, bridge_url = next(iter(self._session_bridges.items()))
            else:
                logger.warning("backfill_chat: no bridge configured for jid=%s", chat_jid)
                return None

        correlation_id = str(uuid4())
        payload = {
            "chat_jid": chat_jid,
            "oldest_msg_key": oldest_msg_key,
            "oldest_msg_ts": int(oldest_msg_ts or 0),
            "count": int(count),
            "correlation_id": correlation_id,
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"{bridge_url}/backfill-request",
                    json=payload,
                    headers=self._bridge_headers(),
                )
                resp.raise_for_status()
            # rate limit per BackfillManager
            per_request_sleep = max(0.0, 60.0 / max(1, self._backfill_rpm))
            await asyncio.sleep(per_request_sleep)
            return correlation_id
        except Exception as exc:
            logger.warning("backfill_chat failed jid=%s err=%s", chat_jid, exc)
            return None

    async def _discover_links(self, text: str, chat_jid: str):
        """Extract WhatsApp invite links and persist for downstream discovery."""
        try:
            links = extract_whatsapp_links(text or "")
        except Exception as e:
            logger.debug("link extraction failed: %s", e)
            return
        if not links:
            return
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                for kind, url in links:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO discovered_links (source, kind, url, context, discovered_at)
                            VALUES ($1, $2, $3, $4, NOW())
                            ON CONFLICT (url) DO NOTHING
                            """,
                            self.SOURCE_NAME, kind, url, chat_jid,
                        )
                    except Exception as e:
                        # table may not exist in some envs; soft-fail
                        logger.debug("discovered_links insert skipped (%s): %s", url, e)
                        break
        except Exception as e:
            logger.debug("discover_links db failure: %s", e)

    async def _media_archival_loop(self):
        """Periodic re-download for messages where media archival has not yet
        completed. Mirrors media_archival/worker semantics: pull pending items,
        decrypt via bridge, persist."""
        while not self._stop.is_set():
            try:
                if self.pool:
                    async with self.pool.acquire() as conn:
                        rows = await conn.fetch(
                            """
                            SELECT platform_message_id, metadata
                            FROM whatsapp_messages
                            WHERE media_url IS NULL
                              AND metadata ? 'mediaKey'
                              AND metadata ? 'directPath'
                            ORDER BY collected_at DESC
                            LIMIT $1
                            """,
                            self._media_batch,
                        )
                    for row in rows or []:
                        if self._stop.is_set():
                            break
                        meta = row["metadata"]
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except Exception:
                                continue
                        await self._handle_message_event(meta, [])
            except Exception as e:
                logger.debug("media archival loop iteration failed: %s", e)
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
