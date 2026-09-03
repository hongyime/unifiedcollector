"""Telegram callback bot for the UnifiedCollector — decision buttons + auth gating.

Base of operations, not a spam feed. This module is the collector-side mirror of
``unifiedanalyzer/src/notifications/merge_bot.py``: a single asyncio Task that
long-polls Telegram getUpdates on the collector's dedicated notification bot
(``NOTIFY_TELEGRAM_BOT_TOKEN`` — @unifiedcollector234bot) and dispatches inline
keyboard button presses to the appropriate handler.

Currently handled callbacks:

  * ``wd:restart:<token>``   — watchdog "Restart <container>" button
  * ``wd:ignore:<token>``    — watchdog "Ignore" button (skip auto-restart for
                               the current grace window)

New decision surfaces (future work) should keep the ``<namespace>:<action>:<token>``
shape and stay under Telegram's 64-byte callback_data cap.

Design constraints echoed from the analyzer:
  * getUpdates + webhook are mutually exclusive — poller calls deleteWebhook on
    startup so a stray webhook from another dev machine cannot silently swallow
    updates.
  * Only ONE poller per bot token (getUpdates returns HTTP 409 otherwise). The
    analyzer's merge_bot runs on ``TELEGRAM_BOT_TOKEN`` (a DIFFERENT bot); this
    poller runs on ``NOTIFY_TELEGRAM_BOT_TOKEN``. See sha comparison in the
    audit note the initial overhaul commit references.
  * Authorized-user gating via ``TELEGRAM_ALLOWED_USER_IDS`` (comma-separated
    integer Telegram user ids). Empty allowlist = permissive-with-warning:
    every press is REJECTED but the presser's id is logged and echoed back so
    the operator can self-discover their id, add it to .env, and unlock buttons.
    This turns setup into a self-service loop instead of a chicken-and-egg
    "you need to know your id to add your id" problem.
  * The token store is in-memory (dict). It does NOT survive a scheduler
    restart — stale buttons will answer "Card expired (scheduler restarted)"
    and no-op, matching the analyzer's stale-card semantics. Persisting would
    require an extra DB table and buys little: watchdog will re-emit a fresh
    card on the next tick if the source is still stale.

Nothing in this module raises: every path returns via logger.warning +
answer_callback_query so a Telegram/Docker blip cannot kill the poller task
(which would kill the operator's ability to intervene until the next scheduler
restart).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from typing import Awaitable, Callable

import aiohttp

from src.notifications import telegram

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")


def _allowed_user_ids() -> set[int]:
    """Parse TELEGRAM_ALLOWED_USER_IDS into a set of ints.

    Comma-separated; whitespace tolerated; empty/malformed -> empty set. Read
    every call (cheap) so an operator can edit .env and re-exec without a
    scheduler restart. Malformed entries are silently dropped with a debug log
    so a typo doesn't wedge the allowlist.
    """
    raw = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            logger.debug("TELEGRAM_ALLOWED_USER_IDS: ignoring non-int entry %r", chunk)
    return out


def is_authorized(user_id: int | None) -> bool:
    """Return True iff the user id is in the current allowlist."""
    if user_id is None:
        return False
    return int(user_id) in _allowed_user_ids()


# ---------------------------------------------------------------------------
# Decision card token store
# ---------------------------------------------------------------------------
#
# Each decision card gets an 8-hex-char token; the token maps to a payload dict
# describing what the buttons should do. Kept in-memory; see module docstring
# for the tradeoff rationale.

# token (8 hex) -> payload dict (schema depends on card kind)
_card_store: dict[str, dict] = {}

# Tokens whose decision has already been recorded (double-tap guard)
_resolved: set[str] = set()

# Tokens whose [Ignore] press should suppress the watchdog auto-restart for
# this grace window. Consumed by watchdog/freshness.py. Cleared automatically
# when the token's grace window elapses (watchdog reads + refreshes).
_ignored: dict[str, float] = {}  # token -> monotonic timestamp of Ignore press


def make_watchdog_card_token(source: str, containers: list[str],
                             age_seconds: float, threshold_seconds: int) -> str:
    """Register a watchdog decision card and return its 8-hex-char token."""
    seed = f"{source}|{','.join(containers)}|{age_seconds:.0f}|{time.monotonic()}|{secrets.token_hex(4)}"
    token = hashlib.sha256(seed.encode()).hexdigest()[:8]
    _card_store[token] = {
        "kind": "watchdog_restart",
        "source": source,
        "containers": list(containers),
        "age_seconds": float(age_seconds),
        "threshold_seconds": int(threshold_seconds),
        "created_at": time.time(),
    }
    return token


def build_watchdog_keyboard(token: str, containers: list[str]) -> list[list[dict]]:
    """Two-button decision card: [🔄 Restart <container(s)>] [🚫 Ignore]."""
    label = "Restart " + (", ".join(containers) if containers else "container")
    if len(label) > 30:  # Telegram caps button text at ~64 chars but shorter reads better
        label = "🔄 Restart"
    else:
        label = f"🔄 {label}"
    return [[
        {"text": label, "callback_data": f"wd:restart:{token}"},
        {"text": "🚫 Ignore", "callback_data": f"wd:ignore:{token}"},
    ]]


def is_token_ignored(token: str, *, within_seconds: float = 3600.0) -> bool:
    """Was this token's card marked [Ignore] within the last ``within_seconds``?

    Watchdog calls this to know whether to skip auto-restart for its current
    grace window. Stale ignore records are pruned lazily.
    """
    now = time.monotonic()
    stamp = _ignored.get(token)
    if stamp is None:
        return False
    if now - stamp > within_seconds:
        _ignored.pop(token, None)
        return False
    return True


# ---------------------------------------------------------------------------
# Docker restart via socket
# ---------------------------------------------------------------------------

async def _docker_restart(container: str, timeout_s: int = 15) -> tuple[bool, str]:
    """POST /containers/<name>/restart via mounted docker socket.

    Mirrors ``watchdog._restart`` (same socket path, same aiohttp UnixConnector).
    Returns (ok, detail) so callers can put the outcome in the message edit.
    """
    try:
        connector = aiohttp.UnixConnector(path=DOCKER_SOCK)
        async with aiohttp.ClientSession(connector=connector) as s:
            url = f"http://docker/containers/{container}/restart?t={timeout_s}"
            async with s.post(url) as r:
                if r.status in (204, 304):
                    return True, f"HTTP {r.status}"
                body = (await r.text())[:200]
                return False, f"HTTP {r.status}: {body}"
    except Exception as exc:  # noqa: BLE001 - never raise into the poller
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Callback handlers (one per card kind)
# ---------------------------------------------------------------------------

async def _handle_watchdog_restart(cq: dict, token: str, card: dict) -> str:
    """Restart the container(s) named in the card. Returns the new message text."""
    containers = card.get("containers") or []
    source = card.get("source") or "?"
    if not containers:
        return f"❌ Restart failed: no containers recorded for source {source}."
    results: list[str] = []
    all_ok = True
    for c in containers:
        ok, detail = await _docker_restart(c)
        results.append(f"{'✅' if ok else '❌'} {c}: {detail}")
        all_ok = all_ok and ok
    who = (cq.get("from") or {}).get("username") or (cq.get("from") or {}).get("id") or "?"
    header = "✅ <b>Restart requested</b>" if all_ok else "⚠️ <b>Restart partial</b>"
    return f"{header} — <code>{source}</code> by @{who}\n" + "\n".join(results)


async def _handle_watchdog_ignore(cq: dict, token: str, card: dict) -> str:
    """Suppress auto-restart for the remainder of the watchdog grace window."""
    _ignored[token] = time.monotonic()
    source = card.get("source") or "?"
    who = (cq.get("from") or {}).get("username") or (cq.get("from") or {}).get("id") or "?"
    return (
        f"🚫 <b>Ignored</b> — <code>{source}</code> by @{who}\n"
        f"Watchdog will not auto-restart this source during the current grace window."
    )


CardHandler = Callable[[dict, str, dict], Awaitable[str]]

_HANDLERS: dict[str, CardHandler] = {
    "wd:restart": _handle_watchdog_restart,
    "wd:ignore": _handle_watchdog_ignore,
}


def _parse_callback_data(data: str) -> tuple[str, str] | None:
    """Parse '<ns>:<action>:<token>' -> ('<ns>:<action>', token) or None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    return f"{parts[0]}:{parts[1]}", parts[2]


# ---------------------------------------------------------------------------
# Callback dispatch
# ---------------------------------------------------------------------------

async def _handle_callback(cq: dict) -> None:
    """Dispatch a single callback_query update.

    Path:
      1. Extract identifiers.
      2. Authorize (allowlist check) — reject early and log presser's id.
      3. Parse callback_data -> handler key + token.
      4. Look up card in _card_store; reject stale.
      5. Acknowledge spinner immediately (Telegram gives ~10s before it errors).
      6. Run handler, edit the original message to reflect the outcome.
    """
    cq_id = cq.get("id", "")
    data = cq.get("data", "") or ""
    frm = cq.get("from") or {}
    user_id = frm.get("id")
    username = frm.get("username") or "?"
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    # --- Auth gate ---------------------------------------------------------
    allowlist = _allowed_user_ids()
    if not allowlist:
        # Fail-closed but self-discoverable: reject, tell the presser their id.
        logger.warning(
            "collector-bot: unauthorized press (allowlist empty) by user_id=%s username=%s data=%s",
            user_id, username, data,
        )
        await asyncio.to_thread(
            telegram.answer_callback_query, cq_id,
            f"Not authorized. Your Telegram id is {user_id} — ask admin to add it to "
            f"TELEGRAM_ALLOWED_USER_IDS.",
            show_alert=True,
        )
        return
    if not is_authorized(user_id):
        logger.warning(
            "collector-bot: unauthorized press by user_id=%s username=%s (not in allowlist) data=%s",
            user_id, username, data,
        )
        await asyncio.to_thread(
            telegram.answer_callback_query, cq_id,
            f"Not authorized (id {user_id}).",
            show_alert=True,
        )
        return

    # --- Parse & lookup ----------------------------------------------------
    parsed = _parse_callback_data(data)
    if parsed is None:
        logger.info("collector-bot: unrecognized callback_data=%r — ignoring", data)
        await asyncio.to_thread(telegram.answer_callback_query, cq_id, "")
        return
    handler_key, token = parsed

    if token in _resolved:
        await asyncio.to_thread(
            telegram.answer_callback_query, cq_id, "Already resolved ✓",
        )
        return

    card = _card_store.get(token)
    if card is None:
        # Scheduler restart cleared the store, or a stray/expired card.
        await asyncio.to_thread(
            telegram.answer_callback_query, cq_id,
            "Card expired (scheduler restarted). Watchdog will re-emit on next tick.",
        )
        return

    handler = _HANDLERS.get(handler_key)
    if handler is None:
        logger.info("collector-bot: no handler for %r (data=%r)", handler_key, data)
        await asyncio.to_thread(telegram.answer_callback_query, cq_id, "")
        return

    # --- Acknowledge + act -------------------------------------------------
    spinner = "Restarting…" if handler_key == "wd:restart" else "Ignoring…"
    await asyncio.to_thread(telegram.answer_callback_query, cq_id, spinner)

    try:
        new_text = await handler(cq, token, card)
    except Exception:  # noqa: BLE001 - poller must never crash
        logger.exception("collector-bot: handler %s crashed", handler_key)
        new_text = "❌ Handler crashed — see scheduler logs."

    _resolved.add(token)
    if chat_id is not None and message_id is not None:
        await asyncio.to_thread(
            telegram.edit_message_text, chat_id, int(message_id), new_text,
        )


# ---------------------------------------------------------------------------
# Long-poll loop (single asyncio.Task; started by Scheduler.start)
# ---------------------------------------------------------------------------

async def run_callback_poller() -> None:
    """Poll Telegram getUpdates and dispatch callback_query events.

    Mirrors the analyzer's merge_bot poller (structure, error handling, backoff).
    Runs as a single asyncio.Task inside the collector scheduler loop. The
    scheduler is the only long-lived collector process that (a) already imports
    ``src.notifications``, (b) has DB access, and (c) already restarts under
    ``restart: unless-stopped``; so if the task ever dies, the container's
    restart policy revives it.
    """
    offset = 0

    logger.info("collector-bot: callback poller starting (long-poll mode)")

    # deleteWebhook — getUpdates + webhooks are mutually exclusive.
    try:
        info = await asyncio.to_thread(telegram.get_webhook_info)
        webhook_url = ((info or {}).get("result") or {}).get("url", "")
        if webhook_url:
            logger.warning(
                "collector-bot: webhook is set to %r — deleting it to enable long-polling",
                webhook_url,
            )
            await asyncio.to_thread(telegram.delete_webhook)
            logger.info("collector-bot: webhook deleted")
        else:
            logger.info("collector-bot: no webhook set, long-polling ready")
    except Exception:
        logger.exception("collector-bot: webhook check failed (non-fatal, continuing)")

    while True:
        try:
            updates = await asyncio.to_thread(telegram.get_updates, offset, 25)

            if not updates.get("ok"):
                logger.debug("collector-bot: getUpdates not ok: %s", updates)
                await asyncio.sleep(5)
                continue

            for upd in updates.get("result") or []:
                # Advance offset FIRST so a crash during handling doesn't
                # re-deliver the same update forever.
                offset = int(upd["update_id"]) + 1
                cq = upd.get("callback_query")
                if not cq:
                    continue
                try:
                    await _handle_callback(cq)
                except Exception:
                    logger.exception(
                        "collector-bot: error handling callback update_id=%s",
                        upd.get("update_id"),
                    )

        except asyncio.CancelledError:
            logger.info("collector-bot: poller cancelled")
            return
        except Exception:
            logger.exception("collector-bot: getUpdates loop error; backing off 30s")
            await asyncio.sleep(30)
