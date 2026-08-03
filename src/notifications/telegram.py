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
import os
import urllib.request

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_MAX_TEXT_CHARS = 3800


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
