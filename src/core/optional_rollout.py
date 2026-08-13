"""Guarded rollout monitor for optional collector features.

Optional paths such as SpiderFoot recon, Lemon8-heavy passes, and browser-heavy
social collection should advance slowly and stop automatically when source
health degrades. This module keeps that policy read-only by default and uses
``collector_seen_targets`` as the candidate source of truth.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

STAGE_CAPS = {
    "dry-run": 0,
    "five": 5,
    "daily25": 25,
}

FEATURE_ALIASES = {
    "recon": "spiderfoot",
    "spiderfoot": "spiderfoot",
    "lemon8": "lemon8",
    "browser-heavy": "browser-heavy",
    "browser_heavy": "browser-heavy",
}

FEATURE_SOURCES = {
    "spiderfoot": (
        "spiderfoot",
        "recon",
        "website",
        "search",
        "github",
        "youtube",
        "telegram",
        "whatsapp",
        "beeper",
        "instagram",
        "threads",
        "tiktok",
        "lemon8",
        "x",
        "facebook",
    ),
    "lemon8": ("lemon8",),
    "browser-heavy": ("instagram", "threads", "tiktok", "lemon8", "x", "facebook"),
}

FEATURE_TARGET_TYPES = {
    "spiderfoot": ("domain", "url", "email", "username", "user"),
    "lemon8": ("user", "username", "url"),
    "browser-heavy": ("user", "username", "url"),
}

STOP_TEXT_MARKERS = (
    "malformed json",
    "jsondecode",
    "invalid json",
    "timed out",
    "timeout",
    "loop",
)

STOP_STATUSES = {"dead", "degraded", "auth_paused"}
STOP_SEVERITIES = {"error", "critical"}
RATE_STOP_CODES = {403, 429}


@dataclass(frozen=True)
class RolloutRequest:
    feature: str = "spiderfoot"
    stage: str = "dry-run"
    window_hours: int = 24
    limit: int | None = None

    @property
    def normalized_feature(self) -> str:
        feature = FEATURE_ALIASES.get(str(self.feature or "").strip().lower())
        if feature is None:
            raise ValueError(f"unsupported optional feature: {self.feature}")
        return feature

    @property
    def normalized_stage(self) -> str:
        stage = str(self.stage or "").strip().lower()
        if stage not in STAGE_CAPS:
            raise ValueError(f"unsupported rollout stage: {self.stage}")
        return stage

    @property
    def target_cap(self) -> int:
        if self.limit is not None:
            return max(0, min(int(self.limit), 500))
        return STAGE_CAPS[self.normalized_stage]


async def _table_exists(conn, table: str) -> bool:
    try:
        return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))
    except Exception:
        return False


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


def _target_host(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname
    if host:
        return host.lower().strip(".")
    if "@" in raw:
        return raw.rsplit("@", 1)[-1].lower().strip(".")
    return None


def _safe_candidate(row: Any) -> dict[str, Any]:
    target = str(_row_get(row, "target_key") or "")
    target_type = str(_row_get(row, "target_type") or "").lower()
    safe = {
        "source": str(_row_get(row, "source") or _row_get(row, "platform") or "").lower(),
        "target_type": target_type,
        "target_hash": hashlib.sha256(target.encode("utf-8")).hexdigest()[:12] if target else None,
        "status": str(_row_get(row, "status") or ""),
        "priority": int(_row_get(row, "priority", 0) or 0),
        "evidence_count": int(_row_get(row, "evidence_count", 0) or 0),
        "last_seen_at": _iso(_row_get(row, "last_seen_at")),
        "origin": str(_row_get(row, "origin") or ""),
    }
    if target_type in {"domain", "url", "email"}:
        safe["target_host"] = _target_host(target)
    return safe


def _text_has_stop_marker(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values).lower()
    return any(marker in text for marker in STOP_TEXT_MARKERS)


def _stop_reason(kind: str, row: Any, reason: str) -> dict[str, Any]:
    metadata = _json_dict(_row_get(row, "metadata"))
    return {
        "kind": kind,
        "source": str(_row_get(row, "source") or "").lower(),
        "reason": reason,
        "status": _row_get(row, "status"),
        "severity": _row_get(row, "severity"),
        "event_type": _row_get(row, "event_type"),
        "status_code": _row_get(row, "status_code"),
        "cooldown_seconds": _row_get(row, "cooldown_seconds"),
        "summary": str(_row_get(row, "summary") or _row_get(row, "last_error") or _row_get(row, "reason") or "")[:180],
        "metadata_keys": sorted(str(key) for key in metadata.keys())[:12],
        "created_at": _iso(_row_get(row, "created_at") or _row_get(row, "updated_at")),
    }


async def _fetch_source_health_stops(conn, sources: tuple[str, ...]) -> list[dict[str, Any]]:
    if not await _table_exists(conn, "source_health"):
        return []
    rows = await conn.fetch(
        """
        SELECT source, status, last_error, updated_at
        FROM source_health
        WHERE source = ANY($1::text[])
          AND status = ANY($2::text[])
        ORDER BY updated_at DESC NULLS LAST
        LIMIT 30
        """,
        list(sources),
        sorted(STOP_STATUSES),
        timeout=3,
    )
    return [
        _stop_reason("source_health", row, f"source_health status {str(_row_get(row, 'status') or '').lower()}")
        for row in rows
    ]


async def _fetch_operational_stops(
    conn,
    sources: tuple[str, ...],
    *,
    window_hours: int,
) -> list[dict[str, Any]]:
    if not await _table_exists(conn, "collector_operational_events"):
        return []
    rows = await conn.fetch(
        """
        SELECT source, event_type, severity, summary, metadata, created_at
        FROM collector_operational_events
        WHERE created_at >= NOW() - ($1::int * INTERVAL '1 hour')
          AND source = ANY($2::text[])
        ORDER BY created_at DESC
        LIMIT 150
        """,
        max(1, int(window_hours)),
        list(sources),
        timeout=3,
    )
    stops: list[dict[str, Any]] = []
    for row in rows:
        severity = str(_row_get(row, "severity") or "").lower()
        summary = _row_get(row, "summary")
        metadata = _json_dict(_row_get(row, "metadata"))
        if severity in STOP_SEVERITIES or _text_has_stop_marker(summary, metadata):
            reason = f"operational {severity}" if severity in STOP_SEVERITIES else "operational stop marker"
            stops.append(_stop_reason("collector_operational_events", row, reason))
    return stops


async def _fetch_rate_limit_stops(
    conn,
    sources: tuple[str, ...],
    *,
    window_hours: int,
) -> list[dict[str, Any]]:
    if not await _table_exists(conn, "rate_limit_events"):
        return []
    rows = await conn.fetch(
        """
        SELECT source, status_code, reason, cooldown_seconds, metadata, created_at
        FROM rate_limit_events
        WHERE created_at >= NOW() - ($1::int * INTERVAL '1 hour')
          AND source = ANY($2::text[])
          AND (
              status_code = ANY($3::int[])
              OR COALESCE(cooldown_seconds, 0) > 0
          )
        ORDER BY created_at DESC
        LIMIT 100
        """,
        max(1, int(window_hours)),
        list(sources),
        sorted(RATE_STOP_CODES),
        timeout=3,
    )
    return [_stop_reason("rate_limit_events", row, "recent cooldown/rate-limit") for row in rows]


async def _fetch_seen_candidates(
    conn,
    *,
    feature: str,
    limit: int,
) -> list[Any]:
    if limit <= 0 or not await _table_exists(conn, "collector_seen_targets"):
        return []
    sources = [source for source in FEATURE_SOURCES[feature] if source not in {"spiderfoot", "recon"}]
    source_filter = sources if feature != "spiderfoot" else []
    rows = await conn.fetch(
        """
        SELECT platform AS source, target_type, target_key, target_display,
               origin, priority, evidence_count, first_seen_at, last_seen_at,
               last_backfill_at, next_backfill_at, status, source_table,
               source_record_id, metadata
        FROM collector_seen_targets
        WHERE target_type = ANY($1::text[])
          AND status = ANY($2::text[])
          AND ($3::text[] = '{}'::text[] OR platform = ANY($3::text[]))
        ORDER BY
          CASE WHEN status IN ('new', 'seen', 'pending') THEN 0 ELSE 1 END,
          priority DESC,
          evidence_count DESC,
          last_seen_at DESC NULLS LAST
        LIMIT $4
        """,
        list(FEATURE_TARGET_TYPES[feature]),
        ["new", "seen", "pending", "stale"],
        source_filter,
        max(1, min(limit, 500)),
        timeout=5,
    )
    return list(rows)


def _feature_policy(feature: str, stage: str, target_cap: int) -> dict[str, Any]:
    if feature == "spiderfoot":
        modules = [item.strip() for item in os.getenv(
            "SPIDERFOOT_MODULES",
            "sfp_dnsresolve,sfp_whois,sfp_names",
        ).split(",") if item.strip()]
        return {
            "weak_lead_only": True,
            "passive_modules_only": os.getenv("SPIDERFOOT_ALLOW_INTRUSIVE", "0").lower() not in {"1", "true", "yes"},
            "hard_identity_links": False,
            "runtime_bounded": True,
            "compose_profile": "recon",
            "service": "collector_spiderfoot",
            "target_cap": target_cap,
            "stage": stage,
            "modules": modules[:10],
        }
    if feature == "lemon8":
        return {
            "weak_lead_only": False,
            "passive_modules_only": False,
            "hard_identity_links": False,
            "runtime_bounded": True,
            "service": "collector_lemon8",
            "target_cap": target_cap,
            "stage": stage,
            "one_browser_family_at_a_time": True,
        }
    return {
        "weak_lead_only": False,
        "passive_modules_only": False,
        "hard_identity_links": False,
        "runtime_bounded": True,
        "service": "browser_social_tabs",
        "target_cap": target_cap,
        "stage": stage,
        "one_tab_per_platform": True,
    }


async def optional_rollout_report(
    conn,
    *,
    feature: str = "spiderfoot",
    stage: str = "dry-run",
    window_hours: int = 24,
    limit: int | None = None,
) -> dict[str, Any]:
    request = RolloutRequest(feature=feature, stage=stage, window_hours=window_hours, limit=limit)
    normalized_feature = request.normalized_feature
    normalized_stage = request.normalized_stage
    target_cap = request.target_cap
    sources = FEATURE_SOURCES[normalized_feature]
    preview_limit = max(10, target_cap)

    stop_reasons: list[dict[str, Any]] = []
    checks = {
        "window_hours": max(1, int(window_hours)),
        "source_health": "checked",
        "collector_operational_events": "checked",
        "rate_limit_events": "checked",
    }
    for fetcher in (
        _fetch_source_health_stops,
        lambda c, s: _fetch_operational_stops(c, s, window_hours=window_hours),
        lambda c, s: _fetch_rate_limit_stops(c, s, window_hours=window_hours),
    ):
        try:
            stop_reasons.extend(await fetcher(conn, sources))
        except Exception as exc:  # noqa: BLE001
            stop_reasons.append({
                "kind": "monitor_error",
                "source": normalized_feature,
                "reason": exc.__class__.__name__,
                "summary": str(exc)[:180],
            })

    candidates = await _fetch_seen_candidates(
        conn,
        feature=normalized_feature,
        limit=preview_limit,
    )
    can_proceed = not stop_reasons
    if normalized_stage == "dry-run":
        action = "dry_run"
    elif can_proceed:
        action = "advance_stage"
    else:
        action = "stop_or_rollback"

    return {
        "feature": normalized_feature,
        "stage": normalized_stage,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "can_proceed": can_proceed,
        "recommended_action": action,
        "target_cap": target_cap,
        "candidate_count": len(candidates),
        "candidate_preview": [_safe_candidate(row) for row in candidates[:10]],
        "stop_reasons": stop_reasons[:50],
        "policy": _feature_policy(normalized_feature, normalized_stage, target_cap),
        "checks": checks,
    }


def _seen_to_recon_type(target_type: str) -> str | None:
    value = str(target_type or "").strip().lower()
    if value in {"domain", "url", "email"}:
        return value
    if value in {"user", "username", "channel"}:
        return "username"
    return None


async def _queue_spiderfoot_seen_candidates(conn, candidates: list[Any], *, target_cap: int) -> dict[str, int]:
    from src.core.recon import queue_recon_target

    queued = 0
    skipped = 0
    for row in candidates[:max(0, target_cap)]:
        target_type = _seen_to_recon_type(str(_row_get(row, "target_type") or ""))
        target_value = str(_row_get(row, "target_key") or "").strip()
        if not target_type or not target_value:
            skipped += 1
            continue
        scope = {
            "collector_derived": True,
            "weak_lead_only": True,
            "hard_identity_link": False,
            "from_seen_registry": True,
            "collector_source": str(_row_get(row, "source") or "").lower(),
            "source_table": str(_row_get(row, "source_table") or "collector_seen_targets"),
            "source_record_id": str(_row_get(row, "source_record_id") or ""),
            "seen_target_status": str(_row_get(row, "status") or ""),
            "modules": ["sfp_accounts"] if target_type == "username" else None,
        }
        scope = {key: value for key, value in scope.items() if value is not None and value != ""}
        try:
            await queue_recon_target(
                conn,
                target_type=target_type,
                target_value=target_value,
                source="collector:collector_seen_targets",
                priority=7,
                scope=scope,
            )
            queued += 1
        except ValueError:
            skipped += 1
    return {"queued": queued, "skipped": skipped}


async def _record_rollout_event(conn, report: dict[str, Any], *, applied: bool) -> None:
    if not await _table_exists(conn, "collector_operational_events"):
        return
    severity = "warning" if report.get("recommended_action") == "stop_or_rollback" else "info"
    summary = (
        f"optional rollout {report['feature']} stage={report['stage']} "
        f"action={report['recommended_action']} cap={report['target_cap']}"
    )
    metadata = {
        "feature": report.get("feature"),
        "stage": report.get("stage"),
        "can_proceed": report.get("can_proceed"),
        "target_cap": report.get("target_cap"),
        "candidate_count": report.get("candidate_count"),
        "stop_reason_count": len(report.get("stop_reasons") or []),
        "applied": applied,
        "policy": report.get("policy"),
    }
    await conn.execute(
        """
        INSERT INTO collector_operational_events (source, event_type, severity, summary, metadata)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        str(report.get("feature") or "optional"),
        "optional_rollout",
        severity,
        summary,
        json.dumps(metadata, default=str),
    )


async def apply_optional_rollout(
    conn,
    *,
    feature: str = "spiderfoot",
    stage: str = "dry-run",
    window_hours: int = 24,
    limit: int | None = None,
) -> dict[str, Any]:
    report = await optional_rollout_report(
        conn,
        feature=feature,
        stage=stage,
        window_hours=window_hours,
        limit=limit,
    )
    applied: dict[str, Any] = {"applied": False}
    if report["recommended_action"] == "stop_or_rollback":
        applied["reason"] = "stop_criteria_active"
    elif report["target_cap"] <= 0:
        applied["reason"] = "dry_run_stage"
    elif report["feature"] == "spiderfoot":
        candidates = await _fetch_seen_candidates(
            conn,
            feature="spiderfoot",
            limit=report["target_cap"],
        )
        applied.update(await _queue_spiderfoot_seen_candidates(conn, candidates, target_cap=report["target_cap"]))
        applied["applied"] = applied.get("queued", 0) > 0
    else:
        applied["reason"] = "manual_service_enable_required"
        applied["service"] = report["policy"].get("service")

    report["applied"] = applied
    await _record_rollout_event(conn, report, applied=bool(applied.get("applied")))
    return report
