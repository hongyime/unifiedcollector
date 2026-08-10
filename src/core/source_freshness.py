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

import os
import time
from datetime import datetime, timezone

_DAY = 86400
_BROWSER_HEARTBEAT_SOURCES = ("instagram", "tiktok", "lemon8", "threads", "facebook", "x", "strava")
_BROWSER_CONTENT_PROGRESS_SOURCES = ("instagram", "tiktok", "lemon8", "threads", "facebook", "x")
_BROWSER_REQUIRED_SOURCES = ("threads", "facebook", "x")
_BROWSER_HYBRID_SOURCES = ("instagram", "tiktok", "lemon8")

STRAVA_PROGRESS_QUERY = """
SELECT extract(epoch FROM now()-max(ts))
FROM (
    SELECT max(updated_at) AS ts FROM strava_athletes
    UNION ALL
    SELECT max(collected_at) AS ts FROM strava_activities
    UNION ALL
    SELECT max(collected_at) AS ts FROM strava_gps_streams
    UNION ALL
    SELECT max(collected_at) AS ts FROM media_items WHERE source='strava'
) progress
"""

GITHUB_PROGRESS_QUERY = """
SELECT extract(epoch FROM now()-max(ts))
FROM (
    SELECT max(collected_at) AS ts FROM github_users
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_repos
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_commits
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_issues
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_issue_comments
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_pr_reviews
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_pr_review_comments
    UNION ALL
    SELECT max(collected_at) AS ts FROM github_edges
) progress
"""

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
    (
        "instagram",
        """
        SELECT extract(epoch FROM now()-max(ts))
        FROM (
            SELECT max(updated_at) AS ts FROM instagram_profiles
            UNION ALL
            SELECT max(collected_at) AS ts FROM media_items WHERE source='instagram'
        ) progress
        """,
        2 * _DAY,
    ),
    (
        "tiktok",
        """
        SELECT extract(epoch FROM now()-max(ts))
        FROM (
            SELECT max(updated_at) AS ts FROM tiktok_profiles
            UNION ALL
            SELECT max(collected_at) AS ts FROM media_items WHERE source='tiktok'
        ) progress
        """,
        2 * _DAY,
    ),
    (
        "lemon8",
        """
        SELECT extract(epoch FROM now()-max(ts))
        FROM (
            SELECT max(updated_at) AS ts FROM lemon8_profiles
            UNION ALL
            SELECT max(collected_at) AS ts FROM media_items WHERE source='lemon8'
        ) progress
        """,
        2 * _DAY,
    ),
    ("threads",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM threads_posts", 2 * _DAY),
    ("facebook",  "SELECT extract(epoch FROM now()-max(collected_at)) FROM facebook_posts", 2 * _DAY),
    (
        "x",
        """
        SELECT extract(epoch FROM now()-max(ts))
        FROM (
            SELECT max(updated_at) AS ts FROM x_profiles
            UNION ALL
            SELECT max(collected_at) AS ts FROM x_posts
            UNION ALL
            SELECT max(collected_at) AS ts FROM media_items WHERE source='x'
        ) progress
        """,
        2 * _DAY,
    ),
    ("youtube",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM youtube_videos", 2 * _DAY),
    ("website",   "SELECT extract(epoch FROM now()-max(collected_at)) FROM website_pages", 3 * _DAY),
    ("github",    GITHUB_PROGRESS_QUERY, 3 * _DAY),
    ("strava",    STRAVA_PROGRESS_QUERY, 3 * _DAY),
    ("search",    "SELECT extract(epoch FROM now()-max(collected_at)) FROM search_results", 3 * _DAY),
]
FRESHNESS_BY_SOURCE = {name: (query, threshold) for name, query, threshold in FRESHNESS}
FRESHNESS_BASIS = {
    "telegram": "telegram_messages.collected_at",
    "whatsapp": "whatsapp_messages.collected_at",
    "beeper": "beeper_shadow_messages.ingested_at",
    "instagram": "newest Instagram profile update or media row",
    "tiktok": "newest TikTok profile update or media row",
    "lemon8": "newest Lemon8 profile update or media row",
    "threads": "threads_posts.collected_at",
    "facebook": "facebook_posts.collected_at",
    "x": "newest X profile update, post row, or media row",
    "youtube": "youtube_videos.collected_at",
    "website": "website_pages.collected_at",
    "github": "newest GitHub profile, repo, commit, issue, PR review, comment, or edge row",
    "strava": "newest Strava athlete profile, activity, GPS stream, or media row",
    "search": "search_results.collected_at",
}

# Realtime messaging feeds (surfaced distinctly by some consumers).
REALTIME = ("telegram", "whatsapp", "beeper")


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    return value


def _stale_watchdog_marker(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return lowered.startswith("stale ") and "watchdog" in lowered


def _watchdog_marker(error: str | None) -> bool:
    if _stale_watchdog_marker(error):
        return True
    if not error:
        return False
    lowered = error.lower()
    return lowered.startswith("browser capture stalled:") and (
        "watchdog" in lowered or "browser media yield warning:" in lowered
    )


def _age_seconds(value, now: datetime) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0.0, (now - value.astimezone(timezone.utc)).total_seconds())


async def _latest_browser_heartbeats(conn, timeout: float) -> dict[str, dict] | None:
    """Return newest Chrome-extension heartbeat per browser-assisted platform."""
    query_timeout = min(3.0, max(0.75, timeout))
    try:
        rows = await conn.fetch(
            """
            WITH wanted(platform) AS (
                SELECT unnest($1::text[])
            )
            SELECT wanted.platform,
                   latest.created_at AS last_seen_at,
                   extract(epoch FROM now() - latest.created_at)::int AS age_seconds,
                   latest.metadata->>'extension_version' AS extension_version,
                   latest.metadata->>'url' AS url,
                   latest.metadata->>'health_status' AS health_status,
                   latest.metadata->>'health_reason' AS health_reason
            FROM wanted
            LEFT JOIN LATERAL (
                SELECT created_at, metadata
                FROM browser_ingest_events
                WHERE endpoint = 'browser_heartbeat'
                  AND platform = wanted.platform
                ORDER BY created_at DESC
                LIMIT 1
            ) latest ON true
            """,
            list(_BROWSER_HEARTBEAT_SOURCES),
            timeout=query_timeout,
        )
        return {
            str(row["platform"]): dict(row)
            for row in rows
            if row["last_seen_at"] is not None
        }
    except Exception as exc:
        if exc.__class__.__name__ == "UndefinedTableError":
            return None
        return None


async def _latest_browser_content_progress(conn, timeout: float) -> dict[str, dict] | None:
    """Return newest useful browser-ingest progress per browser-driven platform.

    Browser-assisted collectors can make real progress without immediately writing
    a source table row: for example a TikTok/X page can report an empty scrape
    probe or a candidate queue event. Those events are still the browser proving
    it ran the content cycle. Login/error-shell recovery probes are deliberately
    excluded: they prove the loop is awake, but not that useful collection is
    flowing.
    """
    query_timeout = min(3.0, max(0.75, timeout))
    try:
        rows = await conn.fetch(
            """
            WITH wanted(platform) AS (
                SELECT unnest($1::text[])
            )
            SELECT wanted.platform,
                   latest.created_at AS last_content_at,
                   extract(epoch FROM now() - latest.created_at)::int AS age_seconds,
                   latest.endpoint,
                   latest.observed_count,
                   latest.stored_count,
                   latest.metadata->>'probe_reason' AS probe_reason
            FROM wanted
            LEFT JOIN LATERAL (
                SELECT created_at, endpoint, observed_count, stored_count, metadata
                FROM browser_ingest_events
                WHERE endpoint <> 'browser_heartbeat'
                  AND platform = wanted.platform
                  AND (
                    observed_count > 0
                    OR stored_count > 0
                    OR (
                      metadata ? 'probe_reason'
                      AND COALESCE(metadata->>'probe_reason', '')
                          NOT IN (
                            'manual_backend_probe',
                            'forced_recovery_started',
                            'recoverable_error_shell'
                          )
                      )
                  )
                ORDER BY created_at DESC
                LIMIT 1
            ) latest ON true
            """,
            list(_BROWSER_CONTENT_PROGRESS_SOURCES),
            timeout=query_timeout,
        )
        return {
            str(row["platform"]): dict(row)
            for row in rows
            if row["last_content_at"] is not None
        }
    except Exception as exc:
        if exc.__class__.__name__ == "UndefinedTableError":
            return None
        return None


async def _recent_browser_media_yield(conn, timeout: float) -> dict[str, dict] | None:
    """Return recent browser media candidate yield per platform.

    Content progress alone can be misleading: a tab may keep reporting media
    candidates while every storage attempt is duplicate, blocked, or failing.
    Keep this query bounded to browser_ingest_events so source liveness can flag
    "awake but storing nothing" without scanning media_items.
    """
    query_timeout = min(3.0, max(0.75, timeout))
    window_seconds = _env_int("BROWSER_MEDIA_ZERO_STORE_WINDOW_SECONDS", 3600, min_value=300)
    try:
        rows = await conn.fetch(
            """
            WITH wanted(platform) AS (
                SELECT unnest($1::text[])
            )
            SELECT wanted.platform,
                   COALESCE(sum(e.observed_count), 0)::int AS observed_count,
                   COALESCE(sum(e.stored_count), 0)::int AS stored_count,
                   COALESCE(sum(
                       CASE
                           WHEN (e.metadata #>> '{reject_stats,duplicate_content_id}') ~ '^[0-9]+$'
                           THEN (e.metadata #>> '{reject_stats,duplicate_content_id}')::int
                           ELSE 0
                       END
                       +
                       CASE
                           WHEN (e.metadata #>> '{reject_stats,duplicate_sha256}') ~ '^[0-9]+$'
                           THEN (e.metadata #>> '{reject_stats,duplicate_sha256}')::int
                           ELSE 0
                       END
                   ), 0)::int AS duplicate_count,
                   max(e.created_at) AS last_media_at
            FROM wanted
            LEFT JOIN browser_ingest_events e
              ON e.platform = wanted.platform
             AND e.endpoint = 'media'
             AND e.created_at >= now() - ($2::int * interval '1 second')
             AND e.observed_count > 0
            GROUP BY wanted.platform
            """,
            list(_BROWSER_CONTENT_PROGRESS_SOURCES),
            window_seconds,
            timeout=query_timeout,
        )
        return {
            str(row["platform"]): dict(row)
            for row in rows
            if int(row["observed_count"] or 0) > 0
        }
    except Exception as exc:
        if exc.__class__.__name__ == "UndefinedTableError":
            return None
        return None


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
        for r in await conn.fetch(
            "SELECT source, status, last_error, last_success_at, updated_at FROM source_health"
        ):
            row = dict(r)
            health[row["source"]] = {
                "status": row["status"],
                "last_error": row["last_error"],
                "last_success_at": row.get("last_success_at"),
                "updated_at": row.get("updated_at"),
            }
    except Exception:
        pass

    try:
        total_budget = max(2.0, float(os.getenv("SOURCE_LIVENESS_TOTAL_BUDGET_SECONDS", "12")))
    except (TypeError, ValueError):
        total_budget = 12.0
    try:
        per_query_timeout = max(0.25, float(os.getenv("SOURCE_LIVENESS_QUERY_TIMEOUT_SECONDS", "1.5")))
    except (TypeError, ValueError):
        per_query_timeout = 1.5
    deadline = time.monotonic() + total_budget
    now_utc = datetime.now(timezone.utc)
    browser_heartbeat_timeout = min(per_query_timeout, max(0.25, deadline - time.monotonic()))
    browser_heartbeats = await _latest_browser_heartbeats(conn, browser_heartbeat_timeout)
    browser_content_timeout = min(per_query_timeout, max(0.25, deadline - time.monotonic()))
    browser_content_progress = await _latest_browser_content_progress(conn, browser_content_timeout)
    browser_media_timeout = min(per_query_timeout, max(0.25, deadline - time.monotonic()))
    browser_media_yield = await _recent_browser_media_yield(conn, browser_media_timeout)
    browser_stale_after = _env_int(
        "BROWSER_HEARTBEAT_STALE_WARN_SECONDS",
        _env_int("BROWSER_CONTENT_STALE_WARN_SECONDS", 3600, min_value=300),
        min_value=300,
    )
    browser_content_stale_after = _env_int("BROWSER_CONTENT_STALE_WARN_SECONDS", 3600, min_value=300)
    browser_media_zero_store_min_observed = _env_int(
        "BROWSER_MEDIA_ZERO_STORE_MIN_OBSERVED",
        100,
        min_value=1,
    )
    out: list[dict] = []
    for name, query, thresh in FRESHNESS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            age = None
        else:
            try:
                age = await conn.fetchval(query, timeout=min(per_query_timeout, remaining))
            except Exception:
                age = None
        data_age = age
        h = health.get(name) or {}
        hs = h.get("status")
        h_error = h.get("last_error")
        health_age = _age_seconds(h.get("last_success_at"), now_utc)
        if hs == "dead":
            status = "dead"
            detail = h_error or "source_health reports the source as dead"
        elif age is None:
            if (
                health_age is not None
                and health_age <= thresh
                and hs not in {"auth_paused", "dead"}
                and not (hs == "degraded" and not _watchdog_marker(h_error))
            ):
                age = health_age
                status = "live"
                detail = "freshness query timed out; source_health heartbeat is fresh"
            else:
                status = "unknown"
                detail = "no freshness row could be read"
        elif age > thresh:
            status = "stale"
            detail = f"newest row is older than {thresh} seconds"
        elif hs == "auth_paused":
            status = "degraded"
            detail = h_error or "source_health reports auth_paused"
        elif hs == "degraded" and not _watchdog_marker(h_error):
            status = "degraded"
            detail = h_error or "source_health reports degraded"
        else:
            status = "live"
            if hs == "degraded" and _watchdog_marker(h_error):
                detail = "newest row is inside the freshness window; watchdog marker ignored"
            else:
                detail = "newest row is inside the freshness window"
        browser_heartbeat = browser_heartbeats.get(name) if browser_heartbeats is not None else None
        browser_content = (
            browser_content_progress.get(name)
            if browser_content_progress is not None
            else None
        )
        browser_content_age = (
            int(browser_content["age_seconds"])
            if browser_content and browser_content.get("age_seconds") is not None
            else None
        )
        media_yield = (
            browser_media_yield.get(name)
            if browser_media_yield is not None
            else None
        )
        media_observed_count = int(media_yield.get("observed_count") or 0) if media_yield else 0
        media_stored_count = int(media_yield.get("stored_count") or 0) if media_yield else 0
        media_duplicate_count = int(media_yield.get("duplicate_count") or 0) if media_yield else 0
        media_unresolved_count = max(
            0,
            media_observed_count - media_stored_count - media_duplicate_count,
        )
        browser_media_zero_store = (
            name in _BROWSER_CONTENT_PROGRESS_SOURCES
            and media_unresolved_count >= browser_media_zero_store_min_observed
            and media_stored_count == 0
        )
        if (
            name in _BROWSER_CONTENT_PROGRESS_SOURCES
            and browser_content_age is not None
            and (age is None or browser_content_age < age)
        ):
            age = browser_content_age
            detail = "fresh browser content/probe event is inside the freshness window"
        browser_age = (
            int(browser_heartbeat["age_seconds"])
            if browser_heartbeat and browser_heartbeat.get("age_seconds") is not None
            else None
        )
        browser_stale = browser_heartbeats is not None and name in _BROWSER_HEARTBEAT_SOURCES and (
            browser_age is None or browser_age > browser_stale_after
        )
        if browser_stale:
            browser_detail = (
                "Chrome extension heartbeat is missing"
                if browser_age is None
                else f"Chrome extension heartbeat is {browser_age}s old (> {browser_stale_after}s)"
            )
            if status == "live":
                if name in _BROWSER_REQUIRED_SOURCES:
                    status = "degraded"
                    detail = browser_detail
                else:
                    detail = f"{detail}; browser capture warning: {browser_detail}"
            else:
                detail = f"{detail}; {browser_detail}"
        browser_content_stale = (
            name in _BROWSER_CONTENT_PROGRESS_SOURCES
            and (
                browser_content_age > browser_content_stale_after
                if browser_content_age is not None
                else (data_age is None or data_age > browser_content_stale_after)
            )
        )
        if browser_content_stale:
            stale_age = browser_content_age if browser_content_age is not None else data_age
            content_detail = (
                "browser content progress is missing"
                if stale_age is None
                else f"browser content progress is {int(stale_age)}s old (> {browser_content_stale_after}s)"
            )
            if status == "live" and name in _BROWSER_HYBRID_SOURCES:
                detail = f"{detail}; browser capture warning: {content_detail}"
            elif status == "live":
                status = "degraded"
                detail = content_detail
            else:
                detail = f"{detail}; {content_detail}"
        if browser_media_zero_store:
            yield_detail = (
                f"browser media yield warning: {media_unresolved_count} unresolved media candidate(s) "
                f"out of {media_observed_count} observed, {media_duplicate_count} duplicate/already archived, "
                "stored 0 in the recent window"
            )
            duplicate_heavy_yield = media_duplicate_count > 0 and media_duplicate_count >= media_unresolved_count
            if status == "live" and duplicate_heavy_yield:
                detail = f"{detail}; {yield_detail}"
            elif status == "live":
                status = "degraded"
                detail = yield_detail
            else:
                detail = f"{detail}; {yield_detail}"
        out.append({
            "source": name,
            "status": status,
            "age_seconds": int(age) if age is not None else None,
            "stale_after_seconds": thresh,
            "collection_mode": SOURCE_MODES.get(name, "unknown"),
            "freshness_basis": FRESHNESS_BASIS.get(name),
            "source_health_status": hs,
            "source_health_error": h_error,
            "source_health_last_success_at": h.get("last_success_at"),
            "source_health_updated_at": h.get("updated_at"),
            "browser_heartbeat_at": browser_heartbeat.get("last_seen_at") if browser_heartbeat else None,
            "browser_heartbeat_age_seconds": browser_age,
            "browser_heartbeat_stale_after_seconds": (
                browser_stale_after if name in _BROWSER_HEARTBEAT_SOURCES else None
            ),
            "browser_content_stale_after_seconds": (
                browser_content_stale_after if name in _BROWSER_CONTENT_PROGRESS_SOURCES else None
            ),
            "browser_content_stale": browser_content_stale,
            "browser_extension_version": (
                browser_heartbeat.get("extension_version") if browser_heartbeat else None
            ),
            "browser_url": (
                browser_heartbeat.get("url") if browser_heartbeat else None
            ),
            "browser_health_status": (
                browser_heartbeat.get("health_status") if browser_heartbeat else None
            ),
            "browser_health_reason": (
                browser_heartbeat.get("health_reason") if browser_heartbeat else None
            ),
            "browser_content_at": (
                browser_content.get("last_content_at") if browser_content else None
            ),
            "browser_content_age_seconds": browser_content_age,
            "browser_content_endpoint": (
                browser_content.get("endpoint") if browser_content else None
            ),
            "browser_content_probe_reason": (
                browser_content.get("probe_reason") if browser_content else None
            ),
            "browser_media_observed_count": media_observed_count,
            "browser_media_stored_count": media_stored_count,
            "browser_media_duplicate_count": media_duplicate_count,
            "browser_media_unresolved_count": media_unresolved_count,
            "browser_media_zero_store": browser_media_zero_store,
            "detail": detail,
        })
    return out
