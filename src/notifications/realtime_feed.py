"""Real-time per-post Telegram feed.

Every newly-inserted media_items row is enqueued (best-effort, never blocking
the collector) into a Redis list. A separate ``realtime_feed`` service (this
module, run as ``python -m src.notifications.realtime_feed``) drains that list,
downloads or resolves the media, and posts a small Telegram message via
``send_photo`` / ``send_video``.

Design points:
  * The collector's insertion path never blocks or raises on this. If Redis is
    down, ``enqueue_from_insert`` silently no-ops.
  * Rate-limit: token bucket in Redis at ``uc:realtime_post_feed:tokens`` with
    a configurable per-minute cap. Bursts above the cap are dropped and their
    count summarized via a scheduled ``sendMessage`` every 15 minutes.
  * Dedupe: a Redis set of sha256s seen in the last N days prevents re-posting
    the same media twice (Instagram carousel resends, WhatsApp forwards, etc).
  * On Telegram 429: exponential backoff, respecting the ``retry_after``
    parameter Telegram supplies.

Env knobs (all optional; safe defaults):
  REALTIME_POST_FEED_ENABLED           default "1"
  REALTIME_POST_FEED_QUEUE_KEY         default "uc:realtime_post_feed"
  REALTIME_POST_FEED_MAX_PER_MINUTE    default "6"
  REALTIME_POST_FEED_DEDUPE_TTL_DAYS   default "7"
  REALTIME_POST_FEED_INCLUDE_PROFILES  default "0"  (skip profile updates)
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
import signal
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# -- Public constants -----------------------------------------------------

QUEUE_KEY_DEFAULT = "uc:realtime_post_feed"
SEEN_SHA_KEY_DEFAULT = "uc:realtime_post_feed:seen_sha"
SKIPPED_KEY_DEFAULT = "uc:realtime_post_feed:skipped_burst"
LAST_BURST_REPORT_KEY = "uc:realtime_post_feed:last_burst_report"

ALLOWED_KINDS = frozenset({"image", "video", "post", "photo"})


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
                  kind: str | None, content_type: str | None) -> dict:
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
        "sha256": str(sha256 or "") or None,
        "caption": caption,
        "sender_id": _metadata_text(
            meta,
            "platform_sender_id",
            "telegram_sender_id",
            "sender_platform_id",
            "sender_id",
        ),
        "kind": str(kind or "").strip().lower() or None,
        "content_type": str(content_type or "").strip().lower() or None,
        "enqueued_at": time.time(),
    }


def enqueue_from_insert(*, source: str, entity_name: str, content_id: str,
                        file_path: str | None, source_url: str | None,
                        sha256: str | None, metadata: dict | None,
                        kind: str | None, content_type: str | None) -> None:
    """Best-effort fire-and-forget enqueue from inside a running event loop.

    Never raises. Never blocks. If Redis or the feed is disabled/unavailable
    it silently no-ops so the collector's insertion path is unaffected.
    """
    if not _flag("REALTIME_POST_FEED_ENABLED", "1"):
        return
    try:
        payload = build_payload(
            source=source, entity_name=entity_name, content_id=content_id,
            file_path=file_path, source_url=source_url, sha256=sha256,
            metadata=metadata, kind=kind, content_type=content_type,
        )
    except Exception:
        logger.debug("realtime_feed build_payload failed", exc_info=True)
        return
    if not _passes_filter(payload):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called from a sync context (unlikely — insert_media_item is async).
        # Best-effort: fire in a fresh loop for good measure, but do not raise.
        try:
            asyncio.run(_enqueue(payload))
        except Exception:
            logger.debug("realtime_feed sync enqueue failed", exc_info=True)
        return
    loop.create_task(_enqueue(payload))


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
    if _is_own_logs_chat_telegram(payload):
        return False
    include_profiles = _flag("REALTIME_POST_FEED_INCLUDE_PROFILES", "0")
    content_type = (payload.get("content_type") or "").lower()
    if content_type == "profile_photo" and not include_profiles:
        return False
    kind = (payload.get("kind") or "").lower()
    if kind not in ALLOWED_KINDS and content_type not in ALLOWED_KINDS:
        # Unknown media kinds still get through if they at least look like a
        # post (have a source_url) — but pure metadata rows are dropped.
        if not payload.get("source_url"):
            return False
    caption = (payload.get("caption") or "").strip()
    file_path = payload.get("file_path") or payload.get("vault_path")
    if not caption and not file_path and not payload.get("source_url"):
        return False
    return True


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
        return
    try:
        await client.rpush(_queue_key(), json.dumps(payload, default=str))
    except Exception:
        logger.debug("realtime_feed enqueue rpush failed", exc_info=True)
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


async def _record_skip(client) -> None:
    with contextlib.suppress(Exception):
        await client.incr(SKIPPED_KEY_DEFAULT)


async def _dedupe_seen(client, sha: str, *, ttl_days: int) -> bool:
    """Return True if we've seen this sha256 recently. Records it either way."""
    if not sha:
        return False
    ttl = max(1, ttl_days) * 86400
    key = f"{SEEN_SHA_KEY_DEFAULT}:{sha}"
    try:
        added = await client.set(key, "1", nx=True, ex=ttl)
    except Exception:
        return False
    # aioredis returns True/None depending on success of NX.
    return not bool(added)


# -- Caption formatting ---------------------------------------------------

_PLATFORM_LABEL = {
    "instagram": "Instagram", "threads": "Threads",
    "x": "Twitter / X", "twitter": "Twitter / X",
    "tiktok": "TikTok", "lemon8": "Lemon8",
    "facebook": "Facebook", "strava": "Strava",
    "telegram": "Telegram", "whatsapp": "WhatsApp",
}


def format_caption(payload: dict, *, max_len: int | None = None) -> str:
    from src.notifications.telegram import MAX_CAPTION_CHARS
    limit = int(max_len or MAX_CAPTION_CHARS)
    platform = _PLATFORM_LABEL.get(payload.get("source") or "", payload.get("source") or "?")
    author = payload.get("author") or "?"
    body = (payload.get("caption") or "").strip()
    source_url = payload.get("source_url") or ""
    header = f"<b>{html.escape(platform)}</b> — <b>{html.escape(author)}</b>"
    parts = [header]
    if body:
        parts.append(html.escape(body))
    if source_url:
        parts.append(f'<a href="{html.escape(source_url, quote=True)}">source</a>')
    text = "\n".join(parts)
    if len(text) > limit:
        # Trim body, keep header + URL intact.
        overhead = len(header) + (len(parts[-1]) if source_url else 0) + 4
        room = max(20, limit - overhead - 1)
        body_short = html.escape(body)
        if len(body_short) > room:
            body_short = body_short[:room].rstrip() + "…"
        parts = [header]
        if body_short:
            parts.append(body_short)
        if source_url:
            parts.append(f'<a href="{html.escape(source_url, quote=True)}">source</a>')
        text = "\n".join(parts)
    return text


def _local_media_text_fallback(caption: str, target: str) -> str:
    path = str(target or "").strip()
    note = (
        "\n\n"
        "<i>Media was collected and stored locally, but Telegram could not upload "
        "the full file here.</i>"
    )
    if path:
        note += f"\n<code>{html.escape(path)}</code>"
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


async def _flush_skip_summary(client) -> None:
    """Send a 'skipped N burst' summary if the last flush was >15 min ago."""
    if not _flag("REALTIME_POST_FEED_BURST_SUMMARY", "1"):
        return
    try:
        now = time.time()
        last = await client.get(LAST_BURST_REPORT_KEY)
        last_f = float(last) if last else 0.0
        interval = _int("REALTIME_POST_FEED_BURST_SUMMARY_SECONDS", 900, min_value=60)
        if now - last_f < interval:
            return
        skipped = await client.get(SKIPPED_KEY_DEFAULT)
        skipped_n = int(skipped) if skipped else 0
        if skipped_n <= 0:
            await client.set(LAST_BURST_REPORT_KEY, now)
            return
        # Reset first (race safe: worst case we double-report a couple).
        await client.delete(SKIPPED_KEY_DEFAULT)
        await client.set(LAST_BURST_REPORT_KEY, now)
    except Exception:
        return
    try:
        from src.notifications import telegram
        await telegram.send(
            f"🌀 <b>Realtime feed burst</b>\n"
            f"Skipped {skipped_n:,} posts in the last "
            f"{_int('REALTIME_POST_FEED_BURST_SUMMARY_SECONDS', 900, min_value=60) // 60} min "
            f"(over {_int('REALTIME_POST_FEED_MAX_PER_MINUTE', 6, min_value=1)}/min cap)."
        )
    except Exception:
        logger.debug("realtime_feed burst summary send failed", exc_info=True)


async def _deliver_one(payload: dict) -> tuple[bool, int]:
    """Send one queued payload. Returns (delivered, retry_after_seconds)."""
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
        return bool(ok), 0

    if _looks_like_video(payload):
        ok, retry_after = await telegram.send_video(target, caption=caption)
    else:
        ok, retry_after = await telegram.send_photo(target, caption=caption)
    if ok or retry_after > 0:
        return ok, retry_after
    if not used_source_url_target:
        text_ok = await telegram.send(_local_media_text_fallback(caption, target))
        return bool(text_ok), 0
    # Remote source_url targets are often post pages or signed URLs Telegram
    # cannot fetch directly. Preserve the operator signal as text instead of
    # silently logging ok=False and dropping the row.
    text_ok = await telegram.send(caption)
    return bool(text_ok), 0


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
            await _flush_skip_summary(client)
            return

        _, raw = popped
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("realtime_feed: dropped malformed payload")
            return
        if not _passes_filter(payload):
            return

        # Dedup exact source occurrences, not global physical files. The vault
        # dedupes bytes by sha256; this feed should still surface that the same
        # media appeared via another platform/source.
        ttl_days = _int("REALTIME_POST_FEED_DEDUPE_TTL_DAYS", 7, min_value=1)
        dedupe_key = _dedupe_key(payload)
        if await _dedupe_seen(client, dedupe_key, ttl_days=ttl_days):
            logger.debug("realtime_feed: dedup skip key=%s", dedupe_key[:24])
            return

        # Rate-limit.
        capacity = _int("REALTIME_POST_FEED_MAX_PER_MINUTE", 6, min_value=1)
        if not await _acquire_token(client, capacity=capacity):
            await _record_skip(client)
            await _flush_skip_summary(client)
            return

        delivered, retry_after = await _deliver_one(payload)
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
        if not delivered and retry_after > 0:
            # 429: back off and requeue this item at the head so we don't lose it.
            with contextlib.suppress(Exception):
                await client.lpush(_queue_key(), raw)
            self._backoff_seconds = min(300.0, max(1.0, float(retry_after)) * 2)
            logger.warning(
                "realtime_feed: telegram 429; sleeping %.1fs and retrying",
                self._backoff_seconds,
            )

        await _flush_skip_summary(client)


def _hash_payload(payload: dict) -> str:
    """Fallback dedup key when the media has no sha256 (e.g. non-file rows)."""
    fingerprint = "|".join(str(payload.get(k) or "") for k in
                           ("source", "content_id", "source_url", "caption"))
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _dedupe_key(payload: dict) -> str:
    """Return a stable key for one source occurrence.

    The collector stores every source occurrence even when the blob hash is the
    same. The realtime feed should mirror that: dedupe duplicate queue attempts
    for the same platform/content_id, but do not hide a WhatsApp copy just
    because Telegram already posted the same sha256.
    """
    source = str(payload.get("source") or "").strip().lower()
    content_id = str(payload.get("content_id") or "").strip()
    if source and content_id:
        return hashlib.sha256(f"{source}|{content_id}".encode("utf-8")).hexdigest()
    return _hash_payload(payload)


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
