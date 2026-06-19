"""Telegram Bot API send wrapper. Send-only, fail-safe.

Config via env:
  TELEGRAM_BOT_TOKEN  - bot token from @BotFather (required to send)
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


def _config() -> tuple[str, str, str]:
    return (
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
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


async def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the configured chat. No-op (False) if unconfigured."""
    token, chat_id, thread = _config()
    if not token or not chat_id:
        logger.debug("telegram send skipped: token/chat_id not set")
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if thread:
        try:
            payload["message_thread_id"] = int(thread)
        except ValueError:
            logger.warning("invalid TELEGRAM_THREAD_ID=%r (not an int)", thread)

    try:
        return await asyncio.to_thread(_post, token, payload)
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders
        logger.warning("telegram send error: %s", e)
        return False
