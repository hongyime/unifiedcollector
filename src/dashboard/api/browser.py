"""Browser / Chrome-extension diagnostics for the dashboard ``/health`` and
``/collectors/source-matrix`` payloads.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 4A step 5.

Everything here is a helper that shapes the ``browser_extension`` sub-payload
consumed by the two big fan-out routes (``/health?include_sources=1`` and
``/collectors/source-matrix``). There are no ``/api/extension/*`` or
``/api/browser/*`` routes today; the plan's ``APIRouter()`` is created here as
an empty placeholder so future browser-scoped routes have a clear home.

Dependencies are strictly leaf-facing: only ``src.dashboard.api.helpers``
(shared DB pool acquire/release and pure datetime utilities) and stdlib. This
keeps browser.py off the ``__init__.py`` import path and avoids circular
imports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from fastapi import APIRouter

from src.db.connection import get_pool
from src.dashboard.api.helpers import _release_dashboard_conn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config constants (module-level defaults).
#
# ``__init__.py`` re-exports these names — so tests written to patch
# ``dashboard_api._X`` continue to work. Functions in this module read the
# effective value at call time via ``_cfg("_X", _X)``. This preserves
# monkey-patch semantics from before the split.
# ---------------------------------------------------------------------------


def _cfg(name: str, default):
    """Look up a config constant on the parent ``dashboard_api`` module first.

    Enables tests to patch ``dashboard_api._NAME`` and have that value picked
    up by browser.py's functions at call time. Falls back to the local default
    (browser.py's own module-level binding) if the parent has no such name.
    """
    root = sys.modules.get("src.dashboard.api")
    if root is not None:
        return getattr(root, name, default)
    return default

_BROWSER_EXTENSION_QUERY_TIMEOUT_SECONDS = float(os.getenv("BROWSER_EXTENSION_QUERY_TIMEOUT_SECONDS", "2.5"))
_BROWSER_EXTENSION_PAYLOAD_BUDGET_SECONDS = float(os.getenv("BROWSER_EXTENSION_PAYLOAD_BUDGET_SECONDS", "8.5"))
_BROWSER_EXTENSION_INGEST_SUMMARY_HOURS = max(1, int(os.getenv("BROWSER_EXTENSION_INGEST_SUMMARY_HOURS", "6")))
_BROWSER_MEDIA_CANDIDATE_SUMMARY_HOURS = max(1, int(os.getenv("BROWSER_MEDIA_CANDIDATE_SUMMARY_HOURS", "6")))
_BROWSER_EXTENSION_OPTIONAL_QUERY_TIMEOUT_SECONDS = float(
    os.getenv("BROWSER_EXTENSION_OPTIONAL_QUERY_TIMEOUT_SECONDS", "0.5")
)
_BROWSER_TAB_MAINTENANCE_STATUS_PATH = os.getenv(
    "BROWSER_TAB_MAINTENANCE_STATUS_PATH",
    "/app/tmp/browser_tab_maintenance_status.json",
)
_BROWSER_TAB_AUDIT_RESULT_PATH = os.getenv(
    "BROWSER_TAB_AUDIT_RESULT_PATH",
    "/app/tmp/browser_tab_audit_result.json",
)
_BROWSER_TAB_MAINTENANCE_STALE_SECONDS = int(os.getenv("BROWSER_TAB_MAINTENANCE_STALE_SECONDS", "2700"))
_BROWSER_TAB_MAINTENANCE_RUNNING_STALLED_SECONDS = int(
    os.getenv("BROWSER_TAB_MAINTENANCE_RUNNING_STALLED_SECONDS", "900")
)
_EXTENSION_RECENT_MISMATCH_SECONDS = 15 * 60

_OPTIONAL_BROWSER_DIAGNOSTIC_SECTIONS = {
    'browser_ingest_events',
    'browser_content_gap',
    'browser_media_candidates_table',
    'browser_media_candidates',
    'browser_media_revisit_queue_table',
    'browser_media_revisit_queue',
    'tiktok_browser_media_candidates_table',
    'tiktok_browser_media_candidates',
    'tiktok_browser_revisit_queue_table',
    'tiktok_browser_revisit_queue',
}


# ---------------------------------------------------------------------------
# Router — empty placeholder; extension/browser routes have never existed as
# separate paths and are not extracted here. Reserved for future work.
# ---------------------------------------------------------------------------

router = APIRouter()


# ---------------------------------------------------------------------------
# Small pure helpers.
# ---------------------------------------------------------------------------

def _expected_extension_version() -> str | None:
    env_version = str(os.getenv("UC_EXTENSION_EXPECTED_VERSION") or "").strip()
    if env_version:
        return env_version
    try:
        manifest = Path(__file__).resolve().parent.parent.parent / "extension" / "manifest.json"
        version = str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "").strip()
        return version or None
    except Exception:
        return None


def _extension_versions_match(current: str | None, expected: str | None) -> bool:
    if not current or not expected:
        return True
    current_norm = current.lstrip("vV")
    expected_norm = expected.lstrip("vV")
    if current_norm == expected_norm:
        return True

    def parse(value: str) -> tuple[int, ...] | None:
        if not re.fullmatch(r"\d+(?:\.\d+)*", value):
            return None
        return tuple(int(part) for part in value.split("."))

    current_parts = parse(current_norm)
    expected_parts = parse(expected_norm)
    if current_parts is None or expected_parts is None:
        return False
    width = max(len(current_parts), len(expected_parts))
    current_parts = current_parts + (0,) * (width - len(current_parts))
    expected_parts = expected_parts + (0,) * (width - len(expected_parts))
    return current_parts >= expected_parts


def _extension_reload_target_from_url(url: object) -> tuple[str | None, str | None]:
    match = re.match(r"^chrome-extension://([a-p]{32})/", str(url or ""))
    if not match:
        return None, None
    extension_id = match.group(1)
    return extension_id, f"chrome-extension://{extension_id}/tabs.html?reload=1"


def _extension_management_url(extension_id: object) -> str | None:
    extension_id = str(extension_id or "").strip()
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        return None
    return f"chrome://extensions/?id={extension_id}"


def _browser_tab_maintenance_payload(
    status_path: str | os.PathLike[str] | None = None,
) -> dict | None:
    stale_after = _cfg("_BROWSER_TAB_MAINTENANCE_STALE_SECONDS", _BROWSER_TAB_MAINTENANCE_STALE_SECONDS)
    running_stalled_after = _cfg(
        "_BROWSER_TAB_MAINTENANCE_RUNNING_STALLED_SECONDS",
        _BROWSER_TAB_MAINTENANCE_RUNNING_STALLED_SECONDS,
    )
    default_path = _cfg("_BROWSER_TAB_MAINTENANCE_STATUS_PATH", _BROWSER_TAB_MAINTENANCE_STATUS_PATH)
    path = Path(status_path or default_path)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break dashboard
        return {
            "state": "unreadable",
            "detail": str(exc),
            "status_path": str(path),
        }
    if not isinstance(raw, dict):
        return {
            "state": "invalid",
            "detail": "maintenance status file is not a JSON object",
            "status_path": str(path),
        }
    try:
        age_seconds = max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        age_seconds = None
    stale = bool(
        age_seconds is not None
        and stale_after > 0
        and age_seconds > stale_after
    )
    state = str(raw.get("state") or "unknown")
    loop = raw.get("loop") if isinstance(raw.get("loop"), dict) else None
    loop_detail = str((loop or {}).get("detail") or "").lower()
    running_without_active_pass = bool(
        state == "running"
        and (
            "sleeping after nonzero pass" in loop_detail
            or "sleeping after successful pass" in loop_detail
        )
    )
    running_stalled = bool(
        state == "running"
        and (
            running_without_active_pass
            or (
                age_seconds is not None
                and running_stalled_after > 0
                and age_seconds > running_stalled_after
            )
        )
    )
    return {
        "state": state,
        "detail": raw.get("detail"),
        "checked_at": raw.get("checked_at"),
        "age_seconds": age_seconds,
        "stale": stale,
        "stale_after_seconds": stale_after,
        "running_stalled": running_stalled,
        "running_without_active_pass": running_without_active_pass,
        "running_stalled_after_seconds": running_stalled_after,
        "cdp_url": raw.get("cdp_url"),
        "audit_result": raw.get("audit_result"),
        "reload_plan": raw.get("reload_plan"),
        "pid": raw.get("pid"),
        "last_terminal_state": raw.get("last_terminal_state"),
        "consecutive_cdp_unavailable_count": raw.get("consecutive_cdp_unavailable_count"),
        "cdp_unavailable_since": raw.get("cdp_unavailable_since"),
        "loop": loop,
        "diagnostics": raw.get("diagnostics") if isinstance(raw.get("diagnostics"), dict) else None,
        "status_path": str(path),
    }


def _browser_tab_audit_platforms(audit_path: object) -> list[str]:
    if not audit_path:
        return []
    raw_path = str(audit_path)
    try:
        path = Path(raw_path)
        if not path.is_file() and ("\\" in raw_path or ":" in raw_path):
            status_path = _cfg("_BROWSER_TAB_MAINTENANCE_STATUS_PATH", _BROWSER_TAB_MAINTENANCE_STATUS_PATH)
            path = Path(status_path).parent / re.split(r"[\\/]", raw_path)[-1]
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    budget = raw.get("tab_budget") or raw.get("_tab_budget")
    if not isinstance(budget, dict) or budget.get("ok") is not True:
        return []
    counts = budget.get("counts")
    if not isinstance(counts, dict):
        return []
    per_platform = counts.get("per_platform")
    if not isinstance(per_platform, dict):
        return []
    return sorted(
        str(platform)
        for platform, count in per_platform.items()
        if platform and int(count or 0) > 0
    )


def _browser_tab_audit_age_seconds(audit_path: object) -> int | None:
    if not audit_path:
        return None
    raw_path = str(audit_path)
    try:
        path = Path(raw_path)
        if not path.is_file() and ("\\" in raw_path or ":" in raw_path):
            status_path = _cfg("_BROWSER_TAB_MAINTENANCE_STATUS_PATH", _BROWSER_TAB_MAINTENANCE_STATUS_PATH)
            path = Path(status_path).parent / re.split(r"[\\/]", raw_path)[-1]
        return max(0, int(time.time() - path.stat().st_mtime))
    except Exception:
        return None


def _browser_tab_audit_page_errors(
    audit_path: object,
    *,
    max_age_seconds: int | None = None,
) -> list[dict]:
    if not audit_path:
        return []
    raw_path = str(audit_path)
    try:
        path = Path(raw_path)
        if not path.is_file() and ("\\" in raw_path or ":" in raw_path):
            status_path = _cfg("_BROWSER_TAB_MAINTENANCE_STATUS_PATH", _BROWSER_TAB_MAINTENANCE_STATUS_PATH)
            path = Path(status_path).parent / re.split(r"[\\/]", raw_path)[-1]
        if not path.is_file():
            return []
        age_seconds = max(0, int(time.time() - path.stat().st_mtime))
        if max_age_seconds is not None and max_age_seconds > 0 and age_seconds > max_age_seconds:
            return []
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    budget = raw.get("tab_budget") or raw.get("_tab_budget")
    if isinstance(budget, dict) and budget.get("ok") is not True:
        return []
    issues = []
    for platform, entries in raw.items():
        if str(platform).startswith("_"):
            continue
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            health_status = str(item.get("page_health_status") or "")
            if health_status != "recoverable_error_shell":
                continue
            platform_name = str(item.get("platform") or platform or "").lower()
            if not platform_name:
                continue
            health_reason = item.get("page_health_reason") or "recoverable error shell"
            issues.append({
                "platform": platform_name,
                "kind": "browser_page_error",
                "severity": "warning",
                "detail": (
                    "Fresh browser tab audit reports a recoverable page shell instead of usable content."
                ),
                "url": item.get("url"),
                "health_status": health_status,
                "health_reason": health_reason,
                "extension_version": item.get("cs_version") or item.get("extension_version"),
                "content_counts": item.get("content_counts"),
                "tab_audit_age_seconds": age_seconds,
                "audit_source": "browser_tab_audit_result",
            })
    return issues


def _browser_extension_add_tab_audit_page_errors(payload: dict) -> dict:
    maintenance = payload.get("maintenance") if isinstance(payload.get("maintenance"), dict) else {}
    default_audit = _cfg("_BROWSER_TAB_AUDIT_RESULT_PATH", _BROWSER_TAB_AUDIT_RESULT_PATH)
    default_stale = _cfg("_BROWSER_TAB_MAINTENANCE_STALE_SECONDS", _BROWSER_TAB_MAINTENANCE_STALE_SECONDS)
    audit_path = (maintenance or {}).get("audit_result") or default_audit
    try:
        max_age = int((maintenance or {}).get("stale_after_seconds") or default_stale)
    except (TypeError, ValueError):
        max_age = default_stale
    existing = {
        (
            str(issue.get("platform") or "").lower(),
            str(issue.get("kind") or ""),
            str(issue.get("health_reason") or ""),
            str(issue.get("url") or ""),
        )
        for issue in payload.get("issues", [])
        if isinstance(issue, dict)
    }
    for issue in _browser_tab_audit_page_errors(audit_path, max_age_seconds=max_age):
        key = (
            str(issue.get("platform") or "").lower(),
            str(issue.get("kind") or ""),
            str(issue.get("health_reason") or ""),
            str(issue.get("url") or ""),
        )
        if key in existing:
            continue
        payload.setdefault("issues", []).append(issue)
        existing.add(key)
    return payload


def _browser_extension_apply_maintenance_ingest_fallback(payload: dict) -> dict:
    maintenance = payload.get("maintenance")
    if not isinstance(maintenance, dict):
        return payload
    state = str(maintenance.get("state") or "")
    last_terminal_state = str(maintenance.get("last_terminal_state") or "")
    if maintenance.get("stale"):
        return payload
    platforms = _browser_tab_audit_platforms(maintenance.get("audit_result"))
    if not platforms:
        return payload
    audit_age_seconds = _browser_tab_audit_age_seconds(maintenance.get("audit_result"))
    stale_after_seconds = maintenance.get("stale_after_seconds")
    try:
        audit_is_fresh = (
            audit_age_seconds is not None
            and int(stale_after_seconds or 0) > 0
            and audit_age_seconds <= int(stale_after_seconds or 0)
        )
    except (TypeError, ValueError):
        audit_is_fresh = False
    maintenance_ok = state == "ok" or (state == "running" and last_terminal_state == "ok")
    if not maintenance_ok and not audit_is_fresh:
        return payload
    payload["ingest_health"] = {
        "state": "active_via_maintenance",
        "active": True,
        "heartbeat_active": True,
        "content_active": False,
        "last_seen_at": maintenance.get("checked_at"),
        "last_content_at": None,
        "last_seen_age_seconds": maintenance.get("age_seconds"),
        "last_content_age_seconds": None,
        "fresh_after_seconds": maintenance.get("stale_after_seconds"),
        "active_platforms": platforms,
        "content_platforms": [],
        "note": (
            "Browser ingest DB diagnostics were unavailable, but fresh tab maintenance "
            "verified responsive extension tabs."
        ),
    }
    payload["maintenance_ingest_fallback"] = True
    return payload


def _browser_extension_fallback_payload(reason: str = "unavailable") -> dict:
    maintenance = _browser_tab_maintenance_payload()
    issues = []
    if maintenance:
        state = str(maintenance.get("state") or "")
        if maintenance.get("running_stalled"):
            issues.append({
                "platform": "browser",
                "kind": "browser_maintenance_stalled",
                "detail": (
                    "Browser tab maintenance has been running longer than its wall-clock "
                    f"limit; ingest diagnostics were not available because the source matrix "
                    f"section returned {reason}."
                ),
                "age_seconds": maintenance.get("age_seconds"),
                "stalled_after_seconds": maintenance.get("running_stalled_after_seconds"),
                "checked_at": maintenance.get("checked_at"),
                "ingest_diagnostics_unavailable": True,
            })
        elif state in {"cdp_unavailable", "unreadable", "invalid"}:
            diagnostics = maintenance.get("diagnostics") or {}
            detail = maintenance.get("detail") or "Browser tab maintenance cannot reach Chrome CDP."
            diag_reason = diagnostics.get("reason")
            hint = diagnostics.get("hint")
            if diag_reason:
                detail = f"{detail} ({diag_reason})"
            if hint:
                detail = f"{detail} {hint}"
            issues.append({
                "platform": "browser",
                "kind": "browser_maintenance_cdp_unavailable",
                "detail": (
                    f"{detail} Browser extension ingest diagnostics were not available "
                    f"because the source matrix section returned {reason}."
                ).strip(),
                "age_seconds": maintenance.get("age_seconds"),
                "cdp_url": maintenance.get("cdp_url"),
                "checked_at": maintenance.get("checked_at"),
                "diagnostics": diagnostics or None,
                "ingest_diagnostics_unavailable": True,
            })
        elif maintenance.get("stale"):
            issues.append({
                "platform": "browser",
                "kind": "browser_maintenance_stale",
                "detail": (
                    "Browser tab maintenance has not reported recently; "
                    f"ingest diagnostics were not available because the source matrix section returned {reason}."
                ),
                "age_seconds": maintenance.get("age_seconds"),
                "stale_after_seconds": maintenance.get("stale_after_seconds"),
                "checked_at": maintenance.get("checked_at"),
                "ingest_diagnostics_unavailable": True,
            })
    payload = {
        "expected_version": _expected_extension_version(),
        "extension_id": None,
        "reload_url": None,
        "maintenance": maintenance,
        "ingest_health": {
            "state": "unknown",
            "active": False,
            "heartbeat_active": False,
            "content_active": False,
            "last_seen_at": None,
            "last_content_at": None,
            "fresh_after_seconds": 600,
            "active_platforms": [],
            "content_platforms": [],
            "note": f"Browser extension ingest diagnostics unavailable: {reason}.",
        },
        "issues": issues,
        "diagnostic_errors": [{"section": "source_matrix_browser_extension", "error": reason}],
    }
    return _browser_extension_apply_maintenance_ingest_fallback(payload)


def _browser_ingest_health_from_items(ingest_items: list[dict]) -> dict:
    try:
        ingest_fresh_after_seconds = int(os.getenv("BROWSER_INGEST_ACTIVE_SECONDS", "600") or "600")
    except Exception:
        ingest_fresh_after_seconds = 600
    ingest_fresh_after_seconds = max(60, ingest_fresh_after_seconds)
    fresh_items = [
        item for item in ingest_items
        if int(item.get("age_seconds") or 0) <= ingest_fresh_after_seconds
    ]
    fresh_heartbeats = [
        item for item in fresh_items
        if str(item.get("endpoint") or "") == "browser_heartbeat"
    ]
    fresh_content = [
        item for item in fresh_items
        if str(item.get("endpoint") or "") != "browser_heartbeat"
        and (int(item.get("observed_count") or 0) > 0 or int(item.get("stored_count") or 0) > 0)
    ]
    latest_seen = min(
        (int(item.get("age_seconds") or 0) for item in ingest_items),
        default=None,
    )
    latest_content = min(
        (
            int(item.get("age_seconds") or 0) for item in ingest_items
            if str(item.get("endpoint") or "") != "browser_heartbeat"
            and (int(item.get("observed_count") or 0) > 0 or int(item.get("stored_count") or 0) > 0)
        ),
        default=None,
    )
    return {
        "state": "active" if fresh_items else ("stale" if ingest_items else "missing"),
        "active": bool(fresh_items),
        "heartbeat_active": bool(fresh_heartbeats),
        "content_active": bool(fresh_content),
        "last_seen_at": max(
            (item.get("last_seen_at") for item in ingest_items if item.get("last_seen_at") is not None),
            default=None,
        ),
        "last_content_at": max(
            (
                item.get("last_seen_at") for item in ingest_items
                if item.get("last_seen_at") is not None
                and str(item.get("endpoint") or "") != "browser_heartbeat"
                and (int(item.get("observed_count") or 0) > 0 or int(item.get("stored_count") or 0) > 0)
            ),
            default=None,
        ),
        "last_seen_age_seconds": latest_seen,
        "last_content_age_seconds": latest_content,
        "fresh_after_seconds": ingest_fresh_after_seconds,
        "active_platforms": sorted({str(item.get("platform")) for item in fresh_items if item.get("platform")}),
        "content_platforms": sorted({str(item.get("platform")) for item in fresh_content if item.get("platform")}),
        "note": (
            "Browser extension ingest has fresh heartbeats and useful content; CDP maintenance/cookie-vault status is separate."
            if fresh_content else
            (
                "Browser extension heartbeats are active, but useful browser content is stale or missing; "
                "CDP maintenance/cookie-vault status is separate."
            )
            if fresh_items else
            "No fresh browser extension ingest event is present in the active window."
        ),
    }


def _browser_extension_apply_ingest_health(payload: dict, ingest_items: list[dict]) -> None:
    health = _browser_ingest_health_from_items(ingest_items)
    payload["ingest_health"] = health
    if health.get("active"):
        active_detail = (
            "Browser extension ingest is still producing useful content; CDP tab maintenance and cookie backup are unavailable."
            if health.get("content_active")
            else (
                "Browser extension heartbeats are still active, but useful browser content is stale; "
                "CDP tab maintenance and cookie backup are unavailable."
            )
        )
        for issue in payload.get("issues", []):
            if issue.get("kind") == "browser_maintenance_cdp_unavailable":
                issue["extension_ingest_active"] = True
                issue["ingest_diagnostics_unavailable"] = False
                detail = str(issue.get("detail") or "").strip()
                if active_detail not in detail:
                    detail = f"{detail} {active_detail}".strip()
                issue["detail"] = detail


def _browser_extension_suppress_optional_diagnostics_when_active(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return payload
    health = payload.get("ingest_health") or {}
    if not health.get("content_active"):
        return payload
    errors = payload.get("diagnostic_errors")
    if not isinstance(errors, list):
        return payload
    optional_sections = _cfg("_OPTIONAL_BROWSER_DIAGNOSTIC_SECTIONS", _OPTIONAL_BROWSER_DIAGNOSTIC_SECTIONS)
    payload["diagnostic_errors"] = [
        item for item in errors
        if not (
            isinstance(item, dict)
            and item.get("section") in optional_sections
        )
    ]
    return payload


async def _browser_extension_fallback_payload_with_fast_ingest(reason: str) -> dict:
    payload = _browser_extension_fallback_payload(reason)
    pool = None
    conn = None
    try:
        # Read ``get_pool`` from the parent api module at call time so tests
        # can patch ``dashboard_api.get_pool`` and have it take effect here.
        _get_pool = _cfg("get_pool", get_pool)
        pool = await asyncio.wait_for(_get_pool(), timeout=1.0)
        conn = await asyncio.wait_for(pool.acquire(), timeout=1.0)
        exists = await conn.fetchval(
            "SELECT to_regclass('browser_ingest_events')",
            timeout=0.5,
        )
        if exists is None:
            return payload
        rows = await conn.fetch(
            """
            SELECT platform,
                   endpoint,
                   count(*)::int AS requests,
                   sum(observed_count)::int AS observed_count,
                   sum(stored_count)::int AS stored_count,
                   max(created_at) AS last_seen_at,
                   extract(epoch FROM now() - max(created_at))::int AS age_seconds,
                   (array_agg(NULLIF(metadata->>'extension_version', '') ORDER BY created_at DESC))[1]
                       AS extension_version
            FROM browser_ingest_events
            WHERE created_at >= now() - interval '30 minutes'
            GROUP BY platform, endpoint
            ORDER BY last_seen_at DESC
            LIMIT 20
            """,
            timeout=1.0,
        )
        ingest_items = [
            {
                "platform": row["platform"],
                "endpoint": row["endpoint"],
                "requests": int(row["requests"] or 0),
                "observed_count": int(row["observed_count"] or 0),
                "stored_count": int(row["stored_count"] or 0),
                "last_seen_at": row["last_seen_at"],
                "age_seconds": int(row["age_seconds"] or 0),
                "extension_version": row["extension_version"],
                "version_ok": _extension_versions_match(row["extension_version"], payload.get("expected_version")),
            }
            for row in rows
        ]
        if ingest_items:
            payload["ingest"] = ingest_items
            _browser_extension_apply_ingest_health(payload, ingest_items)
            payload["fast_ingest_fallback"] = True
    except Exception as exc:  # noqa: BLE001 - fallback diagnostics must not break source matrix
        payload.setdefault("diagnostic_errors", []).append({
            "section": "source_matrix_browser_extension_fast_ingest",
            "error": exc.__class__.__name__,
        })
        logger.debug("browser extension fast ingest fallback failed: %s", exc.__class__.__name__)
    finally:
        if pool is not None and conn is not None:
            _release = _cfg("_release_dashboard_conn", _release_dashboard_conn)
            await _release(pool, conn, "browser extension fast ingest fallback")
    return payload


def _extension_issues_by_source(extension_payload: dict | None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for issue in (extension_payload or {}).get("issues", []):
        source = str(issue.get("platform") or "").lower()
        if source:
            out.setdefault(source, []).append(issue)
    return out


def _fresh_current_extension_seen(payload: dict, platform: str, since) -> bool:
    """True when a fresh, version-matched hook/ingest for ``platform`` post-dates ``since``.

    ``since`` is coerced through ``_dt_for_compare`` at the call site; this
    helper accepts either a datetime or None.
    """
    from src.dashboard.api.helpers import _dt_for_compare
    if not platform or since is None:
        return False
    for item in [*payload.get("hooks", []), *payload.get("ingest", [])]:
        if str(item.get("platform") or "").lower() != platform:
            continue
        if not item.get("version_ok"):
            continue
        seen_at = _dt_for_compare(item.get("last_seen_at"))
        if seen_at and seen_at > since:
            return True
    return False


def _suppress_shadowed_extension_mismatches(payload: dict) -> None:
    from src.dashboard.api.helpers import _dt_for_compare
    kept = []
    for issue in payload.get("issues", []):
        if issue.get("kind") != "extension_version_mismatch":
            kept.append(issue)
            continue
        platform = str(issue.get("platform") or "").lower()
        last_seen = _dt_for_compare(issue.get("last_seen_at"))
        if _fresh_current_extension_seen(payload, platform, last_seen):
            continue
        kept.append(issue)
    payload["issues"] = kept


async def _browser_extension_payload(conn) -> dict:
    """Build the ``browser_extension`` sub-payload consumed by ``/health`` and
    ``/collectors/source-matrix``.

    Uses the passed ``conn`` (an asyncpg connection acquired by the caller) so
    the caller controls DB pool lifecycle. All DB access is best-effort and
    time-bounded via ``_BROWSER_EXTENSION_PAYLOAD_BUDGET_SECONDS``.
    """
    expected = _expected_extension_version()
    payload = {
        "expected_version": expected,
        "extension_id": None,
        "reload_url": None,
        "hooks": [],
        "ingest": [],
        "media_candidates": [],
        "media_revisit_queue": [],
        "tiktok_media": None,
        "maintenance": None,
        "ingest_health": {
            "state": "unknown",
            "active": False,
            "heartbeat_active": False,
            "content_active": False,
            "last_seen_at": None,
            "last_content_at": None,
            "fresh_after_seconds": 600,
            "active_platforms": [],
            "content_platforms": [],
            "note": "No browser extension ingest event has been read yet.",
        },
        "issues": [],
        "diagnostic_errors": [],
    }

    maintenance = _browser_tab_maintenance_payload()
    if maintenance:
        payload["maintenance"] = maintenance
        state = str(maintenance.get("state") or "")
        if maintenance.get("running_stalled"):
            payload["issues"].append({
                "platform": "browser",
                "kind": "browser_maintenance_stalled",
                "detail": (
                    "Browser tab maintenance has been running longer than its wall-clock "
                    "limit; tab reload/audit automation may be wedged."
                ),
                "age_seconds": maintenance.get("age_seconds"),
                "stalled_after_seconds": maintenance.get("running_stalled_after_seconds"),
                "checked_at": maintenance.get("checked_at"),
            })
        elif maintenance.get("stale"):
            payload["issues"].append({
                "platform": "browser",
                "kind": "browser_maintenance_stale",
                "detail": (
                    "Browser tab maintenance has not reported recently; "
                    "tab reload/audit automation may not be running."
                ),
                "age_seconds": maintenance.get("age_seconds"),
                "stale_after_seconds": maintenance.get("stale_after_seconds"),
                "checked_at": maintenance.get("checked_at"),
            })
        if state in {"cdp_unavailable", "unreadable", "invalid"}:
            diagnostics = maintenance.get("diagnostics") or {}
            reason = diagnostics.get("reason")
            hint = diagnostics.get("hint")
            detail = (
                maintenance.get("detail")
                or "Browser tab maintenance cannot reach Chrome CDP."
            )
            if reason:
                detail = f"{detail} ({reason})"
            if hint:
                detail = f"{detail} {hint}"
            payload["issues"].append({
                "platform": "browser",
                "kind": "browser_maintenance_cdp_unavailable",
                "detail": detail,
                "age_seconds": maintenance.get("age_seconds"),
                "cdp_url": maintenance.get("cdp_url"),
                "checked_at": maintenance.get("checked_at"),
                "diagnostics": diagnostics or None,
            })

    deadline = time.monotonic() + max(
        0.5, _cfg("_BROWSER_EXTENSION_PAYLOAD_BUDGET_SECONDS", _BROWSER_EXTENSION_PAYLOAD_BUDGET_SECONDS)
    )
    _query_timeout_default = _cfg(
        "_BROWSER_EXTENSION_QUERY_TIMEOUT_SECONDS", _BROWSER_EXTENSION_QUERY_TIMEOUT_SECONDS
    )
    _optional_query_timeout = _cfg(
        "_BROWSER_EXTENSION_OPTIONAL_QUERY_TIMEOUT_SECONDS", _BROWSER_EXTENSION_OPTIONAL_QUERY_TIMEOUT_SECONDS
    )
    _ingest_summary_hours = _cfg(
        "_BROWSER_EXTENSION_INGEST_SUMMARY_HOURS", _BROWSER_EXTENSION_INGEST_SUMMARY_HOURS
    )
    _media_summary_hours = _cfg(
        "_BROWSER_MEDIA_CANDIDATE_SUMMARY_HOURS", _BROWSER_MEDIA_CANDIDATE_SUMMARY_HOURS
    )
    _recent_mismatch_secs = _cfg(
        "_EXTENSION_RECENT_MISMATCH_SECONDS", _EXTENSION_RECENT_MISMATCH_SECONDS
    )

    def _remaining_timeout(max_timeout: float | None = None) -> float | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            return None
        query_timeout = _query_timeout_default
        if max_timeout is not None:
            query_timeout = min(query_timeout, max(0.1, max_timeout))
        return max(0.1, min(query_timeout, remaining))

    def _record_diagnostic_error(label: str, exc: BaseException | None = None) -> None:
        payload["diagnostic_errors"].append({
            "section": label,
            "error": exc.__class__.__name__ if exc else "SkippedBudget",
        })
        if exc:
            logger.debug("browser extension diagnostic %s failed: %s", label, exc.__class__.__name__)

    async def _fetchval_or_none(
        label: str,
        query: str,
        *args,
        max_timeout: float | None = None,
        record_error: bool = True,
    ):
        timeout = _remaining_timeout(max_timeout)
        if timeout is None:
            if record_error:
                _record_diagnostic_error(label)
            return None
        try:
            return await conn.fetchval(query, *args, timeout=timeout)
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 - dashboard diagnostics are best-effort
            if record_error:
                _record_diagnostic_error(label, exc)
            return None

    async def _fetch_or_empty(
        label: str,
        query: str,
        *args,
        max_timeout: float | None = None,
        record_error: bool = True,
    ):
        timeout = _remaining_timeout(max_timeout)
        if timeout is None:
            if record_error:
                _record_diagnostic_error(label)
            return []
        try:
            return await conn.fetch(query, *args, timeout=timeout)
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 - dashboard diagnostics are best-effort
            if record_error:
                _record_diagnostic_error(label, exc)
            return []

    async def _fetchrow_or_none(label: str, query: str, *args, max_timeout: float | None = None):
        timeout = _remaining_timeout(max_timeout)
        if timeout is None:
            _record_diagnostic_error(label)
            return None
        try:
            return await conn.fetchrow(query, *args, timeout=timeout)
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 - dashboard diagnostics are best-effort
            _record_diagnostic_error(label, exc)
            return None

    def _set_ingest_health(ingest_items: list[dict]) -> None:
        _browser_extension_apply_ingest_health(payload, ingest_items)

    browser_ingest_events_exists = await _fetchval_or_none(
        "browser_ingest_events_table",
        "SELECT to_regclass('browser_ingest_events')",
    ) is not None
    if browser_ingest_events_exists:
        extension_id, reload_url = _extension_reload_target_from_url(await _fetchval_or_none(
            "browser_extension_reload_target",
            """
            SELECT metadata->>'url'
            FROM browser_ingest_events
            WHERE platform = 'bridge'
              AND endpoint = 'browser_heartbeat'
              AND metadata->>'url' LIKE 'chrome-extension://%/background.js'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            max_timeout=_optional_query_timeout,
            record_error=False,
        ))
        payload["extension_id"] = extension_id
        payload["reload_url"] = reload_url
        health_rows = await _fetch_or_empty(
            "browser_ingest_health",
            """
            SELECT platform,
                   endpoint,
                   count(*)::int AS requests,
                   sum(observed_count)::int AS observed_count,
                   sum(stored_count)::int AS stored_count,
                   max(created_at) AS last_seen_at,
                   extract(epoch FROM now() - max(created_at))::int AS age_seconds,
                   (array_agg(NULLIF(metadata->>'extension_version', '') ORDER BY created_at DESC))[1]
                       AS extension_version
            FROM browser_ingest_events
            WHERE created_at >= now() - interval '30 minutes'
            GROUP BY platform, endpoint
            ORDER BY last_seen_at DESC
            LIMIT 20
            """,
        )
        if health_rows:
            health_items = [
                {
                    "platform": row["platform"],
                    "endpoint": row["endpoint"],
                    "requests": int(row["requests"] or 0),
                    "observed_count": int(row["observed_count"] or 0),
                    "stored_count": int(row["stored_count"] or 0),
                    "last_seen_at": row["last_seen_at"],
                    "age_seconds": int(row["age_seconds"] or 0),
                    "extension_version": row["extension_version"],
                    "version_ok": _extension_versions_match(row["extension_version"], expected),
                }
                for row in health_rows
            ]
            _set_ingest_health(health_items)

    if await _fetchval_or_none("dm_hook_table", "SELECT to_regclass('dm_hook_heartbeat')") is not None:
        rows = await _fetch_or_empty(
            "dm_hook_heartbeat",
            """
            SELECT platform,
                   max(last_seen) AS last_seen_at,
                   extract(epoch FROM now() - max(last_seen))::int AS age_seconds,
                   (array_agg(extension_version ORDER BY last_seen DESC))[1] AS extension_version,
                   count(*) FILTER (WHERE COALESCE(owner_account, '') <> '')::int AS owner_count,
                   sum(probes_sent)::int AS probes_sent,
                   sum(samples_shipped)::int AS samples_shipped
            FROM dm_hook_heartbeat
            GROUP BY platform
            ORDER BY last_seen_at DESC
            """,
        )
        for row in rows:
            current = row["extension_version"]
            item = {
                "platform": row["platform"],
                "last_seen_at": row["last_seen_at"],
                "age_seconds": int(row["age_seconds"] or 0),
                "extension_version": current,
                "version_ok": _extension_versions_match(current, expected),
                "owner_count": int(row["owner_count"] or 0),
                "probes_sent": int(row["probes_sent"] or 0),
                "samples_shipped": int(row["samples_shipped"] or 0),
            }
            payload["hooks"].append(item)
            if int(item["age_seconds"]) > 3600:
                payload["issues"].append({
                    "platform": row["platform"],
                    "kind": "hook_stale",
                    "detail": "Chrome extension DM hook heartbeat is older than 1 hour.",
                    "age_seconds": item["age_seconds"],
                    "last_seen_at": item["last_seen_at"],
                })
            if not item["version_ok"]:
                recent = item["age_seconds"] <= _recent_mismatch_secs
                payload["issues"].append({
                    "platform": row["platform"],
                    "kind": "extension_version_mismatch",
                    "detail": (
                        "Chrome extension hook is still running an older bundle."
                        if recent
                        else "Chrome extension hook last reported an older bundle; waiting for a fresh heartbeat."
                    ),
                    "extension_version": current,
                    "expected_version": expected,
                    "age_seconds": item["age_seconds"],
                    "last_seen_at": item["last_seen_at"],
                    "owner_count": item["owner_count"],
                    "needs_new_event": not recent,
                })

    if browser_ingest_events_exists:
        rows = await _fetch_or_empty(
            "browser_ingest_events",
            """
            SELECT platform,
                   endpoint,
                   count(*)::int AS requests,
                   sum(observed_count)::int AS observed_count,
                   sum(stored_count)::int AS stored_count,
                   max(created_at) AS last_seen_at,
                   extract(epoch FROM now() - max(created_at))::int AS age_seconds,
                   (array_agg(NULLIF(metadata->>'extension_version', '') ORDER BY created_at DESC))[1]
                       AS extension_version
            FROM browser_ingest_events
            WHERE created_at >= now() - ($1::int * interval '1 hour')
            GROUP BY platform, endpoint
            ORDER BY last_seen_at DESC
            LIMIT 30
            """,
            _ingest_summary_hours,
        )
        for row in rows:
            current = row["extension_version"]
            item = {
                "platform": row["platform"],
                "endpoint": row["endpoint"],
                "requests": int(row["requests"] or 0),
                "observed_count": int(row["observed_count"] or 0),
                "stored_count": int(row["stored_count"] or 0),
                "last_seen_at": row["last_seen_at"],
                "age_seconds": int(row["age_seconds"] or 0),
                "extension_version": current,
                "version_ok": _extension_versions_match(current, expected),
            }
            payload["ingest"].append(item)
            if not item["version_ok"]:
                recent = item["age_seconds"] <= _recent_mismatch_secs
                payload["issues"].append({
                    "platform": row["platform"],
                    "endpoint": row["endpoint"],
                    "kind": "extension_version_mismatch",
                    "detail": (
                        "Browser ingest event came from an older extension bundle."
                        if recent
                        else "Last browser ingest event used an older extension bundle; waiting for a fresh event."
                    ),
                    "extension_version": current,
                    "expected_version": expected,
                    "age_seconds": item["age_seconds"],
                    "last_seen_at": item["last_seen_at"],
                    "requests": item["requests"],
                    "observed_count": item["observed_count"],
                    "stored_count": item["stored_count"],
                    "needs_new_event": not recent,
                })

        if payload.get("ingest"):
            _set_ingest_health(list(payload.get("ingest") or []))

        try:
            stale_seconds = int(os.getenv("BROWSER_CONTENT_STALE_WARN_SECONDS", "3600") or "3600")
        except Exception:
            stale_seconds = 3600
        content_gap_rows = await _fetch_or_empty(
            "browser_content_gap",
            """
            WITH selected(platform) AS (
                SELECT unnest($2::text[])
            ),
            heartbeat AS (
                SELECT selected.platform,
                       latest.created_at AS heartbeat_at,
                       latest.metadata
                FROM selected
                LEFT JOIN LATERAL (
                    SELECT created_at, metadata
                    FROM browser_ingest_events
                    WHERE endpoint = 'browser_heartbeat'
                      AND platform = selected.platform
                    ORDER BY created_at DESC
                    LIMIT 1
                ) latest ON TRUE
            ),
            content AS (
                SELECT selected.platform,
                       latest.created_at AS last_content_at
                FROM selected
                LEFT JOIN LATERAL (
                    SELECT created_at
                    FROM (
                        SELECT created_at
                        FROM browser_ingest_events
                        WHERE endpoint <> 'browser_heartbeat'
                          AND platform = selected.platform
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
                        UNION ALL
                        SELECT collected_at AS created_at
                        FROM media_items
                        WHERE source = selected.platform
                          AND collected_at IS NOT NULL
                    ) useful
                    ORDER BY created_at DESC
                    LIMIT 1
                ) latest ON TRUE
            )
            SELECT heartbeat.platform,
                   heartbeat.heartbeat_at,
                   extract(epoch FROM now() - heartbeat.heartbeat_at)::int AS heartbeat_age_seconds,
                   content.last_content_at,
                   extract(epoch FROM now() - content.last_content_at)::int AS content_age_seconds,
                   heartbeat.metadata->>'url' AS url,
                   heartbeat.metadata->>'health_status' AS health_status,
                   heartbeat.metadata->>'health_reason' AS health_reason,
                   heartbeat.metadata->>'extension_version' AS extension_version,
                   heartbeat.metadata->'content_counts' AS content_counts
            FROM heartbeat
            LEFT JOIN content ON content.platform = heartbeat.platform
            WHERE heartbeat.heartbeat_at < now() - ($1::int * interval '1 second')
               OR content.last_content_at IS NULL
               OR content.last_content_at < now() - ($1::int * interval '1 second')
            ORDER BY
              CASE
                WHEN heartbeat.heartbeat_at < now() - ($1::int * interval '1 second') THEN 0
                ELSE 1
              END,
              heartbeat.heartbeat_at DESC
            """,
            max(300, stale_seconds),
            ["instagram", "tiktok", "lemon8", "threads", "facebook", "x"],
            record_error=not bool((payload.get("ingest_health") or {}).get("content_active")),
        )
        for row in content_gap_rows:
            raw = dict(row)
            if "heartbeat_age_seconds" not in raw:
                continue
            heartbeat_age = int(raw.get("heartbeat_age_seconds") or 0)
            heartbeat_stale = heartbeat_age > max(300, stale_seconds)
            health_status = str(raw.get("health_status") or "")
            page_error = (not heartbeat_stale and health_status == "recoverable_error_shell")
            payload["issues"].append({
                "platform": raw.get("platform"),
                "kind": (
                    "browser_heartbeat_stale"
                    if heartbeat_stale else
                    "browser_page_error"
                    if page_error else
                    "browser_content_stale"
                ),
                "detail": (
                    "Browser extension heartbeat is stale; browser-driven scraping is not currently active."
                    if heartbeat_stale else
                    "Browser tab is alive, but the page is showing a recoverable error shell instead of usable content."
                    if page_error else
                    "Browser tab heartbeat is fresh, but no useful posts/profile/media/route "
                    "ingest has arrived within the expected window."
                ),
                "heartbeat_age_seconds": heartbeat_age,
                "last_content_at": raw.get("last_content_at"),
                "content_age_seconds": int(raw["content_age_seconds"]) if raw.get("content_age_seconds") is not None else None,
                "url": raw.get("url"),
                "health_status": raw.get("health_status"),
                "health_reason": raw.get("health_reason"),
                "extension_version": raw.get("extension_version"),
                "content_counts": raw.get("content_counts"),
                "stale_after_seconds": max(300, stale_seconds),
            })

    if payload.get("reload_url"):
        for issue in payload["issues"]:
            issue["extension_id"] = payload.get("extension_id")
            issue["reload_url"] = payload.get("reload_url")

    _browser_extension_add_tab_audit_page_errors(payload)

    if await _fetchval_or_none("browser_media_candidates_table", "SELECT to_regclass('browser_media_candidates')") is not None:
        rows = await _fetch_or_empty(
            "browser_media_candidates",
            """
            SELECT platform,
                   outcome,
                   count(*)::int AS candidates,
                   count(*) FILTER (WHERE needs_revisit)::int AS needs_revisit,
                   max(last_seen) AS last_seen_at,
                   extract(epoch FROM now() - max(last_seen))::int AS age_seconds
            FROM browser_media_candidates
            WHERE last_seen >= now() - ($1::int * interval '1 hour')
            GROUP BY platform, outcome
            ORDER BY platform, candidates DESC, last_seen_at DESC
            LIMIT 60
            """,
            _media_summary_hours,
        )
        payload["media_candidates"] = [
            {
                "platform": row["platform"],
                "outcome": row["outcome"],
                "candidates": int(row["candidates"] or 0),
                "needs_revisit": int(row["needs_revisit"] or 0),
                "last_seen_at": row["last_seen_at"],
                "age_seconds": int(row["age_seconds"] or 0),
            }
            for row in rows
        ]

    if await _fetchval_or_none("browser_media_revisit_queue_table", "SELECT to_regclass('browser_media_revisit_queue')") is not None:
        from src.dashboard.api.helpers import _tiktok_revisit_claim_timeout_seconds
        claim_timeout = _tiktok_revisit_claim_timeout_seconds()
        rows = await _fetch_or_empty(
            "browser_media_revisit_queue",
            """
            SELECT platform,
                   count(*) FILTER (
                     WHERE status IN ('pending', 'failed')
                       AND next_visit_at <= now()
                   )::int AS due,
                   count(*) FILTER (WHERE status = 'claimed')::int AS claimed,
                   count(*) FILTER (
                     WHERE status = 'claimed'
                       AND COALESCE(last_attempt_at, updated_at, created_at)
                           <= now() - ($1::int * interval '1 second')
                   )::int AS stale_claimed,
                   count(*) FILTER (WHERE status = 'pending')::int AS pending,
                   count(*) FILTER (WHERE status = 'failed')::int AS failed,
                   count(*) FILTER (WHERE status = 'unavailable')::int AS unavailable,
                   count(*) FILTER (WHERE status = 'completed')::int AS completed,
                   max(updated_at) AS last_seen_at
            FROM browser_media_revisit_queue
            GROUP BY platform
            ORDER BY due DESC, pending DESC, failed DESC, platform
            LIMIT 12
            """,
            claim_timeout,
        )
        payload["media_revisit_queue"] = [
            {
                "platform": row["platform"],
                "due": int(row["due"] or 0),
                "claimed": int(row["claimed"] or 0),
                "stale_claimed": int(row["stale_claimed"] or 0),
                "pending": int(row["pending"] or 0),
                "failed": int(row["failed"] or 0),
                "unavailable": int(row["unavailable"] or 0),
                "completed": int(row["completed"] or 0),
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        ]

    if await _fetchval_or_none("tiktok_browser_media_candidates_table", "SELECT to_regclass('tiktok_browser_media_candidates')") is not None:
        rows = await _fetch_or_empty(
            "tiktok_browser_media_candidates",
            """
            SELECT outcome,
                   count(*)::int AS candidates,
                   count(*) FILTER (WHERE needs_revisit)::int AS needs_revisit,
                   max(last_seen) AS last_seen_at
            FROM tiktok_browser_media_candidates
            WHERE last_seen >= now() - ($1::int * interval '1 hour')
            GROUP BY outcome
            ORDER BY candidates DESC, last_seen_at DESC
            LIMIT 12
            """,
            _media_summary_hours,
        )
        queue = None
        if await _fetchval_or_none("tiktok_browser_revisit_queue_table", "SELECT to_regclass('tiktok_browser_revisit_queue')") is not None:
            from src.dashboard.api.helpers import _tiktok_revisit_claim_timeout_seconds
            claim_timeout = _tiktok_revisit_claim_timeout_seconds()
            queue = await _fetchrow_or_none(
                "tiktok_browser_revisit_queue",
                """
                SELECT count(*) FILTER (
                         WHERE status IN ('pending', 'failed')
                           AND next_visit_at <= now()
                       )::int AS due,
                       count(*) FILTER (WHERE status = 'claimed')::int AS claimed,
                       count(*) FILTER (
                         WHERE status = 'claimed'
                           AND COALESCE(last_attempt_at, updated_at, created_at)
                               <= now() - ($1::int * interval '1 second')
                       )::int AS stale_claimed,
                       count(*) FILTER (WHERE status = 'pending')::int AS pending,
                       count(*) FILTER (WHERE status = 'failed')::int AS failed,
                       count(*) FILTER (WHERE status = 'unavailable')::int AS unavailable,
                       count(*) FILTER (WHERE status = 'completed')::int AS completed,
                       max(updated_at) AS last_seen_at
                FROM tiktok_browser_revisit_queue
                """,
                claim_timeout,
            )
        payload["tiktok_media"] = {
            "outcomes": [
                {
                    "outcome": row["outcome"],
                    "candidates": int(row["candidates"] or 0),
                    "needs_revisit": int(row["needs_revisit"] or 0),
                    "last_seen_at": row["last_seen_at"],
                }
                for row in rows
            ],
            "queue": dict(queue) if queue else None,
        }

    _suppress_shadowed_extension_mismatches(payload)
    return payload
