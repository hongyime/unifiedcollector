"""Telegram bot for onboarding new user accounts via /startcollector.

This bot runs as a separate service and handles the MTProto auth flow:
  1. User sends /startcollector in DM to one of the configured bots
  2. Bot asks for phone number
  3. Bot triggers Telethon auth → Telegram sends SMS/call code
  4. User provides code
  5. If 2FA enabled, bot asks for password
  6. On success, session string is persisted to telegram_user_accounts

The collector service then picks up new accounts via LISTEN/NOTIFY or polling.

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
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
ASK_PHONE, ASK_CODE, ASK_2FA, CONFIRM_NAME = range(4)

# Shared API credentials (all onboarded accounts use same app registration)
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://collector:collector@localhost:5432/unifiedcollector")

# In-memory auth state per user (telegram user_id -> state dict)
_auth_sessions: dict[int, dict] = {}


async def get_db_pool() -> asyncpg.Pool:
    """Get or create database connection pool."""
    if not hasattr(get_db_pool, "_pool") or get_db_pool._pool is None:
        get_db_pool._pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return get_db_pool._pool


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command — show welcome message."""
    await update.message.reply_text(
        "👋 Welcome to the Unified Collector onboarding bot!\n\n"
        "Use /startcollector to add your Telegram account to the collector.\n"
        "Use /status to check your connected accounts.\n"
        "Use /help for more information."
    )
    return ConversationHandler.END


async def startcollector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /startcollector — begin the onboarding flow."""
    user = update.effective_user
    chat = update.effective_chat
    
    # DM only
    if chat.type != "private":
        await update.message.reply_text(
            "⚠️ This command only works in DMs for security.\n"
            "Please message me directly."
        )
        return ConversationHandler.END
    
    if not API_ID or not API_HASH:
        await update.message.reply_text(
            "❌ Bot not configured. TELEGRAM_API_ID/API_HASH missing."
        )
        return ConversationHandler.END
    
    # Initialize auth session for this user
    _auth_sessions[user.id] = {
        "client": None,
        "phone": None,
        "phone_code_hash": None,
        "bot_username": context.bot.username,
    }
    
    await update.message.reply_text(
        "📱 Let's add your Telegram account to the collector.\n\n"
        "Please send your phone number with country code.\n"
        "Example: +6591234567\n\n"
        "Type /cancel to abort."
    )
    return ASK_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive phone number and send auth code."""
    user = update.effective_user
    phone = update.message.text.strip()
    
    # Basic validation
    if not re.match(r"^\+\d{8,15}$", phone):
        await update.message.reply_text(
            "❌ Invalid phone format. Please use international format:\n"
            "Example: +6591234567"
        )
        return ASK_PHONE
    
    session = _auth_sessions.get(user.id)
    if not session:
        await update.message.reply_text("❌ Session expired. Please /startcollector again.")
        return ConversationHandler.END
    
    session["phone"] = phone
    
    # Check if phone already registered
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT name FROM telegram_user_accounts WHERE phone = $1",
            phone,
        )
        if existing:
            await update.message.reply_text(
                f"⚠️ This phone is already registered as '{existing}'.\n"
                "Use /status to see your accounts or contact admin to remove it."
            )
            return ConversationHandler.END
    
    await update.message.reply_text("⏳ Sending verification code...")
    
    try:
        # Create Telethon client with empty StringSession
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        
        # Send code request
        sent_code = await client.send_code_request(phone)
        session["client"] = client
        session["phone_code_hash"] = sent_code.phone_code_hash
        
        await update.message.reply_text(
            "✅ Verification code sent!\n\n"
            "Please enter the code you received.\n"
            "Format: 12345 (just the digits)\n\n"
            "Type /cancel to abort."
        )
        return ASK_CODE
        
    except FloodWaitError as e:
        await update.message.reply_text(
            f"❌ Too many attempts. Please wait {e.seconds} seconds and try again."
        )
        await _cleanup_session(user.id)
        return ConversationHandler.END
    except Exception as e:
        logger.error("send_code_request failed for %s: %s", phone, e)
        await update.message.reply_text(
            f"❌ Failed to send code: {type(e).__name__}\n"
            "Please try again later."
        )
        await _cleanup_session(user.id)
        return ConversationHandler.END


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive verification code and attempt sign-in."""
    user = update.effective_user
    code = update.message.text.strip().replace(" ", "").replace("-", "")
    
    if not re.match(r"^\d{5,6}$", code):
        await update.message.reply_text(
            "❌ Invalid code format. Please enter 5-6 digits."
        )
        return ASK_CODE
    
    session = _auth_sessions.get(user.id)
    if not session or not session.get("client"):
        await update.message.reply_text("❌ Session expired. Please /startcollector again.")
        return ConversationHandler.END
    
    client: TelegramClient = session["client"]
    phone = session["phone"]
    phone_code_hash = session["phone_code_hash"]
    
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        
        # Success! Get session string and save
        return await _complete_auth(update, user.id)
        
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 Two-factor authentication is enabled.\n\n"
            "Please enter your 2FA password.\n\n"
            "Type /cancel to abort."
        )
        return ASK_2FA
        
    except PhoneCodeInvalidError:
        await update.message.reply_text(
            "❌ Invalid code. Please check and try again."
        )
        return ASK_CODE
        
    except PhoneCodeExpiredError:
        await update.message.reply_text(
            "❌ Code expired. Please /startcollector again to get a new code."
        )
        await _cleanup_session(user.id)
        return ConversationHandler.END
        
    except Exception as e:
        logger.error("sign_in failed: %s", e)
        await update.message.reply_text(
            f"❌ Sign-in failed: {type(e).__name__}\n"
            "Please try again."
        )
        return ASK_CODE


async def receive_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive 2FA password and complete sign-in."""
    user = update.effective_user
    password = update.message.text
    
    # Delete the password message for security
    try:
        await update.message.delete()
    except Exception:
        pass
    
    session = _auth_sessions.get(user.id)
    if not session or not session.get("client"):
        await update.message.reply_text("❌ Session expired. Please /startcollector again.")
        return ConversationHandler.END
    
    client: TelegramClient = session["client"]
    
    try:
        await client.sign_in(password=password)
        return await _complete_auth(update, user.id)
        
    except PasswordHashInvalidError:
        await update.message.reply_text(
            "❌ Incorrect password. Please try again."
        )
        return ASK_2FA
        
    except Exception as e:
        logger.error("2FA sign_in failed: %s", e)
        await update.message.reply_text(
            f"❌ Authentication failed: {type(e).__name__}"
        )
        return ASK_2FA


async def _complete_auth(update: Update, user_id: int) -> int:
    """Complete authentication and save session to database."""
    session = _auth_sessions.get(user_id)
    if not session:
        return ConversationHandler.END
    
    client: TelegramClient = session["client"]
    phone = session["phone"]
    bot_username = session.get("bot_username", "unknown")
    
    try:
        # Get user info
        me = await client.get_me()
        session_string = client.session.save()
        
        # Generate account name
        name = me.username or f"user_{me.id}"
        if me.first_name:
            name = me.first_name.lower().replace(" ", "_")[:32]
        
        # Save to database
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Check for name collision
            base_name = name
            suffix = 0
            while True:
                existing = await conn.fetchval(
                    "SELECT 1 FROM telegram_user_accounts WHERE name = $1",
                    name,
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
                """,
                name, API_ID, API_HASH, phone, session_string, bot_username,
            )
            
            # Notify collector of new account (LISTEN/NOTIFY)
            await conn.execute(
                "SELECT pg_notify('telegram_account_added', $1)",
                name,
            )
        
        display_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or name
        await update.message.reply_text(
            f"✅ Success! Account connected.\n\n"
            f"📱 Phone: {phone}\n"
            f"👤 Name: {display_name}\n"
            f"🏷️ Account ID: {name}\n\n"
            f"The collector will start syncing your chats shortly."
        )
        
        logger.info(
            "Onboarded account %s (phone=%s) via bot @%s",
            name, phone, bot_username,
        )
        
    except Exception as e:
        logger.error("Failed to save session: %s", e)
        await update.message.reply_text(
            f"❌ Failed to save account: {type(e).__name__}\n"
            "Please try again or contact admin."
        )
    
    finally:
        await _cleanup_session(user_id)
    
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel — abort the onboarding flow."""
    user = update.effective_user
    await _cleanup_session(user.id)
    await update.message.reply_text("❌ Onboarding cancelled.")
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show connected accounts for this Telegram user."""
    user = update.effective_user
    
    # We can't directly map Telegram user to accounts (phone is private),
    # so just show count of all accounts.
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name, phone, status, last_connected_at FROM telegram_user_accounts ORDER BY created_at"
        )
    
    if not rows:
        await update.message.reply_text("No accounts connected yet.")
        return
    
    lines = ["📊 **Connected Accounts:**\n"]
    for r in rows:
        phone_masked = r["phone"][:4] + "****" + r["phone"][-2:]
        status_emoji = "✅" if r["status"] == "active" else "❌"
        lines.append(f"{status_emoji} `{r['name']}` ({phone_masked})")
    
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cleanup_session(user_id: int) -> None:
    """Clean up auth session for a user."""
    session = _auth_sessions.pop(user_id, None)
    if session and session.get("client"):
        try:
            await session["client"].disconnect()
        except Exception:
            pass


def build_application(token: str, bot_name: str) -> Application:
    """Build a telegram Application with all handlers."""
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
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("status", status))
    
    logger.info("Built application for bot: %s", bot_name)
    return app


async def main():
    """Run all configured bots concurrently."""
    tokens = {
        "bryanseahbot": os.getenv("BRYANSEAH_BOT_TOKEN"),
        "shotsbyseahbot": os.getenv("SHOTSBYSEAH_BOT_TOKEN"),
        "prawnproductionsbot": os.getenv("PRAWNPRODUCTIONS_BOT_TOKEN"),
    }
    
    # Filter to only configured tokens
    tokens = {k: v for k, v in tokens.items() if v}
    
    if not tokens:
        logger.error("No bot tokens configured. Set BRYANSEAH_BOT_TOKEN etc.")
        return
    
    if not API_ID or not API_HASH:
        logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH required")
        return
    
    logger.info("Starting onboard bots: %s", list(tokens.keys()))
    
    # Build and run all applications
    apps = [build_application(token, name) for name, token in tokens.items()]
    
    # Initialize all
    for app in apps:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
    
    logger.info("All bots running. Press Ctrl+C to stop.")
    
    # Keep running until interrupted
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for app in apps:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
