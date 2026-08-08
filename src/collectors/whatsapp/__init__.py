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
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from src.core.base_collector import BaseCollector
from src.core.change_tracker import ChangeTracker
from src.core.document_filter import classify_document
from src.core.rate_limit_events import record_rate_limit_event
from src.core.user_change_tracker import (
    UserChangeTracker,
    WHATSAPP_TRACKED_FIELDS,
)
from src.core.link_extractor import extract_all_links
from src.core.file_naming import sanitize_name
from src.core.raw_archive import report_raw_archive_result
from src.core.vault import VAULT_ROOT, write_atomic_artifact, write_raw_payload

logger = logging.getLogger(__name__)

MEDIA_EXTS = {"jpg", "jpeg", "png", "mp4", "opus", "webp", "gif", "pdf", "3gp", "m4a"}
_MIME_EXT = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "text/plain": "txt",
}


def _tier1_raw_archives_enabled() -> bool:
    raw = os.getenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _first_nonempty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    return bool(value)


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
        self._pairing_health_interval = int(os.getenv("WHATSAPP_PAIRING_HEALTH_INTERVAL_SECONDS", "60"))
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
        self._bridge_ready_cache: dict[str, dict] = {}
        self._bridge_ready_ttl = int(os.getenv("WHATSAPP_BRIDGE_READY_CACHE_SECONDS", "30"))
        self._session_cooldowns_restored = False
        self._use_realtime = bool(self._session_bridges and self._bridge_secret)
        self._use_export = bool(self._export_dir and os.path.isdir(self._export_dir))

        self._change_tracker = ChangeTracker()
        self._link_discovery_enabled = os.getenv("WHATSAPP_LINK_DISCOVERY_ENABLED", "true").lower() == "true"
        _spider_sessions = os.getenv("WHATSAPP_SPIDER_SESSIONS", "")
        self._spider_sessions: set[str] = (
            {s.strip().lower() for s in _spider_sessions.split(",") if s.strip()}
            if _spider_sessions else set()
        )
        self._last_pairing_wait_log = 0.0
        self._last_source_health_status: tuple[str, str] | None = None
        self._last_source_health_write = 0.0
        # Send-side intentionally dropped: no bulk_send_enabled / hourly cap /
        # daily cap / membership gating. This collector is RECEIVE-ONLY.

    def _is_spider_allowed(self, session_name: str) -> bool:
        if not self._spider_sessions:
            return True
        return session_name.lower() in self._spider_sessions

    async def _record_http_event(
        self,
        *,
        session_name: str | None,
        scope: str,
        status_code: int | None,
        reason: str,
        metadata: dict | None = None,
    ) -> bool:
        if status_code not in {401, 403, 429}:
            return False
        await record_rate_limit_event(
            self.pool,
            source=self.SOURCE_NAME,
            account=session_name or "bridge",
            scope=scope,
            status_code=status_code,
            cooldown_seconds=self._session_cooldown if status_code == 429 else None,
            reason=reason,
            metadata=metadata or {},
        )
        if status_code == 429:
            h = self._session_health.setdefault(
                session_name or "bridge",
                {"errors": 0, "risk": 0.0, "cooldown_until": 0},
            )
            h["cooldown_until"] = max(
                float(h.get("cooldown_until") or 0),
                time.time() + self._session_cooldown,
            )
        return True

    async def _record_http_exception(
        self,
        exc: BaseException,
        *,
        session_name: str | None,
        scope: str,
        reason_prefix: str,
        metadata: dict | None = None,
    ) -> bool:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        try:
            status_code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        return await self._record_http_event(
            session_name=session_name,
            scope=scope,
            status_code=status_code,
            reason=f"{reason_prefix}: HTTP {status_code}" if status_code else reason_prefix,
            metadata={**(metadata or {}), "error": str(exc)[:500]},
        )

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
        await self._restore_session_cooldowns()

        tasks = []
        if self._broker_channel:
            tasks.append(asyncio.create_task(self._consume_broker(targets)))
        else:
            tasks.append(asyncio.create_task(self._poll_sessions(targets)))

        if self._session_bridges:
            tasks.append(asyncio.create_task(self._bridge_pairing_health_loop()))
        tasks.append(asyncio.create_task(self._media_archival_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup_connections()

    async def _restore_session_cooldowns(self):
        if self._session_cooldowns_restored or self.pool is None:
            return
        self._session_cooldowns_restored = True
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT account,
                           MAX(created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second')) AS cooldown_until
                    FROM rate_limit_events
                    WHERE source = $1
                      AND status_code = 429
                      AND account IS NOT NULL
                      AND COALESCE(cooldown_seconds, 0) > 0
                      AND created_at + (COALESCE(cooldown_seconds, 0) * INTERVAL '1 second') > NOW()
                    GROUP BY account
                    """,
                    self.SOURCE_NAME,
                )
        except Exception as exc:
            logger.debug("WhatsApp session cooldown restore failed: %s", exc)
            return
        now_utc = datetime.now(timezone.utc)
        restored = 0
        for row in rows:
            account = str(row["account"] or "").strip()
            if not account or not row["cooldown_until"]:
                continue
            cooldown_until = row["cooldown_until"]
            if cooldown_until.tzinfo is None:
                cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
            remaining = (cooldown_until - now_utc).total_seconds()
            if remaining <= 0:
                continue
            h = self._session_health.setdefault(
                account,
                {"errors": 0, "risk": 0.0, "cooldown_until": 0},
            )
            h["cooldown_until"] = max(float(h.get("cooldown_until") or 0), time.time() + remaining)
            restored += 1
        if restored:
            logger.info("WhatsApp restored %d active per-session cooldown(s)", restored)

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
        """Resilient wrapper: auto-reconnect with backoff if the broker
        connection/channel/iterator drops. Previously a single drop killed the
        consume task permanently — 0 consumers on `unifiedcollector.messages`
        while the bridge kept publishing, so messages piled up in RabbitMQ and
        never reached Postgres (observed: 990-msg backlog, WhatsApp 3 days stale).
        Now it re-inits the channel and re-consumes instead of needing a manual
        container restart. (2026-07-17 hardening)"""
        import asyncio as _aio
        backoff = 5
        while not self._stop.is_set():
            try:
                if self._broker_channel is None or getattr(self._broker_channel, "is_closed", False):
                    await self._init_broker()
                if self._broker_channel is None:
                    raise RuntimeError("RabbitMQ channel unavailable")
                await self._consume_broker_once(targets)
                backoff = 5  # returned cleanly (stop requested)
            except _aio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "WhatsApp broker consumer dropped; reconnecting in %ss: %s",
                    backoff, e,
                )
                old_conn = self._broker_conn
                self._broker_channel = None  # force re-init on next loop
                self._broker_conn = None
                if old_conn is not None:
                    try:
                        await old_conn.close()
                    except Exception:
                        pass
                await _aio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _consume_broker_once(self, targets: list[str]):
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

        # Groups queue: group subject/description/member-count metadata
        group_queue = await self._broker_channel.declare_queue(
            "unifiedcollector.groups", durable=True,
        )
        await group_queue.bind(exchange, routing_key="groups.#")

        # Session queue: bridge online/offline/heartbeat events. These are not
        # user messages, so they must not update whatsapp_messages freshness, but
        # they do prove the bridge and RabbitMQ path are alive.
        session_queue = await self._broker_channel.declare_queue(
            "unifiedcollector.sessions", durable=True,
        )
        await session_queue.bind(exchange, routing_key="session.#")

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

        async def _consume_groups():
            async with group_queue.iterator() as qi:
                async for message in qi:
                    if self._stop.is_set():
                        break
                    async with message.process():
                        try:
                            body = json.loads(message.body.decode())
                            await self._handle_group_event(body)
                        except Exception as e:
                            logger.debug("Group event processing failed: %s", e)

        async def _consume_sessions():
            async with session_queue.iterator() as qi:
                async for message in qi:
                    if self._stop.is_set():
                        break
                    async with message.process():
                        try:
                            body = json.loads(message.body.decode())
                            await self._handle_session_event(body)
                        except Exception as e:
                            logger.debug("Session event processing failed: %s", e)

        async def _consume_messages():
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

        tasks = [
            asyncio.create_task(_consume_messages()),
            asyncio.create_task(_consume_contacts()),
            asyncio.create_task(_consume_groups()),
            asyncio.create_task(_consume_sessions()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            first_error: BaseException | None = None
            for task in done:
                try:
                    exc = task.exception()
                except asyncio.CancelledError as exc:
                    first_error = exc
                    break
                if exc is not None:
                    first_error = exc
                    break
            if first_error is not None:
                raise first_error
            if not self._stop.is_set() and pending:
                raise RuntimeError("WhatsApp broker consumer exited unexpectedly")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_session_event(self, event: dict):
        """Persist bridge session heartbeats without pretending messages flowed."""
        if not self.pool:
            return
        session_name = (
            event.get("session_name")
            or (event.get("_metadata") or {}).get("session_name")
            or "unknown"
        )
        status = str(event.get("status") or "heartbeat").strip() or "heartbeat"
        phone = event.get("phone_number") or event.get("phone")
        detail = f"WhatsApp bridge {session_name} {status}"
        if phone:
            digits = "".join(ch for ch in str(phone) if ch.isdigit())
            masked = f"...{digits[-4:]}" if len(digits) >= 4 else "<redacted>"
            detail += f" ({masked})"
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO source_health (source, status, last_success_at, last_error, updated_at)
                VALUES ('whatsapp', 'running', NOW(), NULL, NOW())
                ON CONFLICT (source) DO UPDATE SET
                    status='running',
                    last_success_at=NOW(),
                    last_error=NULL,
                    updated_at=NOW()
                """,
            )
        logger.debug(detail)

    async def _handle_contact_event(self, event: dict):
        """Maintain whatsapp_users and whatsapp_lid_map from contacts.update events.

        When Baileys syncs contacts it may provide both the phone-based JID
        (event['jid']) and the linked-device ID (event['lid']). We store this
        mapping so _track_user_profile can resolve @lid → phone JID for
        group message senders.
        """
        lid = event.get("lid")
        jid = event.get("jid") or event.get("phone_jid")
        platform_user_id = event.get("platform_user_id") or jid or lid
        if not platform_user_id:
            return

        valid_lid_mapping = (
            isinstance(lid, str) and "@lid" in lid
            and isinstance(jid, str) and "@s.whatsapp.net" in jid
        )
        is_user_jid = isinstance(platform_user_id, str) and (
            "@s.whatsapp.net" in platform_user_id or "@lid" in platform_user_id
        )
        if not valid_lid_mapping and not is_user_jid:
            return

        target_tables = ["whatsapp_users"] if is_user_jid else []
        if valid_lid_mapping:
            target_tables.insert(0, "whatsapp_lid_map")
        archive_id = lid if valid_lid_mapping else platform_user_id
        self._archive_raw_event(
            artifact_id=f"contacts/{archive_id}",
            payload=event,
            target_tables=target_tables,
            metadata={
                "lid": lid,
                "phone_jid": jid,
                "platform_user_id": platform_user_id,
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "contact",
            },
        )
        if not self.pool:
            return
        display_name = _first_nonempty(
            event.get("display_name"),
            event.get("name"),
            event.get("notify"),
            event.get("verified_name"),
            event.get("verifiedBizName"),
            event.get("pushName"),
            event.get("push_name"),
        )
        push_name = _first_nonempty(event.get("pushName"), event.get("push_name"), event.get("notify"), display_name)
        phone_number = event.get("phone_number")
        user_jid_for_phone = jid if isinstance(jid, str) and "@s.whatsapp.net" in jid else platform_user_id
        if not phone_number and isinstance(user_jid_for_phone, str) and "@s.whatsapp.net" in user_jid_for_phone:
            prefix = user_jid_for_phone.split("@")[0]
            if re.fullmatch(r"\d{7,15}", prefix):
                phone_number = prefix
        try:
            async with self.pool.acquire() as conn:
                if is_user_jid:
                    await conn.execute("""
                        INSERT INTO whatsapp_users (platform_user_id, name, pushname, phone_number, is_business, collected_at)
                        VALUES ($1, $2, $3, $4, $5, NOW())
                        ON CONFLICT (platform_user_id) DO UPDATE SET
                            name = COALESCE(EXCLUDED.name, whatsapp_users.name),
                            pushname = COALESCE(EXCLUDED.pushname, whatsapp_users.pushname),
                            phone_number = COALESCE(EXCLUDED.phone_number, whatsapp_users.phone_number),
                            is_business = COALESCE(whatsapp_users.is_business, FALSE)
                                OR COALESCE(EXCLUDED.is_business, FALSE),
                            collected_at = NOW()
                    """, platform_user_id, display_name, push_name,
                        phone_number or None, _coerce_bool(event.get("is_business")))
                if valid_lid_mapping:
                    await conn.execute("""
                        INSERT INTO whatsapp_lid_map (lid, phone_jid, display_name, updated_at)
                        VALUES ($1, $2, $3, NOW())
                        ON CONFLICT (lid) DO UPDATE SET
                            phone_jid = EXCLUDED.phone_jid,
                            display_name = COALESCE(EXCLUDED.display_name, whatsapp_lid_map.display_name),
                            updated_at = NOW()
                    """, lid, jid, display_name)
        except Exception as e:
            logger.debug("contact upsert failed: %s", e)

    async def _handle_group_event(self, event: dict):
        """Upsert WhatsApp group metadata from groups.update bridge events."""
        chat_jid = event.get("chat_jid") or event.get("id") or event.get("jid")
        if not chat_jid or "@g.us" not in str(chat_jid):
            return

        name = _first_nonempty(event.get("name"), event.get("subject"), event.get("chat_name"))
        description = _first_nonempty(event.get("description"), event.get("desc"))
        participant_count = _first_nonempty(event.get("participant_count"), event.get("size"))
        if participant_count is None and isinstance(event.get("participants"), list) and not event.get("action"):
            participant_count = len(event["participants"])
        try:
            participant_count = int(participant_count) if participant_count is not None else None
        except (TypeError, ValueError):
            participant_count = None

        self._archive_raw_event(
            artifact_id=f"groups/{chat_jid}",
            payload=event,
            target_tables=["whatsapp_chats"],
            metadata={
                "platform_chat_id": chat_jid,
                "session_name": event.get("session_name"),
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "group",
            },
        )
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO whatsapp_chats
                        (platform_chat_id, name, is_group, chat_type, participant_count, description, updated_at)
                    VALUES ($1, $2, TRUE, 'group', $3, $4, NOW())
                    ON CONFLICT (platform_chat_id) DO UPDATE SET
                        name = COALESCE(EXCLUDED.name, whatsapp_chats.name),
                        is_group = TRUE,
                        chat_type = 'group',
                        participant_count = COALESCE(EXCLUDED.participant_count, whatsapp_chats.participant_count),
                        description = COALESCE(EXCLUDED.description, whatsapp_chats.description),
                        updated_at = NOW()
                """, chat_jid, name, participant_count, description)
        except Exception as e:
            if "chat_type" not in str(e):
                logger.debug("group metadata upsert failed: %s", e)
                return
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO whatsapp_chats
                            (platform_chat_id, name, is_group, participant_count, description, updated_at)
                        VALUES ($1, $2, TRUE, $3, $4, NOW())
                        ON CONFLICT (platform_chat_id) DO UPDATE SET
                            name = COALESCE(EXCLUDED.name, whatsapp_chats.name),
                            is_group = TRUE,
                            participant_count = COALESCE(EXCLUDED.participant_count, whatsapp_chats.participant_count),
                            description = COALESCE(EXCLUDED.description, whatsapp_chats.description),
                            updated_at = NOW()
                    """, chat_jid, name, participant_count, description)
            except Exception as fallback_exc:
                logger.debug("group metadata fallback upsert failed: %s", fallback_exc)

    async def _handle_message_event(self, event: dict, targets: list[str]):
        # WhatsApp "delete for everyone" (revoke) — flag the original message + when.
        if event.get("deletion"):
            rid_for_archive = event.get("revoked_message_id") or event.get("message_id") or event.get("key", {}).get("id", "")
            if rid_for_archive:
                self._archive_raw_event(
                    artifact_id=f"messages/{rid_for_archive}/deletion",
                    payload=event,
                    target_tables=["whatsapp_messages"],
                    metadata={
                        "platform_message_id": rid_for_archive,
                        "ingest_path": self.INGEST_PATH,
                        "raw_payload_kind": "message_deletion",
                    },
                )
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

        self._archive_raw_event(
            artifact_id=f"messages/{chat_jid}/{msg_id}",
            payload=event,
            target_tables=["whatsapp_messages"],
            metadata={
                "platform_message_id": msg_id,
                "platform_chat_id": chat_jid,
                "session_name": event.get("session_name"),
                "ingest_path": self.INGEST_PATH,
                "raw_payload_kind": "message",
            },
        )

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

        # Tier 5: shared/live-location messages -> structured lat/lng.
        await self._extract_wa_location(event, chat_jid, msg_id)

        if self._link_discovery_enabled:
            text = event.get("body", "") or event.get("text", "") or event.get("caption", "")
            if text:
                await self._discover_links(text, chat_jid, session=session)

        # 3. Handle Media if exists
        media_type = event.get("media_type") or event.get("messageType", "")
        has_media = media_type in ("imageMessage", "videoMessage", "audioMessage", "documentMessage", "stickerMessage")

        if not has_media:
            # Check if it has a media_url or directPath even if type is not explicit
            if not (event.get("media_url") or event.get("directPath")):
                return

        classified = self._classify_bridge_media(media_type, event)
        if classified is None:
            return
        ext, content_type = classified
        cid = f"wa_{msg_id}"

        if self.is_known(cid):
            return

        media_key = event.get("mediaKey")
        direct_path = event.get("directPath")

        if media_key and direct_path and session:
            data = await self._download_via_bridge(
                session,
                msg_id,
                media_key,
                direct_path,
                mimetype=event.get("mimetype") or event.get("mime_type"),
            )
        else:
            media_url = event.get("media_url", "")
            data = await self._download_direct(media_url) if media_url else None

        if data:
            await self._save_media(data, cid, chat_jid, chat_name, content_type, ext, event)

    def _archive_raw_event(
        self,
        *,
        artifact_id: str,
        payload: dict,
        target_tables: list[str],
        metadata: dict | None = None,
    ) -> None:
        if not _tier1_raw_archives_enabled():
            return
        try:
            result = write_raw_payload(
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                payload=payload,
                metadata=metadata or {},
                target_tables=target_tables,
                root=VAULT_ROOT,
            )
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=result,
                metadata=metadata,
                log=logger,
            )
        except Exception as exc:
            logger.debug("whatsapp raw archive failed for %s: %s", artifact_id, exc)
            report_raw_archive_result(
                self.pool,
                source=self.SOURCE_NAME,
                artifact_id=artifact_id,
                result=None,
                metadata=metadata,
                log=logger,
                error=str(exc),
            )

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
        from_me = _coerce_bool(_first_nonempty(
            event.get("from_me"),
            event.get("fromMe"),
            event.get("key", {}).get("fromMe"),
        ))
        quoted_message_id = _first_nonempty(event.get("quoted_message_id"), event.get("quoted_msg_id"))
        quoted_text = event.get("quoted_text")
        forward_from_name = _first_nonempty(
            event.get("forward_from_name"),
            event.get("forwarded_newsletter_name"),
            event.get("forwardFromName"),
        )
        
        async with self.pool.acquire() as conn:
            chat_row = await conn.fetchrow("SELECT id FROM whatsapp_chats WHERE platform_chat_id = $1", chat_jid)
            chat_uuid = chat_row['id'] if chat_row else None
            
            await conn.execute("""
                INSERT INTO whatsapp_messages (
                    platform_message_id, chat_id, sender_id, from_me,
                    text, media_mime_type, quoted_message_id, quoted_text,
                    forward_from_name, timestamp, metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (platform_message_id) DO UPDATE SET
                    chat_id = COALESCE(whatsapp_messages.chat_id, EXCLUDED.chat_id),
                    sender_id = COALESCE(EXCLUDED.sender_id, whatsapp_messages.sender_id),
                    from_me = COALESCE(EXCLUDED.from_me, whatsapp_messages.from_me),
                    text = COALESCE(NULLIF(EXCLUDED.text, ''), whatsapp_messages.text),
                    media_mime_type = COALESCE(NULLIF(EXCLUDED.media_mime_type, ''), whatsapp_messages.media_mime_type),
                    quoted_message_id = COALESCE(NULLIF(EXCLUDED.quoted_message_id, ''), whatsapp_messages.quoted_message_id),
                    quoted_text = COALESCE(NULLIF(EXCLUDED.quoted_text, ''), whatsapp_messages.quoted_text),
                    forward_from_name = COALESCE(NULLIF(EXCLUDED.forward_from_name, ''), whatsapp_messages.forward_from_name),
                    timestamp = COALESCE(EXCLUDED.timestamp, whatsapp_messages.timestamp),
                    metadata = COALESCE(whatsapp_messages.metadata, '{}'::jsonb) || COALESCE(EXCLUDED.metadata, '{}'::jsonb)
            """,
            msg_id, chat_uuid, sender_uuid, from_me,
            text, event.get("mimetype"), quoted_message_id, quoted_text,
            forward_from_name, dt, json.dumps(event)
            )

    @staticmethod
    def _build_whatsapp_source_url(chat_jid: str | None, cid: str | None) -> str | None:
        """Stable message-URI for media_items.source_url.

        WhatsApp media has no publicly-openable URL: the CDN URL is an
        expiring, mediaKey-encrypted reference that only the account owner
        can decrypt. But the (chat_jid, message_id) tuple IS a stable
        globally-unique identifier, so we encode it as a whatsapp:// URI:

            whatsapp://<chat_jid>/<message_id>

        The scheme isn't publicly resolvable but it (a) preserves message
        lineage for unifiedanalyzer joins, (b) is a stable, unique key,
        and (c) fits URI semantics — clearly better than NULL for
        downstream analytics. content_id inside this collector is
        ``wa_<message_id>``; we strip that prefix so the URI carries the
        raw platform message id."""
        if not chat_jid or not cid:
            return None
        msg_id = cid[len("wa_"):] if cid.startswith("wa_") else cid
        if not msg_id:
            return None
        return f"whatsapp://{chat_jid}/{msg_id}"

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

        try:
            source_url = self._build_whatsapp_source_url(chat_jid, cid)
            metadata = {
                "entity_id": entity_id,
                "entity_name": chat_name,
                "content_type": content_type,
                "content_id": cid,
                "filename": filename,
                "source_url": source_url,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "raw": event or {},
                "rebuild_target_tables": ["media_items", "whatsapp_messages"],
            }
            # WhatsApp status ("stories") arrive on the status@broadcast JID.
            # Tag them kind='story' (same convention as Instagram/Telegram) so
            # the dashboard Stories view surfaces them.
            _kind = "story" if str(chat_jid) == "status@broadcast" else None
            artifact = write_atomic_artifact(
                source=self.SOURCE_NAME,
                artifact_id=f"{chat_jid}:{cid}",
                artifact_kind="media_blob",
                data=data,
                extension=ext,
                metadata={**metadata, "kind": _kind},
                root=VAULT_ROOT,
            )
            if not artifact.path:
                raise RuntimeError(f"vault artifact write failed: {artifact.error}")
            metadata["vault_artifact"] = {
                "ok": artifact.ok,
                "partial": artifact.partial,
                "path": artifact.relative_path,
                "blob_path": artifact.blob_relative_path,
                "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
                "duplicate_blob": artifact.duplicate_blob,
                "error": artifact.error,
            }

            self.circuit_breaker.record_success()

            await self.insert_media_item(
                entity_id=entity_id,
                entity_name=chat_name,
                content_type=content_type,
                content_id=cid,
                filename=filename,
                file_path=str(artifact.path),
                file_size=artifact.file_size,
                sha256=artifact.sha256,
                metadata=metadata,
                source_url=source_url,
                kind=_kind,
            )
            if artifact.partial:
                await self.send_to_dlq(
                    entity_id,
                    cid,
                    f"vault artifact partial: {artifact.error}",
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
                            str(artifact.path), artifact.file_size, cid[3:],
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

        # ── User-intelligence diff (Tier 4): snapshot the row BEFORE upserting
        # so UserChangeTracker can compare old → new and emit one row per
        # changed field into whatsapp_user_changes. Wrapped in try/except so
        # any failure (DB, schema drift, etc.) is non-fatal to ingestion.
        prev_row = None
        try:
            async with self.pool.acquire() as conn:
                prev_row = await conn.fetchrow(
                    "SELECT name, pushname, is_business "
                    "FROM whatsapp_users WHERE platform_user_id = $1",
                    sender_jid,
                )
        except Exception as exc:
            logger.debug("user_change_tracker[whatsapp]: prev-row fetch failed: %s", exc)

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
                        updated_at = NOW()
                    RETURNING id
                """, sender_jid, payload["display_name"], payload["push_name"],
                    payload["phone_number"] or None, payload["is_business"])
        except Exception as e:
            logger.debug("User profile tracking failed: %s", e)
            return None

        # ── Change-log write (non-fatal). Field names match whatsapp_users
        # column names, so prev_row passes through unmodified. Empty pushname
        # normalizes to None inside the tracker (partial payload — skipped),
        # mirroring the COALESCE semantics of the upsert above.
        try:
            tracker = UserChangeTracker(self.pool)
            new_snapshot = {
                "name":        payload["display_name"],
                "pushname":    payload["push_name"] or None,
                "is_business": payload["is_business"],
            }
            await tracker.detect_and_log(
                table="whatsapp_user_changes",
                pk_col="user_id",
                pk_val=str(sender_jid),
                current_row=dict(prev_row) if prev_row is not None else None,
                new_row=new_snapshot,
                fields=WHATSAPP_TRACKED_FIELDS,
            )
        except Exception as exc:
            logger.debug("user_change_tracker[whatsapp]: detect_and_log failed: %s", exc)

        return row['id'] if row else None

    async def _poll_sessions(self, targets: list[str]):
        while not self._stop.is_set():
            saw_ready_bridge = False
            waiting_sessions: list[str] = []
            missing_credential_sessions: list[str] = []
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
                            await self._record_http_event(
                                session_name=session_name,
                                scope="bridge_health",
                                status_code=resp.status_code,
                                reason=f"WhatsApp bridge health HTTP {resp.status_code}",
                                metadata={"bridge_url": bridge_url},
                            )
                            continue

                        health = resp.json()
                        # Bridge contract is {"status":"ok","whatsapp_ready":bool},
                        # NOT {"status":"connected"}. Accept either the ready flag or
                        # a connected/ok status so the HTTP-poll fallback isn't skipped
                        # forever on a correctly-running bridge.
                        if not health.get("whatsapp_ready") and health.get("status") not in ("connected", "ok"):
                            status = str(health.get("status") or "not_ready").lower()
                            if status in {"awaiting_scan", "refreshing_qr", "requesting_fresh_qr", "waiting_for_fresh_qr"} or health.get("qr_available"):
                                waiting_sessions.append(session_name)
                                auth_state = health.get("auth_state") if isinstance(health.get("auth_state"), dict) else {}
                                if health.get("needs_scan") or auth_state.get("creds_json_exists") is False:
                                    missing_credential_sessions.append(session_name)
                            logger.debug("Session %s not ready: %s", session_name, health.get("status"))
                            continue

                        saw_ready_bridge = True
                        resp = await client.get(
                            f"{bridge_url}/messages/recent",
                            params={"limit": self._backfill_batch},
                            headers=self._bridge_headers(),
                        )
                        if resp.status_code != 200:
                            await self._record_http_event(
                                session_name=session_name,
                                scope="recent_messages",
                                status_code=resp.status_code,
                                reason=f"WhatsApp recent messages HTTP {resp.status_code}",
                                metadata={"bridge_url": bridge_url},
                            )
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

            if saw_ready_bridge:
                await self._mark_source_bridge_ready()
            elif waiting_sessions:
                now = time.time()
                detail = self._format_pairing_wait_detail(
                    waiting_sessions,
                    missing_credential_sessions,
                )
                await self._mark_source_waiting_for_pairing(detail)
                if now - self._last_pairing_wait_log > 300:
                    self._last_pairing_wait_log = now
                    logger.warning(detail)

            await asyncio.sleep(self._backfill_poll)

    @staticmethod
    def _format_pairing_wait_detail(
        waiting_sessions: list[str],
        missing_credential_sessions: list[str] | None = None,
    ) -> str:
        missing = set(missing_credential_sessions or [])
        labels = []
        for session_name in sorted(set(waiting_sessions)):
            if session_name in missing:
                labels.append(f"{session_name} (no saved session credentials; scan required)")
            else:
                labels.append(session_name)
        return (
            "WhatsApp bridge waiting for QR pairing: "
            + ", ".join(labels)
            + ". Open Link WhatsApp and scan a fresh QR."
        )

    async def _bridge_pairing_health_loop(self):
        while not self._stop.is_set():
            try:
                await self._update_bridge_pairing_health()
            except Exception:
                logger.debug("WhatsApp bridge pairing health check failed", exc_info=True)
            await asyncio.sleep(max(15, self._pairing_health_interval))

    async def _update_bridge_pairing_health(self):
        saw_ready_bridge = False
        waiting_sessions: list[str] = []
        missing_credential_sessions: list[str] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for session_name, bridge_url in self._session_bridges.items():
                try:
                    resp = await client.get(f"{bridge_url}/health")
                except Exception:
                    logger.debug("WhatsApp bridge health probe failed for %s", session_name, exc_info=True)
                    continue
                if resp.status_code != 200:
                    logger.debug(
                        "WhatsApp bridge health probe for %s returned HTTP %s",
                        session_name,
                        resp.status_code,
                    )
                    continue
                health = resp.json()
                status = str(health.get("status") or "not_ready").lower()
                if health.get("whatsapp_ready") or status in {"connected", "ok"}:
                    saw_ready_bridge = True
                    continue
                if status in {"awaiting_scan", "refreshing_qr", "requesting_fresh_qr", "waiting_for_fresh_qr"} or health.get("qr_available"):
                    waiting_sessions.append(session_name)
                    auth_state = health.get("auth_state") if isinstance(health.get("auth_state"), dict) else {}
                    if health.get("needs_scan") or auth_state.get("creds_json_exists") is False:
                        missing_credential_sessions.append(session_name)

        if saw_ready_bridge:
            await self._mark_source_bridge_ready()
        elif waiting_sessions:
            now = time.time()
            detail = self._format_pairing_wait_detail(
                waiting_sessions,
                missing_credential_sessions,
            )
            await self._mark_source_waiting_for_pairing(detail)
            if now - self._last_pairing_wait_log > 300:
                self._last_pairing_wait_log = now
                logger.warning(detail)

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
            await self._record_http_exception(
                exc,
                session_name=session_name,
                scope="backfill_request",
                reason_prefix=f"WhatsApp backfill request failed for {chat_jid}",
                metadata={"chat_jid": chat_jid, "bridge_url": bridge_url},
            )
            return None

    async def _extract_wa_location(self, event: dict, chat_jid: str, msg_id: str):
        """Tier 5: pull coords out of a WhatsApp location / live-location message
        into whatsapp_message_locations. Defensive about the bridge's event shape
        (flat or nested location/locationMessage/liveLocationMessage). Best-effort
        — never breaks message handling."""
        if not self.pool or not msg_id:
            return
        try:
            mt = (event.get("media_type") or event.get("messageType") or "").lower()
            loc = None
            for key in ("location", "locationMessage", "liveLocationMessage"):
                v = event.get(key)
                if isinstance(v, dict):
                    loc = v
                    break
            if loc is None and "location" in mt:
                loc = event  # flat shape: coords live on the event itself
            if not isinstance(loc, dict):
                return
            lat = loc.get("degreesLatitude", loc.get("latitude", loc.get("lat")))
            lng = loc.get("degreesLongitude", loc.get("longitude", loc.get("lng", loc.get("long"))))
            if lat is None or lng is None:
                return
            is_live = ("live" in mt) or bool(loc.get("sequenceNumber") or loc.get("isLive"))
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO whatsapp_message_locations
                        (platform_message_id, chat_jid, latitude, longitude, is_live, name, address)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (platform_message_id) DO NOTHING
                    """,
                    str(msg_id), chat_jid, float(lat), float(lng), is_live,
                    loc.get("name"), loc.get("address"),
                )
        except Exception as e:
            logger.debug("wa location extract failed for %s: %s", msg_id, e)

    async def _discover_links(self, text: str, chat_jid: str, session: str = ""):
        """Extract WhatsApp links and persist them for downstream discovery."""
        try:
            links = extract_all_links(text or "")
        except Exception as e:
            logger.debug("link extraction failed: %s", e)
            return
        if not links:
            return
        if not self.pool:
            return
        try:
            from urllib.parse import urlparse
            async with self.pool.acquire() as conn:
                # The real table is wa_discovered_links (chat_id UUID FK, url,
                # domain, link_type, ...). The old code targeted a non-existent
                # `discovered_links` with the wrong schema, so every link was
                # silently dropped (wa_discovered_links stayed 0). Resolve the
                # chat UUID (nullable — still store links from unknown chats).
                chat_uuid = await conn.fetchval(
                    "SELECT id FROM whatsapp_chats WHERE platform_chat_id = $1",
                    chat_jid,
                )
                spider_ok = self._is_spider_allowed(session) if session else True
                for url, kind in links:
                    if isinstance(url, str) and isinstance(kind, str):
                        if (
                            url in {"url", "group_invite", "group_invite_restricted", "contact_link"}
                            and kind.startswith(("http://", "https://"))
                        ):
                            url, kind = kind, url
                    effective_kind = kind
                    if kind == "group_invite" and not spider_ok:
                        effective_kind = "group_invite_restricted"
                    try:
                        domain = (urlparse(url).netloc or None)
                    except Exception:
                        domain = None
                    try:
                        await conn.execute(
                            """
                            INSERT INTO wa_discovered_links
                                (chat_id, url, domain, link_type, discovered_at)
                            SELECT $1, $2, $3, $4, NOW()
                            WHERE NOT EXISTS (
                                SELECT 1 FROM wa_discovered_links
                                WHERE url = $2 AND chat_id IS NOT DISTINCT FROM $1
                            )
                            """,
                            chat_uuid, url, domain, effective_kind,
                        )
                    except Exception as e:
                        logger.debug("wa_discovered_links insert skipped (%s): %s", url, e)
        except Exception as e:
            logger.debug("discover_links db failure: %s", e)

    async def _media_archival_loop(self):
        """Periodic re-download for messages where media archival has not yet
        completed. Mirrors media_archival/worker semantics: pull pending items,
        decrypt via bridge, persist."""
        while not self._stop.is_set():
            try:
                if self._session_bridges and not await self._any_media_bridge_ready():
                    logger.debug("WhatsApp media archival deferred; no paired bridge is ready")
                    await asyncio.sleep(max(self._media_poll, self._bridge_ready_ttl))
                    continue
                if self.pool:
                    async with self.pool.acquire() as conn:
                        rows = await conn.fetch(
                            """
                            SELECT platform_message_id, metadata
                            FROM whatsapp_messages
                            WHERE media_url IS NULL
                              AND metadata ? 'mediaKey'
                              AND metadata ? 'directPath'
                              AND NULLIF(metadata->>'mediaKey', '') IS NOT NULL
                              AND NULLIF(metadata->>'directPath', '') IS NOT NULL
                              AND COALESCE(metadata->>'media_archival_status', '') NOT IN ('unavailable', 'unsupported')
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

    async def _any_media_bridge_ready(self) -> bool:
        for session, bridge_url in self._session_bridges.items():
            if await self._bridge_ready_for_media(session, bridge_url):
                return True
        return False

    async def _mark_source_waiting_for_pairing(self, detail: str):
        """Record an operator-actionable WhatsApp state without treating it as a crash."""
        state = ("degraded", detail)
        now = time.time()
        if self._last_source_health_status == state and now - self._last_source_health_write < 300:
            return
        self._last_source_health_status = state
        self._last_source_health_write = now
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO source_health (source, status, last_error, updated_at)
                    VALUES ($1, 'degraded', $2, NOW())
                    ON CONFLICT (source) DO UPDATE SET
                      status='degraded',
                      last_error=EXCLUDED.last_error,
                      updated_at=NOW()
                    """,
                    self.SOURCE_NAME,
                    detail,
                )
        except Exception:
            logger.debug("WhatsApp source_health pairing-state update failed", exc_info=True)

    async def _mark_source_bridge_ready(self):
        state = ("running", "")
        if self._last_source_health_status == state:
            return
        self._last_source_health_status = state
        self._last_source_health_write = time.time()
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO source_health (source, status, last_success_at, last_error, updated_at)
                    VALUES ($1, 'running', NOW(), NULL, NOW())
                    ON CONFLICT (source) DO UPDATE SET
                      status='running',
                      last_success_at=NOW(),
                      last_error=NULL,
                      updated_at=NOW()
                    """,
                    self.SOURCE_NAME,
                )
        except Exception:
            logger.debug("WhatsApp source_health ready update failed", exc_info=True)

    async def _bridge_ready_for_media(self, session: str, bridge_url: str) -> bool:
        now = time.time()
        cached = self._bridge_ready_cache.get(session)
        if cached and now - float(cached.get("ts") or 0.0) < self._bridge_ready_ttl:
            return bool(cached.get("ready"))
        # Only defer when the bridge explicitly reports "not ready". If the
        # health probe itself is inconclusive, keep the old decrypt-attempt path.
        ready = True
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{bridge_url}/health")
                if resp.status_code == 200:
                    health = resp.json()
                    ready = bool(
                        health.get("whatsapp_ready")
                        or health.get("connected")
                        or health.get("registered")
                        or health.get("status") == "connected"
                    )
        except Exception as exc:
            logger.debug("WhatsApp bridge health check failed for %s: %s", session, exc)
        self._bridge_ready_cache[session] = {"ts": now, "ready": ready}
        return ready

    async def _download_via_bridge(self, session: str, msg_id: str,
                                    media_key: str, direct_path: str,
                                    mimetype: str | None = None) -> bytes | None:
        bridge_url = self._session_bridges.get(session)
        if not bridge_url:
            return None
        if not await self._bridge_ready_for_media(session, bridge_url):
            logger.debug("WhatsApp bridge %s not ready; deferring media decrypt %s", session, msg_id)
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
                            "mimetype": mimetype,
                        },
                        headers={
                            **self._bridge_headers(),
                            "X-Timestamp": timestamp,
                            "X-Signature": sig,
                        },
                    )
                    if resp.status_code == 200:
                        return resp.content
                    error_payload = self._bridge_error_payload(resp)
                    error_code = str(error_payload.get("code") or "")
                    retryable = error_payload.get("retryable")
                    error_text = str(error_payload.get("error") or "").strip()
                    if (
                        retryable is False
                        or resp.status_code in {410, 422}
                        or error_code in {"media_unavailable", "media_decrypt_failed"}
                    ):
                        await self._mark_media_unavailable(
                            msg_id=msg_id,
                            session=session,
                            status_code=resp.status_code,
                            code=error_code or f"http_{resp.status_code}",
                            reason=error_text or f"WhatsApp media decrypt HTTP {resp.status_code}",
                        )
                        logger.info(
                            "Bridge media unavailable %s: HTTP %d %s",
                            msg_id,
                            resp.status_code,
                            error_code or "",
                        )
                        return None
                    logger.warning("Bridge decrypt failed %s: %d", msg_id, resp.status_code)
                    await self._record_http_event(
                        session_name=session,
                        scope="media_decrypt",
                        status_code=resp.status_code,
                        reason=f"WhatsApp media decrypt HTTP {resp.status_code}",
                        metadata={
                            "message_id": msg_id,
                            "bridge_url": bridge_url,
                            "error_code": error_code or None,
                            "retryable": retryable,
                            "error": error_text or None,
                        },
                    )
        except Exception as e:
            logger.error("Bridge download failed %s: %s", msg_id, e)
            await self._record_http_exception(
                e,
                session_name=session,
                scope="media_decrypt",
                reason_prefix=f"WhatsApp bridge download failed for {msg_id}",
                metadata={"message_id": msg_id, "bridge_url": bridge_url},
            )
        return None

    @staticmethod
    def _bridge_error_payload(resp: httpx.Response) -> dict:
        try:
            payload = resp.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _mark_media_unavailable(
        self,
        *,
        msg_id: str,
        session: str | None,
        status_code: int,
        code: str,
        reason: str,
    ) -> None:
        if not self.pool or not msg_id:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE whatsapp_messages
                    SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                        'media_archival_status', 'unavailable',
                        'media_archival_retryable', false,
                        'media_unavailable_at', NOW(),
                        'media_unavailable_code', $2,
                        'media_unavailable_reason', $3,
                        'media_unavailable_status_code', $4,
                        'media_unavailable_session', $5
                    )
                    WHERE platform_message_id = $1
                    """,
                    msg_id,
                    code[:120],
                    reason[:500],
                    int(status_code),
                    session,
                )
        except Exception:
            logger.debug("wa media-unavailable marker failed for %s", msg_id, exc_info=True)

    async def _download_direct(self, url: str) -> bytes | None:
        try:
            async with self._sem:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.content
        except Exception as e:
            logger.error("Direct download failed: %s", e)
            await self._record_http_exception(
                e,
                session_name="direct",
                scope="direct_media",
                reason_prefix="WhatsApp direct media download failed",
                metadata={"url": url},
            )
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

    @staticmethod
    def _event_filename(event: dict) -> str | None:
        for key in ("fileName", "filename", "file_name", "title", "body", "text"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _ext_from_event(mime: str | None, filename: str | None, fallback: str) -> str:
        if filename and "." in Path(filename).name:
            return Path(filename).name.rsplit(".", 1)[-1].lower()[:12]
        clean = (mime or "").split(";")[0].strip().lower()
        return _MIME_EXT.get(clean, fallback)

    def _classify_bridge_media(self, media_type: str, event: dict) -> tuple[str, str] | None:
        content_type = self._media_type_to_content_type(media_type)
        fallback_ext = self._media_type_to_ext(media_type)

        if media_type in {"imageMessage", "videoMessage"}:
            return fallback_ext, content_type

        mime = event.get("mimetype") or event.get("mime_type")
        filename = self._event_filename(event)
        decision = classify_document(
            mime,
            filename,
            is_sticker=media_type == "stickerMessage",
            is_audio=media_type == "audioMessage",
            is_video=media_type == "videoMessage",
        )
        if not decision.download:
            logger.info(
                "Skipping WhatsApp media %s (%s): %s",
                event.get("message_id") or event.get("id") or "<unknown>",
                media_type or "unknown",
                decision.reason,
            )
            return None
        return self._ext_from_event(mime, filename, fallback_ext), decision.content_type

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
