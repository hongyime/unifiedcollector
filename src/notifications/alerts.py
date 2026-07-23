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


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f"{singular}s")


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
    item_word = _plural(collected, "item")
    return await telegram.send(
        f"📦 <b>{_display_source(source)} collection finished</b>\n"
        f"Collected {collected:,} {item_word}. New: {new:,}. Failed: {failed:,}."
    )


async def notify_error(source: str, error) -> bool:
    """Posted when a source's collection run fails."""
    msg = _esc(str(error)[:500])
    return await telegram.send(
        f"❌ <b>{_display_source(source)} collection failed</b>\n"
        f"Error: <code>{msg}</code>"
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

# github/strava are perpetual discovery crawls (expanding frontier — the queue
# never reaches 0), so they get their own "crawl" phase and are NOT counted as
# "draining" backfill that the user is waiting to finish.
_CRAWL = ("github", "strava")

_SOURCE_LABELS = {
    "beeper": "Beeper",
    "discord": "Discord",
    "facebook": "Facebook",
    "github": "GitHub",
    "instagram": "Instagram",
    "lemon8": "Lemon8",
    "search": "Search",
    "strava": "Strava",
    "telegram": "Telegram",
    "threads": "Threads",
    "tiktok": "TikTok",
    "website": "Website",
    "whatsapp": "WhatsApp",
    "x": "Twitter / X",
    "youtube": "YouTube",
}

_SCOPE_LABELS = {
    "feed": "feed fetches",
    "gps_streams": "GPS route streams",
    "profile_fetch": "profile fetches",
    "profile": "profile fetches",
    "stories": "story fetches",
    "story": "story fetches",
}


def _display_source(source) -> str:
    raw = str(source or "unknown").strip()
    if not raw:
        raw = "unknown"
    key = raw.lower().replace("_rate_limit", "").replace("_ratelimit", "")
    return _esc(_SOURCE_LABELS.get(key, raw.replace("_", " ").title()))


def _display_scope(scope) -> str:
    raw = str(scope or "").strip()
    if not raw:
        return ""
    return _esc(_SCOPE_LABELS.get(raw.lower(), raw.replace("_", " ")))


def _current_hour_window() -> str:
    started = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return started.strftime("%Y-%m-%d %H:00 UTC")


def _format_hourly_source(row: dict) -> str:
    source = _display_source(row.get("source", "?"))
    records = int(row.get("records", 0) or 0)
    files = int(row.get("files", 0) or 0)
    rate_limits = int(row.get("rate_limits", 0) or 0)
    access_errors = int(row.get("access_errors", 0) or 0)
    details = []
    if records:
        details.append(f"{records:,} {_plural(records, 'source row')}")
    if files:
        details.append(f"{files:,} {_plural(files, 'media file')}")
    if rate_limits:
        details.append(f"{rate_limits:,} HTTP 429 {_plural(rate_limits, 'event')}")
    if access_errors:
        details.append(f"{access_errors:,} auth/access HTTP {_plural(access_errors, 'error')}")
    if not details:
        details.append("no new rows")
    return f"• {source}: " + ", ".join(details)


def _format_cooldown(row: dict) -> str:
    service = _display_source(row.get("service", "unknown"))
    account = str(row.get("account") or "").strip()
    scope = _display_scope(row.get("scope"))
    remaining = _humanize_age(int(row.get("seconds_remaining", 0) or 0))
    streak = int(row.get("streak", 0) or 0)
    events = int(row.get("events", 0) or 0)
    reason = str(row.get("reason") or "").strip()
    subject = service
    if scope:
        subject += f" {scope}"
    if account:
        subject += f" for {_esc(account)}"
    if streak:
        return f"• {subject}: active cooldown for {remaining} after {streak} consecutive HTTP 429s."
    if events:
        detail = f" ({_esc(reason)})" if reason else ""
        return f"• {subject}: active cooldown for {remaining} after {events:,} HTTP 429 {_plural(events, 'event')}{detail}."
    return f"• {subject}: active cooldown for {remaining}."


def _format_rate_limit_event(row: dict) -> str:
    source = _display_source(row.get("source", "?"))
    account = str(row.get("account") or "").strip()
    scope = _display_scope(row.get("scope"))
    status = int(row.get("status_code", 429) or 429)
    count = int(row.get("count", 0) or 0)

    subject = source
    if scope:
        subject += f" {scope}"
    if account:
        subject += f" for {_esc(account)}"
    events = f"{count:,} recorded event" if count == 1 else f"{count:,} recorded events"
    return f"• {subject}: HTTP {status}, {events} this hour."


def _format_access_event(row: dict) -> str:
    source = _display_source(row.get("source", "?"))
    account = str(row.get("account") or "").strip()
    scope = _display_scope(row.get("scope"))
    status = row.get("status_code")
    count = int(row.get("count", 0) or 0)
    reason = str(row.get("reason") or "").strip()

    subject = source
    if scope:
        subject += f" {scope}"
    if account:
        subject += f" for {_esc(account)}"
    status_text = f"HTTP {int(status)}" if status is not None else "non-429 HTTP failure"
    events = f"{count:,} event" if count == 1 else f"{count:,} events"
    detail = f" ({_esc(reason)})" if reason else ""
    return f"• {subject}: {status_text}, {events} this hour{detail}."


def _fmt_count(n: int) -> str:
    """1_317_543 -> 1.3M, 13213 -> 13k, 940 -> 940."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _fmt_bytes(n) -> str:
    if n is None:
        return "unknown"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _backfill_phase(src: str, snap: dict) -> str:
    """Classify a source's ingestion phase for the Backfill heartbeat line.

    realtime  – messaging caught up (recent inserts carry recent timestamps, no
                meaningful backfill queue)
    draining  – messaging still pulling history (low realtime %% or a non-trivial
                spider-queue backlog of dialogs/profiles)
    crawl     – github/strava perpetual discovery (queue never drains)
    current   – headless/social source that is fresh (cyclic refresh, no backfill)
    stale     – no recent activity (a problem, surfaced elsewhere too)
    idle      – source has produced no data yet
    """
    stale = set(snap.get("stale_sources") or [])
    if src in stale:
        return "stale"
    ages = snap.get("source_ages") or {}
    if src not in ages:
        return "idle"
    if src in _CRAWL:
        return "crawl"
    if src in _REALTIME:
        rt = (snap.get("realtime_pct") or {}).get(src, 0.0)
        pending = (snap.get("queue_pending") or {}).get(src, 0)
        return "realtime" if (rt >= 90.0 and pending < 100) else "draining"
    return "current"  # headless/social cyclic refresh, fresh


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
    lines = [
        f"{head} <b>UnifiedCollector hourly status</b>",
        f"<i>{_now()} · current hour since {_current_hour_window()}</i>",
    ]

    hourly = snapshot.get("hourly_ingestion") or {}
    totals = hourly.get("totals") or {}
    if totals:
        rows = int(totals.get("records", 0) or 0)
        msgs = int(totals.get("messages", 0) or 0)
        files = int(totals.get("files", 0) or 0)
        r429 = int(totals.get("rate_limits", 0) or 0)
        access_errors = int(totals.get("access_errors", 0) or 0)
        lines.append("")
        lines.append("<b>Current hour</b>")
        lines.append(
            f"Stored {rows:,} {_plural(rows, 'source row')}, including "
            f"{msgs:,} {_plural(msgs, 'chat message')} and "
            f"{files:,} {_plural(files, 'media file')}."
        )
        if r429:
            lines.append(f"Recorded HTTP 429 events this hour: {r429:,}.")
        else:
            lines.append("Recorded HTTP 429 events this hour: 0.")
        if access_errors:
            lines.append(f"Recorded auth/access HTTP errors this hour: {access_errors:,}.")

        top = [_format_hourly_source(row) for row in (hourly.get("sources") or [])[:6]]
        if top:
            lines.append("")
            lines.append("<b>Top activity this hour</b>")
            lines.extend(top)

    active_limits = snapshot.get("active_rate_limits") or []
    recent_limits = snapshot.get("rate_limit_events") or []
    access_events = snapshot.get("access_events") or []
    lines.append("")
    lines.append("<b>Rate limits, cooldowns, and sessions</b>")
    if active_limits:
        lines.extend(_format_cooldown(r) for r in active_limits[:4])
    if recent_limits:
        lines.extend(_format_rate_limit_event(r) for r in recent_limits[:5])
    if access_events:
        lines.append("Session/auth HTTP failures this hour:")
        lines.extend(_format_access_event(r) for r in access_events[:5])
    if not active_limits and not recent_limits and not access_events:
        lines.append("No recorded HTTP 429s, active cooldowns, or auth/session failures this hour.")

    vault = snapshot.get("vault") or {}
    if vault:
        available = bool(vault.get("available"))
        writable = bool(vault.get("writable"))
        queued = int(vault.get("artifacts_queued") or 0)
        partial = int(vault.get("artifacts_partial") or 0)
        failures = int(vault.get("sidecar_failures") or 0)
        lines.append("")
        lines.append("<b>Vault</b>")
        if available and writable:
            lines.append(
                f"Writable at <code>{_esc(vault.get('root') or '')}</code>; "
                f"{_fmt_bytes(vault.get('free_bytes'))} free."
            )
        else:
            lines.append(
                f"Not safe for file-backed artifacts at <code>{_esc(vault.get('root') or '')}</code>"
                + (f": {_esc(vault.get('error'))}" if vault.get("error") else ".")
            )
        if queued or partial or failures:
            lines.append(
                f"Artifact health: {queued:,} sidecar DLQ rows, "
                f"{partial:,} media rows with failed sidecar metadata, "
                f"{failures:,} total sidecar failures recorded."
            )
        elif vault.get("counts_error"):
            lines.append(
                "Artifact health counts timed out; vault write check still passed. "
                f"Query error: <code>{_esc(vault.get('counts_error'))}</code>."
            )
        else:
            lines.append("Artifact health: no sidecar DLQ rows or media rows with failed sidecar metadata.")

    ages: dict = snapshot.get("source_ages") or {}
    stale = set(snapshot.get("stale_sources") or [])

    # Live feeds line: realtime platforms with their true last-activity age.
    live = []
    for s in _REALTIME:
        if s in ages:
            stale_note = " (stale)" if s in stale else ""
            live.append(f"{_display_source(s)} {_humanize_age(ages[s])} ago{stale_note}")
    if live:
        lines.append("")
        lines.append("<b>Realtime freshness</b>")
        lines.append("; ".join(live) + ".")

    # Headless coverage: how many are fresh, and name any that are stale.
    headless = [s for s in ages if s not in _REALTIME]
    if headless:
        fresh = sum(1 for s in headless if s not in stale)
        lines.append("")
        lines.append("<b>Browser and API freshness</b>")
        lines.append(f"{fresh}/{len(headless)} non-chat sources are fresh.")

    stale_headless = sorted(s for s in stale if s not in _REALTIME)
    if stale_headless:
        lines.append("Needs attention: " + ", ".join(
            f"{_display_source(s)} ({_humanize_age(ages[s])} ago)"
            for s in stale_headless if s in ages))

    # Backfill vs realtime, per collector. Classify every seen source, summarize
    # the phase counts, and name what's still draining / crawling.
    seen = sorted(
        set(snapshot.get("source_ages") or {})
        | set(snapshot.get("realtime_pct") or {})
        | set(snapshot.get("queue_pending") or {})
    )
    if seen:
        phases = {s: _backfill_phase(s, snapshot) for s in seen}
        n = lambda p: sum(1 for v in phases.values() if v == p)  # noqa: E731
        parts = []
        for label, key in (("realtime", "realtime"), ("draining", "draining"),
                            ("current", "current"), ("crawl", "crawl")):
            if n(key):
                parts.append(f"{n(key)} {label}")
        if parts:
            lines.append("")
            lines.append("<b>Backfill state</b>")
            lines.append(
                "Sources by phase: "
                + "; ".join(parts)
                + ". Realtime means caught up; draining means history is still being pulled; "
                + "crawl means a long-running discovery frontier."
            )

        qp = snapshot.get("queue_pending") or {}
        draining = [s for s in seen if phases[s] == "draining"]
        if draining:
            lines.append("Still draining: " + ", ".join(
                f"{_display_source(s)} ({_fmt_count(qp.get(s, 0))} queued)" if qp.get(s) else _display_source(s)
                for s in draining))
        crawl = [s for s in seen if phases[s] == "crawl"]
        if crawl:
            lines.append("Discovery crawl backlog: " + "; ".join(
                f"{_display_source(s)} {_fmt_count(qp.get(s, 0))}" for s in crawl))

    dead = snapshot.get("dead_sources") or []
    degraded = snapshot.get("degraded_sources") or []
    if dead:
        lines.append("")
        lines.append("Dead sources: " + ", ".join(_display_source(s) for s in dead))
    if degraded:
        lines.append("")
        lines.append("Degraded sources: " + ", ".join(_display_source(s) for s in degraded))

    return await telegram.send("\n".join(lines))
