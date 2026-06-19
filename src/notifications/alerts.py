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


async def notify_status(snapshot: dict) -> bool:
    """Recurring heartbeat. `snapshot` is built by the scheduler's _build_status().

    Expected keys (all optional, tolerated if missing):
      ok (bool), media_items (int), items_24h (int), runs_24h (int),
      failures_24h (int), last_run (str), quiet_sources (list[str]),
      down_sources (list[str]), error (str)
    """
    if snapshot.get("error"):
        return await telegram.send(
            f"⚠️ <b>UnifiedCollector status</b>\nDB unreachable: "
            f"<code>{_esc(snapshot['error'])[:300]}</code>"
        )

    ok = snapshot.get("ok", True)
    head = "✅" if ok else "⚠️"
    lines = [f"{head} <b>UnifiedCollector status</b>"]

    mi = snapshot.get("media_items")
    if mi is not None:
        lines.append(f"Media items: {mi:,}")

    lines.append(
        f"Collected (24h): {snapshot.get('items_24h', 0):,} · "
        f"Runs (24h): {snapshot.get('runs_24h', 0):,} "
        f"({snapshot.get('failures_24h', 0)} failed)"
    )

    if snapshot.get("last_run"):
        lines.append(f"Last run: {_esc(snapshot['last_run'])}")

    down = snapshot.get("down_sources") or []
    quiet = snapshot.get("quiet_sources") or []
    if down:
        lines.append("🔴 Down: " + ", ".join(_esc(s) for s in down))
    if quiet:
        lines.append("⚠️ Quiet (&gt;24h): " + ", ".join(_esc(s) for s in quiet))

    return await telegram.send("\n".join(lines))
