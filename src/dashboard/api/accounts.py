"""Accounts overview + per-platform summary routes.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 18
(cluster 6). Covers:

* ``/platform/{name}/summary`` — per-platform aggregate stats
* ``/accounts`` — unified cross-platform account/session state

Cross-module helpers are looked up on the parent ``dashboard_api`` module at
call time so that test monkey-patches keep working.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Depends

from src.db.connection import get_pool
from src.dashboard.api.auth import require_role

logger = logging.getLogger(__name__)


def _lookup(name: str):
    root = sys.modules.get("src.dashboard.api")
    if root is None:
        return None
    return getattr(root, name, None)


async def _get_pool():
    fn = _lookup("get_pool") or get_pool
    return await fn()


router = APIRouter()


@router.get("/platform/{name}/summary")
async def platform_summary(name: str, _user: dict = Depends(require_role("viewer"))):
    """Everything collected for ONE platform: recent media (what was just scraped),
    counts (media/users/posts/messages), per-account follow graph, and live status.
    Powers the per-platform dashboard sections."""
    name = (name or "").lower()
    pool = await _get_pool()
    out: dict = {"platform": name}
    _safe_fetch_int = _lookup("_safe_fetch_int")
    _LATEST_ACTIVITY_QUERIES = _lookup("_LATEST_ACTIVITY_QUERIES") or {}
    _PLATFORM_POSTS = _lookup("_PLATFORM_POSTS") or {}
    _PLATFORM_MESSAGES = _lookup("_PLATFORM_MESSAGES") or {}
    _with_bridge_overrides = _lookup("_with_bridge_overrides")
    async with pool.acquire() as conn:
        if name == "discord":
            out["source_mode"] = "beeper shadow"
            try:
                media_row = await conn.fetchrow(
                    """
                    SELECT count(*) AS media_count,
                           max(collected_at) AS media_last
                    FROM media_items
                    WHERE source = 'beeper'
                      AND filename LIKE 'beeper_discord_%'
                    """,
                    timeout=8,
                )
                out["media_count"] = int((media_row and media_row["media_count"]) or 0)
                out["media_last"] = media_row["media_last"] if media_row else None
                out["media_recent"] = [dict(r) for r in await conn.fetch(
                    """
                    SELECT id, entity_name, content_type, filename, collected_at
                    FROM media_items
                    WHERE source = 'beeper'
                      AND filename LIKE 'beeper_discord_%'
                    ORDER BY collected_at DESC
                    LIMIT 24
                    """,
                    timeout=8,
                )]
            except Exception:
                out["media_count"] = 0
                out["media_last"] = None
                out["media_recent"] = []
            out["posts_count"] = await _safe_fetch_int(
                conn,
                "SELECT count(*) FROM beeper_shadow_chats WHERE network = $1",
                "Discord",
                timeout=8,
            )
            out["posts_label"] = "chats"
            out["messages_count"] = await _safe_fetch_int(
                conn,
                """
                SELECT count(*)
                FROM beeper_shadow_messages
                WHERE network = $1
                  AND message_id IS NOT NULL
                """,
                "Discord",
                timeout=10,
            )
            try:
                out["messages_last"] = await conn.fetchval(
                    """
                    SELECT "timestamp"
                    FROM beeper_shadow_messages
                    WHERE network = $1
                      AND "timestamp" IS NOT NULL
                    ORDER BY "timestamp" DESC
                    LIMIT 1
                    """,
                    "Discord",
                    timeout=8,
                )
            except Exception:
                out["messages_last"] = out.get("media_last")
            out["users_count"] = await _safe_fetch_int(
                conn,
                """
                SELECT count(DISTINCT participant_id)
                FROM beeper_shadow_participants
                WHERE network = $1
                  AND participant_id IS NOT NULL
                  AND participant_id <> ''
                """,
                "Discord",
                timeout=10,
            )
            out["users_basis"] = "Beeper participants"
            out["follow_edges"] = []
            try:
                from src.core.source_freshness import compute_liveness
                live = {s["source"]: s for s in await compute_liveness(conn)}
                b = live.get("beeper")
                if b:
                    out["live"] = b["status"]
                    out["age_seconds"] = b["age_seconds"]
                    out["stale_after_seconds"] = b.get("stale_after_seconds")
            except Exception:
                pass
            return out

        try:
            out["media_count"] = int(await conn.fetchval(
                "SELECT count(*) FROM media_items WHERE source=$1", name, timeout=8) or 0)
            out["media_last"] = await conn.fetchval(
                "SELECT max(collected_at) FROM media_items WHERE source=$1", name, timeout=8)
            out["media_recent"] = [dict(r) for r in await conn.fetch(
                "SELECT id, entity_name, content_type, filename, collected_at "
                "FROM media_items WHERE source=$1 ORDER BY collected_at DESC LIMIT 24", name, timeout=8)]
        except Exception:
            out.setdefault("media_count", 0)
            out.setdefault("media_recent", [])
        query_spec = _LATEST_ACTIVITY_QUERIES.get(name)
        if query_spec:
            query, basis = query_spec
            try:
                out["last_activity"] = await conn.fetchval(query, timeout=8)
                out["activity_basis"] = basis
            except Exception:
                out["last_activity"] = out.get("media_last")
                out["activity_basis"] = "media"
        try:
            if name == "whatsapp":
                users = await _safe_fetch_int(conn, "SELECT count(*) FROM whatsapp_users", timeout=6)
                if users:
                    out["users_count"] = users
                    out["users_basis"] = "whatsapp_users"
                else:
                    out["users_count"] = await _safe_fetch_int(
                        conn,
                        """
                        SELECT count(DISTINCT sender_id)
                        FROM whatsapp_messages
                        WHERE sender_id IS NOT NULL
                        """,
                        timeout=8,
                    )
                    out["users_basis"] = "distinct whatsapp message senders"
            elif name == "telegram":
                row = await conn.fetchrow(
                    """
                    SELECT count(*) FILTER (WHERE COALESCE(is_bot, false) = false) AS people,
                           count(*) FILTER (WHERE is_bot = true) AS bots
                    FROM telegram_users
                    """,
                    timeout=6,
                )
                out["users_count"] = int((row and row["people"]) or 0)
                out["bots_count"] = int((row and row["bots"]) or 0)
            elif name == "beeper":
                out["users_count"] = int(await conn.fetchval(
                    "SELECT count(DISTINCT NULLIF(sender_id, '')) FROM beeper_shadow_messages",
                    timeout=6,
                ) or 0)
            else:
                out["users_count"] = int(await conn.fetchval(
                    "SELECT count(*) FROM social_users WHERE platform=$1", name, timeout=6) or 0)
        except Exception:
            out["users_count"] = 0
        # Whole-table counts via the planner estimate (instant) — count(*) on
        # 747k-row telegram_messages timed the endpoint out.
        pt = _PLATFORM_POSTS.get(name)
        if pt:
            try:
                out["posts_count"] = int(await conn.fetchval(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname=$1", pt) or 0)
                out["posts_label"] = pt.replace("_", " ")
            except Exception:
                pass
        mt = _PLATFORM_MESSAGES.get(name)
        if mt:
            tbl, col = mt
            try:
                out["messages_count"] = int(await conn.fetchval(
                    "SELECT reltuples::bigint FROM pg_class WHERE relname=$1", tbl) or 0)
                out["messages_last"] = await conn.fetchval(f"SELECT max({col}) FROM {tbl}", timeout=6)
            except Exception:
                pass
        try:
            out["follow_edges"] = [dict(r) for r in await conn.fetch(
                "SELECT owner_account, count(*) FILTER (WHERE direction='follower') AS followers, "
                "count(*) FILTER (WHERE direction='following') AS following "
                "FROM follow_edges WHERE platform=$1 GROUP BY owner_account", name)]
        except Exception:
            out["follow_edges"] = []
        # Live status: canonical per-source data freshness, not source_health's
        # coarse running/idle flag.
        try:
            from src.core.source_freshness import compute_liveness
            live_sources = await compute_liveness(conn)
            if name == "whatsapp":
                live_sources, whatsapp_bridge_health = await _with_bridge_overrides(live_sources)
                out["whatsapp_bridge_health"] = whatsapp_bridge_health
            live = {s["source"]: s for s in live_sources}
            cur = live.get(name)
            if cur:
                out["live"] = cur["status"]
                out["age_seconds"] = cur["age_seconds"]
                out["stale_after_seconds"] = cur.get("stale_after_seconds")
                out["collection_mode"] = cur.get("collection_mode")
                out["freshness_basis"] = cur.get("freshness_basis")
                out["health_detail"] = cur.get("detail")
                out["source_health_status"] = cur.get("source_health_status")
                out["source_health_error"] = cur.get("source_health_error")
        except Exception:
            pass
    return out


@router.get("/accounts")
async def accounts_overview(_user: dict = Depends(require_role("viewer"))):
    """Unified cross-platform account/session state — telegram accounts, whatsapp
    bridge devices, and cookie-auth sources — with health, so one panel covers all
    platforms instead of a telegram-only view.
    """
    _wa_bridge_base = _lookup("_wa_bridge_base")
    _audit_cookie_file = _lookup("_audit_cookie_file")
    _COOKIE_SOURCES = _lookup("_COOKIE_SOURCES") or {}
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            tg = [dict(r) for r in await conn.fetch(
                "SELECT name, phone, status, last_connected_at, last_error "
                "FROM telegram_user_accounts ORDER BY name")]
        except Exception:
            tg = []
        health = {}
        try:
            for r in await conn.fetch("SELECT source, status, last_success_at, last_error FROM source_health"):
                health[r["source"]] = dict(r)
        except Exception:
            pass

    # WhatsApp bridges (health) — concurrent, off the event loop.
    async def _wa_health(bridge: str) -> dict:
        base = _wa_bridge_base(bridge)
        import urllib.request

        def _do():
            with urllib.request.urlopen(f"{base}/health", timeout=6) as r:
                return __import__("json").loads(r.read().decode())
        try:
            h = await asyncio.to_thread(_do)
            return {"session": bridge, "ready": bool(h.get("whatsapp_ready")),
                    "status": "connected" if h.get("whatsapp_ready") else "awaiting_scan",
                    "needs_scan": bool(h.get("needs_scan")),
                    "auth_state": h.get("auth_state")}
        except Exception as exc:  # noqa: BLE001
            return {"session": bridge, "ready": False, "status": "unreachable", "error": str(exc)}

    wa = await asyncio.gather(_wa_health("1"), _wa_health("2"))

    # Persisted per-account validity (collector-tested each cycle).
    async with pool.acquire() as conn:
        try:
            cs_rows = {
                (r["platform"], r["account"]): (r["status"], r["reason"])
                for r in await conn.fetch("SELECT platform, account, status, reason FROM cookie_status")
            }
        except Exception:
            cs_rows = {}

    stale_days = int(os.getenv("COOKIE_STALE_DAYS", "30"))
    cred_dir = Path(os.getenv("COLLECTOR_CREDENTIALS_DIR", "/app/credentials"))
    cookies = []
    for src, keys in _COOKIE_SOURCES.items():
        base = cred_dir / src
        try:
            files = sorted(p for p in base.iterdir()
                           if p.suffix == ".txt" and p.name.lower() != "readme.txt")
        except Exception:
            files = []
        for p in files:
            a = _audit_cookie_file(p, keys)
            if not a:
                continue
            acct = p.stem
            if acct.startswith(src + "_"):
                acct = acct[len(src) + 1:]
            live_status, live_reason = cs_rows.get((src, acct), (None, None))
            # needs-refresh reason: persisted 'dead' (401) wins, then file signals.
            reason = None
            if live_status == "dead":
                reason = live_reason or "session dead (401)"
            elif a.get("reason"):
                reason = a["reason"]
            elif a.get("age_days") is not None and a["age_days"] > stale_days:
                reason = f"stale ({a['age_days']:.0f}d)"
            cookies.append({
                "source": src, "account": acct, "file": a["file"],
                "age_days": a["age_days"], "expiry_days": a.get("expiry_days"),
                "has_session": a["has_session"], "live_status": live_status,
                "needs_refresh": reason is not None, "reason": reason,
                "health": (health.get(src, {}) or {}).get("status", "unknown"),
            })

    return {
        "telegram": tg,
        "whatsapp": list(wa),
        "cookies": cookies,
        "health": {k: v.get("status") for k, v in health.items()},
    }
