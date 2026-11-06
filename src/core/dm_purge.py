"""One-shot, deliberately narrow direct-message purge helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.vault import VAULT_ROOT


_DM_DLQ_WHERE = """
source IN ('dm', 'direct_message')
OR content_id LIKE 'raw:extension/dm_%'
OR lower(COALESCE(error_message, '')) LIKE '%dm_%'
"""


async def purge_direct_message_rows(conn) -> dict[str, dict[str, int]]:
    """Delete only DM tables and DM-labelled DLQ rows in the caller's transaction."""
    before: dict[str, int] = {}
    for table in ("tiktok_dm", "tiktok_dm_thread", "dm_hook_heartbeat", "dm_probe_log"):
        before[table] = int(await conn.fetchval(f"SELECT count(*) FROM {table}") or 0)
        await conn.execute(f"DELETE FROM {table}")
    before["dm_dlq"] = int(await conn.fetchval(
        f"SELECT count(*) FROM dead_letter_queue WHERE {_DM_DLQ_WHERE}"
    ) or 0)
    await conn.execute(f"DELETE FROM dead_letter_queue WHERE {_DM_DLQ_WHERE}")
    before["dm_health"] = int(await conn.fetchval(
        "SELECT count(*) FROM source_health WHERE source IN ('dm', 'direct_message')"
    ) or 0)
    await conn.execute("DELETE FROM source_health WHERE source IN ('dm', 'direct_message')")
    for table in ("instagram_dm", "instagram_dm_thread"):
        if await conn.fetchval("SELECT to_regclass($1)", table):
            before[table] = int(await conn.fetchval(f"SELECT count(*) FROM {table}") or 0)
            await conn.execute(f"DELETE FROM {table}")
    return {"before": before, "after": {table: 0 for table in before}}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def purge_direct_message_artifacts(
    root: Path = VAULT_ROOT,
    sample_root: Path = Path("/tmp/dm_samples"),
) -> dict[str, Any]:
    """Remove validated DM raw artifacts and TikTok sample frames, never media."""
    root = root.resolve(strict=False)
    sample_root = sample_root.resolve(strict=False)
    report: dict[str, Any] = {"vault_artifacts": 0, "vault_sidecars": 0, "samples": 0, "failures": 0}
    sidecars = root / "sidecars"
    if sidecars.is_dir():
        for sidecar in sidecars.rglob("*.json"):
            if not _within(sidecar, sidecars):
                continue
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                endpoint = str((payload.get("metadata") or {}).get("endpoint") or "")
                artifact = payload.get("file") or {}
                if payload.get("artifact_kind") != "raw_payload" or not endpoint.startswith("dm_"):
                    continue
                raw_path = Path(str(artifact.get("absolute_path") or ""))
                if not raw_path.is_absolute():
                    raw_path = root / str(artifact.get("path") or "")
                if not raw_path.name or not _within(raw_path, root):
                    report["failures"] += 1
                    continue
                if raw_path.exists():
                    raw_path.unlink()
                    report["vault_artifacts"] += 1
                sidecar.unlink()
                report["vault_sidecars"] += 1
            except OSError:
                report["failures"] += 1
    if sample_root.is_dir():
        for sample in sample_root.glob("tiktok_*.bin"):
            if not _within(sample, sample_root):
                report["failures"] += 1
                continue
            try:
                sample.unlink()
                report["samples"] += 1
            except OSError:
                report["failures"] += 1
    return report
