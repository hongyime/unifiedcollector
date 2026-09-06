"""Vault / backup / drive health payload builders.

Extracted from ``src/dashboard/api/__init__.py`` during PERF-002 package split
(step 3 of ``docs/plans/perf-file-splits.md`` sub-plan 4A). Re-exported from
``__init__.py`` so ``from src.dashboard.api import _vault_payload`` still works.

These are pure functions that translate raw backup / vault / drive status
dicts into a normalized health-status string ("ok" / "degraded" / "error" /
etc.). They are called by the ``/health`` FastAPI route and its subroutes.
"""
from __future__ import annotations

from src.core.vault import vault_health


def _vault_payload() -> dict:
    health = vault_health()
    return {
        "root": str(health.root),
        "available": health.available,
        "writable": health.writable,
        "free_bytes": health.free_bytes,
        "total_bytes": health.total_bytes,
        "error": health.error,
        "sidecar_failures": 0,
        "artifacts_queued": 0,
        "artifacts_partial": 0,
        "artifacts_quarantined": 0,
        "artifacts_missing_sidecar": 0,
        "artifacts_missing_sidecar_estimated": False,
        "artifacts_missing_sidecar_recent_24h": 0,
    }


def _backup_health_status(backups: dict, *, include_storage: bool) -> str:
    if not include_storage:
        return "skipped_by_config"
    raw = str(backups.get("raw_status") or backups.get("status") or "").lower()
    if raw in {"skipped", "disabled", "off", "backup_disabled"}:
        return "backup_disabled"
    if raw in {"refreshing", "running", "in_progress", "backup_running"} or backups.get("in_progress") is True:
        return "backup_running"
    if raw in {"missing", "missing_restorable_dump"}:
        return "missing_restorable_dump"
    if raw in {"stale", "backup_stale"}:
        return "backup_stale"
    if raw in {"ok", "healthy", "backup_ok"}:
        return "backup_ok"
    if raw == "error" or backups.get("error"):
        return "error"
    return "degraded"


def _normalize_backup_health_payload(backups: dict, *, include_storage: bool) -> dict:
    normalized = dict(backups)
    raw_status = normalized.get("status")
    health_status = _backup_health_status(normalized, include_storage=include_storage)
    normalized["raw_status"] = raw_status
    normalized["status"] = health_status
    normalized["health_status"] = health_status
    return normalized


def _vault_health_status(vault: dict, *, include_storage: bool) -> str:
    if not include_storage:
        return "skipped_by_config"
    if vault.get("mode") == "error" or vault.get("error"):
        return "error"
    if vault.get("available") is False or vault.get("writable") is False:
        return "blocked"
    if vault.get("available") is not True or vault.get("writable") is not True:
        return "degraded"
    if vault.get("counts_error") or vault.get("counts_partial"):
        return "degraded"
    if int(vault.get("artifacts_queued") or 0) > 0 or int(vault.get("artifacts_partial") or 0) > 0:
        return "degraded"
    return "ok"


def _drive_health_status(drive_ok: bool, *, include_storage: bool) -> str:
    if not include_storage:
        return "skipped_by_config"
    return "ok" if drive_ok else "blocked"
