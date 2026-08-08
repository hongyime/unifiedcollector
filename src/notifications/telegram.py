"""Telegram Bot API send wrapper. Send-only, fail-safe.

Config via env:
  NOTIFY_TELEGRAM_BOT_TOKEN - dedicated notify bot token (preferred), falls back
                              to TELEGRAM_BOT_TOKEN. (Here: @unifiedcollector234bot.)
  TELEGRAM_CHAT_ID    - destination chat id (supergroup id is -100...) (required)
  TELEGRAM_THREAD_ID  - optional group-topic thread id (int)

send() never raises: on any failure it logs a warning and returns False so a
Telegram outage can't crash the collector.
"""
import asyncio
import json
import logging
import mimetypes
import os
import secrets
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_MAX_TEXT_CHARS = 3800
# Telegram caption hard limit for sendPhoto / sendVideo. 1024 chars.
MAX_CAPTION_CHARS = 1024


def _config() -> tuple[str, str, str]:
    token = (
        os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip()
        or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )
    return (
        token,
        os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        os.getenv("TELEGRAM_THREAD_ID", "").strip(),
    )


def _post(token: str, payload: dict) -> bool:
    """Blocking POST to sendMessage. Returns True on HTTP 200. Never raises."""
    url = f"{_API}/bot{token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:  # noqa: BLE001 - notifications must never raise
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
        logger.warning("telegram send failed: %s %s", e, body)
        return False


def _split_text(text: str, *, limit: int | None = None) -> list[str]:
    """Split long Telegram messages on line boundaries where possible."""
    limit = int(limit or _MAX_TEXT_CHARS)
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                parts.append(current.rstrip("\n"))
                current = ""
            for start in range(0, len(line), limit):
                chunk = line[start:start + limit]
                if chunk:
                    parts.append(chunk.rstrip("\n"))
            continue

        if current and len(current) + len(line) > limit:
            parts.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        parts.append(current.rstrip("\n"))
    return parts or [""]


async def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured chat. No-op (False) if unconfigured."""
    token, chat_id, thread = _config()
    if not token or not chat_id:
        logger.debug("telegram send skipped: token/chat_id not set")
        return False

    base_payload = {
        "chat_id": chat_id,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if thread:
        try:
            base_payload["message_thread_id"] = int(thread)
        except ValueError:
            logger.warning("invalid TELEGRAM_THREAD_ID=%r (not an int)", thread)

    parts = _split_text(text)
    ok = True
    try:
        for part in parts:
            payload = dict(base_payload)
            payload["text"] = part
            ok = bool(await asyncio.to_thread(_post, token, payload)) and ok
        return ok
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders
        logger.warning("telegram send error: %s", e)
        return False


async def send_many(messages: list[str]) -> bool:
    """Send several Telegram messages back-to-back, one per non-empty entry.

    Each entry becomes its own standalone Telegram message via send(), so long
    entries still transparently split at 3800 chars. Falsy entries (empty
    strings, None) are skipped. A 200ms pause is inserted between consecutive
    sends to stay under Telegram's per-chat throughput limit. Returns True only
    if every non-empty entry was delivered successfully (or if the list was
    entirely empty/falsy). Never raises.
    """
    ok = True
    first = True
    for msg in messages:
        if not msg:
            continue
        if not first:
            await asyncio.sleep(0.2)
        first = False
        ok = bool(await send(msg)) and ok
    return ok


# --- Media send: sendPhoto / sendVideo -----------------------------------
#
# Two shapes: a URL string (Telegram will fetch it — cheap, but only works for
# public direct-media URLs) or a local file Path (multipart upload — always
# works but takes a real HTTP body).
#
# Return value is a tuple (ok, retry_after_seconds) so the caller can back off
# on a 429 without losing information: on any non-429 failure retry_after is 0,
# on 429 it's Telegram's suggested wait window.

_MAX_MEDIA_BYTES = 45 * 1024 * 1024  # sendPhoto is 10MB, sendVideo docs say
                                     # 50MB via bot API; leave a safety margin.


def _truncate_caption(caption: str) -> str:
    """Trim caption to Telegram's 1024-char cap (rounded on a soft boundary)."""
    if caption is None:
        return ""
    caption = str(caption)
    if len(caption) <= MAX_CAPTION_CHARS:
        return caption
    # Try to cut on the last whitespace within the limit so we don't split a word.
    limit = MAX_CAPTION_CHARS - 1  # leave room for ellipsis
    trimmed = caption[:limit]
    space = trimmed.rfind(" ")
    if space > limit * 0.75:
        trimmed = trimmed[:space]
    return trimmed.rstrip() + "…"


def _encode_multipart(fields: dict, file_field: str, file_path: str) -> tuple[bytes, str]:
    """Build a multipart/form-data body for one file + optional text fields."""
    boundary = "----uc-realtime-" + secrets.token_hex(12)
    lines: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        lines.append(f"--{boundary}".encode())
        lines.append(f'Content-Disposition: form-data; name="{name}"'.encode())
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))

    path = Path(file_path)
    filename = path.name
    ctype, _ = mimetypes.guess_type(str(path))
    ctype = ctype or "application/octet-stream"
    with open(path, "rb") as fh:
        payload = fh.read()

    lines.append(f"--{boundary}".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode()
    )
    lines.append(f"Content-Type: {ctype}".encode())
    lines.append(b"")
    lines.append(payload)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    body = b"\r\n".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def _post_media_detailed(token: str, method: str, file_field: str,
                         url_or_path: str, fields: dict) -> tuple[bool, int, str, str]:
    """Call a Telegram media method.

    If ``url_or_path`` is an http(s):// URL the JSON API is used and Telegram
    fetches the media itself. Otherwise it is treated as a local file path and
    uploaded via multipart/form-data. Any error is caught and returned as
    (False, retry_after, error_code, description).
    """
    url = f"{_API}/bot{token}/{method}"
    is_remote = url_or_path.startswith(("http://", "https://"))

    if is_remote:
        payload = {**fields, file_field: url_or_path}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
    else:
        try:
            body, content_type = _encode_multipart(fields, file_field, url_or_path)
        except (OSError, FileNotFoundError) as e:
            logger.warning("telegram %s: cannot read %s: %s", method, url_or_path, e)
            return False, 0, "local_read_failed", str(e)
        # Refuse to upload very large blobs; Telegram bot API will reject them.
        if len(body) > _MAX_MEDIA_BYTES:
            logger.warning(
                "telegram %s: %s exceeds %d bytes; skipping upload",
                method, url_or_path, _MAX_MEDIA_BYTES,
            )
            return False, 0, "too_large", f"exceeds {_MAX_MEDIA_BYTES} bytes"
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type}
        )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200, 0, "", ""
    except HTTPError as e:
        retry_after = 0
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        if e.code == 429:
            parsed = {}
            try:
                parsed = json.loads(body_text or "{}")
                params = parsed.get("parameters") or {}
                retry_after = int(params.get("retry_after") or 0)
            except Exception:
                retry_after = 0
            # Try header too (Telegram sometimes sets Retry-After).
            hdr = e.headers.get("Retry-After") if e.headers else None
            if hdr:
                try:
                    retry_after = max(retry_after, int(hdr))
                except (TypeError, ValueError):
                    pass
            logger.warning("telegram %s 429 retry_after=%ss", method, retry_after)
            return (
                False,
                max(retry_after, 1),
                str(parsed.get("error_code") or e.code),
                str(parsed.get("description") or body_text),
            )
        error_code = str(e.code)
        description = body_text
        try:
            parsed = json.loads(body_text or "{}")
            error_code = str(parsed.get("error_code") or e.code)
            description = str(parsed.get("description") or body_text)
        except Exception:
            pass
        if method == "sendPhoto" and "IMAGE_PROCESS_FAILED" in str(description):
            logger.info("telegram sendPhoto could not process image; trying document fallback")
        else:
            logger.warning("telegram %s HTTPError %s: %s", method, e.code, body_text)
        return False, 0, error_code, description
    except Exception as e:  # noqa: BLE001 - notifications must never raise
        logger.warning("telegram %s failed: %s", method, e)
        return False, 0, e.__class__.__name__, str(e)


def _post_media(token: str, method: str, file_field: str,
                url_or_path: str, fields: dict) -> tuple[bool, int]:
    """Call sendPhoto/sendVideo. Returns (ok, retry_after_seconds)."""
    ok, retry_after, _error_code, _description = _post_media_detailed(
        token, method, file_field, url_or_path, fields,
    )
    return ok, retry_after


async def send_photo(url_or_path: str, caption: str = "",
                     parse_mode: str = "HTML") -> tuple[bool, int]:
    """Send a photo. ``url_or_path`` can be a public URL or local file path.

    Returns (ok, retry_after_seconds). retry_after > 0 only on 429.
    Never raises.
    """
    token, chat_id, thread = _config()
    if not token or not chat_id:
        logger.debug("telegram send_photo skipped: token/chat_id not set")
        return False, 0
    fields = {
        "chat_id": chat_id,
        "caption": _truncate_caption(caption),
        "parse_mode": parse_mode,
    }
    if thread:
        try:
            fields["message_thread_id"] = int(thread)
        except ValueError:
            logger.warning("invalid TELEGRAM_THREAD_ID=%r", thread)
    try:
        ok, retry_after, error_code, description = await asyncio.to_thread(
            _post_media_detailed, token, "sendPhoto", "photo", url_or_path, fields,
        )
        if ok or retry_after > 0:
            return ok, retry_after
        if "IMAGE_PROCESS_FAILED" in str(description):
            return await send_document(url_or_path, caption=caption, parse_mode=parse_mode)
        _ = error_code
        return False, 0
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders
        logger.warning("telegram send_photo error: %s", e)
        return False, 0


async def send_document(url_or_path: str, caption: str = "",
                        parse_mode: str = "HTML") -> tuple[bool, int]:
    """Send media as a document fallback when Telegram cannot process a photo.

    This preserves the operator log entry for odd/corrupt/unsupported images
    instead of dropping the realtime feed item after ``sendPhoto`` returns
    IMAGE_PROCESS_FAILED.
    """
    token, chat_id, thread = _config()
    if not token or not chat_id:
        logger.debug("telegram send_document skipped: token/chat_id not set")
        return False, 0
    fields = {
        "chat_id": chat_id,
        "caption": _truncate_caption(caption),
        "parse_mode": parse_mode,
    }
    if thread:
        try:
            fields["message_thread_id"] = int(thread)
        except ValueError:
            logger.warning("invalid TELEGRAM_THREAD_ID=%r", thread)
    try:
        return await asyncio.to_thread(
            _post_media, token, "sendDocument", "document", url_or_path, fields,
        )
    except Exception as e:  # noqa: BLE001 - notifications must never raise
        logger.warning("telegram send_document error: %s", e)
        return False, 0


async def send_video(url_or_path: str, caption: str = "",
                     parse_mode: str = "HTML",
                     thumbnail_path: str | None = None) -> tuple[bool, int]:
    """Send a video. ``url_or_path`` can be a public URL or local file path.

    ``thumbnail_path`` is currently informational: multipart upload of an extra
    ``thumb`` field is not needed on happy path because Telegram auto-generates
    a preview. The parameter is kept so callers can pass a pre-generated tiny
    thumbnail for oversized clips in a follow-up.

    Returns (ok, retry_after_seconds). Never raises.
    """
    token, chat_id, thread = _config()
    if not token or not chat_id:
        logger.debug("telegram send_video skipped: token/chat_id not set")
        return False, 0
    fields = {
        "chat_id": chat_id,
        "caption": _truncate_caption(caption),
        "parse_mode": parse_mode,
        "supports_streaming": "true",
    }
    if thread:
        try:
            fields["message_thread_id"] = int(thread)
        except ValueError:
            logger.warning("invalid TELEGRAM_THREAD_ID=%r", thread)
    _ = thumbnail_path  # reserved for future oversized-clip thumbnails.
    try:
        return await asyncio.to_thread(
            _post_media, token, "sendVideo", "video", url_or_path, fields,
        )
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders
        logger.warning("telegram send_video error: %s", e)
        return False, 0
