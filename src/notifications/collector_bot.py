"""Durable Telegram control-room callbacks; the executor owns Docker access."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
from html import escape

from src.notifications import telegram

logger = logging.getLogger(__name__)


def _control_chat_id() -> str:
    return (os.getenv("CONTROL_TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("NOTIFY_TELEGRAM_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHAT_ID", "").strip())


def _executor_signature(key: str, action: str, target: str) -> str | None:
    secret = os.getenv("CONTROL_EXECUTOR_SECRET", "")
    if not secret:
        return None
    return hmac.new(secret.encode(), f"{key}|{action}|{target}".encode(), hashlib.sha256).hexdigest()


def build_watchdog_keyboard(token: str, containers: list[str]) -> list[list[dict]]:
    return [[
        {"text": f"🔄 Restart {', '.join(containers)}"[:64], "callback_data": f"wd:restart:{token}"},
        {"text": "🚫 Ignore", "callback_data": f"wd:ignore:{token}"},
    ]]


def build_memory_recovery_keyboard(token: str) -> list[list[dict]]:
    return [[{"text": "🚫 Cancel automatic action", "callback_data": f"wd:ignore:{token}"}]]


def _parse_callback_data(data: str) -> tuple[str, str, str] | None:
    parts = data.split(":")
    if len(parts) != 3:
        return None
    kind, action, token = parts
    if kind == "wd" and action in {"restart", "ignore"}:
        return kind, action, token
    if kind == "ln" and action in {"approve", "reject"}:
        return kind, action, token
    return None


async def create_watchdog_card(db, source: str, containers: list[str], age_seconds: float,
                               threshold_seconds: int, grace_seconds: int) -> str:
    """Persist a watchdog card before Telegram receives a button bearing its token."""
    token = secrets.token_urlsafe(18)
    payload = json.dumps({"source": source, "containers": containers, "age_seconds": age_seconds,
                          "threshold_seconds": threshold_seconds})
    await db.execute(
        """INSERT INTO control_bot_cards(token, kind, payload, chat_id, expires_at)
           VALUES($1, 'watchdog_restart', $2::jsonb, $3, now() + $4::interval)""",
        token, payload, int(_control_chat_id()), f"{max(grace_seconds * 2, 60)} seconds",
    )
    await db.execute(
        "INSERT INTO control_bot_audit(event, card_token, detail) VALUES('card_created', $1, $2::jsonb)",
        token, json.dumps({"source": source, "containers": containers}),
    )
    return token


async def create_memory_recovery_card(db, target: str, action: str, metrics: dict, expires_seconds: int) -> str:
    """Persist a cancellable automatic memory-recovery decision before notifying."""
    token = secrets.token_urlsafe(18)
    await db.execute(
        """INSERT INTO control_bot_cards(token, kind, payload, chat_id, expires_at)
           VALUES($1, 'memory_recovery', $2::jsonb, $3, now() + $4::interval)""",
        token, json.dumps({"source": target, "containers": [target], "action": action, "metrics": metrics}),
        int(_control_chat_id()), f"{max(expires_seconds, 60)} seconds",
    )
    await db.execute(
        "INSERT INTO control_bot_audit(event, card_token, detail) VALUES('memory_recovery_card_created',$1,$2::jsonb)",
        token, json.dumps({"target": target, "action": action, "metrics": metrics}),
    )
    return token


async def record_card_message(db, token: str, message_id: int | None) -> None:
    if message_id is not None:
        await db.execute("UPDATE control_bot_cards SET message_id=$2 WHERE token=$1", token, message_id)


async def is_card_ignored(db, token: str) -> bool:
    return bool(await db.fetchval(
        "SELECT resolution='ignore' AND expires_at > now() FROM control_bot_cards WHERE token=$1", token,
    ))


async def _is_current_chat_admin(chat_id: int, user_id: int) -> bool:
    member = await asyncio.to_thread(telegram.get_chat_member, chat_id, user_id)
    status = ((member.get("result") or {}).get("status") if member.get("ok") else "")
    return status in {"administrator", "creator"}


def _command_name(text: str) -> str | None:
    token = (text or "").strip().split(maxsplit=1)[0].lower()
    if not token.startswith("/"):
        return None
    return token[1:].split("@", 1)[0]


def _command_argument(text: str) -> str | None:
    parts = (text or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else None


def _command_arguments(text: str) -> list[str]:
    return (text or "").strip().split()[1:]


def _format_lane_status(payload: dict) -> str:
    totals = payload.get("totals") or {}
    lines = [
        "<b>Backfill lanes</b>",
        f"Queue depth: <b>{int(totals.get('queue_depth') or 0):,}</b>",
        f"Completed: {int(totals.get('completed') or 0):,}/{int(totals.get('total') or 0):,}",
        "",
    ]
    rows = sorted(payload.get("platforms") or [], key=lambda row: (-int(row.get("queue_depth") or 0), str(row.get("platform") or "")))
    for row in rows[:20]:
        platform = str(row.get("platform") or "unknown")
        queued = int(row.get("queue_depth") or 0)
        completed = int(row.get("completed") or 0)
        total = int(row.get("total") or 0)
        suffix = " (estimated)" if row.get("approximate") else ""
        lines.append(f"• <code>{platform}</code>: queued {queued:,}; completed {completed:,}/{total:,}{suffix}")
        lane = row.get("common_lane") if isinstance(row.get("common_lane"), dict) else row
        lag = lane.get("historical_lag_seconds")
        if lag is not None:
            lines.append(f"  historical lag {int(lag):,}s")
        if platform == "telegram":
            if row.get("active_history_workers") is not None:
                lines.append(f"  active history workers {int(row.get('active_history_workers') or 0)}")
            for account in row.get("account_throughput") or []:
                lines.append(
                    f"  <code>{escape(str(account.get('account_identity') or '?'))}</code>: "
                    f"{int(account.get('active_claims') or 0)} active claim(s), "
                    f"budget {int(account.get('claim_budget') or 1)}, "
                    f"{float(account.get('write_rate') or 0):.2f} msg/s, "
                    f"FloodWaits {int(account.get('flood_waits') or 0)}"
                )
    return "\n".join(lines)


def _format_lane_details(rows: list[dict], source: str | None) -> str:
    label = escape(source) if source else "all sources"
    lines = [f"<b>Backfill lane details</b> — {label}", f"Lanes shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        cursor = json.dumps(row.get("cursor") or {}, separators=(",", ":"), ensure_ascii=True)[:180]
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown'))}</code> / "
            f"<code>{escape(str(row.get('account_identity') or '?'))}</code> / "
            f"<code>{escape(str(row.get('target_identity') or '?'))}</code>\n"
            f"  {escape(str(row.get('status') or 'pending'))}; lease {escape(str(row.get('lease_owner') or 'none'))}; "
            f"fence {int(row.get('fence_token') or 0)}; retries {int(row.get('retry_count') or 0)}; "
            f"cost {int(row.get('item_cost') or 0):,} items / {int(row.get('byte_cost') or 0):,} bytes\n"
            f"  cursor <code>{escape(cursor)}</code>"
        )
    return "\n".join(lines)


def _format_source_recovery(rows: list[dict]) -> str:
    lines = ["<b>Source recovery profiles</b>", f"Profiles shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        lines.append(
            f"• <code>{escape(str(row.get('source_name') or 'unknown'))}</code> / "
            f"<code>{escape(str(row.get('account_identity') or 'source'))}</code>: "
            f"<b>{escape(str(row.get('state') or 'normal'))}</b>; expires "
            f"{escape(str(row.get('restricted_until') or 'n/a'))}; clean passes "
            f"{int(row.get('clean_passes') or 0)}/3\n"
            f"  {escape(str(row.get('reason') or 'provider restriction')[:180])}"
        )
    return "\n".join(lines)


def _format_realtime_feed_policy(policy: dict) -> str:
    excluded = policy.get("excluded_sources") or []
    whatsapp = "excluded" if policy.get("whatsapp_group_media_excluded") else "NOT excluded"
    return "\n".join([
        "<b>Realtime group-feed policy</b>",
        f"Feed: <b>{'enabled' if policy.get('enabled') else 'disabled'}</b>",
        f"WhatsApp and Beeper-WhatsApp media: <b>{whatsapp}</b>",
        f"Excluded sources: <code>{escape(', '.join(str(item) for item in excluded) or 'none')}</code>",
    ])


def _format_realtime_deliveries(payload: dict, source: str | None) -> str:
    label = escape(source) if source else "all sources"
    rows = payload.get("items") or []
    lines = [f"<b>Realtime feed deliveries</b> — {label}", f"Deliveries shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        reason = str(row.get("reason") or "").replace("\n", " ")[:160]
        size = row.get("file_size")
        suffix = f"; {int(size):,} bytes" if isinstance(size, int) else ""
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown'))}</code>: "
            f"<b>{escape(str(row.get('status') or 'unknown'))}</b>; "
            f"{escape(str(row.get('content_type') or 'media'))}{suffix}"
            f"{f'; {escape(reason)}' if reason else ''}"
        )
    if not payload.get("available"):
        lines.append("Delivery ledger is unavailable.")
    return "\n".join(lines)


def _format_seen_targets(payload: dict, source: str | None) -> str:
    label = escape(source) if source else "all sources"
    rows = payload.get("targets") or []
    summary = payload.get("summary") or {}
    total = sum(int(item.get("total") or 0) for item in summary.values())
    lines = [f"<b>Seen targets</b> — {label}", f"Registry total: <b>{total:,}</b>; targets shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        display = str(row.get("target_display") or row.get("target_key") or "unknown")[:100]
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown'))}</code> / "
            f"<code>{escape(display)}</code>: {escape(str(row.get('status') or 'seen'))}; "
            f"{escape(str(row.get('origin') or 'unknown'))}; priority {int(row.get('priority') or 0)}"
        )
    return "\n".join(lines)


def _format_link_approval_status(rows: list[dict], source: str | None) -> str:
    label = escape(source) if source else "all sources"
    lines = [f"<b>Link approvals</b> — {label}", f"Approvals shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        error = str(row.get("last_error") or "").replace("\n", " ")[:160]
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown'))}</code> / "
            f"<code>{escape(str(row.get('account_name') or 'unknown'))}</code>: "
            f"<b>{escape(str(row.get('state') or 'pending'))}</b>\n"
            f"  {escape(str(row.get('target_url') or 'unknown')[:240])}"
            f"{f'; {escape(error)}' if error else ''}"
        )
    return "\n".join(lines)


async def _lane_status(pool) -> dict:
    from src.dashboard.api.ingestion import get_backfill_equilibrium

    return await get_backfill_equilibrium(pool)


async def _lane_details(pool, source: str | None) -> list[dict]:
    from src.core.backfill_lanes import list_backfill_lanes

    return await list_backfill_lanes(pool, source=source, limit=30)


async def _source_recovery_status(pool) -> list[dict]:
    from src.core.recovery_profiles import list_recovery_profiles

    return await list_recovery_profiles(pool, limit=30)


async def _link_approval_status(pool, source: str | None) -> list[dict]:
    from src.core.link_approval_status import list_link_approvals

    return await list_link_approvals(pool, source=source, limit=30)


async def _realtime_deliveries(pool, source: str | None) -> dict:
    from src.core.media_status import list_realtime_media_deliveries

    return await list_realtime_media_deliveries(pool, source=source, limit=30)


async def _seen_targets(pool, source: str | None) -> dict:
    from src.core.seen_targets import list_seen_targets, seen_target_summary_by_source

    async with pool.acquire() as db:
        return {
            "summary": await seen_target_summary_by_source(db, source=source),
            "targets": await list_seen_targets(db, source=source, limit=30),
        }


def _realtime_feed_policy() -> dict:
    from src.core.realtime_feed_policy import get_realtime_feed_policy

    return get_realtime_feed_policy()


async def _control_room_health(pool) -> dict:
    """Use the dashboard's core liveness service; never scrape its HTML/API."""
    from src.core.source_freshness import compute_liveness

    async with pool.acquire() as db:
        sources = await compute_liveness(db)
        transport = await db.fetchrow(
            "SELECT last_poll_at, last_error, failure_count, next_retry_at "
            "FROM control_bot_transport_health WHERE singleton=true",
        )
    return {"sources": sources, "transport": dict(transport) if transport else None}


async def _coverage_status(pool) -> dict:
    """Call the shared dashboard/control-room coverage query layer."""
    from src.core.collection_coverage import get_collection_coverage

    return await get_collection_coverage(pool)


async def _capability_status(pool) -> list[dict]:
    from src.core.capability_receipts import latest_capability_receipts

    async with pool.acquire() as db:
        return await latest_capability_receipts(db)


async def _target_status(pool) -> list[dict]:
    from src.core.target_status import list_collection_targets

    return await list_collection_targets(pool, limit=30)


async def _schedule_status(pool) -> list[dict]:
    from src.core.schedule_status import list_collection_schedules

    return await list_collection_schedules(pool)


async def _set_schedule(pool, source: str, interval_hours: int, enabled: bool) -> dict:
    from src.core.schedule_status import set_collection_schedule

    return await set_collection_schedule(
        pool, source, interval_hours=interval_hours, enabled=enabled,
    )


async def _set_telegram_account_enabled(pool, name: str, enabled: bool) -> bool:
    from src.core.telegram_account_controls import set_telegram_account_enabled

    return await set_telegram_account_enabled(pool, name, enabled=enabled)


async def _add_target(pool, source: str, target: str, priority: int) -> dict:
    from src.core.target_status import add_collection_target

    return await add_collection_target(pool, source, target, priority=priority)


async def _source_controls_status(pool) -> list[dict]:
    from src.core.source_controls import list_source_controls

    return await list_source_controls(pool, limit=100)


async def _set_source_pause(pool, source: str, *, paused: bool) -> None:
    from src.core.source_controls import set_manual_pause

    await set_manual_pause(pool, source, paused=paused)


async def _collection_actions_status(pool, status: str) -> dict:
    from src.core.collection_action_queue import list_collection_actions

    return await list_collection_actions(pool, status=status, limit=30)


async def _rate_limit_status(pool) -> dict:
    from src.core.rate_limit_status import get_recent_rate_limits

    async with pool.acquire() as db:
        return await get_recent_rate_limits(db, hours=24, limit=24)


async def _account_status(pool) -> dict:
    from src.core.account_status import get_runtime_account_status

    return await get_runtime_account_status(pool)


async def _graph_status(pool, source: str) -> dict:
    from src.core.social_graph import get_social_graph

    return await get_social_graph(pool, source=source, limit=30)


async def _chat_status(pool, source: str) -> list[dict]:
    from src.core.messaging_chats import list_messaging_chats

    return await list_messaging_chats(pool, source=source, limit=30)


async def _interaction_status(pool, source: str) -> dict:
    from src.core.messaging_interactions import get_messaging_interactions

    return await get_messaging_interactions(pool, source=source, limit=30)


async def _media_status(pool, source: str | None) -> list[dict]:
    from src.core.media_status import list_recent_media

    return await list_recent_media(pool, source=source, limit=30)


async def _private_media(pool, media_id: str) -> dict:
    from src.core.media_status import get_private_media

    return await get_private_media(pool, media_id)


async def _profile_status(pool, source: str) -> list[dict]:
    from src.core.profile_status import list_profiles

    return await list_profiles(pool, source=source, limit=30)


async def _profile_posts(pool, source: str, username: str) -> dict:
    from src.core.profile_posts import get_profile_posts

    return await get_profile_posts(pool, source=source, username=username, limit=30)


async def _dlq_status(pool, source: str | None) -> list[dict]:
    from src.core.dlq_status import list_dead_letter_queue

    return await list_dead_letter_queue(pool, source=source, limit=30)


async def _stories_status(pool) -> dict:
    from src.core.ephemeral_media_status import get_ephemeral_media_status

    return await get_ephemeral_media_status(pool, limit=30)


async def _member_status(pool, source: str, chat_id: str) -> list[dict]:
    from src.core.messaging_members import list_messaging_members

    return await list_messaging_members(pool, source=source, chat_id=chat_id, limit=100)


async def _message_status(pool, source: str, chat_id: str) -> dict:
    from src.core.messaging_messages import get_messaging_messages

    return await get_messaging_messages(pool, source=source, chat_id=chat_id, limit=30)


async def _whatsapp_users(pool, search: str | None) -> list[dict]:
    from src.core.whatsapp_users import list_whatsapp_users

    return await list_whatsapp_users(pool, search=search, limit=30)


async def _whatsapp_user_history(pool, jid: str) -> list[dict]:
    from src.core.whatsapp_users import get_whatsapp_user_history

    return await get_whatsapp_user_history(pool, jid=jid, limit=30)


async def _messaging_coverage_status(pool) -> list[dict]:
    from src.core.messaging_coverage import get_messaging_coverage

    return await get_messaging_coverage(pool)


def _format_control_room_health(payload: dict) -> str:
    rows = payload.get("sources") or []
    status_icon = {"live": "✅", "degraded": "⚠️", "stale": "⚠️", "dead": "🛑"}
    live = sum(row.get("status") == "live" for row in rows)
    lines = [f"<b>Collector health</b> — {live}/{len(rows)} live", ""]
    for row in sorted(rows, key=lambda item: (item.get("status") != "live", str(item.get("source") or "")))[:30]:
        status = str(row.get("status") or "unknown")
        age = row.get("age_seconds")
        age_text = "no durable write" if age is None else f"{int(age):,}s ago"
        modes = row.get("collection_modes") or [row.get("collection_mode") or "unknown"]
        mode_text = "/".join(str(mode) for mode in modes if mode) or "unknown"
        target = row.get("freshness_target_seconds") or row.get("stale_after_seconds")
        target_text = f"; target {int(target):,}s" if target is not None else ""
        lines.append(
            f"{status_icon.get(status, '❔')} <code>{row.get('source', 'unknown')}</code>: "
            f"{status}; {mode_text}; {age_text}{target_text}"
        )
    transport = payload.get("transport")
    if transport:
        failures = int(transport.get("failure_count") or 0)
        detail = "healthy" if failures == 0 and not transport.get("last_error") else str(transport.get("last_error") or "retrying")[:180]
        lines.extend(["", f"<b>Bot transport:</b> {detail}"])
    return "\n".join(lines)


def _format_dlq_status(source: str | None, rows: list[dict]) -> str:
    label = escape(source) if source else "all sources"
    lines = [f"<b>Dead-letter queue</b> — {label}", f"Entries shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        error = escape(str(row.get("error_message") or "unknown error"))[:180]
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown'))}</code> "
            f"{escape(str(row.get('status') or 'pending'))}; retries {int(row.get('retry_count') or 0)}\n"
            f"  <code>{escape(str(row.get('entity_id') or row.get('content_id') or '?'))}</code> — {error}"
        )
    return "\n".join(lines)


def _format_stories_status(payload: dict) -> str:
    stats = payload.get("stats") or {}
    lines = [
        "<b>Stories and highlights</b>",
        f"Stories: <b>{int(stats.get('stories') or 0):,}</b> · Highlights: <b>{int(stats.get('highlights') or 0):,}</b>",
        f"Accounts: {int(stats.get('accounts') or 0):,} · Sources: {int(stats.get('sources') or 0):,}",
        "",
    ]
    for row in (payload.get("accounts") or [])[:30]:
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown'))}</code> / "
            f"<code>{escape(str(row.get('entity_name') or 'unknown'))}</code>: "
            f"{int(row.get('story_count') or 0):,} stories, {int(row.get('highlight_count') or 0):,} highlights"
        )
    return "\n".join(lines)


def _format_member_status(source: str, chat_id: str, rows: list[dict]) -> str:
    lines = [f"<b>{escape(source.title())} members</b> — <code>{escape(chat_id)}</code>", f"Members shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        label = row.get("username") or row.get("pushname") or row.get("name") or row.get("member_id") or "unknown"
        lines.append(f"• <code>{escape(str(label))}</code> — {escape(str(row.get('role') or 'member'))}")
    return "\n".join(lines)


def _format_message_status(source: str, chat_id: str, payload: dict) -> str:
    rows = payload.get("messages") or []
    lines = [f"<b>{escape(source.title())} messages</b> — <code>{escape(chat_id)}</code>", f"Messages shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        sender = row.get("sender_username") or row.get("sender_pushname") or row.get("sender_name") or row.get("sender_platform_id") or row.get("sender_jid") or "unknown"
        body = str(row.get("text") or row.get("caption") or "[media or empty message]").replace("\n", " ")[:500]
        lines.append(f"• <code>{escape(str(sender))}</code>: {escape(body)}")
    return "\n".join(lines)


def _format_whatsapp_users(rows: list[dict]) -> str:
    lines = ["<b>WhatsApp users</b>", f"Users shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        jid = str(row.get("platform_user_id") or "unknown")
        label = row.get("pushname") or row.get("name") or jid
        lines.append(f"• <code>{escape(jid)}</code> — {escape(str(label))[:160]}")
    return "\n".join(lines)


def _format_whatsapp_user_history(jid: str, rows: list[dict]) -> str:
    lines = [f"<b>WhatsApp user history</b> — <code>{escape(jid)}</code>", f"Messages shown: <b>{len(rows)}</b>", ""]
    for row in rows:
        body = str(row.get("text") or "[media or empty message]").replace("\n", " ")[:500]
        lines.append(f"• {escape(body)}")
    return "\n".join(lines)


def _format_messaging_coverage(rows: list[dict]) -> str:
    lines = ["<b>Messaging coverage</b>", "Native counts are canonical; Beeper mirrors are not additive.", ""]
    for row in rows[:30]:
        lines.append(
            f"• <code>{escape(str(row.get('network') or 'unknown'))}</code>: native "
            f"{int(row.get('native_messages') or 0):,} messages / {int(row.get('native_chats') or 0):,} chats; "
            f"Beeper {int(row.get('beeper_messages') or 0):,} messages / {int(row.get('beeper_chats') or 0):,} chats"
        )
    return "\n".join(lines)


def _format_coverage_status(payload: dict) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "<b>Collection coverage</b>",
        "Fresh: <b>{fresh}</b> · Degraded: <b>{degraded}</b> · Stale: <b>{stale}</b> · Unknown: <b>{unknown}</b>".format(
            fresh=int(summary.get("fresh") or 0), degraded=int(summary.get("degraded") or 0),
            stale=int(summary.get("stale") or 0), unknown=int(summary.get("unknown") or 0),
        ),
        "",
    ]
    for row in sorted(payload.get("sources") or [], key=lambda item: (str(item.get("status") or ""), str(item.get("source") or "")))[:30]:
        lines.append(
            f"• <code>{row.get('source', 'unknown')}</code>: {row.get('status', 'unknown')}; "
            f"{int(row.get('rows_24h') or 0):,} rows / {int(row.get('media_24h') or 0):,} media (24h)"
        )
    return "\n".join(lines)


def _format_capability_status(rows: list[dict]) -> str:
    lines = ["<b>Source capability receipts</b>"]
    if not rows:
        return "\n".join(lines + ["No receipts recorded yet."])
    for row in rows[:30]:
        path = f" via {row['path']}" if row.get("path") else ""
        lines.append(f"• <code>{row['source']}</code> / <code>{row['capability']}</code>: <b>{row['state']}</b>{path}")
    return "\n".join(lines)


def _format_target_status(rows: list[dict]) -> str:
    lines = ["<b>Collection targets</b>", f"Showing highest-priority {len(rows)} target(s).", ""]
    for row in rows:
        label = str(row.get("target_name") or row.get("target_id") or "unknown")[:80]
        lines.append(f"• <code>{row.get('source', 'unknown')}</code> / <code>{label}</code>: {row.get('status', 'pending')}; priority {int(row.get('priority') or 0)}")
    return "\n".join(lines)


def _format_schedule_status(rows: list[dict]) -> str:
    lines = ["<b>Collection schedules</b>"]
    for row in rows[:30]:
        enabled = "enabled" if row.get("enabled") else "paused"
        next_run = row.get("next_run")
        next_text = next_run.isoformat() if hasattr(next_run, "isoformat") else str(next_run or "unscheduled")
        lines.append(f"• <code>{row.get('source', 'unknown')}</code>: {enabled}; every {int(row.get('interval_hours') or 0)}h; next {next_text}")
    return "\n".join(lines)


def _format_source_controls_status(rows: list[dict]) -> str:
    lines = [f"<b>Source controls</b> — {len(rows)} source(s)", ""]
    for row in rows:
        state = escape(str(row.get("state") or "unknown")[:40])
        source = escape(str(row.get("source_name") or "unknown")[:80])
        enabled = "enabled" if row.get("enabled") else "disabled"
        failures = int(row.get("consecutive_failures") or 0)
        blocked = row.get("blocked_until")
        reason = str(row.get("stop_reason") or "")[:100]
        suffix = f"; blocked until {escape(str(blocked))}" if blocked else ""
        if reason:
            suffix += f"; {escape(reason)}"
        lines.append(f"• <code>{source}</code>: <b>{state}</b>; {enabled}; {failures} failure(s){suffix}")
    if not rows:
        lines.append("No source-control records.")
    return "\n".join(lines)


def _format_collection_actions_status(payload: dict) -> str:
    rows = payload.get("actions") or []
    lines = [f"<b>Collector actions</b> — {len(rows)} {escape(str(payload.get('filter') or 'open'))} record(s)", ""]
    for row in rows:
        lines.append(
            f"• <code>{escape(str(row.get('source') or 'unknown')[:60])}</code> / "
            f"<code>{escape(str(row.get('action_type') or 'review')[:60])}</code>: "
            f"priority {int(row.get('priority') or 0)}; {escape(str(row.get('reason') or '')[:180])}"
        )
    if not rows:
        lines.append("No matching collector actions.")
    return "\n".join(lines)


def _format_rate_limit_status(payload: dict) -> str:
    active = payload.get("active_event_summary") or []
    rows = payload.get("recent_summary") or []
    lines = [f"<b>Rate limits</b> — {len(active)} active / {len(rows)} recent", ""]
    for row in rows[:24]:
        status = "active" if row.get("active_now") else "expired"
        reason = str(row.get("reason") or "no reason")[:120]
        lines.append(
            f"• <code>{row.get('source') or 'unknown'}</code> / <code>{row.get('account') or 'default'}</code>: "
            f"{status}; {int(row.get('count') or 0)} event(s); {reason}"
        )
    if not rows:
        lines.append("No rate-limit events in the last 24 hours.")
    return "\n".join(lines)


def _format_account_status(payload: dict) -> str:
    lines = ["<b>Account state</b>", ""]
    telegram_accounts = payload.get("telegram") or []
    lines.append(f"<b>Telegram</b> — {len(telegram_accounts)} configured")
    for row in telegram_accounts[:12]:
        detail = str(row.get("last_error") or row.get("status") or "unknown")[:120]
        lines.append(f"• <code>{row.get('name') or 'unknown'}</code>: {detail}")
    whatsapp = payload.get("whatsapp") or []
    lines.extend(["", f"<b>WhatsApp</b> — {len(whatsapp)} bridge slot(s)"])
    for row in whatsapp:
        status = str(row.get("status") or "unknown")
        invite = (
            f"; invite join {row.get('invite_accept_state')}"
            if row.get("invite_accept_available") else ""
        )
        history = row.get("history") if isinstance(row.get("history"), dict) else {}
        history_text = ""
        if history.get("chats"):
            history_text = (
                f"; history {int(history.get('complete') or 0):,}/{int(history.get('chats') or 0):,} complete; "
                f"{int(history.get('pending') or 0):,} in flight; {int(history.get('eligible') or 0):,} eligible"
            )
        lines.append(f"• bridge <code>{row.get('session') or '?'}</code>: {status}{invite}{history_text}")
    beeper = payload.get("beeper") or []
    lines.extend(["", f"<b>Beeper</b> — {len(beeper)} connected account(s)"])
    for row in beeper[:30]:
        network = escape(str(row.get("network") or "unknown")[:60])
        label = escape(str(row.get("user_username") or row.get("user_full_name") or row.get("account_id") or "unknown")[:80])
        phone = str(row.get("user_phone") or "")
        phone_hint = f"; phone ••••{escape(phone[-4:])}" if len(phone) >= 4 else ""
        lines.append(f"• <code>{network}</code> / <code>{label}</code>: {escape(str(row.get('status') or 'unknown'))}{phone_hint}")
    cookie_status = payload.get("cookie_status") or {}
    lines.extend(["", f"<b>Cookie accounts</b> — {len(cookie_status)} recorded"])
    for (platform, account), (status, reason) in list(cookie_status.items())[:20]:
        detail = f"; {str(reason)[:100]}" if reason else ""
        lines.append(f"• <code>{platform}</code> / <code>{account}</code>: {status}{detail}")
    return "\n".join(lines)


def _format_graph_status(source: str, payload: dict) -> str:
    edges = payload.get("edges") or []
    lines = [f"<b>{escape(source)} relationship graph</b> — {len(payload.get('nodes') or [])} nodes / {len(edges)} edges", ""]
    for edge in edges[:30]:
        source_user = escape(str(edge.get("source_user") or "unknown")[:80])
        target_user = escape(str(edge.get("target_user") or "unknown")[:80])
        edge_type = escape(str(edge.get("edge_type") or "related")[:40])
        lines.append(f"• <code>{source_user}</code> — {edge_type} → <code>{target_user}</code>")
    if not edges:
        lines.append("No captured relationships for this source.")
    return "\n".join(lines)


def _format_chat_status(source: str, rows: list[dict]) -> str:
    lines = [f"<b>{escape(source)} chats</b> — showing {len(rows)}", ""]
    for row in rows:
        title = escape(str(row.get("title") or row.get("platform_chat_id") or "unknown")[:90])
        kind = escape(str(row.get("type") or "unknown")[:30])
        members = row.get("members_count")
        suffix = f"; {int(members):,} members" if isinstance(members, int) else ""
        lines.append(f"• <code>{title}</code>: {kind}{suffix}")
    if not rows:
        lines.append("No captured chats.")
    return "\n".join(lines)


def _format_interaction_status(source: str, payload: dict) -> str:
    reactions = payload.get("reactions") or []
    polls = payload.get("polls") or []
    lines = [f"<b>{escape(source)} interactions</b> — {len(reactions)} reactions / {len(polls)} polls", "", "<b>Reactions</b>"]
    for row in reactions[:20]:
        actor = escape(str(row.get("actor") or "unknown")[:70])
        emoji = escape(str(row.get("emoji") or "removed")[:20])
        state = "removed" if row.get("removed") else emoji
        lines.append(f"• <code>{actor}</code>: {state}")
    if not reactions:
        lines.append("No captured reactions.")
    lines.extend(["", "<b>Polls</b>"])
    for row in polls[:20]:
        question = escape(str(row.get("question") or "untitled")[:120])
        voters = row.get("total_voters")
        suffix = f"; {int(voters):,} voters" if isinstance(voters, int) else ""
        lines.append(f"• <code>{question}</code>{suffix}")
    if not polls:
        lines.append("No captured polls.")
    return "\n".join(lines)


def _format_media_status(source: str | None, rows: list[dict]) -> str:
    title = f"{escape(source)} media" if source else "Recent media"
    lines = [f"<b>{title}</b> — showing {len(rows)}", ""]
    for row in rows:
        label = escape(str(row.get("filename") or row.get("entity_name") or "unknown")[:100])
        content_type = escape(str(row.get("content_type") or "unknown")[:40])
        size = row.get("file_size")
        suffix = f"; {int(size):,} bytes" if isinstance(size, int) else ""
        lines.append(f"• <code>{label}</code>: {content_type}{suffix}")
    if not rows:
        lines.append("No matching media items.")
    return "\n".join(lines)


def _format_profile_status(source: str, rows: list[dict]) -> str:
    lines = [f"<b>{escape(source)} profiles</b> — showing {len(rows)}", ""]
    for row in rows:
        username = escape(str(row.get("username") or row.get("platform_user_id") or "unknown")[:80])
        followers = row.get("followers_count")
        posts = row.get("posts_collected")
        details = []
        if isinstance(followers, int):
            details.append(f"{followers:,} followers")
        if isinstance(posts, int):
            details.append(f"{posts:,} posts")
        lines.append(f"• <code>{username}</code>{': ' + '; '.join(details) if details else ''}")
    if not rows:
        lines.append("No captured profiles.")
    return "\n".join(lines)


def _format_profile_posts(source: str, username: str, payload: dict) -> str:
    posts = payload.get("posts") or []
    lines = [f"<b>{escape(source)} posts for {escape(username)}</b> — showing {len(posts)}", ""]
    for post in posts:
        title = escape(str(post.get("title") or post.get("description") or "untitled")[:140])
        likes = post.get("like_count")
        suffix = f"; {int(likes):,} likes" if isinstance(likes, int) else ""
        lines.append(f"• <code>{title}</code>{suffix}")
    if not posts:
        lines.append("No captured posts for this profile.")
    return "\n".join(lines)


async def _verified_manifest(pool) -> dict:
    from src.core.verified_exclusion import persist_verified_exclusion_manifest

    async with pool.acquire() as db:
        return await persist_verified_exclusion_manifest(db)


def _format_verified_manifest(manifest: dict) -> str:
    lines = [
        "<b>Verified-exclusion dry run</b>",
        f"Manifest: <code>{manifest.get('id', 'pending')}</code>",
        f"Explicit profile candidates: <b>{int(manifest.get('profile_candidates') or 0):,}</b>",
    ]
    for source in manifest.get("sources") or []:
        lines.append(f"• <code>{source['source']}</code>: {int(source['profile_candidates']):,}")
    lines.append("No data or media was deleted.")
    return "\n".join(lines)


async def _prepare_link_card(pool, actor_id: int) -> dict | None:
    token = secrets.token_urlsafe(18)
    async with pool.acquire() as db:
        async with db.transaction():
            approval = await db.fetchrow(
                """SELECT * FROM link_approvals
                   WHERE source IN ('telegram', 'whatsapp') AND state='pending' AND expires_at > now() AND control_card_token IS NULL
                   ORDER BY requested_at, id FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
            if approval is None:
                return None
            approval = dict(approval)
            await db.execute(
                """UPDATE link_approvals SET control_card_token=$2, requested_by=$3, updated_at=now()
                   WHERE id=$1""",
                approval["id"], token, actor_id,
            )
            await db.execute(
                """INSERT INTO control_bot_cards(token,kind,payload,chat_id,expires_at)
                   VALUES($1,'link_approval',$2::jsonb,$3,$4)""",
                token,
                json.dumps({"approval_id": str(approval["id"]), "source": approval["source"],
                            "account_name": approval["account_name"]}),
                int(_control_chat_id()),
                approval["expires_at"],
            )
            await db.execute(
                """INSERT INTO control_bot_audit(event,actor_id,card_token,detail)
                   VALUES('link_approval_requested',$1,$2,$3::jsonb)""",
                actor_id, token, json.dumps({"approval_id": str(approval["id"])}),
            )
            approval["card_token"] = token
            return approval


async def _release_link_card(pool, approval_id, token: str) -> None:
    async with pool.acquire() as db:
        async with db.transaction():
            await db.execute("DELETE FROM control_bot_cards WHERE token=$1 AND resolved_at IS NULL", token)
            await db.execute(
                """UPDATE link_approvals SET control_card_token=NULL, requested_by=NULL, updated_at=now()
                   WHERE id=$1 AND control_card_token=$2 AND state='pending'""",
                approval_id, token,
            )


async def _handle_links_command(pool, chat_id: int, actor_id: int) -> None:
    approval = await _prepare_link_card(pool, actor_id)
    if approval is None:
        await asyncio.to_thread(telegram.send_message_to, chat_id, "✅ <b>No pending link approvals.</b>")
        return
    detail = (
        "<b>Pending link approval</b>\n"
        f"Source: <code>{approval['source']}</code>\n"
        f"Account: <code>{approval['account_name']}</code>\n"
        f"URL: <code>{approval['target_url']}</code>"
    )
    dm = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
    if not dm.get("ok"):
        await _release_link_card(pool, approval["id"], approval["card_token"])
        await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Could not DM the pending link.</b> Start a private chat with this bot, then retry <code>/links</code>.")
        return
    keyboard = [[
        {"text": "✅ Approve", "callback_data": f"ln:approve:{approval['card_token']}"},
        {"text": "🚫 Reject", "callback_data": f"ln:reject:{approval['card_token']}"},
    ]]
    card = await telegram.send_with_keyboard(
        f"🔗 <b>{escape(str(approval['source']).title())} link approval</b>\nAccount: <code>{approval['account_name']}</code>", keyboard,
    )
    if not card.get("ok"):
        await _release_link_card(pool, approval["id"], approval["card_token"])
        await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Could not show the approval card.</b> Retry <code>/links</code>.")
        return
    async with pool.acquire() as db:
        await record_card_message(db, approval["card_token"], (card.get("result") or {}).get("message_id"))
    await asyncio.to_thread(telegram.send_message_to, chat_id, "🔗 <b>Approval card ready.</b> Link details were sent by private DM.")


async def _handle_command(pool, message: dict) -> None:
    chat = message.get("chat") or {}
    actor = message.get("from") or {}
    chat_id = chat.get("id")
    actor_id = actor.get("id")
    command = _command_name(str(message.get("text") or ""))
    if command not in {"accounts", "actions", "add-target", "capabilities", "chats", "coverage", "disable-telegram", "dlq", "enable-telegram", "feed-deliveries", "feed-status", "graph", "health", "interactions", "lane-details", "lanes", "link-status", "links", "media", "media-get", "members", "messages", "messaging-coverage", "pause-source", "posts", "profiles", "rates", "resume-source", "schedule", "schedules", "seen-targets", "source-controls", "source-recovery", "stories", "targets", "verified", "wa-user", "wa-users"} or str(chat_id) != _control_chat_id() or not isinstance(actor_id, int):
        return
    if not await _is_current_chat_admin(chat_id, actor_id):
        await asyncio.to_thread(telegram.send_message_to, chat_id, "Current chat admins only.")
        return
    if command in {"pause-source", "resume-source"}:
        source = _command_argument(str(message.get("text") or "")).lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use:</b> <code>/pause-source telegram</code> or <code>/resume-source telegram</code>.")
            return
        paused = command == "pause-source"
        try:
            await _set_source_pause(pool, source, paused=paused)
            detail = f"<b>{'Paused' if paused else 'Resumed'}</b> <code>{escape(source)}</code>."
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = f"🎛️ <b>Source {'pause' if paused else 'resume'} recorded.</b>"
            if not sent.get("ok"):
                group_text = "⚠️ <b>Source control was recorded, but private DM could not be delivered.</b>"
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES($1,$2,$3::jsonb)",
                    f"source_{'paused' if paused else 'resumed'}", actor_id, json.dumps({"source": source}),
                )
        except Exception:
            logger.exception("control-bot source control command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Source control is temporarily unavailable.</b>")
        return
    if command == "source-recovery":
        try:
            sent = await asyncio.to_thread(
                telegram.send_message_to, actor_id,
                _format_source_recovery(await _source_recovery_status(pool)),
            )
            group_text = "🛡️ <b>Recovery status sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM recovery status.</b>"
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
        except Exception:
            logger.exception("control-bot source recovery command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Recovery status is temporarily unavailable.</b>")
        return
    if command == "schedule":
        args = _command_arguments(str(message.get("text") or ""))
        if len(args) not in {2, 3} or not re.fullmatch(r"[a-z0-9_-]{1,32}", args[0].lower()):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/schedule &lt;source&gt; &lt;1-720h&gt; [on|off]</code>.")
            return
        try:
            interval_hours = int(args[1])
        except ValueError:
            interval_hours = 0
        enabled_text = args[2].lower() if len(args) == 3 else "on"
        if not 1 <= interval_hours <= 720 or enabled_text not in {"on", "off"}:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/schedule &lt;source&gt; &lt;1-720h&gt; [on|off]</code>.")
            return
        source, enabled = args[0].lower(), enabled_text == "on"
        try:
            result = await _set_schedule(pool, source, interval_hours, enabled)
            detail = (
                f"<b>Schedule updated</b> <code>{escape(source)}</code>\n"
                f"Every <b>{result['interval_hours']}h</b>; {'enabled' if result['enabled'] else 'disabled'}."
            )
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🗓️ <b>Schedule update recorded.</b>" if sent.get("ok") else "⚠️ <b>Schedule update recorded, but private DM could not be delivered.</b>"
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('schedule_updated',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "interval_hours": interval_hours, "enabled": enabled}),
                )
        except Exception:
            logger.exception("control-bot schedule command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Schedule update is temporarily unavailable.</b>")
        return
    if command in {"disable-telegram", "enable-telegram"}:
        name = _command_argument(str(message.get("text") or ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/disable-telegram &lt;account&gt;</code> or <code>/enable-telegram &lt;account&gt;</code>.")
            return
        enabled = command == "enable-telegram"
        try:
            changed = await _set_telegram_account_enabled(pool, name, enabled)
            if not changed:
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Account was not found or is already in that state.</b>")
                return
            detail = f"<b>Telegram account {'enabled' if enabled else 'disabled'}</b> <code>{escape(name)}</code>."
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "👤 <b>Telegram account state recorded.</b>" if sent.get("ok") else "⚠️ <b>Telegram account state changed, but private DM could not be delivered.</b>"
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES($1,$2,$3::jsonb)",
                    f"telegram_account_{'enabled' if enabled else 'disabled'}", actor_id, json.dumps({"name": name}),
                )
        except Exception:
            logger.exception("control-bot Telegram account command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Telegram account control is temporarily unavailable.</b>")
        return
    if command == "add-target":
        args = _command_arguments(str(message.get("text") or ""))
        if len(args) not in {2, 3} or not re.fullmatch(r"[a-z0-9_-]{1,32}", args[0].lower()) or len(args[1]) > 512 or any(char.isspace() for char in args[1]):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/add-target &lt;source&gt; &lt;target&gt; [priority]</code>.")
            return
        try:
            priority = int(args[2]) if len(args) == 3 else 0
        except ValueError:
            priority = 10001
        if not -10000 <= priority <= 10000:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Priority must be between -10000 and 10000.</b>")
            return
        source, target = args[0].lower(), args[1]
        try:
            await _add_target(pool, source, target, priority)
            detail = f"<b>Target added</b> <code>{escape(source)}</code> / <code>{escape(target)}</code> (priority {priority})."
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🎯 <b>Target addition recorded.</b>" if sent.get("ok") else "⚠️ <b>Target was added, but private DM could not be delivered.</b>"
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('target_added',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "priority": priority}),
                )
        except Exception as exc:
            from src.core.target_status import TargetAlreadyKnown

            if isinstance(exc, TargetAlreadyKnown):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Target already has durable discovery evidence.</b>")
                return
            logger.exception("control-bot target add command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Target addition is temporarily unavailable.</b>")
        return
    if command == "links":
        try:
            await _handle_links_command(pool, chat_id, actor_id)
        except Exception:
            logger.exception("control-bot links command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Link approvals are temporarily unavailable.</b>")
        return
    if command == "link-status":
        source = _command_argument(str(message.get("text") or ""))
        if source:
            source = source.lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/link-status telegram</code>.")
                return
        try:
            detail = _format_link_approval_status(await _link_approval_status(pool, source), source)
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🔗 <b>Link approval status sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM link approval status.</b> Start a private chat with this bot, then retry <code>/link-status</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('link_status_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot link status command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Link approval status is temporarily unavailable.</b>")
        return
    if command == "feed-status":
        try:
            detail = _format_realtime_feed_policy(_realtime_feed_policy())
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📡 <b>Realtime feed policy sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM realtime feed policy.</b> Start a private chat with this bot, then retry <code>/feed-status</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('feed_status_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot feed status command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Realtime feed policy is temporarily unavailable.</b>")
        return
    if command == "feed-deliveries":
        source = _command_argument(str(message.get("text") or ""))
        if source:
            source = source.lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/feed-deliveries whatsapp</code>.")
                return
        try:
            detail = _format_realtime_deliveries(await _realtime_deliveries(pool, source), source)
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📨 <b>Realtime delivery status sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM realtime delivery status.</b> Start a private chat with this bot, then retry <code>/feed-deliveries</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('feed_deliveries_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot feed deliveries command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Realtime delivery status is temporarily unavailable.</b>")
        return
    if command == "seen-targets":
        source = _command_argument(str(message.get("text") or ""))
        if source:
            source = source.lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/seen-targets instagram</code>.")
                return
        try:
            detail = _format_seen_targets(await _seen_targets(pool, source), source)
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🎯 <b>Seen targets sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM seen targets.</b> Start a private chat with this bot, then retry <code>/seen-targets</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('seen_targets_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot seen targets command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Seen targets are temporarily unavailable.</b>")
        return
    if command == "verified":
        try:
            detail = _format_verified_manifest(await _verified_manifest(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🧾 <b>Verified-exclusion manifest sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM the manifest.</b> Start a private chat with this bot, then retry <code>/verified</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
        except Exception:
            logger.exception("control-bot verified manifest command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Verified-exclusion manifest is temporarily unavailable.</b>")
        return
    if command == "dlq":
        source = _command_argument(str(message.get("text") or ""))
        if source:
            source = source.lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/dlq telegram</code>.")
                return
        try:
            detail = _format_dlq_status(source, await _dlq_status(pool, source))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📥 <b>Dead-letter queue sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM dead-letter queue details.</b> Start a private chat with this bot, then retry <code>/dlq</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('dlq_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot dlq command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Dead-letter queue is temporarily unavailable.</b>")
        return
    if command == "stories":
        try:
            detail = _format_stories_status(await _stories_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📚 <b>Story and highlight counts sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM story and highlight counts.</b> Start a private chat with this bot, then retry <code>/stories</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('stories_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot stories command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Story and highlight counts are temporarily unavailable.</b>")
        return
    if command == "members":
        args = _command_arguments(str(message.get("text") or ""))
        if len(args) != 2 or args[0].lower() not in {"telegram", "whatsapp", "beeper"} or not re.fullmatch(r"[A-Za-z0-9_@.!:/-]{1,512}", args[1]):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/members telegram|whatsapp|beeper &lt;chat-id&gt;</code>.")
            return
        source, member_chat_id = args[0].lower(), args[1]
        try:
            detail = _format_member_status(source, member_chat_id, await _member_status(pool, source, member_chat_id))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "👥 <b>Member roster sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM the member roster.</b> Start a private chat with this bot, then retry <code>/members</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('members_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "chat_id": member_chat_id, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot members command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Member roster is temporarily unavailable.</b>")
        return
    if command == "wa-users":
        search = _command_argument(str(message.get("text") or ""))
        if search and (len(search) > 80 or not re.fullmatch(r"[A-Za-z0-9_@.+ -]+", search)):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a short name or JID search:</b> <code>/wa-users name</code>.")
            return
        try:
            detail = _format_whatsapp_users(await _whatsapp_users(pool, search))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "👤 <b>WhatsApp users sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM WhatsApp users.</b> Start a private chat with this bot, then retry <code>/wa-users</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('wa_users_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"search": search, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot WhatsApp users command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>WhatsApp users are temporarily unavailable.</b>")
        return
    if command == "wa-user":
        jid = _command_argument(str(message.get("text") or ""))
        if not jid or not re.fullmatch(r"[A-Za-z0-9_@.!:/-]{1,512}", jid):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/wa-user &lt;user-jid&gt;</code>.")
            return
        try:
            detail = _format_whatsapp_user_history(jid, await _whatsapp_user_history(pool, jid))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "✉️ <b>WhatsApp user history sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM WhatsApp user history.</b> Start a private chat with this bot, then retry <code>/wa-user</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('wa_user_history_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"jid": jid, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot WhatsApp user history command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>WhatsApp user history is temporarily unavailable.</b>")
        return
    if command == "messages":
        args = _command_arguments(str(message.get("text") or ""))
        if len(args) != 2 or args[0].lower() not in {"telegram", "whatsapp", "beeper"} or not re.fullmatch(r"[A-Za-z0-9_@.!:/-]{1,512}", args[1]):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/messages telegram|whatsapp|beeper &lt;chat-id&gt;</code>.")
            return
        source, message_chat_id = args[0].lower(), args[1]
        try:
            detail = _format_message_status(source, message_chat_id, await _message_status(pool, source, message_chat_id))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "✉️ <b>Message history sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM message history.</b> Start a private chat with this bot, then retry <code>/messages</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('messages_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "chat_id": message_chat_id, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot messages command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Message history is temporarily unavailable.</b>")
        return
    if command == "messaging-coverage":
        try:
            detail = _format_messaging_coverage(await _messaging_coverage_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📶 <b>Messaging coverage sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM messaging coverage.</b> Start a private chat with this bot, then retry <code>/messaging-coverage</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('messaging_coverage_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot messaging coverage command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Messaging coverage is temporarily unavailable.</b>")
        return
    if command == "health":
        try:
            detail = _format_control_room_health(await _control_room_health(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🩺 <b>Collector health sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM collector health.</b> Start a private chat with this bot, then retry <code>/health</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('health_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot health command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Collector health is temporarily unavailable.</b>")
        return
    if command == "coverage":
        try:
            detail = _format_coverage_status(await _coverage_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📈 <b>Collection coverage sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM collection coverage.</b> Start a private chat with this bot, then retry <code>/coverage</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('coverage_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot coverage command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Collection coverage is temporarily unavailable.</b>")
        return
    if command == "capabilities":
        try:
            detail = _format_capability_status(await _capability_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🧪 <b>Capability receipts sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM capability receipts.</b> Start a private chat with this bot, then retry <code>/capabilities</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('capabilities_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot capability command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Capability receipts are temporarily unavailable.</b>")
        return
    if command == "targets":
        try:
            detail = _format_target_status(await _target_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🎯 <b>Collection targets sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM collection targets.</b> Start a private chat with this bot, then retry <code>/targets</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('targets_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot targets command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Collection targets are temporarily unavailable.</b>")
        return
    if command == "schedules":
        try:
            detail = _format_schedule_status(await _schedule_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🗓️ <b>Collection schedules sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM collection schedules.</b> Start a private chat with this bot, then retry <code>/schedules</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('schedules_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot schedules command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Collection schedules are temporarily unavailable.</b>")
        return
    if command == "source-controls":
        try:
            detail = _format_source_controls_status(await _source_controls_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🎛️ <b>Source controls sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM source controls.</b> Start a private chat with this bot, then retry <code>/source-controls</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('source_controls_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot source-controls command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Source controls are temporarily unavailable.</b>")
        return
    if command == "actions":
        status = (_command_argument(str(message.get("text") or "")) or "open").lower()
        if status not in {"open", "resolved", "all"}:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/actions open</code>, <code>/actions resolved</code>, or <code>/actions all</code>.")
            return
        try:
            detail = _format_collection_actions_status(await _collection_actions_status(pool, status))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🧭 <b>Collector actions sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM collector actions.</b> Start a private chat with this bot, then retry <code>/actions</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('actions_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"status": status, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot actions command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Collector actions are temporarily unavailable.</b>")
        return
    if command == "rates":
        try:
            detail = _format_rate_limit_status(await _rate_limit_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "⏱️ <b>Rate limits sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM rate limits.</b> Start a private chat with this bot, then retry <code>/rates</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('rates_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot rates command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Rate limits are temporarily unavailable.</b>")
        return
    if command == "accounts":
        try:
            detail = _format_account_status(await _account_status(pool))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "👤 <b>Account state sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM account state.</b> Start a private chat with this bot, then retry <code>/accounts</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('accounts_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot accounts command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Account state is temporarily unavailable.</b>")
        return
    if command == "graph":
        source = (_command_argument(str(message.get("text") or "")) or "instagram").lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/graph instagram</code>.")
            return
        try:
            detail = _format_graph_status(source, await _graph_status(pool, source))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🕸️ <b>Relationship graph sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM the relationship graph.</b> Start a private chat with this bot, then retry <code>/graph</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('graph_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot graph command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Relationship graph is temporarily unavailable.</b>")
        return
    if command == "chats":
        source = (_command_argument(str(message.get("text") or "")) or "telegram").lower()
        if source not in {"telegram", "whatsapp", "beeper"}:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Choose</b> <code>/chats telegram</code>, <code>/chats whatsapp</code>, or <code>/chats beeper</code>.")
            return
        try:
            detail = _format_chat_status(source, await _chat_status(pool, source))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "💬 <b>Chat list sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM chat details.</b> Start a private chat with this bot, then retry <code>/chats</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('chats_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot chats command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Chat details are temporarily unavailable.</b>")
        return
    if command == "interactions":
        source = (_command_argument(str(message.get("text") or "")) or "telegram").lower()
        if source not in {"telegram", "whatsapp"}:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Choose</b> <code>/interactions telegram</code> <b>or</b> <code>/interactions whatsapp</code>.")
            return
        try:
            detail = _format_interaction_status(source, await _interaction_status(pool, source))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🗳️ <b>Interactions sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM interaction details.</b> Start a private chat with this bot, then retry <code>/interactions</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('interactions_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot interactions command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Interaction details are temporarily unavailable.</b>")
        return
    if command == "media":
        source = _command_argument(str(message.get("text") or ""))
        if source:
            source = source.lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/media instagram</code>.")
                return
        try:
            detail = _format_media_status(source, await _media_status(pool, source))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "🗂️ <b>Media listing sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM the media listing.</b> Start a private chat with this bot, then retry <code>/media</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('media_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot media command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Media listing is temporarily unavailable.</b>")
        return
    if command == "media-get":
        media_id = _command_argument(str(message.get("text") or ""))
        if not media_id:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/media-get &lt;media UUID&gt;</code>.")
            return
        try:
            item = await _private_media(pool, media_id)
            caption = f"<b>{escape(str(item.get('filename') or 'media')[:180])}</b>\n<code>{escape(str(item.get('source') or 'unknown'))}</code>"
            delivered = await telegram.send_document_to_detailed(actor_id, str(item["path"]), caption=caption)
            if delivered[0]:
                await asyncio.to_thread(telegram.send_message_to, chat_id, "📎 <b>Media delivered by private DM.</b>")
            else:
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Could not deliver that media privately.</b>")
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('media_retrieval_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"media_id": media_id, "source": item.get("source"), "dm_delivered": bool(delivered[0])}),
                )
        except ValueError:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>That media item is unavailable.</b>")
        except Exception:
            logger.exception("control-bot private media retrieval failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Private media retrieval is temporarily unavailable.</b>")
        return
    if command == "profiles":
        source = (_command_argument(str(message.get("text") or "")) or "").lower()
        if source not in {"tiktok", "lemon8", "threads"}:
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Choose</b> <code>/profiles tiktok</code>, <code>/profiles lemon8</code>, or <code>/profiles threads</code>.")
            return
        try:
            detail = _format_profile_status(source, await _profile_status(pool, source))
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "👥 <b>Profile list sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM profile details.</b> Start a private chat with this bot, then retry <code>/profiles</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('profiles_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot profiles command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Profile details are temporarily unavailable.</b>")
        return
    if command == "posts":
        args = _command_arguments(str(message.get("text") or ""))
        if len(args) != 2 or args[0].lower() not in {"lemon8", "tiktok", "threads"} or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args[1]):
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Usage:</b> <code>/posts lemon8|tiktok|threads &lt;username&gt;</code>.")
            return
        source, username = args[0].lower(), args[1]
        try:
            payload = await _profile_posts(pool, source, username)
            detail = _format_profile_posts(source, username, payload)
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📝 <b>Posts sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM post details.</b> Start a private chat with this bot, then retry <code>/posts</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('posts_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "username": username, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot posts command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Post details are temporarily unavailable.</b>")
        return
    if command == "lane-details":
        source = _command_argument(str(message.get("text") or ""))
        if source:
            source = source.lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", source):
                await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Use a simple source name:</b> <code>/lane-details whatsapp</code>.")
                return
        try:
            detail = _format_lane_details(await _lane_details(pool, source), source)
            sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
            group_text = "📋 <b>Lane details sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM lane details.</b> Start a private chat with this bot, then retry <code>/lane-details</code>."
            await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
            async with pool.acquire() as db:
                await db.execute(
                    "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('lane_details_requested',$1,$2::jsonb)",
                    actor_id, json.dumps({"source": source, "dm_delivered": bool(sent.get("ok"))}),
                )
        except Exception:
            logger.exception("control-bot lane details command failed")
            await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Lane details are temporarily unavailable.</b>")
        return
    try:
        detail = _format_lane_status(await _lane_status(pool))
        sent = await asyncio.to_thread(telegram.send_message_to, actor_id, detail)
        group_text = "📊 <b>Lane status sent by private DM.</b>" if sent.get("ok") else "⚠️ <b>Could not DM lane status.</b> Start a private chat with this bot, then retry <code>/lanes</code>."
        await asyncio.to_thread(telegram.send_message_to, chat_id, group_text)
        async with pool.acquire() as db:
            await db.execute(
                "INSERT INTO control_bot_audit(event,actor_id,detail) VALUES('lanes_requested',$1,$2::jsonb)",
                actor_id, json.dumps({"dm_delivered": bool(sent.get("ok"))}),
            )
    except Exception:
        logger.exception("control-bot lanes command failed")
        await asyncio.to_thread(telegram.send_message_to, chat_id, "⚠️ <b>Lane status is temporarily unavailable.</b>")


async def _accept_update(pool, update: dict) -> bool:
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return False
    async with pool.acquire() as db:
        result = await db.execute(
            "INSERT INTO control_bot_updates(update_id, payload) VALUES($1, $2::jsonb) ON CONFLICT DO NOTHING",
            update_id, json.dumps(update),
        )
    return result.endswith("1")


async def _health(pool, *, error: str | None = None) -> None:
    async with pool.acquire() as db:
        if error:
            await db.execute(
                """INSERT INTO control_bot_transport_health(singleton,last_poll_at,last_error,failure_count,next_retry_at)
                   VALUES(true,now(),$1,1,now()+interval '5 seconds')
                   ON CONFLICT(singleton) DO UPDATE SET last_poll_at=now(),last_error=$1,
                     failure_count=control_bot_transport_health.failure_count+1,
                     next_retry_at=now()+interval '5 seconds',updated_at=now()""", error[:500])
        else:
            await db.execute(
                """INSERT INTO control_bot_transport_health(singleton,last_poll_at,last_success_at,last_error,failure_count,next_retry_at)
                   VALUES(true,now(),now(),NULL,0,NULL)
                   ON CONFLICT(singleton) DO UPDATE SET last_poll_at=now(),last_success_at=now(),
                     last_error=NULL,failure_count=0,next_retry_at=NULL,updated_at=now()"""
            )


async def _flush_outbox(pool) -> None:
    async with pool.acquire() as db:
        rows = await db.fetch(
            "SELECT id, chat_id, message_id, payload FROM control_bot_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 20"
        )
    for row in rows:
        payload = row["payload"] or {}
        source = payload.get("source", "?")
        action = str(payload.get("action") or "restart").title()
        if payload.get("status") == "succeeded":
            text = f"✅ <b>{action} completed</b> — <code>{source}</code>"
        else:
            text = f"❌ <b>{action} failed</b> — <code>{source}</code>: {payload.get('detail', 'executor failed')}"
        result = await asyncio.to_thread(telegram.edit_message_text, row["chat_id"], row["message_id"], text)
        if result.get("ok"):
            async with pool.acquire() as db:
                await db.execute("UPDATE control_bot_outbox SET sent_at=now() WHERE id=$1", row["id"])


async def _queue_or_ignore(pool, card: dict, action: str, actor: dict) -> tuple[dict | None, str]:
    token = card["token"]
    target = ((card["payload"] or {}).get("containers") or [""])[0]
    actor_id = int(actor["id"])
    async with pool.acquire() as db:
        async with db.transaction():
            active = await db.fetchrow(
                """SELECT * FROM control_bot_cards WHERE token=$1 AND resolved_at IS NULL
                   AND expires_at > now() FOR UPDATE""", token)
            if active is None:
                return None, "Card is already resolved or expired."
            if action == "restart" and (not target or len((active["payload"] or {}).get("containers") or []) != 1):
                await db.execute("UPDATE control_bot_cards SET resolved_at=now(),resolution='rejected',resolved_by=$2 WHERE token=$1", token, actor_id)
                return None, "Card target is not eligible for control."
            key = f"{token}:{action}"
            signature = _executor_signature(key, action, target)
            if action == "restart" and signature is None:
                return None, "Control executor is not configured."
            status = "succeeded" if action == "ignore" else "queued"
            result = json.dumps({"detail": "auto-restart suppressed"}) if action == "ignore" else None
            operation = await db.fetchrow(
                """INSERT INTO control_bot_operations(idempotency_key,card_token,action,target,actor_id,actor_username,
                       signature,status,result,expires_at,completed_at)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,CASE WHEN $8='succeeded' THEN now() END)
                   ON CONFLICT(idempotency_key) DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key
                   RETURNING *""",
                key, token, action, target, actor_id, actor.get("username"), signature, status, result,
                active["expires_at"],
            )
            await db.execute(
                "UPDATE control_bot_cards SET resolved_at=now(),resolution=$2,resolved_by=$3 WHERE token=$1",
                token, action, actor_id,
            )
            await db.execute(
                """INSERT INTO control_bot_audit(event,actor_id,card_token,operation_id,detail)
                   VALUES('callback_accepted',$1,$2,$3,$4::jsonb)""",
                actor_id, token, operation["id"], json.dumps({"action": action}),
            )
            return dict(operation), ""


async def _resolve_link_approval(pool, card: dict, action: str, actor: dict) -> tuple[bool, str]:
    if card.get("kind") != "link_approval":
        return False, "This is not a link approval card."
    approval_id = (card.get("payload") or {}).get("approval_id")
    if not approval_id:
        return False, "Approval record is missing."
    actor_id = int(actor["id"])
    state = "approved" if action == "approve" else "rejected"
    async with pool.acquire() as db:
        async with db.transaction():
            active = await db.fetchrow(
                """SELECT * FROM control_bot_cards WHERE token=$1 AND resolved_at IS NULL
                   AND expires_at > now() FOR UPDATE""", card["token"],
            )
            if active is None:
                return False, "Card is already resolved or expired."
            result = await db.fetchrow(
                """UPDATE link_approvals SET state=$2, approved_by=$3,
                       approved_at=CASE WHEN $2='approved' THEN now() ELSE NULL END,
                       completed_at=CASE WHEN $2='rejected' THEN now() ELSE NULL END,
                       updated_at=now()
                   WHERE id=$1 AND state='pending' AND expires_at > now()
                   RETURNING source, account_name""",
                approval_id, state, actor_id,
            )
            if result is None:
                return False, "Approval is no longer pending."
            await db.execute(
                "UPDATE control_bot_cards SET resolved_at=now(),resolution=$2,resolved_by=$3 WHERE token=$1",
                card["token"], state, actor_id,
            )
            await db.execute(
                """INSERT INTO control_bot_audit(event,actor_id,card_token,detail)
                   VALUES('link_approval_resolved',$1,$2,$3::jsonb)""",
                actor_id, card["token"], json.dumps({"state": state, "approval_id": str(approval_id)}),
            )
    return True, state


async def _handle_callback(pool, cq: dict) -> None:
    callback_id = str(cq.get("id") or "")
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    actor = cq.get("from") or {}
    actor_id = actor.get("id")
    parsed = _parse_callback_data(str(cq.get("data") or ""))
    if str(chat_id) != _control_chat_id() or not isinstance(actor_id, int):
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, "This control is only valid in the control room.", show_alert=True)
        return
    if parsed is None:
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, "Unknown control.", show_alert=True)
        return
    await asyncio.to_thread(telegram.answer_callback_query, callback_id, "Checking authorization…")
    if not await _is_current_chat_admin(chat_id, actor_id):
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, "Current chat admins only.", show_alert=True)
        return
    kind, action, token = parsed
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT * FROM control_bot_cards WHERE token=$1", token)
    if row is None:
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, "Card expired.")
        return
    card = dict(row)
    if int(card["chat_id"]) != chat_id:
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, "Card belongs to another chat.", show_alert=True)
        return
    if kind == "ln":
        resolved, error = await _resolve_link_approval(pool, card, action, actor)
        if not resolved:
            await asyncio.to_thread(telegram.answer_callback_query, callback_id, error, show_alert=True)
            return
        if message_id is not None:
            await asyncio.to_thread(telegram.edit_message_text, chat_id, int(message_id), f"{'✅ Approved' if action == 'approve' else '🚫 Rejected'} <b>link approval</b>")
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, "Approval recorded.")
        return
    operation, error = await _queue_or_ignore(pool, card, action, actor)
    if operation is None:
        await asyncio.to_thread(telegram.answer_callback_query, callback_id, error, show_alert=True)
        return
    source = (card["payload"] or {}).get("source", "?")
    if action == "ignore":
        if message_id is not None:
            await asyncio.to_thread(telegram.edit_message_text, chat_id, int(message_id), f"🚫 <b>Ignored</b> — <code>{source}</code>")
        return
    if message_id is not None:
        await asyncio.to_thread(telegram.edit_message_text, chat_id, int(message_id), f"⏳ <b>Restart queued</b> — <code>{source}</code>")


async def run_callback_poller(pool) -> None:
    """Accept each update durably before advancing Telegram's offset."""
    offset = 0
    delay = 1
    try:
        info = await asyncio.to_thread(telegram.get_webhook_info)
        if ((info.get("result") or {}).get("url")):
            await asyncio.to_thread(telegram.delete_webhook)
    except Exception:
        logger.exception("control-bot webhook reset failed")
    while True:
        try:
            updates = await asyncio.to_thread(telegram.get_updates, offset, 25)
            if not updates.get("ok"):
                await _health(pool, error=str(updates.get("error") or "getUpdates failed"))
                await asyncio.sleep(min(delay, 30)); delay = min(delay * 2, 30)
                continue
            await _health(pool)
            await _flush_outbox(pool)
            delay = 1
            for update in updates.get("result") or []:
                accepted = await _accept_update(pool, update)
                offset = int(update["update_id"]) + 1
                callback = update.get("callback_query")
                if callback is not None:
                    if not accepted:
                        await asyncio.to_thread(telegram.answer_callback_query, str(callback.get("id") or ""), "Already accepted.")
                        continue
                    await _handle_callback(pool, callback)
                    continue
                if accepted and update.get("message") is not None:
                    await _handle_command(pool, update["message"])
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.exception("control-bot poll failed")
            try:
                await _health(pool, error=f"{type(exc).__name__}: {exc}")
            except Exception:
                logger.exception("control-bot health write failed")
            await asyncio.sleep(min(delay, 30)); delay = min(delay * 2, 30)


async def run_control_bot_service() -> None:
    from src.db.connection import close_pool, get_pool
    if not _control_chat_id():
        logger.error("control-bot disabled: CONTROL_TELEGRAM_CHAT_ID is not configured")
        return
    pool = await get_pool()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    task = asyncio.create_task(run_callback_poller(pool))
    await stop.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await close_pool()
