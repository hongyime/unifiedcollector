"""Login bot entrypoint — Phase 5 implementation."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import TypedDict

try:
    from telethon import TelegramClient, events
    from telethon.errors import (
        FloodWaitError,
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        PasswordHashInvalidError,
    )
    _TELETHON_AVAILABLE = True
except (ImportError, SystemError):
    _TELETHON_AVAILABLE = False

logger = logging.getLogger(__name__)

# Lazy import — deferred until runtime so that pure helper functions can be
# imported and tested without requiring a fully configured environment.
def _get_settings():
    from shared.config import settings  # noqa: PLC0415
    return settings

# --- In-memory state ---
login_sessions: dict = {}          # user_id -> LoginState
user_bot_mapping: dict = {}        # user_id -> TelegramClient
active_login_bots: dict = {}       # username -> BotInfo
messages_to_delete: dict = {}      # (chat_id, msg_id) -> delete_at float
login_attempts: defaultdict = defaultdict(list)  # user_id -> [timestamps]
session_locks: dict = {}           # phone -> asyncio.Lock

_global_lock = Lock()
_active_bots_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class LoginState:
    WAITING_PHONE = "waiting_phone"
    WAITING_CODE  = "waiting_code"
    WAITING_2FA   = "waiting_2fa"

    def __init__(self) -> None:
        self.state: str = self.WAITING_PHONE
        self.phone: str | None = None
        self.client = None  # TelegramClient
        self.phone_code_hash: str | None = None
        self.session_file_name: str | None = None  # stem only


class BotInfo(TypedDict):
    client: object  # TelegramClient
    name: str
    token: str
    locked: bool
    locked_until: float


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

def sanitise_phone(raw: str) -> str:
    """Strip spaces, dashes, '(' and ')' from a raw phone string.

    No other transformation is applied — the result may still be invalid.
    Requirements: 3.2
    """
    return re.sub(r"[ \-()\t]", "", raw)


def validate_phone(clean: str) -> bool:
    """Return True iff *clean* starts with '+', the rest are all digits, and
    the total length is at least 7 characters ('+' plus 6 digits minimum).

    Requirements: 3.3
    """
    if not clean.startswith("+"):
        return False
    digits_part = clean[1:]
    return digits_part.isdigit() and len(clean) >= 7


def session_stem_from_phone(phone: str) -> str:
    """Return the E.164 digits-only stem for a sanitised phone number.

    Strips the leading '+' so the result contains only ASCII digits.
    Example: '+12345678900' -> '12345678900'
    Requirements: 7.2
    """
    return phone.lstrip("+")


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW = 300  # seconds (5 minutes)
_RATE_LIMIT_MAX = 5       # max attempts per window


def check_rate_limit(user_id: int) -> bool:
    """Rolling 5-minute window rate limiter.

    Prunes timestamps older than 300 s, rejects if count >= 5, otherwise
    records the current timestamp and returns True.

    Thread-safe via _global_lock.
    Requirements: 5.1, 5.2, 5.3
    """
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW

    with _global_lock:
        # Prune expired timestamps
        login_attempts[user_id] = [
            ts for ts in login_attempts[user_id] if ts > cutoff
        ]
        if len(login_attempts[user_id]) >= _RATE_LIMIT_MAX:
            return False
        login_attempts[user_id].append(now)
        return True


# ---------------------------------------------------------------------------
# Stub async handlers (to be implemented in later tasks)
# ---------------------------------------------------------------------------

async def auto_delete_loop(bot) -> None:
    while True:
        await asyncio.sleep(5)
        for (chat_id, msg_id), delete_at in list(messages_to_delete.items()):
            if delete_at <= time.time():
                try:
                    await bot.delete_messages(chat_id, [msg_id])
                    logger.debug("Deleted message %s in chat %s", msg_id, chat_id)
                except Exception as exc:
                    logger.debug("Failed to delete message %s in chat %s: %s", msg_id, chat_id, exc)
                messages_to_delete.pop((chat_id, msg_id), None)


async def bot_lock_checker() -> None:
    while True:
        await asyncio.sleep(10)
        async with _active_bots_lock:
            for username, bot_info in active_login_bots.items():
                if bot_info["locked"] and bot_info["locked_until"] <= time.time():
                    bot_info["locked"] = False
                    bot_info["locked_until"] = 0
                    logger.info("Bot %s lock expired, unlocking", username)


async def schedule_delete(bot, chat_id: int, message_id: int, delay: float = 30) -> None:
    """Add (chat_id, message_id) → now+delay to messages_to_delete."""
    messages_to_delete[(chat_id, message_id)] = time.time() + delay


async def send_and_track(bot, event, text: str, **kwargs):
    """Send a message and schedule both the sent message and the incoming
    event message for deletion after 30 seconds.

    Returns the sent message object.
    Requirements: 4.1
    """
    sent_msg = await bot.send_message(event.chat_id, text, **kwargs)
    messages_to_delete[(event.chat_id, sent_msg.id)] = time.time() + 30
    messages_to_delete[(event.chat_id, event.message.id)] = time.time() + 30
    return sent_msg


async def nuke_tracked_messages(bot, chat_id: int) -> None:
    """Immediately delete all tracked messages for chat_id and remove them
    from messages_to_delete.

    Requirements: 4.3
    """
    keys = [k for k in messages_to_delete if k[0] == chat_id]
    if not keys:
        return
    msg_ids = [k[1] for k in keys]
    try:
        await bot.delete_messages(chat_id, msg_ids)
    except Exception as exc:
        logger.debug("nuke_tracked_messages: error deleting messages in %s: %s", chat_id, exc)
    for k in keys:
        messages_to_delete.pop(k, None)


async def handle_start(event) -> None:
    """Handle /startcollector command.

    Requirements: 2.5, 3.1, 5.2, 6.2, 6.3
    """
    bot = event.client
    user_id = event.sender_id

    # Rate limit check
    if not check_rate_limit(user_id):
        await send_and_track(bot, event, "Rate limit exceeded. Please wait a few minutes before trying again.")
        return

    # Get bot username
    me = await bot.get_me()
    bot_username = me.username

    # Check if this bot is locked; redirect if so
    async with _active_bots_lock:
        bot_info = active_login_bots.get(bot_username)
        if bot_info and bot_info["locked"]:
            # Find an alternative unlocked bot
            alternative = None
            for uname, info in active_login_bots.items():
                if uname != bot_username and not info["locked"]:
                    alternative = uname
                    break
            if alternative:
                await send_and_track(
                    bot, event,
                    f"This bot is temporarily unavailable. Please use @{alternative} instead."
                )
            else:
                await send_and_track(
                    bot, event,
                    "All bots are temporarily unavailable due to rate limiting. Please retry in a few minutes."
                )
            return

    # Initialise session state
    with _global_lock:
        login_sessions[user_id] = LoginState()
        user_bot_mapping[user_id] = bot

    await send_and_track(bot, event, "Please enter your phone number with country code (e.g. +12345678900):")


async def handle_cancel(event) -> None:
    """Handle /cancel command.

    Requirements: 3.9
    """
    bot = event.client
    user_id = event.sender_id

    client_to_disconnect = None
    with _global_lock:
        session = login_sessions.get(user_id)
        if session is not None:
            client_to_disconnect = session.client
            login_sessions.pop(user_id, None)
            user_bot_mapping.pop(user_id, None)

    if client_to_disconnect is not None:
        await _disconnect_quietly(client_to_disconnect)

    await send_and_track(bot, event, "Login cancelled.")


async def _disconnect_quietly(client) -> None:
    """Disconnect a Telethon client, swallowing any errors."""
    try:
        await client.disconnect()
    except Exception as exc:
        logger.debug("_disconnect_quietly: %s", exc)


async def handle_message(event) -> None:
    """Route incoming messages to the appropriate handler based on session state."""
    bot = event.client
    user_id = event.sender_id

    session = login_sessions.get(user_id)
    if session is None:
        return

    if session.state == LoginState.WAITING_PHONE:
        await handle_phone(bot, event, session, event.message.text)
    elif session.state == LoginState.WAITING_CODE:
        await handle_code(bot, event, session, event.message.text)
    elif session.state == LoginState.WAITING_2FA:
        await handle_2fa(bot, event, session, event.message.text)


async def handle_phone(bot, event, session, phone: str) -> None:
    """Process a phone number submission.

    Requirements: 3.2, 3.3, 3.4, 6.1, 7.1, 7.2, 7.4
    """
    clean = sanitise_phone(phone)
    if not validate_phone(clean):
        await send_and_track(bot, event, "Invalid phone format. Please use E.164 format, e.g. +12345678900")
        return

    settings = _get_settings()

    stem = session_stem_from_phone(clean)
    collector_dir = os.path.join(settings.SESSIONS_BASE_PATH, "collector")

    # Delete stale session files
    for ext in (".session", ".session-journal"):
        stale = os.path.join(collector_dir, stem + ext)
        try:
            if os.path.exists(stale):
                os.remove(stale)
                logger.debug("Removed stale file: %s", stale)
        except Exception as exc:
            logger.warning("Could not remove stale file %s: %s", stale, exc)

    # Ensure collector directory exists
    os.makedirs(collector_dir, exist_ok=True)

    session_path = os.path.join(collector_dir, stem)

    if not _TELETHON_AVAILABLE:
        await send_and_track(bot, event, "Telethon is not available. Cannot proceed.")
        return

    client = TelegramClient(session_path, settings.TG_API_ID, settings.TG_API_HASH)
    try:
        await client.connect()
        result = await client.send_code_request(clean)
    except FloodWaitError as e:
        logger.warning("FloodWaitError on handle_phone: locking bot for %s seconds", e.seconds)
        me = await bot.get_me()
        bot_username = me.username
        async with _active_bots_lock:
            if bot_username in active_login_bots:
                active_login_bots[bot_username]["locked"] = True
                active_login_bots[bot_username]["locked_until"] = time.time() + e.seconds

        # Find alternative bot
        alternative = None
        async with _active_bots_lock:
            for uname, info in active_login_bots.items():
                if uname != bot_username and not info["locked"]:
                    alternative = uname
                    break

        if alternative:
            await send_and_track(
                bot, event,
                f"Rate limited. Please try again with @{alternative}."
            )
        else:
            await send_and_track(
                bot, event,
                f"Rate limited. Please retry in {e.seconds} seconds."
            )

        # Clear session
        with _global_lock:
            user_id = event.sender_id
            login_sessions.pop(user_id, None)
            user_bot_mapping.pop(user_id, None)

        try:
            await client.disconnect()
        except Exception:
            pass
        return
    except Exception as exc:
        logger.error("handle_phone: unexpected error: %s", exc)
        await send_and_track(bot, event, "An error occurred. Please try again.")
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    session.phone = clean
    session.phone_code_hash = result.phone_code_hash
    session.client = client
    session.session_file_name = stem
    session.state = LoginState.WAITING_CODE

    await send_and_track(bot, event, "Please enter the verification code sent to your phone:")


async def handle_code(bot, event, session, code: str) -> None:
    """Process a verification code submission.

    Requirements: 3.5, 3.6, 3.7, 3.10, 3.11, 4.3
    """
    me = await bot.get_me()
    bot_username = me.username

    digits = re.sub(r'\D', '', code)[:5]

    try:
        me_user = await session.client.sign_in(
            session.phone, digits, phone_code_hash=session.phone_code_hash
        )
    except SessionPasswordNeededError:
        session.state = LoginState.WAITING_2FA
        await send_and_track(bot, event, "Please enter your 2FA password:")
        return
    except PhoneCodeInvalidError:
        await send_and_track(bot, event, "Invalid code. Please try again:")
        return
    except PhoneCodeExpiredError:
        await send_and_track(bot, event, "Code expired. Please start over with /startcollector")
        try:
            await session.client.disconnect()
        except Exception:
            pass
        user_id = event.sender_id
        with _global_lock:
            login_sessions.pop(user_id, None)
            user_bot_mapping.pop(user_id, None)
        return
    except Exception as exc:
        logger.error("handle_code: unexpected error: %s", exc)
        await send_and_track(bot, event, "An error occurred. Please try again.")
        return

    # Success
    account_id = await save_account(session, me_user)
    await nuke_tracked_messages(bot, event.chat_id)
    asyncio.ensure_future(
        perform_post_login_cleanup(session.client, bot_username, account_id)
    )

    user_id = event.sender_id
    with _global_lock:
        login_sessions.pop(user_id, None)
        user_bot_mapping.pop(user_id, None)


async def handle_2fa(bot, event, session, password: str) -> None:
    """Process a 2FA password submission.

    Requirements: 3.8, 3.12, 4.2, 4.3
    """
    me = await bot.get_me()
    bot_username = me.username

    # Delete the password message immediately (not scheduled)
    try:
        await event.delete()
    except Exception as exc:
        logger.debug("handle_2fa: could not delete password message: %s", exc)

    try:
        me_user = await session.client.sign_in(password=password)
    except PasswordHashInvalidError:
        await send_and_track(bot, event, "Wrong password. Please try again:")
        return
    except Exception as exc:
        logger.error("handle_2fa: unexpected error: %s", exc)
        await send_and_track(bot, event, "An error occurred. Please try again.")
        return

    # Success
    account_id = await save_account(session, me_user)
    await nuke_tracked_messages(bot, event.chat_id)
    asyncio.ensure_future(
        perform_post_login_cleanup(session.client, bot_username, account_id)
    )

    user_id = event.sender_id
    with _global_lock:
        login_sessions.pop(user_id, None)
        user_bot_mapping.pop(user_id, None)


async def save_account(session, me) -> int:
    """Upsert collector.telegram_accounts and return the account_id.

    Requirements: 10.1, 10.2, 10.3
    """
    from shared.database import get_db_connection  # noqa: PLC0415 — lazy to avoid circular deps

    settings = _get_settings()
    session_file_path = (
        f"{settings.SESSIONS_BASE_PATH}/collector/{session.session_file_name}.session"
    )

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                display_name = " ".join(
                    filter(None, [getattr(me, "first_name", None), getattr(me, "last_name", None)])
                ) or getattr(me, "username", None) or session.phone

                await cur.execute(
                    """
                    INSERT INTO collector.telegram_accounts
                        (phone_number, display_name, session_file_path, status)
                    VALUES (%s, %s, %s, 'active')
                    ON CONFLICT (phone_number) DO UPDATE SET
                        display_name      = EXCLUDED.display_name,
                        session_file_path = EXCLUDED.session_file_path,
                        status            = 'active',
                        last_error        = NULL,
                        last_active       = NOW()
                    RETURNING id;
                    """,
                    (session.phone, display_name, session_file_path),
                )
                row = await cur.fetchone()
                return row[0] if row else 0
    except Exception as exc:
        logger.error("save_account: DB error for phone %s: %s", session.phone, exc)
        return 0


async def create_backfill_jobs(client, account_id: int) -> None:
    """Insert one backfill_jobs row per dialog (ON CONFLICT DO NOTHING).

    Requirements: 11.1, 11.2, 11.3, 11.4
    """
    from shared.database import get_db_connection  # noqa: PLC0415

    try:
        dialogs = await client.get_dialogs()
    except Exception as exc:
        logger.error("create_backfill_jobs: failed to fetch dialogs: %s", exc)
        return

    for dialog in dialogs:
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO collector.backfill_jobs
                            (account_id, chat_id, status)
                        VALUES (%s, %s, 'pending')
                        ON CONFLICT (account_id, chat_id) DO NOTHING;
                        """,
                        (account_id, dialog.id),
                    )
        except Exception as exc:
            logger.error(
                "create_backfill_jobs: failed to insert job for chat %s: %s",
                dialog.id,
                exc,
            )


async def perform_post_login_cleanup(
    client, bot_username: str, account_id: int = 0
) -> None:
    """Run post-login housekeeping: backfill jobs, session distribution,
    dialog cleanup, and client disconnect.

    Requirements: 4.4, 4.5, 7.3, 8.1–8.5
    """
    from services.login_bot.session_router import SessionRouter  # noqa: PLC0415

    settings = _get_settings()

    # Derive stem from the client's session filename while still connected.
    # Use .stem to strip the .session extension — distribute() appends it.
    stem = Path(client.session.filename).stem

    try:
        # 1. Create backfill jobs (must happen while client is connected)
        await create_backfill_jobs(client, account_id)

        # 2. Distribute session file to all service subdirectories
        router = SessionRouter(settings.SESSIONS_BASE_PATH)
        await router.distribute(stem)

        # 3. Delete dialog with the login bot
        try:
            await client.delete_dialog(bot_username, revoke=True)
        except Exception as exc:
            logger.warning("perform_post_login_cleanup: could not delete bot dialog: %s", exc)

        # 4. Delete dialog with Telegram Service 777000
        try:
            await client.delete_dialog(777000, revoke=True)
        except Exception as exc:
            logger.warning(
                "perform_post_login_cleanup: could not delete 777000 dialog: %s", exc
            )
    finally:
        # 5. Always disconnect
        try:
            await client.disconnect()
        except Exception as exc:
            logger.debug("perform_post_login_cleanup: disconnect error: %s", exc)


async def main() -> None:
    settings = _get_settings()
    tokens = settings.parsed_bot_tokens

    if not tokens:
        logger.critical("No bot tokens configured. Set BOT_TOKENS or BOT_TOKEN.")
        sys.exit(1)

    bots = []

    for token_info in tokens:
        name = token_info["name"]
        token = token_info["token"]
        try:
            client = TelegramClient(
                f"bot_{name}",  # session name
                settings.TG_API_ID,
                settings.TG_API_HASH,
            )
            await client.start(bot_token=token)
            me = await client.get_me()
            bot_username = me.username

            # Register handlers
            client.add_event_handler(
                handle_start,
                events.NewMessage(pattern=r"/startcollector", func=lambda e: e.is_private)
            )
            client.add_event_handler(
                handle_cancel,
                events.NewMessage(pattern=r"/cancel", func=lambda e: e.is_private)
            )
            client.add_event_handler(
                handle_message,
                events.NewMessage(func=lambda e: e.is_private and not e.message.text.startswith("/"))
            )

            # Create auto_delete_loop task for this bot
            asyncio.create_task(auto_delete_loop(client))

            # Register in active_login_bots
            async with _active_bots_lock:
                active_login_bots[bot_username] = BotInfo(
                    client=client,
                    name=name,
                    token=token,
                    locked=False,
                    locked_until=0.0,
                )

            bots.append(client)
            logger.info("Bot %s (@%s) started successfully", name, bot_username)
        except Exception as exc:
            logger.error("Failed to start bot %s: %s", name, exc)
            continue

    if not bots:
        logger.critical("No bots started successfully. Exiting.")
        sys.exit(1)

    # Start global lock checker once
    asyncio.create_task(bot_lock_checker())

    # Run all bots until disconnected
    await asyncio.gather(*[b.run_until_disconnected() for b in bots])


if __name__ == "__main__":
    asyncio.run(main())
