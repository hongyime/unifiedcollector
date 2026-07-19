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
    health: dict[str, str] = {}
    try:
        for r in await conn.fetch("SELECT source, status FROM source_health"):
            health[r["source"]] = r["status"]
    except Exception:
        pass

    out: list[dict] = []
    for name, query, thresh in FRESHNESS:
        try:
            age = await conn.fetchval(query, timeout=8)
        except Exception:
            age = None
        hs = health.get(name)
        if hs == "dead":
            status = "dead"
        elif age is None:
            status = "unknown"
        elif age > thresh:
            status = "stale"
        elif hs in ("degraded", "auth_paused"):
            status = "degraded"
        else:
            status = "live"
        out.append({
            "source": name,
            "status": status,
            "age_seconds": int(age) if age is not None else None,
            "stale_after_seconds": thresh,
        })
    return out
