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
    "comments": "comments",
    "feed": "feed fetches",
    "flood_wait": "FloodWait throttles",
    "gps_streams": "GPS route streams",
    "media": "media files",
    "posts": "posts",
    "profile_fetch": "profile fetches",
    "profile": "profiles",
    "strava_streams": "browser route captures",
    "strava_route_visit": "browser route visits",
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
        details.append(f"{rate_limits:,} rate-limit {_plural(rate_limits, 'event')}")
    if access_errors:
        details.append(
            f"{access_errors:,} login/access or other HTTP {_plural(access_errors, 'error')}"
        )
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
    raw_scope = str(row.get("scope") or "").strip().lower()
    event_label = "FloodWait" if raw_scope == "flood_wait" else "recorded rate-limit"
    subject = service
    if scope:
        subject += f" {scope}"
    if account:
        subject += f" for {_esc(account)}"
    if streak:
        return f"• {subject}: active cooldown for {remaining} after {streak} instrumented rate-limit {_plural(streak, 'event')}."
    if events:
        detail = f" ({_esc(reason)})" if reason else ""
        return f"• {subject}: active cooldown for {remaining} after {events:,} {event_label} {_plural(events, 'event')}{detail}."
    return f"• {subject}: active cooldown for {remaining}."


def _format_rate_limit_event(row: dict) -> str:
    source = _display_source(row.get("source", "?"))
    account = str(row.get("account") or "").strip()
    scope = _display_scope(row.get("scope"))
    status = int(row.get("status_code", 429) or 429)
    count = int(row.get("count", 0) or 0)
    raw_scope = str(row.get("scope") or "").strip().lower()
    status_text = "FloodWait" if raw_scope == "flood_wait" else f"HTTP {status}"

    subject = source
    if scope:
        subject += f" {scope}"
    if account:
        subject += f" for {_esc(account)}"
    events = f"{count:,} recorded event" if count == 1 else f"{count:,} recorded events"
    return f"• {subject}: {status_text}, {events} this hour."


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
    status_text = f"HTTP {int(status)}" if status is not None else "HTTP failure"
    events = f"{count:,} event" if count == 1 else f"{count:,} events"
    detail = f" ({_esc(reason)})" if reason else ""
    return f"• {subject}: {status_text}, {events} this hour{detail}."


def _format_quota_usage(row: dict) -> str:
    source = _display_source(row.get("platform", "?"))
    account = str(row.get("account") or "").strip()
    hour = int(row.get("requests_hour", 0) or 0)
    today = int(row.get("requests_today", 0) or 0)
    hourly_limit = row.get("hourly_limit")
    daily_limit = row.get("daily_limit")
    subject = source
    if account:
        subject += f" for {_esc(account)}"
    if hourly_limit:
        limit = int(hourly_limit)
        pct = (hour / limit * 100.0) if limit else 0.0
        return (
            f"• {subject}: {hour:,}/{limit:,} requests this hour "
            f"({pct:.0f}% of hourly budget); {today:,} today."
        )
    if daily_limit:
        limit = int(daily_limit)
        pct = (today / limit * 100.0) if limit else 0.0
        return (
            f"• {subject}: {hour:,} requests this hour; "
            f"{today:,}/{limit:,} today ({pct:.0f}% of daily budget)."
        )
    return f"• {subject}: {hour:,} requests this hour; {today:,} today."


def _format_extension_hook(row: dict) -> str:
    source = _display_source(row.get("platform", "?"))
    version = str(row.get("extension_version") or "").strip()
    expected = str(row.get("expected_extension_version") or "").strip()
    if version:
        version_text = _esc(version if version.lower().startswith("v") else f"v{version}")
    else:
        version_text = "version unknown"
    expected_text = _esc(expected if expected.lower().startswith("v") else f"v{expected}") if expected else ""
    age = _humanize_age(int(row.get("age_seconds", 0) or 0))
    owners = int(row.get("owner_count", 0) or 0)
    probes_hour = int(row.get("probes_current_hour", 0) or 0)
    samples_hour = int(row.get("samples_current_hour", 0) or 0)
    probes_sent = int(row.get("probes_sent", 0) or 0)
    samples_shipped = int(row.get("samples_shipped", 0) or 0)
    frame_age = row.get("last_frame_age_seconds")

    details = [
        f"hook {version_text} last heartbeat {age} ago",
        f"{owners:,} {_plural(owners, 'account')}" if owners else "no owner account recorded",
        f"this hour {probes_hour:,} probe {_plural(probes_hour, 'frame')} and "
        f"{samples_hour:,} sample {_plural(samples_hour, 'frame')}",
        f"session counters {probes_sent:,} probes / {samples_shipped:,} samples shipped",
    ]
    if frame_age is not None:
        details.append(f"last decoded frame {_humanize_age(int(frame_age or 0))} ago")
    if expected and version and version.lstrip("vV") != expected.lstrip("vV"):
        details.append(f"repo expects {expected_text}; reload the unpacked extension")
    return f"• {source}: " + "; ".join(details) + "."


def _format_browser_ingest_event(row: dict) -> str:
    source = _display_source(row.get("platform", "?"))
    endpoint = _display_scope(row.get("endpoint"))
    if not endpoint:
        endpoint = _esc(str(row.get("endpoint") or "browser ingest").replace("_", " "))
    requests = int(row.get("requests", 0) or 0)
    observed = int(row.get("observed_count", 0) or 0)
    stored = int(row.get("stored_count", 0) or 0)
    return (
        f"• {source} {endpoint}: browser saw {observed:,} "
        f"{_plural(observed, 'item')}; stored {stored:,}; "
        f"{requests:,} {_plural(requests, 'POST')} this hour."
    )


def _format_browser_content_gap(row: dict) -> str:
    source = _display_source(row.get("platform", "?"))
    heartbeat_age_raw = int(row.get("heartbeat_age_seconds", 0) or 0)
    heartbeat_age = _humanize_age(heartbeat_age_raw)
    content_age_raw = row.get("content_age_seconds")
    if content_age_raw is None:
        content_age = "never"
    else:
        content_age = _humanize_age(int(content_age_raw or 0)) + " ago"
    stale_after = _humanize_age(int(row.get("stale_after_seconds", 3600) or 3600))
    url = str(row.get("url") or "").strip()
    where = f" Current tab: <code>{_esc(url[:180])}</code>." if url else ""
    if heartbeat_age_raw > int(row.get("stale_after_seconds", 3600) or 3600):
        version = str(row.get("extension_version") or "").strip()
        version_text = f" running v{_esc(version)}." if version else "."
        return (
            f"• {source}: browser heartbeat is stale ({heartbeat_age} ago; expected within {stale_after}); "
            f"use Chrome's extension Reload button or reopen the browser bridge{version_text}"
            f"{where}"
        )
    return (
        f"• {source}: browser heartbeat is fresh ({heartbeat_age} ago), "
        f"but useful content ingest is {content_age}; expected within {stale_after}."
        f"{where}"
    )


def _format_tiktok_media_diagnostics(rows: list[dict], queue: dict | None = None) -> list[str]:
    if not rows and not queue:
        return []
    labels = {
        "stored": "stored as media",
        "duplicate": "already had the file",
        "tiny_thumbnail": "tiny thumbnail/avatar rejected",
        "short_lived_url": "short-lived video URL queued for browser revisit",
        "browser_fetch_failed": "browser fetch failed",
        "http_error": "server fetch HTTP error",
        "invalid_media": "invalid media/error page",
        "vault_unavailable": "vault unavailable",
    }
    parts = []
    revisit = 0
    for row in rows[:6]:
        count = int(row.get("candidates", 0) or 0)
        revisit += int(row.get("needs_revisit", 0) or 0)
        outcome = str(row.get("outcome") or "unknown")
        parts.append(f"{labels.get(outcome, outcome.replace('_', ' '))}: {count:,}")
    lines = []
    if parts:
        lines.append(
            "• TikTok media diagnosis this hour: "
            + "; ".join(parts)
            + (f". {revisit:,} {_plural(revisit, 'candidate')} need browser detail revisit." if revisit else ".")
        )
    if queue:
        due = int(queue.get("due", 0) or 0)
        claimed = int(queue.get("claimed", 0) or 0)
        stale_claimed = int(queue.get("stale_claimed", 0) or 0)
        pending = int(queue.get("pending", 0) or 0)
        failed = int(queue.get("failed", 0) or 0)
        unavailable = int(queue.get("unavailable", 0) or 0)
        completed = int(queue.get("completed", 0) or 0)
        stale_text = f", {stale_claimed:,} stale claimed ready to reclaim" if stale_claimed else ""
        lines.append(
            f"• TikTok detail revisit queue: {due:,} due now, "
            f"{claimed:,} claimed by browser{stale_text}, {pending:,} pending, "
            f"{failed:,} failed/retry, {unavailable:,} unavailable, "
            f"{completed:,} completed."
        )
    return lines


def _format_browser_media_diagnostics(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    labels = {
        "stored": "stored as media",
        "duplicate": "already had the file",
        "tiny_thumbnail": "tiny thumbnail/avatar rejected",
        "short_lived_url": "short-lived URL",
        "browser_fetch_failed": "browser fetch failed",
        "http_error": "server fetch HTTP error",
        "invalid_media": "invalid media/error page",
        "vault_unavailable": "vault unavailable",
        "browser_upload_failed": "browser upload failed",
    }
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        platform = str(row.get("platform") or "").strip().lower()
        if not platform or platform == "tiktok":
            continue
        grouped.setdefault(platform, []).append(row)
    lines = []
    for platform, platform_rows in sorted(grouped.items()):
        parts = []
        revisit = 0
        for row in platform_rows[:4]:
            count = int(row.get("candidates", 0) or 0)
            revisit += int(row.get("needs_revisit", 0) or 0)
            outcome = str(row.get("outcome") or "unknown")
            parts.append(f"{labels.get(outcome, outcome.replace('_', ' '))}: {count:,}")
        if parts:
            revisit_text = (
                f"; {revisit:,} {_plural(revisit, 'candidate')} need detail revisit"
                if revisit else ""
            )
            lines.append(f"• {_display_source(platform)}: " + "; ".join(parts) + revisit_text + ".")
    return lines


def _format_browser_media_revisit_queue(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    lines = []
    for row in rows[:6]:
        platform = str(row.get("platform") or "").strip().lower()
        if not platform:
            continue
        due = int(row.get("due", 0) or 0)
        claimed = int(row.get("claimed", 0) or 0)
        stale_claimed = int(row.get("stale_claimed", 0) or 0)
        pending = int(row.get("pending", 0) or 0)
        failed = int(row.get("failed", 0) or 0)
        unavailable = int(row.get("unavailable", 0) or 0)
        completed = int(row.get("completed", 0) or 0)
        stale_text = f", {stale_claimed:,} stale claimed ready to reclaim" if stale_claimed else ""
        lines.append(
            f"• {_display_source(platform)} detail revisit queue: {due:,} due now, "
            f"{claimed:,} claimed by browser{stale_text}, {pending:,} pending, "
            f"{failed:,} failed/retry, {unavailable:,} unavailable, {completed:,} completed."
        )
    return lines


def _format_x_collection_health(row: dict) -> list[str]:
    targets = int(row.get("targets", 0) or 0)
    due = int(row.get("due_targets", 0) or 0)
    claimed = int(row.get("claimed_targets", 0) or 0)
    failed = int(row.get("failed_targets", 0) or 0)
    unavailable = int(row.get("unavailable_targets", 0) or 0)
    edges = int(row.get("edge_count", 0) or 0)
    profiles = int(row.get("profiles_hour", 0) or 0)
    posts = int(row.get("posts_hour", 0) or 0)
    media = int(row.get("media_hour", 0) or 0)
    observed = int(row.get("browser_observed_hour", 0) or 0)
    stored = int(row.get("browser_stored_hour", 0) or 0)
    requests = int(row.get("browser_requests_hour", 0) or 0)
    age = row.get("last_profile_success_age_seconds")
    lines = [
        (
            f"Queue: {targets:,} profile {_plural(targets, 'target')}; "
            f"{due:,} due now, {claimed:,} claimed, {failed:,} failed, "
            f"{unavailable:,} unavailable."
        ),
        (
            f"This hour: {profiles:,} profile refreshes, {posts:,} posts, "
            f"{media:,} media files. Browser saw {observed:,} media "
            f"{_plural(observed, 'item')} and stored {stored:,} across "
            f"{requests:,} {_plural(requests, 'POST')}."
        ),
        f"Edges: {edges:,} mention/reply/quote/repost/seen-author records.",
    ]
    if age is not None:
        lines.append(f"Last successful queued profile/media pass {_humanize_age(int(age or 0))} ago.")
    return lines


def _format_degraded_source(row: dict) -> str:
    source = _display_source(row.get("source", "?"))
    status = str(row.get("status") or "degraded").replace("_", " ")
    reason = str(row.get("reason") or "").strip()
    age = row.get("age_seconds")
    stale_after = row.get("stale_after_seconds")

    details = [status]
    if age is not None:
        freshness = f"newest row {_humanize_age(int(age or 0))} ago"
        if stale_after is not None:
            freshness += f"; expected within {_humanize_age(int(stale_after or 0))}"
        details.append(freshness)
    if reason:
        details.append(_esc(reason))
    return f"• {source}: " + "; ".join(details) + "."


def _format_operational_event(row: dict) -> str:
    source = _display_source(row.get("source", "?"))
    event_type = str(row.get("event_type") or "event").replace("_", " ")
    severity = str(row.get("severity") or "info")
    summary = _esc(str(row.get("summary") or "").strip())
    age = row.get("age_seconds")
    age_text = f" {_humanize_age(int(age or 0))} ago" if age is not None else ""
    metadata = row.get("metadata") or {}
    hit_count = metadata.get("hit_count") if isinstance(metadata, dict) else None
    suffix = f"; {int(hit_count):,} fatal log events" if hit_count is not None else ""
    resolved = ""
    if row.get("resolved_by_success"):
        success_age = row.get("last_success_age_seconds")
        if success_age is not None:
            resolved = (
                f" Source has collected successfully since then; "
                f"last success {_humanize_age(int(success_age or 0))} ago."
            )
        else:
            resolved = " Source has collected successfully since then."
    return f"• {source}: {event_type} ({severity}){age_text}{suffix}. {summary}{resolved}"


def _format_operational_events(rows: list[dict]) -> list[str]:
    active = [row for row in rows if not row.get("resolved_by_success")]
    resolved = [row for row in rows if row.get("resolved_by_success")]
    lines = [_format_operational_event(row) for row in active[:5]]

    remaining_slots = max(0, 5 - len(lines))
    if remaining_slots and resolved:
        newest_age = min(
            int(row.get("age_seconds") or 0)
            for row in resolved
            if row.get("age_seconds") is not None
        ) if any(row.get("age_seconds") is not None for row in resolved) else None
        latest_success_age = min(
            int(row.get("last_success_age_seconds") or 0)
            for row in resolved
            if row.get("last_success_age_seconds") is not None
        ) if any(row.get("last_success_age_seconds") is not None for row in resolved) else None
        sources = ", ".join(sorted({_display_source(row.get("source", "?")) for row in resolved}))
        age_text = f"; newest {_humanize_age(newest_age)} ago" if newest_age is not None else ""
        success_text = (
            f"; latest successful collection {_humanize_age(latest_success_age)} ago"
            if latest_success_age is not None else ""
        )
        lines.append(
            f"• Resolved history: {len(resolved):,} older operational "
            f"{_plural(len(resolved), 'event')} for {sources} already recovered"
            f"{age_text}{success_text}. Kept for audit, not an active failure."
        )

    return lines


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


def _format_backup_status(backups: dict) -> str:
    status = str(backups.get("status") or "unknown")
    root = _esc(backups.get("root") or "")
    in_progress = bool(backups.get("in_progress"))
    progress = " Backup is currently writing a replacement dump." if in_progress else ""
    stale_temp_count = int(backups.get("stale_in_progress_count") or 0)
    stale_temp_age = backups.get("stale_in_progress_oldest_age_seconds")
    stale_temp = ""
    if stale_temp_count:
        age = _humanize_age(int(stale_temp_age or 0))
        stale_temp = (
            f" {stale_temp_count:,} abandoned temp {_plural(stale_temp_count, 'dump')} "
            f"older than the active-window threshold; oldest is {age} old."
        )

    if status == "refreshing" and not backups.get("latest_path"):
        return f"⏳ No completed collector DB backup dump found under <code>{root}</code> yet." + progress + stale_temp

    if status in {"ok", "stale", "refreshing"}:
        age = _humanize_age(int(backups.get("latest_age_seconds") or 0))
        size = _fmt_bytes(backups.get("latest_size_bytes"))
        count = int(backups.get("backup_count") or 0)
        max_age = backups.get("max_age_hours")
        base = (
            f"Latest collector DB backup is {age} old ({size}); "
            f"{count:,} {_plural(count, 'dump')} retained under <code>{root}</code>."
        )
        if status == "stale":
            expected = f" Expected within {max_age}h." if max_age else ""
            return "⚠️ " + base + expected + progress + stale_temp
        if status == "refreshing":
            expected = f" Previous completed dump is older than {max_age}h." if max_age else ""
            return "⏳ " + base + expected + progress + stale_temp
        return "✅ " + base + progress + stale_temp

    if status == "missing":
        return f"⚠️ No collector DB backup dump found under <code>{root}</code>." + progress + stale_temp

    if status == "error":
        err = _esc(backups.get("error") or "unknown error")
        return f"❌ Could not read collector DB backup status at <code>{root}</code>: <code>{err}</code>."

    return f"⚠️ Collector DB backup status is unknown at <code>{root}</code>." + progress + stale_temp


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
        lines.append("This is the partial clock-hour window; it resets at the top of each hour.")
        if r429:
            lines.append(f"Recorded rate-limit events this hour: {r429:,}.")
        else:
            lines.append("Recorded rate-limit events this hour: 0.")
        if access_errors:
            lines.append(f"Recorded login/access or other HTTP errors this hour: {access_errors:,}.")

        top = [_format_hourly_source(row) for row in (hourly.get("sources") or [])[:6]]
        if top:
            lines.append("")
            lines.append("<b>Top activity this hour</b>")
            lines.extend(top)

        previous = hourly.get("previous_complete_hour") or {}
        previous_totals = previous.get("totals") or {}
        if previous_totals:
            prev_rows = int(previous_totals.get("records", 0) or 0)
            prev_msgs = int(previous_totals.get("messages", 0) or 0)
            prev_files = int(previous_totals.get("files", 0) or 0)
            prev_429 = int(previous_totals.get("rate_limits", 0) or 0)
            prev_access = int(previous_totals.get("access_errors", 0) or 0)
            lines.append("")
            lines.append("<b>Previous complete hour</b>")
            lines.append(
                f"Stored {prev_rows:,} {_plural(prev_rows, 'source row')}, including "
                f"{prev_msgs:,} {_plural(prev_msgs, 'chat message')} and "
                f"{prev_files:,} {_plural(prev_files, 'media file')}; "
                f"{prev_429:,} rate-limit {_plural(prev_429, 'event')} and "
                f"{prev_access:,} login/access or other HTTP {_plural(prev_access, 'error')}."
            )
            previous_top = [_format_hourly_source(row) for row in (previous.get("sources") or [])[:4]]
            if previous_top:
                lines.extend(previous_top)

    active_limits = snapshot.get("active_rate_limits") or []
    recent_limits = snapshot.get("rate_limit_events") or []
    access_events = snapshot.get("access_events") or []
    quota_usage = snapshot.get("quota_usage") or []
    lines.append("")
    lines.append("<b>Rate limits, cooldowns, and sessions</b>")
    if active_limits:
        lines.extend(_format_cooldown(r) for r in active_limits[:4])
    if recent_limits:
        lines.extend(_format_rate_limit_event(r) for r in recent_limits[:5])
    if access_events:
        lines.append("Login/access or other HTTP errors this hour:")
        lines.extend(_format_access_event(r) for r in access_events[:5])
    if quota_usage:
        lines.append("Quota/request counters:")
        lines.extend(_format_quota_usage(r) for r in quota_usage[:6])
    if not active_limits and not recent_limits and not access_events and not quota_usage:
        lines.append("No recorded rate-limit events, active cooldowns, or login/session failures this hour.")

    operational_events = snapshot.get("operational_events") or []
    if operational_events:
        lines.append("")
        lines.append("<b>Recent self-heals and operational events</b>")
        lines.extend(_format_operational_events(operational_events))

    vault = snapshot.get("vault") or {}
    if vault:
        available = bool(vault.get("available"))
        writable = bool(vault.get("writable"))
        queued = int(vault.get("artifacts_queued") or 0)
        partial = int(vault.get("artifacts_partial") or 0)
        quarantined = int(vault.get("artifacts_quarantined") or 0)
        missing = int(vault.get("artifacts_missing_sidecar") or 0)
        recent_missing = int(vault.get("artifacts_missing_sidecar_recent_24h") or 0)
        missing_label = f"about {missing:,}" if vault.get("artifacts_missing_sidecar_estimated") else f"{missing:,}"
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
        if queued or partial or quarantined or missing or recent_missing or failures:
            lines.append(
                f"Artifact health: {queued:,} repair queue {_plural(queued, 'item')}, "
                f"{partial:,} active partial media {_plural(partial, 'record')}, "
                f"{quarantined:,} quarantined bad media {_plural(quarantined, 'record')}, "
                f"{recent_missing:,} media {_plural(recent_missing, 'record')} from the last 24h missing occurrence sidecars, "
                f"{missing_label} historical media {_plural(missing, 'record')} missing occurrence sidecars, "
                f"{failures:,} total sidecar write {_plural(failures, 'failure')} recorded."
            )
        elif vault.get("counts_error"):
            lines.append(
                "Artifact health counts partially timed out; vault write check still passed. "
                f"Query error: <code>{_esc(vault.get('counts_error'))}</code>."
            )
        else:
            lines.append(
                "Artifact health: no queued artifact repairs and no recent media records missing occurrence sidecars."
            )

    backups = snapshot.get("backups") or {}
    if backups:
        lines.append("")
        lines.append("<b>DB backups</b>")
        lines.append(_format_backup_status(backups))

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

    hooks = snapshot.get("extension_hooks") or []
    if hooks:
        lines.append("")
        lines.append("<b>Chrome extension hooks</b>")
        lines.extend(_format_extension_hook(row) for row in hooks[:4])

    browser_ingest = snapshot.get("browser_ingest_events") or []
    if browser_ingest:
        lines.append("")
        lines.append("<b>Browser extension ingest</b>")
        lines.extend(_format_browser_ingest_event(row) for row in browser_ingest[:6])

    browser_content_gaps = snapshot.get("browser_content_gaps") or []
    if browser_content_gaps:
        lines.append("")
        lines.append("<b>Browser content gaps</b>")
        lines.extend(_format_browser_content_gap(row) for row in browser_content_gaps[:5])

    browser_media_diagnostics = snapshot.get("browser_media_diagnostics") or []
    browser_media_lines = _format_browser_media_diagnostics(browser_media_diagnostics)
    if browser_media_lines:
        lines.append("")
        lines.append("<b>Browser media diagnosis</b>")
        lines.extend(browser_media_lines)

    browser_revisit_lines = _format_browser_media_revisit_queue(snapshot.get("browser_media_revisit_queue") or [])
    if browser_revisit_lines:
        lines.append("")
        lines.append("<b>Browser media detail follow-up</b>")
        lines.extend(browser_revisit_lines)

    tiktok_diagnostics = snapshot.get("tiktok_browser_media_diagnostics") or []
    tiktok_revisit = snapshot.get("tiktok_browser_revisit_queue") or {}
    if tiktok_diagnostics or tiktok_revisit:
        lines.append("")
        lines.append("<b>TikTok media follow-up</b>")
        lines.extend(_format_tiktok_media_diagnostics(tiktok_diagnostics, tiktok_revisit))

    x_health = snapshot.get("x_collection_health") or {}
    if x_health:
        lines.append("")
        lines.append("<b>Twitter / X profile and media queue</b>")
        lines.extend(_format_x_collection_health(x_health))

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
        details = snapshot.get("degraded_details") or []
        if details:
            lines.append("Why degraded:")
            lines.extend(_format_degraded_source(row) for row in details[:5])

    return await telegram.send("\n".join(lines))
