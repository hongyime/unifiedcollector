"""Module-level helpers for :mod:`src.collectors.telegram`.

Pure functions + regex constants extracted from
``src/collectors/telegram/__init__.py`` (PERF-004 sub-plan 4C step 2).
The names here are re-exported from :mod:`src.collectors.telegram` so
existing callers — including ``tests/collectors/test_telegram*.py`` —
keep working unchanged.

Kept intentionally leaf: no dependency on the collector class or the
mixins. Any Telegram-collector module may import from here safely.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime

from src.collectors.telegram.parse import (
    _MIME_EXT_MAP as _parse_MIME_EXT_MAP,
    ext_from_mime as _parse_ext_from_mime,
)

# Log under the parent module name so log records still surface as
# ``src.collectors.telegram`` — parity with pre-extraction behaviour.
logger = logging.getLogger("src.collectors.telegram")

_TELEGRAM_USERNAME_RE = re.compile(
    r"(?<![\w.])@(?P<username>[A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])"
)
_TELEGRAM_LINK_USERNAME_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me|telegram\.dog)/(?:(?:s)/)?"
    r"(?P<username>[A-Za-z][A-Za-z0-9_]{4,31})(?:[/?#]|$)",
    re.IGNORECASE,
)
_TELEGRAM_RESERVED_PATHS = {
    "addstickers",
    "c",
    "joinchat",
    "share",
}


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


def _tg_jsonb(obj) -> str:
    """Serialize a value for a Postgres jsonb column.

    NOTE: `_tg_json` is the json.dumps `default=` *callback*, not a serializer —
    calling it directly returns ``str(obj)`` (single-quoted Python repr), which
    is INVALID JSON and makes the `::jsonb` cast fail silently. This wraps it
    correctly so dicts/lists become real JSON. (Fixes silent loss of
    telegram_reaction_counts / telegram_polls rows.)
    """
    return json.dumps(obj, default=_tg_json, ensure_ascii=False)


def _normalize_telegram_username(value) -> str | None:
    if value is None:
        return None
    handle = str(value).strip().lstrip("@").lower()
    if not handle or handle in _TELEGRAM_RESERVED_PATHS:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", handle):
        return None
    return handle


def _message_text_for_mentions(message) -> str:
    return " ".join(
        str(v)
        for v in (
            _obj_get(message, "message"),
            _obj_get(message, "caption"),
        )
        if v
    )


def _obj_get(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _telegram_message_content_id(chat_id, message_id) -> str:
    return f"{chat_id}_{message_id}"


def _tier1_raw_archives_enabled() -> bool:
    raw = os.getenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _parse_optional_int_env(name: str, default: str | None) -> int | None:
    """Read an int from env `name`, falling back to `default` (string or None).

    Returns None if unset/empty/unparseable — self-bot filtering must never
    break the collector on a malformed value; it just degrades to Layer 2 (or
    off entirely).
    """
    raw = os.getenv(name, "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw and default is not None:
        raw = str(default).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid int for env %s=%r; ignoring", name, raw)
        return None


def _parse_int_set_env(name: str, default: str) -> frozenset[int]:
    """Read a comma/space-separated int list from env `name`.

    Empty/malformed entries are silently dropped so a bad operator override
    cannot disable ingestion. Falls back to `default` when env is unset.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raw = default
    out: set[int] = set()
    for tok in raw.replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except (TypeError, ValueError):
            logger.warning("invalid int in env %s=%r; ignoring token %r", name, raw, tok)
    return frozenset(out)


def _telethon_payload(obj) -> dict:
    if hasattr(obj, "to_dict"):
        try:
            payload = obj.to_dict()
            if isinstance(payload, dict):
                return payload
        except Exception:
            logger.debug("Telethon to_dict failed for raw archive", exc_info=True)
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {
            key: value
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    return {"repr": str(obj)}


_MIME_EXT_MAP = _parse_MIME_EXT_MAP


def _ext_from_mime(mime_type):
    return _parse_ext_from_mime(mime_type)


def _is_flood_wait(exc):
    """Detect FloodWaitError without importing telethon at module scope."""
    name = type(exc).__name__
    if name == "FloodWaitError":
        return True
    return hasattr(exc, "seconds") and "flood" in name.lower()


def _is_file_reference_expired(exc):
    """Detect expired Telethon media references without importing telethon."""
    name = type(exc).__name__
    if name == "FileReferenceExpiredError":
        return True
    return "file reference has expired" in str(exc).lower()


def _format_exception(exc) -> str:
    """Stable exception text for Telegram alerts/DLQ rows.

    Several async timeout classes stringify to an empty string. Include the
    type name so operational logs still explain what happened.
    """
    detail = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


def _is_transient_realtime_write_error(exc) -> bool:
    """Return True for DB/network blips worth retrying on the hot realtime path."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    exc_type = type(exc)
    name = exc_type.__name__.lower()
    module = getattr(exc_type, "__module__", "").lower()
    if "asyncpg" not in module:
        return False
    return any(
        token in name
        for token in (
            "timeout",
            "connection",
            "interface",
            "cannotconnect",
            "connectiondoesnotexist",
        )
    )


def _as_int(x):
    """int(x) or None — for trying a chat id as numeric then raw string."""
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _is_transient(exc):
    """A resolve/collect error that is worth retrying (vs the chat being dead).

    Connection drops, timeouts, and 'server closed'/'disconnected' blips are
    transient — the account may resolve the chat fine on the next cycle.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    blob = _format_exception(exc).lower()
    return any(s in blob for s in (
        "disconnect", "server closed", "timeout", "connection", "not connected",
    ))
