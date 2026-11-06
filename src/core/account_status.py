"""Shared runtime account/session status for the dashboard and control room."""
from __future__ import annotations

from src.core.whatsapp_bridge_health import fetch_whatsapp_bridge_health


async def get_runtime_account_status(pool) -> dict:
    async with pool.acquire() as conn:
        try:
            telegram = [dict(row) for row in await conn.fetch(
                "SELECT name, phone, status, last_connected_at, last_error FROM telegram_user_accounts ORDER BY name"
            )]
        except Exception:
            telegram = []
        try:
            health = {
                row["source"]: dict(row)
                for row in await conn.fetch("SELECT source, status, last_success_at, last_error FROM source_health")
            }
        except Exception:
            health = {}
        try:
            cookie_status = {
                (row["platform"], row["account"]): (row["status"], row["reason"])
                for row in await conn.fetch("SELECT platform, account, status, reason FROM cookie_status")
            }
        except Exception:
            cookie_status = {}
        try:
            beeper = [dict(row) for row in await conn.fetch(
                """
                SELECT account_id, network, login_id, user_id, user_full_name, user_username,
                       user_phone, status, last_seen_at
                FROM beeper_shadow_accounts
                ORDER BY network, account_id
                """
            )]
        except Exception:
            beeper = []

    raw_bridges = await fetch_whatsapp_bridge_health()
    whatsapp = []
    for bridge in raw_bridges:
        ready = bool(bridge.get("whatsapp_ready") or bridge.get("ready") or bridge.get("connected"))
        needs_scan = bool(bridge.get("needs_scan") or bridge.get("qr_available"))
        whatsapp.append({
            "session": str(bridge.get("bridge") or "unknown"),
            "ready": ready,
            "status": "connected" if ready else ("awaiting_scan" if needs_scan else str(bridge.get("status") or "unreachable")),
            "needs_scan": needs_scan,
            "invite_accept_available": bool(bridge.get("invite_accept_available")),
            "invite_accept_state": str(bridge.get("invite_accept_state") or "unavailable"),
            "auth_state": bridge.get("auth_state"),
            "history": bridge.get("history") if isinstance(bridge.get("history"), dict) else None,
        })
    return {
        "telegram": telegram, "whatsapp": whatsapp, "beeper": beeper,
        "cookie_status": cookie_status, "health": health,
    }
