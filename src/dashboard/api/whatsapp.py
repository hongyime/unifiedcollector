"""``whatsapp`` route handlers for the dashboard.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A refactor.
Routes registered on ``router`` (APIRouter) and included by ``__init__.py``.

Cross-module helpers that still live in ``__init__.py`` are late-imported
inside thin wrappers to preserve test monkey-patch semantics.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role
from src.dashboard.api.helpers import (
    _acquire_dashboard_conn,
    _release_dashboard_conn,
)

logger = logging.getLogger(__name__)


def _lookup(name: str):
    """Look up a name on the parent dashboard_api module at call time.

    Used inside route handlers to resolve helpers like ``_wa_bridge_post`` /
    ``_wa_bridge_get`` (defined in this module but patched by tests on the
    parent ``dashboard_api`` module).
    """
    import sys as _sys
    root = _sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    """``get_pool`` proxy that respects test monkey-patches on ``dashboard_api``."""
    fn = _lookup("get_pool") or get_pool
    return await fn()


def _bridge_post():
    fn = _lookup("_wa_bridge_post")
    return fn if fn is not None else _wa_bridge_post


def _bridge_get():
    fn = _lookup("_wa_bridge_get")
    return fn if fn is not None else _wa_bridge_get


router = APIRouter()


# ---------------------------------------------------------------------------
# WA link filter aliases and SQL expressions. Moved from __init__.py during
# PERF-002 4A step 10.
# ---------------------------------------------------------------------------

_WA_LINK_TYPE_FILTER_ALIASES = {
    # Backward compatibility for older dashboard filter values.
    "invite": ["group_invite", "group_invite_restricted"],
    "phone": ["contact_link"],
    "contact": ["contact_link"],
}


_WA_LINK_STATUS_FILTER_ALIASES = {
    # Backward compatibility for older dashboard filter values.
    "new": ["pending"],
    "visited": ["fetched"],
    "collected": ["fetched"],
}


_WA_LINK_TYPE_VALUES = {"url", "group_invite", "group_invite_restricted", "contact_link"}

_WA_LINK_TYPE_SQL_VALUES = "'url', 'group_invite', 'group_invite_restricted', 'contact_link'"

_WA_LINK_TYPE_EXPR = (
    "CASE WHEN (l.link_type ILIKE 'http://%' OR l.link_type ILIKE 'https://%') "
    f"AND lower(l.url) IN ({_WA_LINK_TYPE_SQL_VALUES}) THEN lower(l.url) "
    "WHEN l.link_type IS NULL OR btrim(l.link_type) = '' "
    "OR l.link_type ILIKE 'http://%' OR l.link_type ILIKE 'https://%' "
    "THEN 'url' ELSE l.link_type END"
)


_WA_LINK_STATS_TYPE_EXPR = (
    "CASE WHEN (link_type ILIKE 'http://%' OR link_type ILIKE 'https://%') "
    f"AND lower(url) IN ({_WA_LINK_TYPE_SQL_VALUES}) THEN lower(url) "
    "WHEN link_type IS NULL OR btrim(link_type) = '' "
    "OR link_type ILIKE 'http://%' OR link_type ILIKE 'https://%' "
    "THEN 'url' ELSE link_type END"
)


def _should_wait_for_fresh_wa_qr(health: dict, qrd: dict) -> bool:
    """Render a waiting state for an unpaired bridge in the no-QR retry gap.

    Baileys expires a QR after several refresh attempts and briefly reports
    disconnected with no QR until the next reconnect timer fires. The link page
    polls /whatsapp/qr/{bridge}; the bridge's read-only /qr handler already
    nudges reconnect if a viewer is active. Do not POST /fresh-qr from this
    automatic polling path because that route can clear unregistered local auth
    state while the phone is still pairing.
    """
    if bool(health.get("whatsapp_ready")) or bool(health.get("registered")):
        return False
    if qrd.get("qr"):
        return False
    status = str(qrd.get("status") or health.get("status") or "").lower()
    return status in {
        "disconnected",
        "connecting_unpaired",
        "refreshing_qr",
        "fresh_qr_requested",
        "auth_cleared",
    }


@router.get("/whatsapp/users")
async def list_wa_users(search: str | None = None, limit: int = 50,
                         _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if search:
            esc = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_users "
                "WHERE platform_user_id ILIKE $1 ESCAPE '\\' OR name ILIKE $1 ESCAPE '\\' "
                "OR pushname ILIKE $1 ESCAPE '\\' "
                "ORDER BY updated_at DESC NULLS LAST LIMIT $2",
                f"%{esc}%", limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM whatsapp_users ORDER BY updated_at DESC NULLS LAST LIMIT $1",
                limit,
            )
    return [dict(r) for r in rows]


@router.get("/whatsapp/users/{jid}/history")
async def wa_user_history(jid: str, limit: int = 100,
                           _user: dict = Depends(require_role("viewer"))):
    """Message history for one WhatsApp user (by platform_user_id / JID).

    There is no separate wa_user_history table; we return the user's recent
    messages joined through whatsapp_users.id -> whatsapp_messages.sender_id.
    """
    limit = max(1, min(limit, 1000))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.* FROM whatsapp_messages m "
            "JOIN whatsapp_users u ON u.id = m.sender_id "
            "WHERE u.platform_user_id = $1 "
            "ORDER BY m.collected_at DESC LIMIT $2",
            jid, limit,
        )
    return [dict(r) for r in rows]


@router.get("/whatsapp/chats")
async def list_wa_chats(limit: int = 100,
                        _user: dict = Depends(require_role("viewer"))):
    """Recent WhatsApp chats with last-message preview and unread count.

    Ordered by newest activity (max(chat.updated_at, latest message timestamp))
    so an idle group with a fresh reply floats to the top. `message_count` is
    the total, `last_text` is the preview (truncated) of the latest message.
    """
    limit = max(1, min(limit, 500))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('whatsapp_chats')") is None:
            return []
        # whatsapp_chats is tiny (~100 rows) vs. whatsapp_messages (~46k). Driving
        # from the chats side and doing per-chat LATERAL lookups turns each
        # last-message fetch into a single-tuple hit on
        # idx_wa_messages_chat_ts (chat_id, timestamp DESC). Beats DISTINCT ON
        # over the whole messages table (which forced a full seq scan +
        # external-merge sort, ~4s).
        #
        # Deliberately NO per-chat message_count here: a count(*) LATERAL adds
        # ~500ms for 101 chats (indexed but still walks every leaf per chat) —
        # not worth the wall-clock. participant_count comes from the chats row
        # and is enough for the sidebar; the detail view shows the loaded
        # message run itself.
        rows = await conn.fetch(
            """
            SELECT c.platform_chat_id,
                   c.name,
                   c.is_group,
                   c.chat_type,
                   c.participant_count,
                   c.updated_at,
                   lm."timestamp"          AS last_message_ts,
                   lm.text                 AS last_text,
                   lm.from_me              AS last_from_me,
                   lm.media_mime_type      AS last_media_mime
            FROM whatsapp_chats c
            LEFT JOIN LATERAL (
                SELECT "timestamp", text, from_me, media_mime_type
                FROM whatsapp_messages
                WHERE chat_id = c.id
                ORDER BY "timestamp" DESC NULLS LAST
                LIMIT 1
            ) lm ON true
            ORDER BY COALESCE(lm."timestamp", c.updated_at) DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


@router.get("/whatsapp/chat/{jid:path}")
async def wa_chat_messages(jid: str, limit: int = 200,
                            _user: dict = Depends(require_role("viewer"))):
    """Messages for one WhatsApp chat (chronological, oldest first).

    `jid` is the whatsapp_chats.platform_chat_id (uses :path so JIDs containing
    slashes — e.g. broadcast lists — are accepted). Joins whatsapp_users for
    sender display name + phone_number, and media_items for a stable media_id
    the frontend can pass to /media/{id}/thumbnail.
    """
    limit = max(1, min(limit, 2000))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('whatsapp_chats')") is None:
            return {"chat": None, "messages": []}
        chat = await conn.fetchrow(
            """
            SELECT platform_chat_id, name, is_group, chat_type,
                   participant_count, description, updated_at
            FROM whatsapp_chats WHERE platform_chat_id = $1
            """,
            jid,
        )
        if chat is None:
            return {"chat": None, "messages": []}
        # Message-slice-then-join with scalar chat_id lookup: joining
        # whatsapp_chats inside the WHERE stops the planner from using
        # idx_wa_messages_chat_ts for ORDER BY (it can only use it when
        # chat_id is bound to a scalar). With the scalar the ORDER BY DESC
        # + LIMIT is a single index-range walk, ~200ms even for a 3.5k-msg
        # channel. Reversed to chronological for display.
        rows = await conn.fetch(
            """
            WITH msgs AS (
                SELECT m.*
                FROM whatsapp_messages m
                WHERE m.chat_id = (
                    SELECT id FROM whatsapp_chats WHERE platform_chat_id = $1
                )
                ORDER BY m."timestamp" DESC NULLS LAST
                LIMIT $2
            )
            SELECT m.platform_message_id,
                   m.from_me,
                   m.text,
                   m.media_url,
                   m.media_mime_type,
                   m.media_size,
                   m.thumbnail_url,
                   m."timestamp",
                   m.is_deleted,
                   m.deleted_at,
                   m.quoted_text,
                   m.forward_from_name,
                   u.platform_user_id  AS sender_jid,
                   u.pushname          AS sender_pushname,
                   u.name              AS sender_name,
                   u.phone_number      AS sender_phone,
                   mi.id::text         AS media_id
            FROM msgs m
            LEFT JOIN whatsapp_users u ON u.id = m.sender_id
            LEFT JOIN LATERAL (
                SELECT mi.id
                FROM media_items mi
                WHERE mi.source = 'whatsapp'
                  AND (
                       mi.content_id = 'wa_' || m.platform_message_id
                    OR (m.media_url IS NOT NULL AND mi.file_path = m.media_url)
                  )
                ORDER BY
                    CASE WHEN mi.content_id = 'wa_' || m.platform_message_id THEN 0 ELSE 1 END,
                    mi.collected_at DESC
                LIMIT 1
            ) mi ON TRUE
            ORDER BY m."timestamp" DESC NULLS LAST
            """,
            jid, limit,
        )
    # Reverse to chronological (oldest first) for the chat UI. Cap individual
    # message text at 1500 chars to bound the response body — the whatsapp_
    # messages p95 text length is 638 chars, so 1500 is a comfortable ceiling
    # for anything a person actually typed. A handful of forwards / bot
    # dumps are 6–38KB each; multiplied by 200 rows those blow the payload
    # past 1.4 MB and take stdlib json.dumps 5+ seconds to encode. Rows
    # carrying truncated text set text_truncated so the frontend can offer
    # a "show full" affordance later.
    _TEXT_CAP = 1500
    messages = []
    for r in reversed(rows):
        d = dict(r)
        text = d.get("text")
        if text is not None and len(text) > _TEXT_CAP:
            d["text"] = text[:_TEXT_CAP]
            d["text_truncated"] = True
            d["text_full_length"] = len(text)
        messages.append(d)
    return {"chat": dict(chat), "messages": messages}


def _wa_link_filter_values(value: str | None, aliases: dict[str, list[str]]) -> list[str] | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return aliases.get(normalized, [normalized])


def _wa_looks_like_url(value) -> bool:
    return str(value or "").strip().lower().startswith(("http://", "https://"))


def _wa_link_type_value(value) -> str:
    text = str(value or "").strip()
    if not text or _wa_looks_like_url(text):
        return "url"
    return text


def _wa_link_payload(row) -> dict:
    payload = dict(row)
    raw_link_type = payload.pop("_raw_link_type", payload.get("link_type"))
    url = payload.get("url") or payload.get("link")
    link_type = payload.get("link_type")
    if _wa_looks_like_url(raw_link_type) and not _wa_looks_like_url(url):
        url = raw_link_type
        link_type = _wa_link_type_value(payload.get("url"))
        if link_type not in _WA_LINK_TYPE_VALUES:
            link_type = "url"
    if url is not None:
        # The database column is url; older frontend code rendered link.
        payload["url"] = url
        payload["link"] = url
    payload["link_type"] = _wa_link_type_value(link_type)
    payload["source_jid"] = payload.get("source_jid") or payload.get("platform_chat_id")
    return payload


@router.get("/whatsapp/links")
async def list_wa_links(link_type: str | None = None, status: str | None = None,
                        limit: int = 100,
                        _user: dict = Depends(require_role("viewer"))):
    limit = max(1, min(limit, 1000))
    pool = await _get_pool()
    conditions = []
    params = []
    idx = 1
    link_type_values = _wa_link_filter_values(link_type, _WA_LINK_TYPE_FILTER_ALIASES)
    status_values = _wa_link_filter_values(status, _WA_LINK_STATUS_FILTER_ALIASES)
    if link_type_values:
        conditions.append(f"{_WA_LINK_TYPE_EXPR} = ANY(${idx}::text[])")
        params.append(link_type_values)
        idx += 1
    if status_values:
        conditions.append(f"l.status = ANY(${idx}::text[])")
        params.append(status_values)
        idx += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with pool.acquire() as conn:
        # wa_discovered_links is an optional feature table; return [] if absent
        # rather than surfacing a 500 for a table that was never created.
        if await conn.fetchval("SELECT to_regclass('wa_discovered_links')") is None:
            return []
        rows = await conn.fetch(
            f"""
            SELECT l.id,
                   l.chat_id::text AS chat_id,
                   l.message_id::text AS message_id,
                   l.url,
                   l.url AS link,
                   c.platform_chat_id AS source_jid,
                   l.domain,
                   l.link_type AS _raw_link_type,
                   {_WA_LINK_TYPE_EXPR} AS link_type,
                   l.status,
                   l.title,
                   l.description,
                   l.thumbnail_url,
                   l.discovered_at,
                   l.fetched_at,
                   l.metadata
            FROM wa_discovered_links l
            LEFT JOIN whatsapp_chats c ON c.id = l.chat_id
            {where}
            ORDER BY l.discovered_at DESC
            LIMIT ${idx}
            """,
            *params, limit,
        )
    return [_wa_link_payload(r) for r in rows]


@router.get("/whatsapp/links/stats")
async def wa_link_stats(_user: dict = Depends(require_role("viewer"))):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT to_regclass('wa_discovered_links')") is None:
            return []
        rows = await conn.fetch(
            f"SELECT {_WA_LINK_STATS_TYPE_EXPR} AS link_type, status, COUNT(*) AS count "
            f"FROM wa_discovered_links GROUP BY {_WA_LINK_STATS_TYPE_EXPR}, status ORDER BY count DESC"
        )
    return [dict(r) for r in rows]


@router.get("/whatsapp/qr/{bridge}")
async def whatsapp_qr(bridge: str):
    """Proxy the live QR / status from a wa-bridge.

    bridge is '1' or '2'. Returns {status, qr, ready, error}. The bridge
    regenerates the QR on its own cadence; the client polls this endpoint and
    re-renders, so a QR never goes stale on screen. Unauthenticated like the
    bridge's own /qr route (link page is behind the dashboard already).
    """
    if bridge not in ("1", "2"):
        raise HTTPException(400, "bridge must be 1 or 2")
    out = {
        "bridge": bridge,
        "status": "unknown",
        "qr": "",
        "ready": False,
        "error": None,
        "qr_available": False,
        "last_qr_at": None,
        "registered": None,
        "connected": None,
        "needs_scan": False,
        "auth_state": None,
        "last_disconnect_status_code": None,
        "last_disconnect_reason": None,
        "last_disconnect_at": None,
        "pairing_recovery_until": None,
        "pairing_recovery_active": False,
    }
    try:
        # /health tells us if already paired; /qr gives the code when waiting
        health = await _bridge_get()(bridge, "health", timeout=8)
        if not health.get("ok"):
            raise RuntimeError(str(health.get("error") or "bridge health unavailable"))
        out["ready"] = bool(health.get("whatsapp_ready"))
        out["status"] = health.get("status", "unknown")
        out["registered"] = health.get("registered")
        out["connected"] = health.get("connected")
        out["needs_scan"] = bool(health.get("needs_scan"))
        out["auth_state"] = health.get("auth_state")
        out["last_disconnect_status_code"] = health.get("last_disconnect_status_code")
        out["last_disconnect_reason"] = health.get("last_disconnect_reason")
        out["last_disconnect_at"] = health.get("last_disconnect_at")
        out["pairing_recovery_until"] = health.get("pairing_recovery_until")
        out["pairing_recovery_active"] = bool(health.get("pairing_recovery_active"))
        if out["ready"]:
            out["status"] = "connected"
            return out
        qrd = await _bridge_get()(bridge, "qr", timeout=8)
        if not qrd.get("ok"):
            raise RuntimeError(str(qrd.get("error") or "bridge QR unavailable"))
        out["status"] = qrd.get("status", out["status"])
        out["qr_available"] = bool(qrd.get("qr_available") or qrd.get("qr"))
        out["last_qr_at"] = qrd.get("last_qr_at") or health.get("last_qr_at")
        out["registered"] = qrd.get("registered", out["registered"])
        out["connected"] = qrd.get("connected", out["connected"])
        out["needs_scan"] = bool(qrd.get("needs_scan", out["needs_scan"]))
        out["auth_state"] = qrd.get("auth_state") or out["auth_state"]
        out["last_disconnect_status_code"] = qrd.get(
            "last_disconnect_status_code",
            out["last_disconnect_status_code"],
        )
        out["last_disconnect_reason"] = qrd.get(
            "last_disconnect_reason",
            out["last_disconnect_reason"],
        )
        out["last_disconnect_at"] = qrd.get("last_disconnect_at", out["last_disconnect_at"])
        out["pairing_recovery_until"] = qrd.get(
            "pairing_recovery_until",
            out["pairing_recovery_until"],
        )
        out["pairing_recovery_active"] = bool(
            qrd.get("pairing_recovery_active", out["pairing_recovery_active"])
        )
        raw_qr = qrd.get("qr", "")
        if not raw_qr and _should_wait_for_fresh_wa_qr(health, qrd):
            out["status"] = "waiting_for_fresh_qr"
            out["last_disconnect_status_code"] = None
            out["last_disconnect_reason"] = None
            out["error"] = (
                "Bridge has no active QR yet. The read-only QR poll nudged the "
                "bridge reconnect path; waiting for the next code without "
                "clearing WhatsApp auth state."
            )
        if raw_qr:
            # Convert the raw Baileys QR string to a base64-encoded PNG so the
            # browser can use it directly as <img src="data:image/png;base64,…">
            import base64
            import io

            try:
                import qrcode  # noqa: PLC0415
            except Exception as qr_exc:  # noqa: BLE001
                out["status"] = "qr_renderer_missing"
                out["error"] = f"dashboard QR renderer missing: {qr_exc}"
                return out

            buf = io.BytesIO()
            qrcode.make(raw_qr).save(buf, "PNG")
            out["qr"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        else:
            out["qr"] = ""
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        out["status"] = "unreachable"
    return out


def _wa_bridge_base(bridge: str) -> str:
    if bridge not in ("1", "2"):
        raise HTTPException(400, "bridge must be 1 or 2")
    return os.getenv(f"WA_BRIDGE_{bridge}_URL", f"http://wa-bridge-{bridge}:3001")


async def _wa_bridge_post(bridge: str, path: str, payload: dict | None = None) -> dict:
    """POST to a wa-bridge control route (disconnect/reconnect), off the event loop."""
    import urllib.request

    base = _wa_bridge_base(bridge)

    def _do():
        data = b""
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{base}/{path}", data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return __import__("json").loads(r.read().decode())

    try:
        body = await asyncio.to_thread(_do)
        return {"bridge": bridge, "ok": True, **body}
    except Exception as exc:  # noqa: BLE001
        return {"bridge": bridge, "ok": False, "error": str(exc)}


async def _wa_bridge_get(bridge: str, path: str, timeout: int = 5) -> dict:
    """GET a wa-bridge read-only route (session/qr), off the event loop.

    Returns {ok: True, ...body} on 2xx; {ok: False, error: <str>} on transport
    or non-2xx. Never raises — the dashboard renders 'unreachable' cleanly."""
    import urllib.request
    base = _wa_bridge_base(bridge)

    def _do():
        with urllib.request.urlopen(f"{base}/{path}", timeout=timeout) as r:
            return __import__("json").loads(r.read().decode())

    try:
        body = await asyncio.to_thread(_do)
        return {"bridge": bridge, "ok": True, **body}
    except Exception as exc:  # noqa: BLE001
        return {"bridge": bridge, "ok": False, "error": str(exc)}


@router.get("/whatsapp/sessions")
async def whatsapp_sessions(_user: dict = Depends(require_role("viewer"))):
    """Identity of each linked WhatsApp session (phone number, push name,
    connection state) for both bridges. Bryan uses this after a QR scan to
    verify WHICH account got linked to WHICH bridge slot — the dashboard
    previously only exposed 'bridge 1' vs 'bridge 2' labels with no way to
    tell them apart.

    Sourced from each bridge's GET /session (added in src/bridges/whatsapp/
    src/index.ts). Called in parallel; unreachable bridges return
    ``ok: false`` with an error string rather than 500ing the endpoint."""
    results = await asyncio.gather(
        _wa_bridge_get("1", "session"),
        _wa_bridge_get("2", "session"),
    )
    return {"sessions": list(results)}


@router.post("/whatsapp/{bridge}/disconnect")
async def whatsapp_disconnect(bridge: str, _user: dict = Depends(require_role("viewer"))):
    """Unpair a wa-bridge device (logout) so it can be re-scanned as a new device."""
    return await _bridge_post()(bridge, "disconnect")


@router.post("/whatsapp/{bridge}/reconnect")
async def whatsapp_reconnect(bridge: str, _user: dict = Depends(require_role("viewer"))):
    """Soft-reconnect a wa-bridge (keeps creds — no re-scan)."""
    return await _bridge_post()(bridge, "reconnect")


@router.post("/whatsapp/{bridge}/fresh-qr")
async def whatsapp_fresh_qr(bridge: str, _user: dict = Depends(require_role("viewer"))):
    """Request a new QR only for an unregistered bridge slot.

    This endpoint used to proxy straight through to the bridge, whose fresh-QR
    route clears local auth. That is correct only for unpaired slots. For a
    registered slot, a stale dashboard click can otherwise unlink a live phone.
    """
    health = await _bridge_get()(bridge, "health", timeout=8)
    if health.get("ok") and (
        health.get("whatsapp_ready") or health.get("connected") or health.get("registered")
    ):
        return {
            "bridge": bridge,
            "ok": True,
            "status": "registered_session",
            "ready": bool(health.get("whatsapp_ready") or health.get("connected")),
            "registered": health.get("registered"),
            "connected": health.get("connected"),
            "note": "Bridge is already registered; use Reconnect to recover or Disconnect to unpair.",
        }
    return await _bridge_post()(bridge, "fresh-qr")


@router.post("/whatsapp/{bridge}/pairing-code")
async def whatsapp_pairing_code(
    bridge: str,
    request: Request,
    _user: dict = Depends(require_role("viewer")),
):
    """Request a phone-number pairing code for an unregistered bridge slot."""
    health = await _bridge_get()(bridge, "health", timeout=8)
    if health.get("ok") and (
        health.get("whatsapp_ready") or health.get("connected") or health.get("registered")
    ):
        return {
            "bridge": bridge,
            "ok": True,
            "status": "registered_session",
            "ready": bool(health.get("whatsapp_ready") or health.get("connected")),
            "registered": health.get("registered"),
            "connected": health.get("connected"),
            "note": "Bridge is already registered; use Reconnect to recover or Disconnect to unpair.",
        }
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_phone = str(body.get("phone") or "").strip()
    digits = re.sub(r"\D+", "", raw_phone)
    if not raw_phone or not re.fullmatch(r"\+?[1-9]\d{1,14}", raw_phone):
        return {
            "bridge": bridge,
            "ok": False,
            "status": "invalid_phone",
            "error": "Enter the WhatsApp phone number in E.164 format, for example +6591234567.",
        }
    result = await _bridge_post()(bridge, "pairing-code", {"phone": raw_phone})
    if result.get("ok"):
        result["phone_last4"] = result.get("phone_last4") or (digits[-4:] if digits else None)
    return result


@router.get("/whatsapp/link")
async def whatsapp_link_page():
    """Self-contained QR linking page with auto-refresh.

    Polls /whatsapp/qr/{1,2} every 3s, re-renders the QR image, and flips each
    panel to a green 'Connected' state the moment the bridge reports
    whatsapp_ready. No build step -- inline HTML so it ships without rebuilding
    the SPA bundle.
    """
    from fastapi.responses import HTMLResponse

    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Link WhatsApp</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0b141a;color:#e9edef;margin:0;padding:24px}
 h1{font-size:20px;font-weight:600;margin:0 0 4px}
 p.sub{color:#8696a0;margin:0 0 24px;font-size:14px}
 .grid{display:flex;gap:24px;flex-wrap:wrap}
 .card{background:#111b21;border:1px solid #222d34;border-radius:12px;padding:20px;width:320px}
 .card h2{font-size:16px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
 .qrbox{width:280px;height:280px;display:flex;align-items:center;justify-content:center;background:#fff;border-radius:8px;margin:0 auto}
 .qrbox img{width:264px;height:264px;image-rendering:pixelated}
 .status{margin-top:14px;font-size:13px;text-align:center;color:#8696a0}
 .identity{margin-top:10px;text-align:center}
 .identity .phone{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:15px;color:#e9edef;font-weight:600}
 .identity .name{font-size:13px;color:#8696a0;margin-top:2px}
 .pairing{display:flex;gap:8px;margin-top:14px}
 .pairing input{min-width:0;flex:1;background:#0b141a;border:1px solid #2a3942;border-radius:8px;color:#e9edef;padding:9px 10px;font-size:13px}
 .pairing button{background:#00a884;border:0;border-radius:8px;color:#06130f;font-weight:700;padding:9px 10px;cursor:pointer}
 .pairing button:disabled{opacity:.55;cursor:not-allowed}
 .code{margin-top:10px;text-align:center;color:#d1fae5;font-size:13px;min-height:18px}
 .code strong{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:22px;letter-spacing:2px;color:#e9edef}
 .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
 .dot.wait{background:#f0b232}.dot.ok{background:#22c55e}.dot.err{background:#ef4444}
 .connected{color:#22c55e;font-weight:600}
 .spinner{display:inline-block;width:12px;height:12px;border:2px solid #2a3942;border-top-color:#00a884;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px}
 @keyframes spin{to{transform:rotate(360deg)}}
 .steps{color:#8696a0;font-size:13px;line-height:1.7;margin:20px 0 0;max-width:680px}
 code{background:#202c33;padding:2px 6px;border-radius:4px}
</style></head>
<body>
 <h1>Link WhatsApp accounts</h1>
 <p class="sub">Two independent account slots. Link either one in any order. QR refreshes automatically &mdash; just leave this open.</p>
 <div class="grid">
  <div class="card"><h2>Bridge 1 <span id="t1"></span></h2><div class="qrbox" id="q1"><span class="spinner"></span></div><div class="status" id="s1">Loading&hellip;</div><div class="identity" id="i1"></div><div class="pairing"><input id="p1" inputmode="tel" autocomplete="tel" placeholder="+6591234567"><button id="b1" onclick="pairCode('1')">Code</button></div><div class="code" id="c1"></div></div>
  <div class="card"><h2>Bridge 2 <span id="t2"></span></h2><div class="qrbox" id="q2"><span class="spinner"></span></div><div class="status" id="s2">Loading&hellip;</div><div class="identity" id="i2"></div><div class="pairing"><input id="p2" inputmode="tel" autocomplete="tel" placeholder="+6591234567"><button id="b2" onclick="pairCode('2')">Code</button></div><div class="code" id="c2"></div></div>
 </div>
 <div class="steps">
  <b>On your phone:</b> WhatsApp &rarr; Settings &rarr; <b>Linked Devices</b> &rarr; <b>Link a Device</b> &rarr; point the camera at a QR above.<br>
  If QR scanning keeps failing, enter the phone number for that bridge and use the code fallback. The panel turns <span class="connected">green</span> automatically once linked.
 </div>
<script>
function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
async function fetchJsonWithRetry(url, tries){
  let lastErr=null;
  for(let i=0;i<(tries||2);i++){
    const ctrl=new AbortController();
    const timer=setTimeout(()=>ctrl.abort(),12000);
    try{
      const r=await fetch(url,{cache:'no-store',credentials:'same-origin',signal:ctrl.signal});
      clearTimeout(timer);
      if(!r.ok) throw new Error('HTTP '+r.status);
      return await r.json();
    }catch(e){
      clearTimeout(timer);
      lastErr=e;
      await new Promise(resolve=>setTimeout(resolve,700*(i+1)));
    }
  }
  throw lastErr;
}
async function pairCode(b){
  const input=document.getElementById('p'+b), btn=document.getElementById('b'+b), out=document.getElementById('c'+b);
  const phone=(input&&input.value||'').trim();
  if(!phone){ out.innerHTML='Enter the phone number first.'; return; }
  btn.disabled=true; out.innerHTML='<span class="spinner"></span> requesting code&hellip;';
  try{
    const r=await fetch('/whatsapp/'+b+'/pairing-code',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      credentials:'same-origin',
      body:JSON.stringify({phone})
    });
    const d=await r.json();
    if(d.ok&&d.code){
      out.innerHTML='Use code<br><strong>'+esc(d.code)+'</strong><br>WhatsApp &rarr; Linked Devices &rarr; Link with phone number';
      poll(b);
    }else{
      out.innerHTML=esc(d.error||d.note||d.status||'pairing code failed');
    }
  }catch(e){
    out.innerHTML='Pairing-code request failed: '+esc(e&&e.message?e.message:e);
  }finally{
    btn.disabled=false;
  }
}
async function pollSession(b){
  // Fetch the paired-account identity (phone, push name) for this bridge and
  // paint it under the QR box. Called on each poll tick so a new pairing
  // shows up within one poll cycle. Uses /whatsapp/sessions which fans out
  // to both bridges — cheap, and matches what the bridge itself reports.
  try{
    const d=await fetchJsonWithRetry('/whatsapp/sessions',2);
    const iEl=document.getElementById('i'+b);
    if(!iEl) return;
    const idx = b === '1' ? 0 : 1;
    const s = (d.sessions||[])[idx];
    if(!s || !s.ok){ iEl.innerHTML=''; return; }
    if(s.connected && s.phone_number){
      const parts = [];
      parts.push('<div class="phone">+' + s.phone_number + '</div>');
      if(s.push_name) parts.push('<div class="name">' + s.push_name + '</div>');
      iEl.innerHTML = parts.join('');
    } else if(!s.connected){
      iEl.innerHTML='';
    }
  }catch(e){/* ignore */}
}
async function poll(b){
  const sEl=document.getElementById('s'+b), qEl=document.getElementById('q'+b), tEl=document.getElementById('t'+b);
  try{
    const d=await fetchJsonWithRetry('/whatsapp/qr/'+b,2);
    if(d.ready||d.status==='connected'){
      qEl.innerHTML='&#10003;'; qEl.style.background='#0b3d24'; qEl.style.color='#22c55e'; qEl.style.fontSize='90px';
      sEl.innerHTML='<span class="dot ok"></span> <span class="connected">Connected</span>';
      tEl.innerHTML='<span class="dot ok"></span>';
      pollSession(b);          // populate phone + push_name
      setTimeout(()=>poll(b),15000);   // slow poll once connected
      return;
    }
    if(d.qr){
      qEl.innerHTML='<img src="'+d.qr+'" width="264" height="264" style="image-rendering:pixelated">';
      qEl.style.background='#fff';
      sEl.innerHTML='<span class="dot wait"></span> Waiting for scan&hellip; (auto-refreshing)';
      tEl.innerHTML='<span class="dot wait"></span>';
    } else if(d.status==='unreachable'){
      qEl.innerHTML='&#9888;'; qEl.style.background='#3d1414'; qEl.style.color='#ef4444'; qEl.style.fontSize='60px';
      sEl.innerHTML='<span class="dot err"></span> Bridge unreachable: '+(d.error||'');
      tEl.innerHTML='<span class="dot err"></span>';
    } else if(d.status==='refreshing_qr'||d.status==='requesting_fresh_qr'||d.status==='waiting_for_fresh_qr'||d.status==='fresh_qr_reconnect_requested'){
      qEl.innerHTML='<span class="spinner"></span>'; qEl.style.background='#fff'; qEl.style.color='#111b21'; qEl.style.fontSize='16px';
      sEl.innerHTML='<span class="dot wait"></span> Waiting for the next QR code&hellip;';
      tEl.innerHTML='<span class="dot wait"></span>';
    } else {
      sEl.innerHTML='<span class="dot wait"></span> '+(d.status||'starting')+'&hellip;';
    }
  }catch(e){
    sEl.innerHTML='<span class="dot wait"></span> Dashboard poll missed; retrying automatically ('+(e&&e.message?e.message:e)+')';
    tEl.innerHTML='<span class="dot wait"></span>';
  }
  setTimeout(()=>poll(b),3000);
}
poll('1'); poll('2');
</script>
</body></html>"""
    return HTMLResponse(html)


