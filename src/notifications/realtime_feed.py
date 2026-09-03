"""Real-time per-post Telegram feed.

Every newly-inserted media_items row is enqueued (best-effort, never blocking
the collector) into a Redis list. A separate ``realtime_feed`` service (this
module, run as ``python -m src.notifications.realtime_feed``) drains that list,
downloads or resolves the media, and posts a small Telegram message via
``send_photo`` / ``send_video``.

Design points:
  * The collector's insertion path never blocks or raises on this. If Redis is
    down, ``enqueue_from_insert`` silently no-ops.
  * Rate-limit: token bucket in Redis with a configurable per-minute cap.
    Bursts above the cap are requeued/deferred and summarized every 15 minutes;
    stored media is not dropped just because Telegram notification pacing is
    saturated.
  * Dedupe: sha256/source-url keys seen in the last N days prevent repeated
    operator-chat posts. Public/social sources dedupe globally so the same
    photo from Threads/Lemon8 only posts once; private chat sources stay
    source-scoped.
  * On Telegram 429: exponential backoff, respecting the ``retry_after``
    parameter Telegram supplies.

Env knobs (all optional; safe defaults):
  REALTIME_POST_FEED_ENABLED           default "1"
  REALTIME_POST_FEED_QUEUE_KEY         default "uc:realtime_post_feed"
  REALTIME_POST_FEED_MAX_PER_MINUTE    default "12"
  REALTIME_POST_FEED_DEDUPE_TTL_DAYS   default "7"
  REALTIME_POST_FEED_INCLUDE_PROFILES  default "0"  (skip profile updates)
  REALTIME_POST_FEED_DEDUPE_BY_MEDIA   default "1"  (sha/url dedupe)
  REALTIME_POST_FEED_GLOBAL_MEDIA_DEDUPE_SOURCES
                                      default public/social sources
  REALTIME_POST_FEED_SKIP_VIDEO_THUMBNAILS default "1"  (skip poster-only video rows)
  REALTIME_POST_FEED_SOURCE_SUMMARY_SECONDS default "900" (per-source status digest)
  REDIS_URL / REDIS_HOST / REDIS_PASSWORD  (same pattern as io_pacer)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import os
import re
import signal
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


# -- Public constants -----------------------------------------------------

QUEUE_KEY_DEFAULT = "uc:realtime_post_feed"
SEEN_SHA_KEY_DEFAULT = "uc:realtime_post_feed:seen_sha"
DEFERRED_KEY_DEFAULT = "uc:realtime_post_feed:skipped_burst"  # legacy key name
SKIPPED_KEY_DEFAULT = DEFERRED_KEY_DEFAULT  # compatibility for dashboard/tests
FAILED_KEY_DEFAULT = "uc:realtime_post_feed:failed"
LOCAL_FALLBACK_TOTAL_KEY = "uc:realtime_post_feed:local_fallback_total"
LOCAL_FALLBACK_BY_SOURCE_KEY = "uc:realtime_post_feed:local_fallback_by_source"
LOCAL_FALLBACK_BY_REASON_KEY = "uc:realtime_post_feed:local_fallback_by_reason"
LOCAL_FALLBACK_BY_SOURCE_REASON_KEY = "uc:realtime_post_feed:local_fallback_by_source_reason"
LOCAL_FALLBACK_LAST_KEY = "uc:realtime_post_feed:local_fallback_last"
LAST_BURST_REPORT_KEY = "uc:realtime_post_feed:last_burst_report"
SOURCE_COUNTER_TOTALS_KEY = "uc:realtime_post_feed:source_counters_total"
SOURCE_COUNTER_WINDOW_KEY = "uc:realtime_post_feed:source_counters_window"
LAST_SOURCE_REPORT_KEY = "uc:realtime_post_feed:last_source_report"

ALLOWED_KINDS = frozenset({"image", "video", "post", "photo"})
GLOBAL_MEDIA_DEDUPE_SOURCES_DEFAULT = frozenset({
    "instagram",
    "threads",
    "tiktok",
    "lemon8",
    "facebook",
    "x",
    "twitter",
    "youtube",
    "website",
    "search",
    "github",
})
SOURCE_COUNTER_OUTCOMES = frozenset({
    "sent",
    "deferred",
    "deduped",
    "too_large",
    "local_fallback",
    "failed",
})
FALLBACK_REASON_BUCKETS = frozenset({
    "remote_fetch_failed",
    "local_missing",
    "unsupported",
    "telegram_error",
    "too_large",
})


# -- Env helpers ----------------------------------------------------------

def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, *, min_value: int = 0) -> int:
    try:
        val = int(os.getenv(name, str(default)).strip() or default)
    except (TypeError, ValueError):
        val = default
    return max(min_value, val)


def _queue_key() -> str:
    return os.getenv("REALTIME_POST_FEED_QUEUE_KEY", QUEUE_KEY_DEFAULT).strip() or QUEUE_KEY_DEFAULT


def _csv_set(name: str, default: frozenset[str]) -> frozenset[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    values = frozenset(item.strip().lower() for item in raw.split(",") if item.strip())
    return values or default


def _redis_url() -> str:
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        return url
    host = os.getenv("REDIS_HOST", "redis").strip() or "redis"
    pw = os.getenv("REDIS_PASSWORD", "").strip()
    auth = f":{pw}@" if pw else ""
    return f"redis://{auth}{host}:6379/0"


# -- Payload construction -------------------------------------------------

def _metadata_text(meta: dict | None, *keys: str) -> str | None:
    if not isinstance(meta, dict):
        return None
    for key in keys:
        value = meta.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None

def build_payload(*, source: str, entity_name: str, content_id: str,
                  file_path: str | None, source_url: str | None,
                  sha256: str | None, metadata: dict | None,
                  kind: str | None, content_type: str | None,
                  file_size: int | None = None) -> dict:
    """Build the JSON payload the drain consumes.

    Kept side-effect-free so ``enqueue_from_insert`` can call it from the
    collector's hot path without touching the DB or network.
    """
    meta = metadata or {}
    vault_path = None
    if isinstance(meta, dict):
        vault_path = meta.get("vault_path") or meta.get("legacy_path")
    caption = ""
    if isinstance(meta, dict):
        caption = str(
            meta.get("caption")
            or meta.get("text")
            or meta.get("post_caption")
            or meta.get("description")
            or ""
        )
    return {
        "source": str(source or "").strip().lower(),
        "author": str(entity_name or "").strip(),
        "content_id": str(content_id or ""),
        "file_path": str(file_path or "") or None,
        "vault_path": str(vault_path or "") or None,
        "source_url": str(source_url or "") or None,
        "post_url": _metadata_text(
            meta,
            "post_url",
            "page_url",
            "canonical_url",
            "verify_url",
            "permalink",
        ),
        "sha256": str(sha256 or "") or None,
        "file_size": file_size,
        "caption": caption,
        "sender_id": _metadata_text(
            meta,
            "platform_sender_id",
            "telegram_sender_id",
            "sender_platform_id",
            "sender_id",
        ),
        "media_role": _metadata_text(
            meta,
            "tiktok_asset_role",
            "dom_asset_role",
            "asset_role",
            "media_role",
            "x_asset_role",
        ),
        "is_video": bool(meta.get("is_video")) if isinstance(meta, dict) and "is_video" in meta else None,
        "kind": str(kind or "").strip().lower() or None,
        "content_type": str(content_type or "").strip().lower() or None,
        "enqueued_at": time.time(),
    }


def enqueue_from_insert(*, source: str, entity_name: str, content_id: str,
                        file_path: str | None, source_url: str | None,
                        sha256: str | None, metadata: dict | None,
                        kind: str | None, content_type: str | None,
                        file_size: int | None = None) -> dict[str, str | None]:
    """Best-effort fire-and-forget enqueue from inside a running event loop.

    Never raises. Never blocks. If Redis or the feed is disabled/unavailable
    it silently no-ops so the collector's insertion path is unaffected.
    """
    if not _flag("REALTIME_POST_FEED_ENABLED", "1"):
        return {"status": "stored_only", "reason": "realtime_feed_disabled"}
    try:
        payload = build_payload(
            source=source, entity_name=entity_name, content_id=content_id,
            file_path=file_path, source_url=source_url, sha256=sha256,
            metadata=metadata, kind=kind, content_type=content_type,
            file_size=file_size,
        )
    except Exception:
        logger.debug("realtime_feed build_payload failed", exc_info=True)
        return {"status": "stored_only", "reason": "build_payload_failed"}
    skip_reason = _skip_reason(payload)
    if skip_reason:
        return {"status": "skipped", "reason": skip_reason}
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called from a sync context (unlikely — insert_media_item is async).
        # Best-effort: fire in a fresh loop for good measure, but do not raise.
        try:
            asyncio.run(_enqueue(payload))
        except Exception:
            logger.debug("realtime_feed sync enqueue failed", exc_info=True)
            return {"status": "stored_only", "reason": "redis_unavailable"}
        return {"status": "enqueued", "reason": None}
    loop.create_task(_enqueue(payload))
    return {"status": "enqueued", "reason": None}


def _logs_chat_id() -> int | None:
    """Return the chat_id of the log group whose telegram messages must NOT be
    re-broadcast (circular-loop safety). Falls back to TELEGRAM_CHAT_ID and then
    NOTIFY_TELEGRAM_CHAT_ID if the dedicated var is unset. Returns None when
    none of the vars is set to a parseable integer — filter degrades to allow
    (the primary defence is in the telegram collector itself)."""
    for name in ("TELEGRAM_LOGS_CHAT_ID", "TELEGRAM_CHAT_ID", "NOTIFY_TELEGRAM_CHAT_ID"):
        raw = os.getenv(name, "")
        raw = raw.strip() if isinstance(raw, str) else ""
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def _notify_bot_user_id() -> int | None:
    raw = os.getenv("UC_NOTIFY_BOT_USER_ID", "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _is_own_logs_chat_telegram(payload: dict) -> bool:
    """Defence-in-depth for telegram payloads from the realtime log chat.

    If the notifier bot id and sender id are both known, only the bot's own
    messages are dropped. That prevents the circular loop while still allowing
    operator/manual media posted in the logs chat to flow through. If either id
    is missing, fall back to dropping the whole logs chat for safety.

    Telegram media_items use content_id shape ``<chat_id>_<message_id>`` (see
    src/collectors/telegram._telegram_message_content_id), which is stable
    enough to parse without touching the DB.
    """
    if (payload.get("source") or "").lower() != "telegram":
        return False
    logs_chat = _logs_chat_id()
    if logs_chat is None:
        return False
    content_id = payload.get("content_id") or ""
    prefix, sep, _rest = str(content_id).partition("_")
    if not sep or not prefix:
        return False
    try:
        chat_id = int(prefix)
    except ValueError:
        return False
    if chat_id != logs_chat:
        return False
    bot_id = _notify_bot_user_id()
    sender_raw = payload.get("sender_id") or payload.get("telegram_sender_id")
    try:
        sender_id = int(str(sender_raw).strip()) if sender_raw is not None else None
    except ValueError:
        sender_id = None
    if bot_id is not None and sender_id is not None:
        return sender_id == bot_id
    return True


def _passes_filter(payload: dict) -> bool:
    """Filter posts to what the operator actually wants surfaced in real time.

    * Any collector source is allowed through. This feed is the operator's
      cross-source media/log surface, so source filtering belongs in collection
      config, not here.
    * Telegram payloads from our own logs chat are sender-aware when possible:
      bot-authored messages are dropped, human/manual messages can pass.
    * Kind (post/image/video/photo) or content_type must be recognised media.
    * We need either a caption or a downloadable file to make a useful message.
    * Profile updates are opt-in via REALTIME_POST_FEED_INCLUDE_PROFILES.
    """
    return _skip_reason(payload) is None


def _skip_reason(payload: dict) -> str | None:
    if _is_own_logs_chat_telegram(payload):
        return "filtered_by_policy"
    include_profiles = _flag("REALTIME_POST_FEED_INCLUDE_PROFILES", "0")
    content_type = (payload.get("content_type") or "").lower()
    if content_type == "profile_photo" and not include_profiles:
        return "profile_photo_skipped"
    if (
        _flag("REALTIME_POST_FEED_SKIP_VIDEO_THUMBNAILS", "1")
        and _looks_like_video_thumbnail(payload)
    ):
        return "video_thumbnail_skipped"
    kind = (payload.get("kind") or "").lower()
    if kind not in ALLOWED_KINDS and content_type not in ALLOWED_KINDS:
        # Unknown media kinds still get through if they at least look like a
        # post (have a source_url) — but pure metadata rows are dropped.
        if not payload.get("source_url"):
            return "filtered_by_policy"
    caption = (payload.get("caption") or "").strip()
    file_path = payload.get("file_path") or payload.get("vault_path")
    if not caption and not file_path and not payload.get("source_url"):
        return "missing_file_or_url"
    return None


def _payload_metadata(payload: dict) -> dict:
    metadata = payload.get("metadata")
    out = dict(metadata) if isinstance(metadata, dict) else {}
    for key in ("media_role", "is_video"):
        if payload.get(key) is not None:
            out[key] = payload.get(key)
    return out


def _looks_like_video_thumbnail(payload: dict) -> bool:
    """Detect poster/cover rows that are only thumbnails for a video post."""
    content_type = str(payload.get("content_type") or "").strip().lower()
    kind = str(payload.get("kind") or "").strip().lower()
    if content_type == "video" or kind == "video":
        return False
    metadata = _payload_metadata(payload)
    role_values = [
        metadata.get("tiktok_asset_role"),
        metadata.get("dom_asset_role"),
        metadata.get("asset_role"),
        metadata.get("media_role"),
        metadata.get("x_asset_role"),
    ]
    role = " ".join(str(v or "").strip().lower() for v in role_values if v is not None)
    if any(marker in role for marker in ("video_poster", "poster", "cover")):
        return True
    if metadata.get("is_video") is True and content_type in {"photo", "image", "post_image"}:
        return True
    return False


async def _redis_client():
    """Return a ready redis.asyncio client, or None if unavailable."""
    try:
        import redis.asyncio as aioredis  # local import — keeps the collector
                                          # hot path free of a redis dependency
                                          # if the feed is disabled.
    except Exception as e:
        logger.debug("realtime_feed: redis module unavailable: %s", e)
        return None
    url = _redis_url()
    try:
        client = aioredis.from_url(
            url, decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2,
        )
        await client.ping()
        return client
    except Exception as e:
        logger.debug("realtime_feed: redis connect failed (%s)", e)
        return None


async def _enqueue(payload: dict) -> None:
    client = await _redis_client()
    if client is None:
        from src.notifications import realtime_delivery
        await realtime_delivery.record_from_payload(
            payload,
            status="stored_only",
            reason="redis_unavailable",
        )
        return
    try:
        await client.rpush(_queue_key(), json.dumps(payload, default=str))
    except Exception:
        logger.debug("realtime_feed enqueue rpush failed", exc_info=True)
        from src.notifications import realtime_delivery
        await realtime_delivery.record_from_payload(
            payload,
            status="stored_only",
            reason="redis_unavailable",
        )
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


# -- Rate limit (token bucket in Redis) -----------------------------------

async def _acquire_token(client, *, capacity: int) -> bool:
    """Refill and consume a token. Returns True on success (allowed to send).

    Refill window is one minute — we hand out ``capacity`` tokens per rolling
    60s. When empty, the caller must record a skip.
    """
    if capacity <= 0:
        return False
    key_count = "uc:realtime_post_feed:tokens_count"
    key_ts = "uc:realtime_post_feed:tokens_refilled_at"
    now = time.time()
    try:
        last_refill = await client.get(key_ts)
        last_refill_f = float(last_refill) if last_refill else 0.0
    except Exception:
        last_refill_f = 0.0

    if now - last_refill_f >= 60 or last_refill_f == 0.0:
        # Refill the bucket and reset the window.
        with contextlib.suppress(Exception):
            await client.set(key_count, capacity)
            await client.set(key_ts, now)

    try:
        remaining = await client.decr(key_count)
    except Exception:
        return True  # fail-open: if Redis errors mid-loop, still send.
    if remaining is None:
        return True
    if int(remaining) < 0:
        # Bucket empty for this minute; restore the counter to 0 to avoid
        # wrapping and record a skip in the caller.
        with contextlib.suppress(Exception):
            await client.set(key_count, 0)
        return False
    return True


def _rate_limit_delay_seconds(capacity: int) -> float:
    return max(2.0, min(30.0, 60.0 / max(1, capacity)))


async def _record_deferred(client) -> None:
    with contextlib.suppress(Exception):
        await client.incr(DEFERRED_KEY_DEFAULT)


async def _record_failed_delivery(client, payload: dict, raw: str) -> None:
    failure = {
        "failed_at": time.time(),
        "reason": "telegram_delivery_failed",
        "payload": payload,
        "raw": raw,
    }
    with contextlib.suppress(Exception):
        await client.rpush(FAILED_KEY_DEFAULT, json.dumps(failure, default=str))


def _declared_local_media_path(payload: dict) -> str | None:
    for candidate in (payload.get("file_path"), payload.get("vault_path")):
        text = str(candidate or "").strip()
        if text and not text.startswith(("http://", "https://")):
            return text
    return None


def _fallback_bucket_for(
    payload: dict,
    *,
    outcome: str,
    target: str | None,
    ledger_status: str,
    ledger_reason: str | None,
    telegram_result: dict[str, Any],
) -> str | None:
    error_code = str(telegram_result.get("error_code") or "").lower()
    description = str(telegram_result.get("description") or "").lower()
    reason = str(ledger_reason or "").lower()
    if ledger_status == "too_large" or reason == "telegram_too_large" or error_code == "too_large":
        return "too_large"
    if outcome == "remote_text_fallback":
        return "remote_fetch_failed"
    declared_local = _declared_local_media_path(payload)
    if outcome == "text_only" and declared_local and not Path(declared_local).exists():
        return "local_missing"
    if target and not str(target).startswith(("http://", "https://")) and not Path(str(target)).exists():
        return "local_missing"
    unsupported_markers = (
        "image_process_failed",
        "wrong file identifier/http url specified",
        "unsupported",
        "can't parse",
        "could not process",
    )
    if any(marker in error_code or marker in description for marker in unsupported_markers):
        return "unsupported"
    if outcome == "local_media_text_fallback" or (
        ledger_status == "failed" and str(target or "").startswith(("http://", "https://"))
    ):
        return "telegram_error"
    return None


async def _record_local_media_fallback(
    client,
    payload: dict,
    target: str | None,
    *,
    bucket: str | None = None,
) -> None:
    source = str(payload.get("source") or "unknown").strip().lower() or "unknown"
    bucket = bucket if bucket in FALLBACK_REASON_BUCKETS else "telegram_error"
    record = {
        "at": time.time(),
        "source": source,
        "content_id": payload.get("content_id"),
        "target_name": Path(str(target)).name if target else None,
        "reason_bucket": bucket,
    }
    with contextlib.suppress(Exception):
        await client.incr(LOCAL_FALLBACK_TOTAL_KEY)
    with contextlib.suppress(Exception):
        await client.hincrby(LOCAL_FALLBACK_BY_SOURCE_KEY, source, 1)
    with contextlib.suppress(Exception):
        await client.hincrby(LOCAL_FALLBACK_BY_REASON_KEY, bucket, 1)
    with contextlib.suppress(Exception):
        await client.hincrby(LOCAL_FALLBACK_BY_SOURCE_REASON_KEY, f"{source}:{bucket}", 1)
    with contextlib.suppress(Exception):
        await client.set(LOCAL_FALLBACK_LAST_KEY, json.dumps(record, default=str))


def source_counters_from_hash(raw: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    counters: dict[str, dict[str, int]] = {}
    if not raw:
        return counters
    for field, count in raw.items():
        source, sep, outcome = str(field or "").partition(":")
        if not sep or outcome not in SOURCE_COUNTER_OUTCOMES:
            continue
        source = source.strip().lower() or "unknown"
        try:
            value = int(count or 0)
        except (TypeError, ValueError):
            value = 0
        counters.setdefault(source, {})[outcome] = value
    return counters


def _source_label(source: str) -> str:
    source = str(source or "").strip().lower()
    return _PLATFORM_LABEL.get(source, source.replace("_", " ").title() or "Unknown")


def _source_counter_outcome(status: str, reason: str | None) -> str | None:
    status = str(status or "").strip().lower()
    reason = str(reason or "").strip().lower()
    if status == "delivered":
        if reason == "local_media_text_fallback":
            return "local_fallback"
        return "sent"
    if status == "enqueued":
        return "deferred"
    if status == "deduped":
        return "deduped"
    if status == "too_large":
        return "too_large"
    if status == "failed":
        return "failed"
    return None


async def _record_source_counter(
    client,
    payload: dict,
    *,
    status: str,
    reason: str | None = None,
) -> None:
    outcome = _source_counter_outcome(status, reason)
    if outcome is None:
        return
    source = str(payload.get("source") or "unknown").strip().lower() or "unknown"
    field = f"{source}:{outcome}"
    with contextlib.suppress(Exception):
        await client.hincrby(SOURCE_COUNTER_TOTALS_KEY, field, 1)
    with contextlib.suppress(Exception):
        await client.hincrby(SOURCE_COUNTER_WINDOW_KEY, field, 1)


async def _dedupe_seen(client, sha: str, *, ttl_days: int) -> bool:
    """Return True if we've seen this occurrence recently."""
    if not sha:
        return False
    key = f"{SEEN_SHA_KEY_DEFAULT}:{sha}"
    try:
        seen = await client.get(key)
    except Exception:
        return False
    return bool(seen)


async def _dedupe_seen_any(client, keys: list[str], *, ttl_days: int) -> str | None:
    """Return the first recently seen dedupe key, if any."""
    for key in keys:
        if await _dedupe_seen(client, key, ttl_days=ttl_days):
            return key
    return None


async def _mark_dedupe_seen(client, sha: str, *, ttl_days: int) -> None:
    """Record a delivered/non-retryable occurrence in the dedupe set.

    This deliberately happens after delivery. If Telegram returns a 429, the
    item is requeued and must not be poisoned by a pre-delivery dedupe mark.
    """
    if not sha:
        return
    ttl = max(1, ttl_days) * 86400
    key = f"{SEEN_SHA_KEY_DEFAULT}:{sha}"
    with contextlib.suppress(Exception):
        await client.set(key, "1", ex=ttl)


async def _mark_dedupe_seen_many(client, keys: list[str], *, ttl_days: int) -> None:
    for key in keys:
        await _mark_dedupe_seen(client, key, ttl_days=ttl_days)


# -- Caption formatting ---------------------------------------------------

_PLATFORM_LABEL = {
    "instagram": "Instagram", "threads": "Threads",
    "x": "Twitter / X", "twitter": "Twitter / X",
    "tiktok": "TikTok", "lemon8": "Lemon8",
    "facebook": "Facebook", "strava": "Strava",
    "telegram": "Telegram", "whatsapp": "WhatsApp",
    "youtube": "YouTube", "website": "Website",
    "search": "Search", "github": "GitHub",
    "beeper": "Beeper",
    "beeper_discord": "Discord via Beeper",
    "beeper_slack": "Slack via Beeper",
    "beeper_linkedin": "LinkedIn via Beeper",
    "beeper_telegram": "Telegram via Beeper",
    "beeper_whatsapp": "WhatsApp via Beeper",
    "beeper_instagram": "Instagram via Beeper",
    "beeper_signal": "Signal via Beeper",
    "beeper_facebook_messenger": "Facebook/Messenger via Beeper",
    "beeper_google_chat": "Google Chat via Beeper",
    "beeper_beeper_matrix": "Beeper Matrix",
}


def _source_anchor(source_url: str, *, max_len: int) -> str:
    if not source_url:
        return ""
    link = f'<a href="{html.escape(source_url, quote=True)}">source</a>'
    # Some social CDN/story URLs are extremely long. Keeping that link can make
    # Telegram's final caption trim cut inside the href and break HTML parsing.
    if len(link) > max_len:
        return ""
    return link


def _escape_body_for_room(body: str, room: int) -> str:
    if room <= 0 or not body:
        return ""
    escaped = html.escape(body)
    if len(escaped) <= room:
        return escaped
    suffix = "…"
    room = max(0, room - len(suffix))
    candidate = body[:room]
    while candidate and len(html.escape(candidate)) > room:
        candidate = candidate[:-1]
    return html.escape(candidate).rstrip() + suffix if candidate else suffix


def format_caption(payload: dict, *, max_len: int | None = None) -> str:
    from src.notifications.telegram import MAX_CAPTION_CHARS
    limit = int(max_len or MAX_CAPTION_CHARS)
    platform = _PLATFORM_LABEL.get(payload.get("source") or "", payload.get("source") or "?")
    author = payload.get("author") or "?"
    body = (payload.get("caption") or "").strip()
    source_url = payload.get("source_url") or ""
    header = f"<b>{html.escape(platform)}</b> — <b>{html.escape(author)}</b>"
    source_part = _source_anchor(str(source_url), max_len=max(160, limit // 3))
    parts = [header]
    if body:
        parts.append(html.escape(body))
    if source_part:
        parts.append(source_part)
    text = "\n".join(parts)
    if len(text) > limit:
        # Trim body, keep header + URL intact.
        if source_part and len(header) + len(source_part) + 4 >= limit:
            source_part = ""
        overhead = len(header) + (len(source_part) if source_part else 0) + 4
        room = max(20, limit - overhead - 1)
        body_short = _escape_body_for_room(body, room)
        parts = [header]
        if body_short:
            parts.append(body_short)
        if source_part:
            parts.append(source_part)
        text = "\n".join(parts)
    return text


def _local_media_text_fallback(caption: str, target: str) -> str:
    path = str(target or "").strip()
    note = (
        "\n\n"
        "<i>Telegram upload failed; media remains in Collector vault.</i>"
    )
    if path:
        label = path if _flag("REALTIME_POST_FEED_INCLUDE_LOCAL_PATHS", "0") else Path(path).name
        if label:
            note += f"\n<code>{html.escape(label)}</code>"
    text = f"{caption}{note}"
    max_len = 3800
    if len(text) > max_len:
        keep = max(200, max_len - len(note) - 1)
        text = caption[:keep].rstrip() + "…" + note
    return text


# -- Drain loop -----------------------------------------------------------

def _resolve_media_path(payload: dict) -> Optional[str]:
    """Prefer a local file (from the vault) over a fetched URL."""
    for candidate in (payload.get("file_path"), payload.get("vault_path")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _looks_like_video(payload: dict) -> bool:
    kind = (payload.get("kind") or "").lower()
    content_type = (payload.get("content_type") or "").lower()
    if kind == "video" or "video" in content_type:
        return True
    file_path = payload.get("file_path") or payload.get("vault_path") or ""
    if file_path.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".m4v")):
        return True
    return False


def _payload_file_size(payload: dict, target: str | None = None) -> int | None:
    try:
        value = payload.get("file_size")
        if value not in {None, ""}:
            return int(value)
    except (TypeError, ValueError):
        pass
    if target and not str(target).startswith(("http://", "https://")):
        try:
            return Path(str(target)).stat().st_size
        except OSError:
            return None
    return None


def _telegram_media_cap(payload: dict) -> int:
    if _looks_like_video(payload):
        return 45 * 1024 * 1024
    return 10 * 1024 * 1024


def _delivery_status(
    payload: dict,
    *,
    delivered: bool,
    retry_after: int,
    outcome: str,
    target: str | None,
) -> tuple[str, str | None]:
    if outcome == "local_media_text_fallback":
        size = _payload_file_size(payload, target)
        if size is not None and size > _telegram_media_cap(payload):
            return "too_large", "telegram_too_large"
        return "delivered", "local_media_text_fallback"
    if delivered:
        return "delivered", outcome
    if retry_after > 0:
        return "enqueued", "telegram_retry_after"
    return "failed", "telegram_send_failed"


async def _flush_deferred_summary(client) -> None:
    """Send a 'deferred N burst' summary if the last flush was >15 min ago."""
    if not _flag("REALTIME_POST_FEED_BURST_SUMMARY", "1"):
        return
    try:
        now = time.time()
        last = await client.get(LAST_BURST_REPORT_KEY)
        last_f = float(last) if last else 0.0
        interval = _int("REALTIME_POST_FEED_BURST_SUMMARY_SECONDS", 900, min_value=60)
        if now - last_f < interval:
            return
        deferred = await client.get(DEFERRED_KEY_DEFAULT)
        deferred_n = int(deferred) if deferred else 0
        if deferred_n <= 0:
            await client.set(LAST_BURST_REPORT_KEY, now)
            return
        # Reset first (race safe: worst case we double-report a couple).
        await client.delete(DEFERRED_KEY_DEFAULT)
        await client.set(LAST_BURST_REPORT_KEY, now)
    except Exception:
        return
    try:
        from src.notifications import telegram
        await telegram.send(
            f"<b>Realtime feed</b>\n"
            f"Deferred {deferred_n:,} posts "
            f"({_int('REALTIME_POST_FEED_MAX_PER_MINUTE', 12, min_value=1)}/min cap). "
            f"Queued for retry."
        )
    except Exception:
        logger.debug("realtime_feed burst summary send failed", exc_info=True)


def _source_summary_lines(counters: dict[str, dict[str, int]], *, limit: int = 8) -> list[str]:
    labels = {
        "sent": "media delivered",
        "deferred": "deferred",
        "deduped": "deduped",
        "too_large": "too large",
        "local_fallback": "upload fallback",
        "failed": "failed",
    }
    order = ("sent", "deferred", "deduped", "too_large", "local_fallback", "failed")
    ranked = sorted(
        counters.items(),
        key=lambda item: sum(max(0, int(v or 0)) for v in item[1].values()),
        reverse=True,
    )
    lines: list[str] = []
    for source, values in ranked[:limit]:
        parts = [
            f"{labels[outcome]} {int(values[outcome]):,}"
            for outcome in order
            if int(values.get(outcome) or 0) > 0
        ]
        if parts:
            lines.append(f"{html.escape(_source_label(source))}: {', '.join(parts)}")
    if len(ranked) > limit:
        lines.append(f"{len(ranked) - limit:,} more sources")
    return lines


async def _flush_source_counter_summary(client) -> None:
    if not _flag("REALTIME_POST_FEED_SOURCE_SUMMARY", "1"):
        return
    try:
        now = time.time()
        last = await client.get(LAST_SOURCE_REPORT_KEY)
        if not last:
            await client.set(LAST_SOURCE_REPORT_KEY, now)
            return
        last_f = float(last)
        interval = _int("REALTIME_POST_FEED_SOURCE_SUMMARY_SECONDS", 900, min_value=60)
        if now - last_f < interval:
            return
        raw = await client.hgetall(SOURCE_COUNTER_WINDOW_KEY)
        counters = source_counters_from_hash(raw)
        lines = _source_summary_lines(counters)
        await client.set(LAST_SOURCE_REPORT_KEY, now)
        if not lines:
            return
        await client.delete(SOURCE_COUNTER_WINDOW_KEY)
    except Exception:
        return
    try:
        from src.notifications import telegram
        await telegram.send("<b>Realtime media</b>\n" + "\n".join(lines))
    except Exception:
        logger.debug("realtime_feed source summary send failed", exc_info=True)


async def _deliver_one(payload: dict) -> tuple[bool, int, str, str | None, dict[str, Any]]:
    """Send one queued payload. Returns delivery state plus bounded Telegram detail."""
    from src.notifications import telegram

    caption = format_caption(payload)
    file_path = _resolve_media_path(payload)
    used_source_url_target = False
    if file_path:
        target = file_path
    elif payload.get("source_url"):
        target = payload["source_url"]  # Telegram will fetch it if it's a
                                        # direct-media URL. If it's a post page
                                        # this will fail; we fall back to text.
        used_source_url_target = True
    else:
        target = None

    if target is None:
        # No media accessible; still surface the post as text.
        ok = await telegram.send(caption)
        return bool(ok), 0, "text_only", None, {}

    # SHIP #1: media reposts are silent — Telegram won't ping phones for
    # these. Decision cards and alerts (sent via telegram.send()) still ping.
    if _looks_like_video(payload):
        detailed = getattr(telegram, "send_video_detailed", None)
        if detailed:
            ok, retry_after, error_code, description = await detailed(
                target, caption=caption, disable_notification=True,
            )
        else:
            ok, retry_after = await telegram.send_video(
                target, caption=caption, disable_notification=True,
            )
            error_code, description = "", ""
    else:
        detailed = getattr(telegram, "send_photo_detailed", None)
        if detailed:
            ok, retry_after, error_code, description = await detailed(
                target, caption=caption, disable_notification=True,
            )
        else:
            ok, retry_after = await telegram.send_photo(
                target, caption=caption, disable_notification=True,
            )
            error_code, description = "", ""
    telegram_result = {
        "error_code": str(error_code or "")[:120],
        "description": str(description or "")[:400],
    }
    if ok or retry_after > 0:
        return ok, retry_after, "media", target, telegram_result
    if not used_source_url_target:
        text_ok = await telegram.send(_local_media_text_fallback(caption, target))
        return bool(text_ok), 0, "local_media_text_fallback", target, telegram_result
    # Remote source_url targets are often post pages or signed URLs Telegram
    # cannot fetch directly. Preserve the operator signal as text instead of
    # silently logging ok=False and dropping the row.
    text_ok = await telegram.send(caption)
    return bool(text_ok), 0, "remote_text_fallback", target, telegram_result


class RealtimeFeedDrain:
    """Blocking-style asyncio drain worker.

    Uses ``BLPOP`` with a short timeout so ``stop()`` remains responsive without
    burning CPU. A single running instance is enough.
    """

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._backoff_seconds = 0.0

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        client = None
        # Reconnect loop keeps the drain alive across a Redis restart.
        while not self._stop.is_set():
            if client is None:
                client = await _redis_client()
                if client is None:
                    await asyncio.sleep(5)
                    continue
            try:
                await self._tick(client)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("realtime_feed tick error", exc_info=True)
                with contextlib.suppress(Exception):
                    await client.aclose()
                client = None
                await asyncio.sleep(2)

        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _tick(self, client) -> None:
        # Honor an exponential backoff after a 429.
        if self._backoff_seconds > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._backoff_seconds)
                return
            except asyncio.TimeoutError:
                pass
            self._backoff_seconds = 0.0

        # BLPOP with a short timeout keeps stop() responsive.
        try:
            popped = await client.blpop(_queue_key(), timeout=5)
        except Exception:
            await asyncio.sleep(2)
            return

        # No message this cycle — still run housekeeping.
        if not popped:
            await _flush_deferred_summary(client)
            await _flush_source_counter_summary(client)
            return

        _, raw = popped
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("realtime_feed: dropped malformed payload")
            return
        skip_reason = _skip_reason(payload)
        if skip_reason:
            from src.notifications import realtime_delivery
            await realtime_delivery.record_from_payload(
                payload,
                status="skipped",
                reason=skip_reason,
            )
            return

        # Dedup public/social media globally so a Threads/Lemon8 duplicate photo
        # does not hit the operator chat twice. Private chat sources remain
        # source-scoped so independent private sightings are not hidden.
        ttl_days = _int("REALTIME_POST_FEED_DEDUPE_TTL_DAYS", 7, min_value=1)
        dedupe_keys = _dedupe_keys(payload)
        dedupe_key = dedupe_keys[0]
        seen_dedupe_key = await _dedupe_seen_any(client, dedupe_keys, ttl_days=ttl_days)
        if seen_dedupe_key:
            logger.debug("realtime_feed: dedup skip key=%s", seen_dedupe_key[:24])
            from src.notifications import realtime_delivery
            await realtime_delivery.record_from_payload(
                payload,
                status="deduped",
                reason="duplicate_suppressed",
                dedupe_key=seen_dedupe_key,
            )
            await _record_source_counter(
                client,
                payload,
                status="deduped",
                reason="duplicate_suppressed",
            )
            await _flush_source_counter_summary(client)
            return

        # Rate-limit.
        capacity = _int("REALTIME_POST_FEED_MAX_PER_MINUTE", 12, min_value=1)
        if not await _acquire_token(client, capacity=capacity):
            await _record_deferred(client)
            from src.notifications import realtime_delivery
            await realtime_delivery.record_from_payload(
                payload,
                status="enqueued",
                reason="rate_cap_deferred",
                dedupe_key=dedupe_key,
            )
            await _record_source_counter(
                client,
                payload,
                status="enqueued",
                reason="rate_cap_deferred",
            )
            with contextlib.suppress(Exception):
                await client.lpush(_queue_key(), raw)
            self._backoff_seconds = _rate_limit_delay_seconds(capacity)
            await _flush_deferred_summary(client)
            await _flush_source_counter_summary(client)
            return

        delivered, retry_after, delivery_outcome, delivery_target, telegram_detail = await _deliver_one(payload)
        ledger_status, ledger_reason = _delivery_status(
            payload,
            delivered=bool(delivered),
            retry_after=int(retry_after or 0),
            outcome=delivery_outcome,
            target=delivery_target,
        )
        telegram_result = {
            "delivered": bool(delivered),
            "retry_after": int(retry_after or 0),
            "outcome": delivery_outcome,
            **telegram_detail,
        }
        fallback_bucket = _fallback_bucket_for(
            payload,
            outcome=delivery_outcome,
            target=delivery_target,
            ledger_status=ledger_status,
            ledger_reason=ledger_reason,
            telegram_result=telegram_result,
        )
        if fallback_bucket:
            telegram_result["fallback_bucket"] = fallback_bucket
        from src.notifications import realtime_delivery
        await realtime_delivery.record_from_payload(
            payload,
            status=ledger_status,
            reason=ledger_reason,
            dedupe_key=dedupe_key,
            telegram_result=telegram_result,
            target=delivery_target,
        )
        await _record_source_counter(
            client,
            payload,
            status=ledger_status,
            reason=ledger_reason,
        )
        # One-line outcome log so operators can see whether individual posts
        # actually reached Telegram. Kept at INFO so `docker logs` shows it
        # without turning on debug noise. Format: `sent to telegram: ok=<bool>
        # source=<src> author=<who> cid=<cid> retry_after=<s>`.
        try:
            src = payload.get("source") or "?"
            author = payload.get("author") or "?"
            cid = payload.get("content_id") or "?"
            logger.info(
                "sent to telegram: ok=%s source=%s author=%s cid=%s retry_after=%d",
                bool(delivered), src, author, cid, int(retry_after or 0),
            )
        except Exception:
            logger.debug("realtime_feed outcome log failed", exc_info=True)
        if fallback_bucket:
            await _record_local_media_fallback(
                client,
                payload,
                delivery_target,
                bucket=fallback_bucket,
            )
        if not delivered and retry_after > 0:
            # 429: back off and requeue this item at the head so we don't lose it.
            with contextlib.suppress(Exception):
                await client.lpush(_queue_key(), raw)
            self._backoff_seconds = min(300.0, max(1.0, float(retry_after)) * 2)
            logger.warning(
                "realtime_feed: telegram 429; sleeping %.1fs and retrying",
                self._backoff_seconds,
            )
            await _flush_source_counter_summary(client)
            return

        if not delivered:
            await _record_failed_delivery(client, payload, raw)
            logger.warning(
                "realtime_feed: preserved failed telegram delivery source=%s cid=%s",
                payload.get("source") or "?",
                payload.get("content_id") or "?",
            )
            await _flush_source_counter_summary(client)
            return

        await _mark_dedupe_seen_many(client, dedupe_keys, ttl_days=ttl_days)

        await _flush_deferred_summary(client)
        await _flush_source_counter_summary(client)


def _hash_payload(payload: dict) -> str:
    """Fallback dedup key when stronger media keys are unavailable."""
    fingerprint = "|".join(str(payload.get(k) or "") for k in
                           ("source", "content_id", "source_url", "caption"))
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _canonical_url_for_dedupe(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


def _canonical_url_without_query(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _public_media_family_url(payload: dict, source: str) -> str:
    url = str(payload.get("source_url") or "").strip()
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except Exception:
        return ""
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    source = source.strip().lower()
    public_variant_sources = {"threads", "lemon8"}
    cdn_markers = (
        "cdn",
        "fbcdn",
        "tiktokcdn",
        "byteimg",
        "lemon8",
        "threads",
        "instagram",
    )
    media_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm", ".m4v")
    if source not in public_variant_sources and not any(marker in host for marker in cdn_markers):
        return ""
    if not any(path.endswith(ext) for ext in media_exts) and "/image" not in path and "/video" not in path:
        return ""
    return _canonical_url_without_query(url)


def _threads_synthetic_base(payload: dict) -> str:
    if str(payload.get("source") or "").strip().lower() != "threads":
        return ""
    content_id = str(payload.get("content_id") or "").strip().lower()
    if not content_id.startswith(("img_", "vid_")):
        return ""
    return re.sub(r"_[0-9a-f]{12}$", "", content_id)


def _dedupe_keys(payload: dict) -> list[str]:
    """Return ordered stable dedupe keys for realtime operator notifications."""
    source = str(payload.get("source") or "").strip().lower()
    keys: list[str] = []
    if _flag("REALTIME_POST_FEED_DEDUPE_BY_MEDIA", "1") and source:
        global_sources = _csv_set(
            "REALTIME_POST_FEED_GLOBAL_MEDIA_DEDUPE_SOURCES",
            GLOBAL_MEDIA_DEDUPE_SOURCES_DEFAULT,
        )
        scope = "global" if source in global_sources else source
        sha = str(payload.get("sha256") or "").strip().lower()
        if sha:
            keys.append(hashlib.sha256(f"{scope}|sha|{sha}".encode("utf-8")).hexdigest())
        canonical_url = _canonical_url_for_dedupe(str(payload.get("source_url") or ""))
        if canonical_url:
            keys.append(hashlib.sha256(f"{scope}|url|{canonical_url}".encode("utf-8")).hexdigest())
        family_url = _public_media_family_url(payload, source)
        if family_url:
            keys.append(hashlib.sha256(f"{scope}|media_family_url|{family_url}".encode("utf-8")).hexdigest())
        threads_base = _threads_synthetic_base(payload)
        if threads_base:
            post_url = _canonical_url_for_dedupe(
                str(payload.get("post_url") or payload.get("source_url") or "")
            )
            if post_url:
                keys.append(hashlib.sha256(f"global|threads_base|{post_url}|{threads_base}".encode("utf-8")).hexdigest())
    content_id = str(payload.get("content_id") or "").strip()
    if source == "youtube" and content_id.startswith("video_"):
        content_id = content_id.removeprefix("video_")
    if source and content_id:
        keys.append(hashlib.sha256(f"{source}|{content_id}".encode("utf-8")).hexdigest())
    keys.append(_hash_payload(payload))
    out: list[str] = []
    for key in keys:
        if key and key not in out:
            out.append(key)
    return out


def _dedupe_key(payload: dict) -> str:
    """Return the primary dedupe key for compatibility with older callers."""
    return _dedupe_keys(payload)[0]


# -- Module entrypoint ----------------------------------------------------

async def _amain() -> None:
    if not _flag("REALTIME_POST_FEED_ENABLED", "1"):
        logger.info("REALTIME_POST_FEED_ENABLED=0; drain idle")
        # Idle-wait forever so the container's healthcheck stays happy.
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await stop.wait()
        return
    drain = RealtimeFeedDrain()
    stop = asyncio.Event()

    def _signal_stop() -> None:
        drain.stop()
        stop.set()

    _install_signal_handlers_cb(_signal_stop)
    logger.info("realtime_feed drain started (queue=%s)", _queue_key())
    await drain.run()
    logger.info("realtime_feed drain stopped")


def _install_signal_handlers(stop: asyncio.Event) -> None:
    _install_signal_handlers_cb(stop.set)


def _install_signal_handlers_cb(cb) -> None:
    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, cb)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_: cb())
    except Exception:
        logger.debug("signal handler install failed", exc_info=True)


def _configure_logging() -> None:
    lvl = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":  # pragma: no cover - executed inside container
    _configure_logging()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
