"""Module-level caches, TTL config, and small pure helpers shared across dashboard/api submodules.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 package split
(step 2 of ``docs/plans/perf-file-splits.md`` sub-plan 4A). All symbols remain
private (underscore-prefixed); ``__init__.py`` re-exports them for back-compat
so ``from src.dashboard.api import _foo`` and internal call sites keep working.

Not moved: ``_SOURCE_MATRIX_PAYLOAD_BUILD_TASK`` (mutated via ``global`` in
callers — moving would break rebind semantics).

Step 5 (browser.py) additions: DB pool acquire/release helpers and
``_dt_for_compare`` were promoted to this leaf module so domain-specific
submodules (browser, source_matrix, telegram_ops, ...) can depend on helpers
without a circular ``src.dashboard.api`` import.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caches and TTL / timeout config
# ---------------------------------------------------------------------------

_MESSAGING_COVERAGE_CACHE: dict[str, object] = {"ts": 0.0, "rows": None}
_SOURCE_MEDIA_TOTALS_CACHE: dict[str, object] = {"ts": 0.0, "rows": None}
_SOURCE_MEDIA_TOTALS_TTL_SECONDS = int(os.getenv("SOURCE_MEDIA_TOTALS_TTL_SECONDS", "300"))
_BEEPER_SUBSOURCE_CONTENT_CACHE: dict[tuple[str, str | None], dict[str, object]] = {}
_BEEPER_SUBSOURCE_CONTENT_TTL_SECONDS = int(os.getenv("BEEPER_SUBSOURCE_CONTENT_TTL_SECONDS", "45"))
_BEEPER_SUBSOURCE_QUERY_TIMEOUT_SECONDS = float(os.getenv("BEEPER_SUBSOURCE_QUERY_TIMEOUT_SECONDS", "4"))
_BEEPER_SUBSOURCE_TOTAL_TIMEOUT_SECONDS = float(os.getenv("BEEPER_SUBSOURCE_TOTAL_TIMEOUT_SECONDS", "1.5"))
_BEEPER_SUBSOURCE_MEDIA_TOTALS_CACHE: dict[str, object] = {"ts": 0.0, "rows": None, "failed": False}
_BEEPER_SUBSOURCE_MEDIA_TOTALS_TTL_SECONDS = int(os.getenv("BEEPER_SUBSOURCE_MEDIA_TOTALS_TTL_SECONDS", "300"))
_BEEPER_SUBSOURCE_MEDIA_TOTALS_FAILURE_TTL_SECONDS = int(os.getenv("BEEPER_SUBSOURCE_MEDIA_TOTALS_FAILURE_TTL_SECONDS", "60"))
_BEEPER_SUBSOURCE_LIVENESS_CACHE: dict[str, object] = {"ts": 0.0, "rows": None}
_BEEPER_SUBSOURCE_LIVENESS_TTL_SECONDS = int(os.getenv("BEEPER_SUBSOURCE_LIVENESS_TTL_SECONDS", "75"))
_BEEPER_SUBSOURCE_STALE_SECONDS = int(os.getenv("BEEPER_SUBSOURCE_STALE_SECONDS", str(24 * 3600)))
_BEEPER_SUBSOURCE_PREFIX = "beeper_"
_SOURCE_MATRIX_SECTION_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_SECTION_TIMEOUT_SECONDS", "2"))
_SOURCE_MATRIX_LIVENESS_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_LIVENESS_TIMEOUT_SECONDS", "20"))
_SOURCE_MATRIX_DAY_CONTENT_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_DAY_CONTENT_TIMEOUT_SECONDS", "3"))
_SOURCE_MATRIX_MEDIA_TOTALS_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_MEDIA_TOTALS_TIMEOUT_SECONDS", "3"))
_SOURCE_MATRIX_YOUTUBE_BACKLOG_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_YOUTUBE_BACKLOG_TIMEOUT_SECONDS", "2"))
_SOURCE_MATRIX_ENABLE_YOUTUBE_BACKLOG = os.getenv("SOURCE_MATRIX_ENABLE_YOUTUBE_BACKLOG", "0").lower() in {
    "1", "true", "yes", "on"
}
_SOURCE_MATRIX_BROWSER_EXTENSION_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_BROWSER_EXTENSION_TIMEOUT_SECONDS", "10"))
_SOURCE_MATRIX_SECTION_CACHE: dict[str, dict[str, object]] = {}
_SOURCE_MATRIX_SECTION_CACHE_TTL_SECONDS = int(os.getenv("SOURCE_MATRIX_SECTION_CACHE_TTL_SECONDS", "30"))
_SOURCE_MATRIX_SECTION_STALE_SECONDS = int(os.getenv("SOURCE_MATRIX_SECTION_STALE_SECONDS", "900"))
_SOURCE_MATRIX_PAYLOAD_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS = float(os.getenv("SOURCE_MATRIX_PAYLOAD_CACHE_TTL_SECONDS", "10"))
_SOURCE_MATRIX_PAYLOAD_STALE_SECONDS = float(os.getenv("SOURCE_MATRIX_PAYLOAD_STALE_SECONDS", "300"))
_SOURCE_MATRIX_PAYLOAD_BUILD_TIMEOUT_SECONDS = float(os.getenv("SOURCE_MATRIX_PAYLOAD_BUILD_TIMEOUT_SECONDS", "15"))
_SOURCE_MATRIX_PAYLOAD_CACHE_PATH = os.getenv(
    "SOURCE_MATRIX_PAYLOAD_CACHE_PATH",
    "/app/tmp/source_matrix_payload_cache.json",
)
_COLLECTORS_LIVE_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_COLLECTORS_LIVE_CACHE_TTL_SECONDS = int(os.getenv("COLLECTORS_LIVE_CACHE_TTL_SECONDS", "15"))
_COLLECTORS_LIVE_STALE_SECONDS = int(os.getenv("COLLECTORS_LIVE_STALE_SECONDS", "300"))
_TELEGRAM_STATS_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_TELEGRAM_STATS_TTL_SECONDS = int(os.getenv("TELEGRAM_STATS_TTL_SECONDS", "30"))
_YOUTUBE_MEDIA_BACKLOG_CACHE: dict[str, object] = {"ts": 0.0, "row": None}
_YOUTUBE_MEDIA_BACKLOG_TTL_SECONDS = int(os.getenv("YOUTUBE_MEDIA_BACKLOG_TTL_SECONDS", "600"))
_YOUTUBE_COMPLETENESS_CACHE: dict[str, object] = {"ts": 0.0, "payload": None}
_YOUTUBE_COMPLETENESS_TTL_SECONDS = int(os.getenv("YOUTUBE_COMPLETENESS_TTL_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def _tiktok_revisit_claim_timeout_seconds() -> int:
    try:
        return max(60, int(os.getenv("TIKTOK_BROWSER_REVISIT_CLAIM_TIMEOUT_SECONDS", "1800")))
    except (TypeError, ValueError):
        return 1800


def _encode_polyline(points, precision: int = 5) -> str:
    """Encode GPS points into a compact map thumbnail polyline."""
    if not points:
        return ""
    max_points = 600
    if len(points) > max_points:
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points)]
    factor = 10 ** precision

    def _enc(v: int) -> str:
        v = v << 1 if v >= 0 else ~(v << 1)
        out = []
        while v >= 0x20:
            out.append(chr((0x20 | (v & 0x1F)) + 63))
            v >>= 5
        out.append(chr(v + 63))
        return "".join(out)

    prev_lat = prev_lng = 0
    result = []
    for pt in points:
        if not pt or len(pt) != 2:
            continue
        lat_i = int(round(float(pt[0]) * factor))
        lng_i = int(round(float(pt[1]) * factor))
        result.append(_enc(lat_i - prev_lat))
        result.append(_enc(lng_i - prev_lng))
        prev_lat, prev_lng = lat_i, lng_i
    return "".join(result)


def _jsonb_points(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    return value if isinstance(value, list) else []


def _row_get(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def _iso_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_row(row, keys: list[str]) -> dict | None:
    if not row:
        return None
    out = {}
    for key in keys:
        value = _row_get(row, key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, _uuid.UUID):
            out[key] = str(value)
        else:
            out[key] = value
    return out


def _strava_route_status(row: dict) -> dict[str, str | None]:
    """Explain why a Strava activity does or does not have a renderable route."""
    if row.get("summary_polyline"):
        return {"route_status": "mapped", "route_status_detail": "GPS route available"}

    stream_status = row.get("stream_status")
    if stream_status == "truncated_empty":
        return {
            "route_status": "privacy_zone",
            "route_status_detail": "Strava returned an empty GPS stream, usually because the activity route is hidden.",
        }
    if stream_status == "incomplete":
        return {
            "route_status": "no_gps",
            "route_status_detail": "Strava returned the activity without GPS stream points.",
        }
    if stream_status == "ok_unverifiable":
        return {
            "route_status": "unverifiable",
            "route_status_detail": "Older activity could not be rechecked, so the collector will not keep retrying it.",
        }

    cooldown_until = row.get("gps_rate_limit_until")
    if isinstance(cooldown_until, datetime):
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        if cooldown_until > datetime.now(timezone.utc):
            reason = row.get("gps_rate_limit_reason") or "GPS stream fetch hit HTTP 429"
            return {
                "route_status": "rate_limited",
                "route_status_detail": f"{reason}; retry after {cooldown_until.isoformat()}",
            }

    if row.get("start_latlng"):
        return {
            "route_status": "start_only",
            "route_status_detail": "Collector has a start coordinate, but no route line yet.",
        }
    if row.get("gps_rate_limit_at"):
        return {
            "route_status": "recent_429",
            "route_status_detail": row.get("gps_rate_limit_reason") or "Recent GPS stream request hit HTTP 429.",
        }
    return {
        "route_status": "queued",
        "route_status_detail": "GPS stream has not reached a definitive result yet and remains eligible for backfill.",
    }


async def _estimated_table_rows(conn, table: str) -> int:
    value = await conn.fetchval(
        "SELECT GREATEST(0, reltuples)::bigint FROM pg_class WHERE oid = $1::regclass",
        table,
    )
    return int(value or 0)


# ---------------------------------------------------------------------------
# Shared DB-pool acquire/release helpers.
#
# Promoted from ``__init__.py`` during PERF-002 4A step 5 (browser.py split).
# Domain submodules import these instead of reaching back into
# ``src.dashboard.api``. ``__init__.py`` re-exports them so existing routes and
# tests keep working.
# ---------------------------------------------------------------------------

_DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS = float(os.getenv("DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS", "2.5"))


async def _acquire_dashboard_conn(pool):
    return await asyncio.wait_for(pool.acquire(), timeout=_DASHBOARD_DB_ACQUIRE_TIMEOUT_SECONDS)


async def _release_dashboard_conn(pool, conn, label: str = "dashboard") -> None:
    try:
        await asyncio.shield(pool.release(conn))
    except asyncio.CancelledError:
        logger.warning("%s DB release cancelled; continuing with degraded response", label)
    except Exception as exc:  # noqa: BLE001 - release failures should not 500 dashboards
        logger.warning("%s DB release failed: %s", label, exc.__class__.__name__)


def _dt_for_compare(value) -> datetime | None:
    """Normalize a datetime-like value to timezone-aware UTC, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None
