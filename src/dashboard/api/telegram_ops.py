"""``/api/telegram/*`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 7.

Scope: dashboard-driven Telegram account onboarding (request-code / verify-code
/ disable / enable / delete) and the aggregate ``/api/telegram/stats`` payload.

Legacy ``/telegram/chats`` and ``/telegram/chat/{chat_id}`` routes stay in
``__init__.py`` — they are the legacy per-source browse routes, not part of the
onboarding-ops cluster.

Router is included by ``__init__.py`` via ``app.include_router(router)``.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _TELEGRAM_STATS_CACHE,
    _TELEGRAM_STATS_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


def _existing_public_tables(*args, **kwargs):
    """Thin re-import from the parent api module (call-time lookup)."""
    from src.dashboard.api import _existing_public_tables as _impl
    return _impl(*args, **kwargs)


def _safe_estimated_table_rows(*args, **kwargs):
    from src.dashboard.api import _safe_estimated_table_rows as _impl
    return _impl(*args, **kwargs)


def _safe_fetch_int(*args, **kwargs):
    from src.dashboard.api import _safe_fetch_int as _impl
    return _impl(*args, **kwargs)


router = APIRouter()



class TelegramAccountCreate(BaseModel):
    phone: str
    name: str | None = None


class TelegramAccountAuth(BaseModel):
    phone: str
    code: str
    password: str | None = None  # For 2FA


_dashboard_auth_sessions: dict[str, dict] = {}


@router.get("/api/telegram/accounts")
async def list_telegram_accounts(_user: dict = Depends(require_role("viewer"))):
    """List all onboarded Telegram accounts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, phone, status, owner_bot, created_at, last_connected_at, last_error
            FROM telegram_user_accounts
            ORDER BY created_at DESC
            """
        )
    return [
        {
            "name": r["name"],
            "phone": r["phone"][:4] + "****" + r["phone"][-2:] if r["phone"] else None,
            "phone_full": r["phone"],  # Only for admin, could filter
            "status": r["status"],
            "owner_bot": r["owner_bot"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_connected_at": r["last_connected_at"].isoformat() if r["last_connected_at"] else None,
            "last_error": r["last_error"],
        }
        for r in rows
    ]


@router.post("/api/telegram/accounts/request-code")
async def telegram_request_code(
    body: TelegramAccountCreate,
    _user: dict = Depends(require_role("admin")),
):
    """Step 1: Request verification code for a new phone number."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import FloodWaitError

    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    if not api_id or not api_hash:
        raise HTTPException(500, "TELEGRAM_API_ID/API_HASH not configured")

    phone = body.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must include country code (e.g. +6591234567)")

    # Check if already registered
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT name FROM telegram_user_accounts WHERE phone = $1",
            phone,
        )
        if existing:
            raise HTTPException(400, f"Phone already registered as '{existing}'")

    try:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.connect()
        sent_code = await client.send_code_request(phone)

        # Store session for next step
        _dashboard_auth_sessions[phone] = {
            "client": client,
            "phone_code_hash": sent_code.phone_code_hash,
            "api_id": api_id,
            "api_hash": api_hash,
            "name": body.name,
        }

        return {"status": "code_sent", "phone": phone}

    except FloodWaitError as e:
        raise HTTPException(429, f"Rate limited. Wait {e.seconds} seconds.")
    except Exception as e:
        logger.error("telegram_request_code failed: %s", e)
        raise HTTPException(500, f"Failed to send code: {type(e).__name__}")


@router.post("/api/telegram/accounts/verify-code")
async def telegram_verify_code(
    body: TelegramAccountAuth,
    _user: dict = Depends(require_role("admin")),
):
    """Step 2: Verify code and complete sign-in (handles 2FA if needed)."""
    from telethon.errors import (
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        PasswordHashInvalidError,
    )

    session = _dashboard_auth_sessions.get(body.phone)
    if not session:
        raise HTTPException(400, "No pending auth for this phone. Call request-code first.")

    client = session["client"]
    phone_code_hash = session["phone_code_hash"]

    try:
        if body.password:
            # 2FA step
            await client.sign_in(password=body.password)
        else:
            # Code verification step
            await client.sign_in(body.phone, body.code, phone_code_hash=phone_code_hash)

        # Success — save to DB
        me = await client.get_me()
        session_string = client.session.save()

        name = session.get("name") or me.username or f"user_{me.id}"
        if me.first_name and not session.get("name"):
            name = me.first_name.lower().replace(" ", "_")[:32]

        pool = await get_pool()
        async with pool.acquire() as conn:
            # Handle name collision
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
                VALUES ($1, $2, $3, $4, $5, 'dashboard', 'active', NOW())
                """,
                name,
                session["api_id"],
                session["api_hash"],
                body.phone,
                session_string,
            )

            # Notify collector
            await conn.execute("SELECT pg_notify('telegram_account_added', $1)", name)

        # Cleanup
        del _dashboard_auth_sessions[body.phone]

        return {
            "status": "success",
            "name": name,
            "display_name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
        }

    except SessionPasswordNeededError:
        return {"status": "2fa_required", "phone": body.phone}

    except PhoneCodeInvalidError:
        raise HTTPException(400, "Invalid code")

    except PhoneCodeExpiredError:
        del _dashboard_auth_sessions[body.phone]
        raise HTTPException(400, "Code expired. Request a new one.")

    except PasswordHashInvalidError:
        raise HTTPException(400, "Incorrect 2FA password")

    except Exception as e:
        logger.error("telegram_verify_code failed: %s", e)
        raise HTTPException(500, f"Verification failed: {type(e).__name__}")


@router.delete("/api/telegram/accounts/{name}")
async def delete_telegram_account(
    name: str,
    _user: dict = Depends(require_role("admin")),
):
    """Remove a Telegram account."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM telegram_user_accounts WHERE name = $1",
            name,
        )
        if result == "DELETE 0":
            raise HTTPException(404, "Account not found")

    return {"status": "deleted", "name": name}


@router.post("/api/telegram/accounts/{name}/disable")
async def disable_telegram_account(
    name: str,
    _user: dict = Depends(require_role("admin")),
):
    """Disable a Telegram account (stops collection but keeps session)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE telegram_user_accounts SET status = 'disabled' WHERE name = $1",
            name,
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "Account not found")

    return {"status": "disabled", "name": name}


@router.post("/api/telegram/accounts/{name}/enable")
async def enable_telegram_account(
    name: str,
    _user: dict = Depends(require_role("admin")),
):
    """Re-enable a disabled Telegram account."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE telegram_user_accounts SET status = 'active' WHERE name = $1 AND status = 'disabled'",
            name,
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "Account not found or not disabled")

        # Notify collector
        await conn.execute("SELECT pg_notify('telegram_account_added', $1)", name)

    return {"status": "enabled", "name": name}


@router.get("/api/telegram/stats")
async def telegram_stats(_user: dict = Depends(require_role("viewer"))):
    """Aggregate Telegram collection stats for the dashboard Telegram section.

    Uses planner estimates for large all-time tables and exact indexed counts
    for recent windows. The previous exact COUNT(*) + all-chat aggregate timed
    out once telegram_messages grew into the seven-figure range.
    """
    cached_payload = _TELEGRAM_STATS_CACHE.get("payload")
    if (
        cached_payload is not None
        and _TELEGRAM_STATS_TTL_SECONDS > 0
        and time.time() - float(_TELEGRAM_STATS_CACHE.get("ts") or 0.0) < _TELEGRAM_STATS_TTL_SECONDS
    ):
        return cached_payload

    pool = await get_pool()
    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {},
        "estimated": {},
        "top_chats": [],
        "top_chats_window": "24h",
        "recent": {},
    }
    async with pool.acquire() as conn:
        tables = await _existing_public_tables(
            conn,
            [
                "telegram_messages",
                "telegram_users",
                "telegram_chats",
                "telegram_reactions",
                "telegram_user_accounts",
                "telegram_spider_queue",
                "media_items",
            ],
        )

        async def _estimate(table: str) -> int:
            return await _safe_estimated_table_rows(conn, table) if table in tables else 0

        async def _exact(query: str, *args, timeout: float = 8.0) -> int:
            return await _safe_fetch_int(conn, query, *args, timeout=timeout)

        queue_counts = {
            "total": 0,
            "pending": 0,
            "processing": 0,
            "failed": 0,
            "unresolvable": 0,
            "completed": 0,
        }
        if "telegram_spider_queue" in tables:
            row = await conn.fetchrow(
                """
                SELECT count(*)::bigint AS total,
                       count(*) FILTER (WHERE status = 'pending')::bigint AS pending,
                       count(*) FILTER (WHERE status = 'processing')::bigint AS processing,
                       count(*) FILTER (WHERE status = 'failed')::bigint AS failed,
                       count(*) FILTER (WHERE status = 'unresolvable')::bigint AS unresolvable,
                       count(*) FILTER (WHERE status = 'completed')::bigint AS completed
                FROM telegram_spider_queue
                """,
                timeout=10,
            )
            if row:
                queue_counts = {key: int(row[key] or 0) for key in queue_counts}

        out["totals"] = {
            "messages": await _estimate("telegram_messages"),
            "users": await _estimate("telegram_users"),
            "chats": await _estimate("telegram_chats"),
            "reactions": await _estimate("telegram_reactions"),
            "accounts": await _exact("SELECT COUNT(*) FROM telegram_user_accounts", timeout=6)
            if "telegram_user_accounts" in tables else 0,
            "spider_queue": queue_counts["total"],
            "spider_queue_pending": queue_counts["pending"],
            "spider_queue_processing": queue_counts["processing"],
            "spider_queue_failed": queue_counts["failed"],
            "spider_queue_unresolvable": queue_counts["unresolvable"],
            "spider_queue_completed": queue_counts["completed"],
        }
        out["estimated"] = {
            "messages": "telegram_messages" in tables,
            "users": "telegram_users" in tables,
            "chats": "telegram_chats" in tables,
            "reactions": "telegram_reactions" in tables,
            "accounts": False,
            "spider_queue": False,
        }
        out["recent"] = {
            "messages_24h": await _exact(
                "SELECT COUNT(*) FROM telegram_messages "
                "WHERE collected_at > now() - interval '24 hours'",
                timeout=12,
            ) if "telegram_messages" in tables else 0,
            "messages_1h": await _exact(
                "SELECT COUNT(*) FROM telegram_messages "
                "WHERE collected_at > now() - interval '1 hour'",
                timeout=8,
            ) if "telegram_messages" in tables else 0,
            "media_24h": await _exact(
                "SELECT COUNT(*) FROM media_items "
                "WHERE source = 'telegram' AND collected_at > now() - interval '24 hours'",
                timeout=12,
            ) if "media_items" in tables else 0,
            "media_1h": await _exact(
                "SELECT COUNT(*) FROM media_items "
                "WHERE source = 'telegram' AND collected_at > now() - interval '1 hour'",
                timeout=8,
            ) if "media_items" in tables else 0,
        }
        if {"telegram_messages", "telegram_chats"}.issubset(tables):
            rows = await conn.fetch(
                """
                SELECT c.title,
                       c.username,
                       count(*)::bigint AS messages,
                       max(m.platform_created_at) AS last_message_at
                FROM telegram_messages m
                JOIN telegram_chats c ON c.id = m.chat_id
                WHERE m.collected_at > now() - interval '24 hours'
                GROUP BY c.id, c.title, c.username
                ORDER BY messages DESC
                LIMIT 10
                """,
                timeout=12,
            )
            out["top_chats"] = [dict(r) for r in rows]
    _TELEGRAM_STATS_CACHE.update({"ts": time.time(), "payload": out})
    return out


