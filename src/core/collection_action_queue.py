from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any


ACTION_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS collection_action_queue (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    action_type TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    priority INTEGER NOT NULL DEFAULT 5,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_by TEXT NOT NULL DEFAULT 'source_matrix',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_action_queue_open_key
    ON collection_action_queue (source, action_type, scope_key)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_collection_action_queue_status_priority
    ON collection_action_queue (status, priority, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_collection_action_queue_source_status
    ON collection_action_queue (source, status, last_seen_at DESC);
"""


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except TypeError:
        return json.loads(json.dumps(value, default=str))


def _scope_key(scope: dict[str, Any] | None) -> str:
    if not scope:
        return ""
    body = json.dumps(_jsonable(scope), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _useful_count(window: dict[str, Any] | None) -> int:
    if not isinstance(window, dict):
        return 0
    candidates = [
        window.get("stored"),
        window.get("stored_count"),
        window.get("media"),
        window.get("media_items"),
        window.get("media_count"),
        window.get("records"),
        window.get("records_count"),
        window.get("messages"),
        window.get("messages_count"),
    ]
    values = []
    for item in candidates:
        try:
            values.append(int(item or 0))
        except (TypeError, ValueError):
            continue
    return max(values or [0])


def _media_output_count(window: dict[str, Any] | None) -> int:
    if not isinstance(window, dict):
        return 0
    candidates = [
        window.get("stored"),
        window.get("stored_count"),
        window.get("media"),
        window.get("media_items"),
        window.get("media_count"),
    ]
    values = []
    for item in candidates:
        try:
            values.append(int(item or 0))
        except (TypeError, ValueError):
            continue
    return max(values or [0])


def _rolling_output_count(row: dict[str, Any], *, media_only: bool) -> int:
    candidates = [
        row.get("rolling_60m"),
        row.get("last_60m"),
        row.get("rolling_hour"),
    ]
    inline = {
        "stored": row.get("stored_rolling_60m"),
        "stored_count": row.get("stored_rolling_60m"),
        "media_items": row.get("stored_rolling_60m"),
        "observed": row.get("observed_rolling_60m"),
        "observed_count": row.get("observed_rolling_60m"),
        "records": row.get("records_rolling_60m"),
        "messages": row.get("messages_rolling_60m"),
    }
    candidates.append(inline)
    counter = _media_output_count if media_only else _useful_count
    return max(counter(item) for item in candidates if isinstance(item, dict))


def _yield_action_sources() -> set[str]:
    return {
        item.strip().lower()
        for item in os.getenv(
            "COLLECTION_ACTION_YIELD_SOURCES",
            (
                "facebook,instagram,lemon8,threads,tiktok,website,x"
            ),
        ).split(",")
        if item.strip()
    }


def _media_floor_sources() -> set[str]:
    return {
        item.strip().lower()
        for item in os.getenv(
            "COLLECTION_ACTION_MEDIA_FLOOR_SOURCES",
            "facebook,instagram,lemon8,threads,tiktok,x",
        ).split(",")
        if item.strip()
    }


def _slow_yield_window_sources() -> set[str]:
    return {
        item.strip().lower()
        for item in os.getenv(
            "COLLECTION_ACTION_SLOW_YIELD_SOURCES",
            "website",
        ).split(",")
        if item.strip()
    }


def _window_pressure(window: dict[str, Any] | None) -> int:
    if not isinstance(window, dict):
        return 0
    total = 0
    for key in ("rate_limits", "rate_limits_current_hour", "access_errors", "access_errors_current_hour"):
        try:
            total += int(window.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _active_rate_pressure(row: dict[str, Any], current: dict[str, Any], last_complete: dict[str, Any]) -> bool:
    rate_limit = row.get("rate_limit") if isinstance(row.get("rate_limit"), dict) else {}
    active_until = _parse_datetime(rate_limit.get("active_until"))
    if active_until is not None and active_until <= datetime.now(timezone.utc):
        return _window_pressure(current) > 0
    if rate_limit.get("active_now") is True:
        return True
    if rate_limit.get("active_now") is False:
        return _window_pressure(current) > 0
    return bool(_window_pressure(current) or _window_pressure(last_complete))


def _quiet_informational_row(row: dict[str, Any]) -> bool:
    blocker = row.get("blocker") or {}
    if not isinstance(blocker, dict):
        return False
    severity = str(blocker.get("severity") or "").lower()
    kind = str(blocker.get("kind") or "").lower()
    summary = str(blocker.get("summary") or "").lower()
    next_action = str(blocker.get("next_action") or "").lower()
    if severity == "ok" and kind in {"none", "quiet_beeper_subsource"} and "no action" in next_action:
        return True
    if severity == "ok" and kind == "stats_unavailable":
        return True
    timeout_text = " ".join([kind, summary, next_action, str(row.get("detail") or "").lower()])
    return "source liveness query timed out" in timeout_text and "known source skeleton" in timeout_text


def _window_stats_unavailable(*windows: dict[str, Any] | None) -> bool:
    for window in windows:
        if not isinstance(window, dict):
            continue
        if window.get("stats_unavailable") or window.get("media_stats_unavailable"):
            return True
    return False


def _window_has_yield_stats(window: dict[str, Any] | None) -> bool:
    if not isinstance(window, dict) or not window:
        return False
    keys = {
        "stored",
        "stored_count",
        "media",
        "media_items",
        "media_count",
        "records",
        "records_count",
        "messages",
        "messages_count",
    }
    return any(key in window for key in keys)


def _has_recent_output(row: dict[str, Any]) -> bool:
    for key in ("current_hour", "last_complete_hour", "last_24h"):
        window = row.get(key)
        if not isinstance(window, dict):
            continue
        if window.get("liveness_floor"):
            return True
        if _useful_count(window) > 0:
            return True
    return False


def _browser_ingest_currently_healthy(browser_extension: dict[str, Any]) -> bool:
    issues = browser_extension.get("issues") or []
    hard_issues = [
        issue
        for issue in issues
        if isinstance(issue, dict)
        and str(issue.get("severity") or "error").lower() not in {"ok", "info", "warning"}
    ]
    if hard_issues:
        return False
    ingest_health = browser_extension.get("ingest_health") or browser_extension.get("browser_ingest") or {}
    if not isinstance(ingest_health, dict):
        return False
    if not bool(ingest_health.get("active")):
        return False
    if ingest_health.get("content_active") is False:
        return False
    age = ingest_health.get("last_content_age_seconds", ingest_health.get("last_seen_age_seconds"))
    fresh_after = int(ingest_health.get("fresh_after_seconds") or 600)
    try:
        return float(age) <= fresh_after
    except (TypeError, ValueError):
        return True


def _fresh_extension_content_for_source(browser_extension: dict[str, Any], source: str) -> bool:
    ingest_rows = browser_extension.get("ingest") or []
    for item in ingest_rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("platform") or "").strip().lower() != source:
            continue
        endpoint = str(item.get("endpoint") or "").strip().lower()
        if endpoint in {"", "browser_heartbeat"}:
            continue
        try:
            age = float(item.get("age_seconds"))
        except (TypeError, ValueError):
            continue
        if age > float(item.get("fresh_after_seconds") or 600):
            continue
        if _useful_count(item) > 0:
            return True
    return False


def _covered_warning_blocker(
    row: dict[str, Any],
    blocker: dict[str, Any],
    browser_extension: dict[str, Any] | None = None,
) -> bool:
    severity = str(blocker.get("severity") or "").lower()
    kind = str(blocker.get("kind") or "").lower()
    status = str(row.get("status") or "").lower()
    if severity != "warning" or status != "live":
        return False
    if kind == "browser_page_error" and _has_recent_output(row):
        return True
    if kind == "browser_capture_stalled" and _fresh_extension_content_for_source(
        browser_extension or {},
        str(row.get("source") or "").strip().lower(),
    ):
        return True
    return False


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cooldown_until_from_text(text: str) -> datetime | None:
    marker = " until "
    lowered = text.lower()
    idx = lowered.rfind(marker)
    if idx < 0:
        return None
    candidate = text[idx + len(marker):].strip().rstrip(".")
    return _parse_datetime(candidate)


def _cooldown_expired(row: dict[str, Any], blocker: dict[str, Any]) -> bool:
    kind = str(blocker.get("kind") or "").lower()
    combined = " ".join(
        str(blocker.get(key) or "")
        for key in ("summary", "reason", "detail", "message", "next_action")
    )
    if kind != "cooldown" and "cooldown" not in combined.lower():
        return False
    rate_limit = row.get("rate_limit") if isinstance(row.get("rate_limit"), dict) else {}
    until = _parse_datetime(rate_limit.get("active_until")) or _cooldown_until_from_text(combined)
    if until is None:
        return False
    return until <= datetime.now(timezone.utc)


def _blocker_action(source: str, blocker: dict[str, Any]) -> dict[str, Any] | None:
    if not blocker:
        return None
    severity = str(blocker.get("severity") or "").lower()
    if severity in {"", "ok", "info"}:
        return None
    text = " ".join(
        str(blocker.get(key) or "")
        for key in ("kind", "reason", "detail", "message", "status")
    ).lower()
    if (
        "auth" in text
        or "login" in text
        or "manual" in text
        or "challenge" in text
        or "pair" in text
        or "qr" in text
        or "unpaired" in text
    ):
        action_type = "manual_auth_needed"
        priority = 1
    elif "rate" in text or "429" in text or "quota" in text or "cooldown" in text:
        action_type = "source_blocked"
        priority = 3
    else:
        action_type = "source_blocked"
        priority = 2 if severity == "error" else 4
    return {
        "source": source,
        "action_type": action_type,
        "scope": {"blocker_kind": blocker.get("kind") or blocker.get("status") or "blocker"},
        "priority": priority,
        "reason": str(
            blocker.get("reason")
            or blocker.get("detail")
            or blocker.get("message")
            or blocker.get("summary")
            or blocker.get("next_action")
            or "source blocker"
        ),
        "evidence": {"blocker": _jsonable(blocker)},
    }


def derive_collection_actions(
    source_matrix: dict[str, Any],
    *,
    min_useful_per_hour: int | None = None,
) -> list[dict[str, Any]]:
    """Derive operator actions from source-matrix/browser health evidence."""
    if min_useful_per_hour is None:
        try:
            min_useful_per_hour = max(1, int(os.getenv("COLLECTION_ACTION_MIN_USEFUL_PER_HOUR", "5")))
        except (TypeError, ValueError):
            min_useful_per_hour = 5
    actions: list[dict[str, Any]] = []
    browser_extension = source_matrix.get("browser_extension") or {}
    for row in source_matrix.get("sources") or []:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "").strip().lower()
        if not source:
            continue
        if _quiet_informational_row(row):
            continue
        blocker = row.get("blocker") or {}
        if _cooldown_expired(row, blocker):
            blocker = {}
        if _covered_warning_blocker(row, blocker, browser_extension):
            continue
        blocker_action = _blocker_action(source, blocker)
        if blocker_action:
            actions.append(blocker_action)
            continue
        status = str(row.get("status") or "").lower()
        if status in {"degraded", "stale", "dead", "unknown", "unreachable"}:
            actions.append({
                "source": source,
                "action_type": "source_blocked",
                "scope": {"status": status},
                "priority": 3 if status in {"degraded", "unknown"} else 2,
                "reason": str(row.get("detail") or f"source status is {status}"),
                "evidence": {"source_row": _jsonable(row)},
            })
            continue
        current = row.get("current_hour") or {}
        last_complete = row.get("last_complete_hour") or {}
        if _window_stats_unavailable(current, last_complete):
            continue
        if not _window_has_yield_stats(current) or not _window_has_yield_stats(last_complete):
            continue
        if source in _yield_action_sources() and _active_rate_pressure(row, current, last_complete):
            actions.append({
                "source": source,
                "action_type": "source_blocked",
                "scope": {"window": "current_or_last_complete_hour", "pressure": "rate_or_access"},
                "priority": 3,
                "reason": "recent rate-limit or access pressure",
                "evidence": {
                    "current_hour": _jsonable(current),
                    "last_complete_hour": _jsonable(last_complete),
                },
            })
            continue
        if source not in _yield_action_sources():
            continue
        current = row.get("current_hour") or {}
        last_complete = row.get("last_complete_hour") or {}
        if source in _media_floor_sources():
            current_useful = _media_output_count(current)
            last_useful = _media_output_count(last_complete)
            rolling_useful = _rolling_output_count(row, media_only=True)
            floor_label = "media-output"
        else:
            current_useful = _useful_count(current)
            last_useful = _useful_count(last_complete)
            rolling_useful = _rolling_output_count(row, media_only=False)
            floor_label = "useful-output"
        if rolling_useful >= min_useful_per_hour:
            continue
        if source in _slow_yield_window_sources():
            day_useful = _useful_count(row.get("last_24h") or {})
            if day_useful >= min_useful_per_hour:
                continue
        if current_useful < min_useful_per_hour and last_useful < min_useful_per_hour:
            actions.append({
                "source": source,
                "action_type": "target_starved",
                "scope": {"window": "current_and_last_complete_hour"},
                "priority": 5,
                "reason": f"below {min_useful_per_hour}/hour {floor_label} floor",
                "evidence": {
                    "current_hour": _jsonable(current),
                    "last_complete_hour": _jsonable(last_complete),
                    "threshold": min_useful_per_hour,
                    "floor": floor_label,
                },
            })
    maintenance = browser_extension.get("maintenance") or {}
    maintenance_state = str(maintenance.get("state") or "").lower()
    maintenance_needs_repair = maintenance_state in {
        "degraded",
        "failed",
        "cdp_unavailable",
        "overlap_skipped",
    } or bool(maintenance.get("running_stalled"))
    if (
        maintenance_needs_repair
        and maintenance_state in {"degraded", "overlap_skipped"}
        and not bool(maintenance.get("running_stalled"))
        and _browser_ingest_currently_healthy(browser_extension)
    ):
        maintenance_needs_repair = False
    if maintenance_needs_repair:
        actions.append({
            "source": "browser_extension",
            "action_type": "repair_browser",
            "scope": {"kind": "browser_maintenance", "state": maintenance_state or "unknown"},
            "priority": 2,
            "reason": str(maintenance.get("detail") or f"browser maintenance state is {maintenance_state}"),
            "evidence": {"maintenance": _jsonable(maintenance)},
        })
    for issue in browser_extension.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        severity = str(issue.get("severity") or "error").lower()
        if severity in {"ok", "info", "warning"}:
            continue
        actions.append({
            "source": str(issue.get("source") or "browser_extension"),
            "action_type": "repair_browser",
            "scope": {"kind": issue.get("kind") or "browser_extension_issue"},
            "priority": 1,
            "reason": str(issue.get("message") or issue.get("detail") or "browser extension issue"),
            "evidence": {"issue": _jsonable(issue), "maintenance": _jsonable(browser_extension.get("maintenance"))},
        })
    return actions


async def ensure_collection_action_queue(conn) -> None:
    await conn.execute(ACTION_QUEUE_DDL)


async def sync_collection_action_queue(
    conn,
    source_matrix: dict[str, Any],
    *,
    generated_by: str = "source_matrix",
) -> dict[str, Any]:
    """Upsert current actions and resolve stale auto-generated ones."""
    await ensure_collection_action_queue(conn)
    actions = derive_collection_actions(source_matrix)
    active_keys = []
    for action in actions:
        scope = action.get("scope") or {}
        scope_key = _scope_key(scope)
        active_keys.append((action["source"], action["action_type"], scope_key))
        await conn.execute(
            """
            INSERT INTO collection_action_queue (
                source, action_type, scope_key, status, priority, reason,
                evidence, generated_by, first_seen_at, last_seen_at, updated_at
            )
            VALUES ($1, $2, $3, 'open', $4, $5, $6::jsonb, $7, NOW(), NOW(), NOW())
            ON CONFLICT (source, action_type, scope_key) WHERE status = 'open'
            DO UPDATE SET
                priority = EXCLUDED.priority,
                reason = EXCLUDED.reason,
                evidence = EXCLUDED.evidence,
                last_seen_at = NOW(),
                updated_at = NOW()
            """,
            action["source"],
            action["action_type"],
            scope_key,
            int(action.get("priority") or 5),
            str(action.get("reason") or action["action_type"]),
            json.dumps(_jsonable(action.get("evidence") or {}), default=str),
            generated_by,
        )
    if active_keys:
        sources = [item[0] for item in active_keys]
        action_types = [item[1] for item in active_keys]
        scope_keys = [item[2] for item in active_keys]
        resolved = await conn.fetchval(
            """
            WITH active AS (
                SELECT *
                FROM unnest($1::text[], $2::text[], $3::text[])
                     AS t(source, action_type, scope_key)
            ),
            updated AS (
                UPDATE collection_action_queue q
                   SET status = 'resolved',
                       resolved_at = NOW(),
                       updated_at = NOW()
                 WHERE q.status = 'open'
                   AND q.generated_by = $4
                   AND NOT EXISTS (
                       SELECT 1
                       FROM active a
                       WHERE a.source = q.source
                         AND a.action_type = q.action_type
                         AND a.scope_key = q.scope_key
                   )
                 RETURNING 1
            )
            SELECT count(*)::int FROM updated
            """,
            sources,
            action_types,
            scope_keys,
            generated_by,
        )
    else:
        resolved = await conn.fetchval(
            """
            WITH updated AS (
                UPDATE collection_action_queue
                   SET status = 'resolved',
                       resolved_at = NOW(),
                       updated_at = NOW()
                 WHERE status = 'open'
                   AND generated_by = $1
                 RETURNING 1
            )
            SELECT count(*)::int FROM updated
            """,
            generated_by,
        )
    rows = await conn.fetch(
        """
        SELECT source, action_type, scope_key, status, priority, reason,
               evidence, first_seen_at, last_seen_at, resolved_at
        FROM collection_action_queue
        WHERE status = 'open'
        ORDER BY priority ASC, last_seen_at DESC
        LIMIT 50
        """
    )
    return {
        "derived": len(actions),
        "open": len(rows),
        "resolved": int(resolved or 0),
        "actions": [dict(row) for row in rows],
    }


async def resolve_stale_actions_from_direct_health(
    conn,
    *,
    browser_extension: dict[str, Any] | None = None,
) -> int:
    """Resolve stale open actions using direct, low-cost health proof.

    This is narrower than a normal action-queue sync. It is used when the full
    source matrix is unavailable, so it only closes actions that current direct
    evidence clearly contradicts.
    """
    await ensure_collection_action_queue(conn)
    resolved = 0
    maintenance = (browser_extension or {}).get("maintenance") or {}
    maintenance_state = str(maintenance.get("state") or "").lower()
    maintenance_detail = str(maintenance.get("detail") or "").lower()
    if (
        maintenance
        and maintenance_state not in {"failed", "cdp_unavailable"}
        and "timed out" not in maintenance_detail
        and not bool(maintenance.get("running_stalled"))
    ):
        result = await conn.execute(
            """
            UPDATE collection_action_queue
            SET status = 'resolved',
                resolved_at = NOW(),
                updated_at = NOW(),
                evidence = evidence || jsonb_build_object(
                    'resolved_reason', 'direct maintenance evidence superseded stale timeout action',
                    'resolved_by', 'direct_health',
                    'current_maintenance', $1::jsonb
                )
            WHERE status = 'open'
              AND source = 'browser_extension'
              AND action_type = 'repair_browser'
              AND reason ILIKE 'maintenance pass timed out%'
            """,
            json.dumps(_jsonable(maintenance), default=str),
        )
        try:
            resolved += int(result.split()[-1])
        except (IndexError, ValueError):
            pass
    result = await conn.execute(
        """
        UPDATE collection_action_queue q
        SET status = 'resolved',
            resolved_at = NOW(),
            updated_at = NOW(),
            evidence = evidence || jsonb_build_object(
                'resolved_reason', 'source_health returned to running after stale browser-capture action',
                'resolved_by', 'direct_health'
            )
        WHERE q.status = 'open'
          AND q.action_type = 'source_blocked'
          AND q.evidence->'blocker'->>'kind' = 'browser_capture_stalled'
          AND EXISTS (
              SELECT 1
              FROM source_health sh
              WHERE sh.source = q.source
                AND sh.status = 'running'
                AND sh.updated_at > q.last_seen_at
          )
        """
    )
    try:
        resolved += int(result.split()[-1])
    except (IndexError, ValueError):
        pass
    return resolved
