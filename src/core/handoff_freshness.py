"""Compare the shared handoff marker with live operational evidence."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


_MARKER = re.compile(r"<!-- handoff-freshness:\s*([^>]+)\s*-->")
_OBSERVATION_SLOP = timedelta(seconds=60)


def _marker_at(state_path: Path) -> datetime | None:
    try:
        contents = state_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _MARKER.search(contents)
    if not match:
        return None
    try:
        value = datetime.fromisoformat(match.group(1).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def check_handoff_freshness(conn, *, state_path: Path) -> dict:
    """Return a bounded live snapshot and fail when it outdates STATE's marker."""
    marker_at = _marker_at(state_path)
    checks = {
        "source_health": ("source_health", "SELECT max(updated_at) AS changed_at, count(*) FILTER (WHERE status <> 'running')::int AS attention_count FROM source_health"),
        "backfill_lanes": ("backfill_lanes", "SELECT max(updated_at) AS changed_at, count(*) FILTER (WHERE status='pending')::int AS pending_count, count(*) FILTER (WHERE status='leased' AND lease_expires_at > now())::int AS active_leases FROM backfill_lanes"),
        "capability_receipts": ("source_capability_receipts", "SELECT max(created_at) AS changed_at, count(*)::int AS receipt_count FROM source_capability_receipts"),
    }
    snapshot: dict[str, dict] = {}
    newest: datetime | None = None
    for name, (table, sql) in checks.items():
        if await conn.fetchval("SELECT to_regclass($1)", table) is None:
            snapshot[name] = {"available": False}
            continue
        row = dict(await conn.fetchrow(sql))
        changed_at = row.pop("changed_at")
        if changed_at and (newest is None or changed_at > newest):
            newest = changed_at
        snapshot[name] = {"available": True, "changed_at": changed_at, **row}
    ok = marker_at is not None and (newest is None or marker_at + _OBSERVATION_SLOP >= newest)
    return {
        "ok": ok,
        "state_marker_at": marker_at,
        "live_changed_at": newest,
        "observation_slop_seconds": int(_OBSERVATION_SLOP.total_seconds()),
        "reason": None if ok else (
            "STATE.md is missing a handoff-freshness marker"
            if marker_at is None else "live operational state is newer than STATE.md"
        ),
        "snapshot": snapshot,
    }
