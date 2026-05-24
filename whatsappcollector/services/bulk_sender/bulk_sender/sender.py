from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .observability import get_logger

from .config import settings
from .database import database

logger = get_logger(__name__)


def file_sha256(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def effective_external_hourly_cap(configured: int) -> int:
    return min(int(configured), 30)


def _is_old_enough(joined_at, min_age_hours: int) -> bool:
    if joined_at is None:
        return False
    # DB stores naive UTC timestamps; compare against naive UTC now
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    if hasattr(joined_at, 'tzinfo') and joined_at.tzinfo is not None:
        # If the DB ever returns aware datetimes, normalize to naive UTC
        joined_at = joined_at.astimezone(timezone.utc).replace(tzinfo=None)
    delta = now_utc - joined_at
    return delta.total_seconds() >= max(min_age_hours, 24) * 3600


class InMemoryRateStore:
    def __init__(self) -> None:
        self._daily_counts: dict[str, int] = {}
        self._hour_events: dict[str, list[float]] = {}

    def daily_key(self, session_name: str) -> str:
        return f"bulk:daily:{session_name}:{datetime.now(timezone.utc).date().isoformat()}"

    async def inc_daily(self, session_name: str) -> int:
        key = self.daily_key(session_name)
        self._daily_counts[key] = self._daily_counts.get(key, 0) + 1
        return self._daily_counts[key]

    async def get_daily(self, session_name: str) -> int:
        return self._daily_counts.get(self.daily_key(session_name), 0)

    async def push_hour_event(self, session_name: str) -> None:
        now = datetime.now(timezone.utc).timestamp()
        events = self._hour_events.get(session_name, [])
        events = [ev for ev in events if now - ev < 3600]
        events.append(now)
        self._hour_events[session_name] = events

    async def count_hour(self, session_name: str) -> int:
        now = datetime.now(timezone.utc).timestamp()
        events = [ev for ev in self._hour_events.get(session_name, []) if now - ev < 3600]
        self._hour_events[session_name] = events
        return len(events)


class RedisRateStore:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self.redis = None

    async def connect(self) -> None:
        import redis.asyncio as aioredis

        self.redis = await aioredis.from_url(self.redis_url, decode_responses=True)

    @staticmethod
    def _daily_key(session_name: str) -> str:
        return f"bulk:daily:{session_name}:{datetime.now(timezone.utc).date().isoformat()}"

    @staticmethod
    def _hour_key(session_name: str) -> str:
        return f"bulk:hour:{session_name}"

    def _require(self):
        if not self.redis:
            raise RuntimeError("Redis rate store not initialized")
        return self.redis

    async def inc_daily(self, session_name: str) -> int:
        redis = self._require()
        key = self._daily_key(session_name)
        value = await redis.incr(key)
        await redis.expire(key, 60 * 60 * 48)
        return int(value)

    async def get_daily(self, session_name: str) -> int:
        redis = self._require()
        value = await redis.get(self._daily_key(session_name))
        return int(value or 0)

    async def push_hour_event(self, session_name: str) -> None:
        redis = self._require()
        now = datetime.now(timezone.utc).timestamp()
        key = self._hour_key(session_name)
        score = str(now)
        await redis.zadd(key, {score: now})
        await redis.zremrangebyscore(key, 0, now - 3600)
        await redis.expire(key, 60 * 60 * 2)

    async def count_hour(self, session_name: str) -> int:
        redis = self._require()
        now = datetime.now(timezone.utc).timestamp()
        key = self._hour_key(session_name)
        await redis.zremrangebyscore(key, 0, now - 3600)
        return int(await redis.zcount(key, now - 3600, now))


@dataclass
class SendResult:
    sent: bool
    wa_message_id: str | None = None
    reason: str | None = None


class BulkSender:
    def __init__(self) -> None:
        self._memory_rate_store = InMemoryRateStore()
        self.rate_store = self._memory_rate_store
        self._rate_store_initialized = False
        self._redis_rate_store = RedisRateStore(settings.REDIS_URL) if settings.REDIS_URL else None
        self._bridge_secret = settings.MEDIA_BRIDGE_SECRET.encode("utf-8") if settings.MEDIA_BRIDGE_SECRET else b""
        self._session_bridges = self._parse_session_bridges(settings.SESSION_BRIDGES_JSON)

    @staticmethod
    def _parse_session_bridges(raw: str) -> dict[str, str]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v).rstrip("/") for k, v in parsed.items() if str(v).strip()}
        except Exception:
            logger.warning("bulk_sender_invalid_session_bridges_json")
        return {}

    @staticmethod
    def _session_container_name(session_name: str) -> str:
        normalized = (session_name or "").strip()
        if normalized.startswith("session_"):
            suffix = normalized.split("session_", 1)[1]
            if suffix.isdigit():
                return f"wa-client-ts-{suffix}"
        return f"wa-client-ts-{normalized.replace('_', '-')}"

    def _bridge_url_for_session(self, session_name: str) -> str:
        if session_name in self._session_bridges:
            return self._session_bridges[session_name]

        default_bridge = str(settings.MEDIA_BRIDGE_URL or "").strip().rstrip("/")
        if session_name in {"session_1", "default"} and default_bridge:
            return default_bridge

        host = self._session_container_name(session_name)
        return f"http://{host}:{settings.MEDIA_BRIDGE_PORT}"

    _ensure_lock: asyncio.Lock | None = None

    async def _ensure_rate_store(self) -> None:
        # Double-checked locking: skip the lock after first init (hot path)
        if self._rate_store_initialized:
            return
        # Lazy-init the lock (can't create it at __init__ time — no running loop yet)
        if self._ensure_lock is None:
            self._ensure_lock = asyncio.Lock()
        async with self._ensure_lock:
            if self._rate_store_initialized:  # recheck under lock
                return
            if self._redis_rate_store:
                try:
                    await self._redis_rate_store.connect()
                    self.rate_store = self._redis_rate_store
                    logger.info("bulk_sender_rate_store_initialized", backend="redis")
                except Exception as exc:
                    logger.warning("bulk_sender_rate_store_fallback", backend="memory", error=str(exc))
                    self.rate_store = self._memory_rate_store
            self._rate_store_initialized = True

    async def send_media(self, session_name: str, target_chat_jid: str, file_path: str) -> SendResult:
        if not self._bridge_secret:
            return SendResult(sent=False, reason="media_bridge_secret_missing")

        payload = {
            "session_name": session_name,
            "target_chat_jid": target_chat_jid,
            "file_path": file_path,
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        signature = hmac.new(self._bridge_secret, payload_bytes, hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-Signature": signature,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._bridge_url_for_session(session_name).rstrip('/')}/send-media",
                    content=payload_bytes,
                    headers=headers,
                )
        except Exception as exc:
            return SendResult(sent=False, reason=f"bridge_request_failed:{exc}")

        if response.status_code == 200:
            try:
                body = response.json()
            except Exception:
                body = {}
            return SendResult(sent=True, wa_message_id=body.get("message_id") or body.get("wa_message_id"))

        reason = f"bridge_status_{response.status_code}"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                reason = str(body["error"])
        except Exception:
            pass
        return SendResult(sent=False, reason=reason)

    async def run_internal_send(self, job, target_chat_jid: str, file_path: str) -> SendResult:
        await self._ensure_rate_store()
        hourly_cap = 200
        if await self.rate_store.count_hour(job["session_name"]) >= hourly_cap:
            return SendResult(sent=False, reason="hourly_rate_limited")

        result = await self.send_media(job["session_name"], target_chat_jid, file_path)
        if result.sent:
            await self.rate_store.push_hour_event(job["session_name"])

        base = max(0.0, settings.BULK_SENDER_INTERNAL_MIN_DELAY)
        await asyncio.sleep(max(0.0, base + random.uniform(-0.5, 0.5)))
        return result

    async def run_external_send(self, job, target_chat_jid: str, file_path: str) -> SendResult:
        await self._ensure_rate_store()
        if not bool(job["operator_confirmed"]):
            return SendResult(sent=False, reason="operator_confirmation_required")

        if not await database.is_session_connected(str(job["session_name"])):
            await database.set_job_cooldown(int(job["id"]), minutes=30)
            return SendResult(sent=False, reason="session_disconnected_cooldown")

        joined_at = await database.get_membership_joined_at(str(job["session_name"]), target_chat_jid)
        if not _is_old_enough(joined_at, settings.BULK_SENDER_MEMBERSHIP_MIN_AGE_HOURS):
            return SendResult(sent=False, reason="membership_too_new")

        if await self.rate_store.get_daily(str(job["session_name"])) >= 150:
            return SendResult(sent=False, reason="daily_cap_reached")

        hourly_cap = effective_external_hourly_cap(settings.BULK_SENDER_EXTERNAL_MAX_PER_HOUR)
        if await self.rate_store.count_hour(str(job["session_name"])) >= hourly_cap:
            return SendResult(sent=False, reason="hourly_rate_limited")

        result = await self.send_media(str(job["session_name"]), target_chat_jid, file_path)
        if result.sent:
            await self.rate_store.push_hour_event(str(job["session_name"]))
            await self.rate_store.inc_daily(str(job["session_name"]))

        base = max(0.0, settings.BULK_SENDER_EXTERNAL_MIN_DELAY)
        await asyncio.sleep(max(0.0, base + random.uniform(-4.0, 4.0)))
        return result


sender = BulkSender()
