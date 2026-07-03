"""Event + status notifications. Thin HTML message builders over telegram.send().

Every function is best-effort and returns telegram.send()'s bool. None raise.
"""
import html
import logging
from datetime import datetime, timezone

from . import telegram

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _esc(v) -> str:
    return html.escape(str(v))


async def notify_startup() -> bool:
    return await telegram.send(f"🟢 <b>UnifiedCollector started</b>\n{_now()}")


async def notify_shutdown() -> bool:
    return await telegram.send(f"🔴 <b>UnifiedCollector stopped</b>\n{_now()}")


async def notify_collection_summary(source: str, stats: dict) -> bool:
    """Posted when a source's collection run finishes. Skips no-op runs."""
    collected = int(stats.get("collected", 0) or 0)
    new = int(stats.get("new", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)
    if not (collected or new or failed):
        return False
    return await telegram.send(
        f"📦 <b>{_esc(source)}</b> run done\n"
        f"collected {collected} · new {new} · failed {failed}"
    )


async def notify_error(source: str, error) -> bool:
    """Posted when a source's collection run fails."""
    msg = _esc(str(error)[:500])
    return await telegram.send(
        f"❌ <b>{_esc(source)}</b> failed\n<code>{msg}</code>"
    )


def _humanize_age(secs: int) -> str:
    """Compact age: 45s / 8m / 3.2h / 2.1d."""
    s = max(0, int(secs))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


# Realtime platforms surfaced explicitly on their own line (the "live" feeds).
_REALTIME = ("telegram", "whatsapp", "beeper")


async def notify_status(snapshot: dict) -> bool:
    """Recurring heartbeat, built by the scheduler's _build_status().

    Accurate keys (all optional, tolerated if missing):
      ok (bool), media_items (int), media_items_estimate (bool), media_24h (int),
      msgs_24h (int), source_ages ({source: secs}), stale_sources (list),
      dead_sources (list), degraded_sources (list), error (str)
    """
    if snapshot.get("error"):
        return await telegram.send(
            f"⚠️ <b>UnifiedCollector status</b>\nDB unreachable: "
            f"<code>{_esc(snapshot['error'])[:300]}</code>"
        )

    head = "✅" if snapshot.get("ok", True) else "⚠️"
    lines = [f"{head} <b>UnifiedCollector status</b>  <i>{_now()}</i>"]

    mi = snapshot.get("media_items")
    if mi is not None:
        approx = "~" if snapshot.get("media_items_estimate") else ""
        m24 = snapshot.get("media_24h")
        tail = f" · +{m24:,} (24h)" if m24 is not None else ""
        lines.append(f"📦 Media: {approx}{mi:,}{tail}")

    if snapshot.get("msgs_24h") is not None:
        lines.append(f"💬 Messages (24h): {snapshot['msgs_24h']:,}")

    ages: dict = snapshot.get("source_ages") or {}
    stale = set(snapshot.get("stale_sources") or [])

    # Live feeds line: realtime platforms with their true last-activity age.
    live = []
    for s in _REALTIME:
        if s in ages:
            flag = "⚠️" if s in stale else ""
            live.append(f"{s[:2]} {_humanize_age(ages[s])}{flag}")
    if live:
        lines.append("🔴 Live: " + " · ".join(live))

    # Headless coverage: how many are fresh, and name any that are stale.
    headless = [s for s in ages if s not in _REALTIME]
    if headless:
        fresh = sum(1 for s in headless if s not in stale)
        lines.append(f"🌐 Headless: {fresh}/{len(headless)} fresh")

    stale_headless = sorted(s for s in stale if s not in _REALTIME)
    if stale_headless:
        lines.append("⚠️ Stale: " + ", ".join(
            f"{_esc(s)} ({_humanize_age(ages[s])})" for s in stale_headless if s in ages))

    dead = snapshot.get("dead_sources") or []
    degraded = snapshot.get("degraded_sources") or []
    if dead:
        lines.append("🔴 Dead: " + ", ".join(_esc(s) for s in dead))
    if degraded:
        lines.append("🟠 Degraded: " + ", ".join(_esc(s) for s in degraded))

    return await telegram.send("\n".join(lines))
