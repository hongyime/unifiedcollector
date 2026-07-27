"""Canonical per-source data-freshness definitions — single source of truth.

Real liveness = "did the newest row for this source arrive recently", read from the
actual data tables. This is what the watchdog and the scheduler heartbeat already
use, and what the dashboard's live pill should use — NOT service_cursors.status
(which flips to 'idle' between cycles for healthy long-sleep collectors and is
'never' for realtime feeds like beeper) nor collection_runs (which the realtime
collectors never populate). Both proxies make healthy collectors look down.

Each entry: (source, freshness_query -> seconds since newest row, stale_threshold_s).
Thresholds are generous per source: realtime feeds always have SOME activity inside
the window, and headless sources idle for long stretches by design.
"""
from __future__ import annotations

_DAY = 86400

SOURCE_MODES = {
    "telegram": "messaging",
    "whatsapp": "whatsapp bridge",
    "beeper": "messaging bridge",
    "instagram": "chrome extension + headless",
    "tiktok": "chrome extension + headless",
    "lemon8": "chrome extension + headless",
    "threads": "chrome extension",
    "facebook": "chrome extension",
    "x": "chrome extension",
    "youtube": "headless cookies",
    "website": "headless web crawl",
    "github": "api/headless",
    "strava": "api cookies + browser route capture",
    "search": "headless web search",
}

FRESHNESS: list[tuple[str, str, int]] = [
    ("telegram",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM telegram_messages", 7200),
    ("whatsapp",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM whatsapp_messages", 14400),
    ("beeper",    "SELECT extract(epoch FROM now()-max(ingested_at)) FROM beeper_shadow_messages", 10800),
    ("instagram", "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='instagram'", 2 * _DAY),
    ("tiktok",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='tiktok'", 2 * _DAY),
    ("lemon8",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM media_items WHERE source='lemon8'", 2 * _DAY),
    ("threads",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM threads_posts", 2 * _DAY),
    ("facebook",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM facebook_posts", 2 * _DAY),
    ("x",         "SELECT extract(epoch FROM now()-max(collected_at)) FROM x_posts", 2 * _DAY),
    ("youtube",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM youtube_videos", 2 * _DAY),
    ("website",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM website_pages", 3 * _DAY),
    ("github",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM github_commits", 3 * _DAY),
    ("strava",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM strava_activities", 3 * _DAY),
    ("search",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM search_results", 3 * _DAY),
]
FRESHNESS_BY_SOURCE = {name: (query, threshold) for name, query, threshold in FRESHNESS}
FRESHNESS_BASIS = {
    "telegram": "telegram_messages.collected_at",
    "whatsapp": "whatsapp_messages.collected_at",
    "beeper": "beeper_shadow_messages.ingested_at",
    "instagram": "media_items.collected_at where source=instagram",
    "tiktok": "media_items.collected_at where source=tiktok",
    "lemon8": "media_items.collected_at where source=lemon8",
    "threads": "threads_posts.collected_at",
    "facebook": "facebook_posts.collected_at",
    "x": "x_posts.collected_at",
    "youtube": "youtube_videos.collected_at",
    "website": "website_pages.collected_at",
    "github": "github_commits.collected_at",
    "strava": "strava_activities.collected_at",
    "search": "search_results.collected_at",
}

# Realtime messaging feeds (surfaced distinctly by some consumers).
REALTIME = ("telegram", "whatsapp", "beeper")


async def compute_liveness(conn) -> list[dict]:
    """Return per-source liveness using an open asyncpg connection.

    Each item: {source, status, age_seconds}. status is one of:
      live      – newest row within threshold (and not degraded/dead)
      stale     – newest row older than threshold
      degraded  – source_health says degraded / auth_paused (creds/backoff)
      dead      – source_health says dead (crash-looped out)
      unknown   – no data yet / query failed
    """
    health: dict[str, dict] = {}
    try:
        for r in await conn.fetch("SELECT source, status, last_error FROM source_health"):
            health[r["source"]] = {"status": r["status"], "last_error": r["last_error"]}
    except Exception:
        pass

    out: list[dict] = []
    for name, query, thresh in FRESHNESS:
        try:
            age = await conn.fetchval(query, timeout=8)
        except Exception:
            age = None
        h = health.get(name) or {}
        hs = h.get("status")
        h_error = h.get("last_error")
        if hs == "dead":
            status = "dead"
            detail = h_error or "source_health reports the source as dead"
        elif age is None:
            status = "unknown"
            detail = "no freshness row could be read"
        elif age > thresh:
            status = "stale"
            detail = f"newest row is older than {thresh} seconds"
        elif hs in ("degraded", "auth_paused"):
            status = "degraded"
            detail = h_error or f"source_health reports {hs}"
        else:
            status = "live"
            detail = "newest row is inside the freshness window"
        out.append({
            "source": name,
            "status": status,
            "age_seconds": int(age) if age is not None else None,
            "stale_after_seconds": thresh,
            "collection_mode": SOURCE_MODES.get(name, "unknown"),
            "freshness_basis": FRESHNESS_BASIS.get(name),
            "source_health_status": hs,
            "source_health_error": h_error,
            "detail": detail,
        })
    return out
