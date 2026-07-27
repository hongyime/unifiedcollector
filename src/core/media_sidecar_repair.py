"""Repair media_items rows missing source-occurrence vault sidecar metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.vault import VAULT_ROOT, write_media_sidecar


@dataclass
class MediaSidecarRepairReport:
    scanned: int = 0
    repaired: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "repaired": self.repaired,
            "failed": self.failed,
            "skipped": self.skipped,
            "dry_run": self.dry_run,
            "failures": self.failures,
        }


async def repair_missing_media_sidecars(
    conn,
    *,
    source: str | None = None,
    limit: int = 500,
    since_hours: int | None = None,
    vault_root: str | Path | None = None,
    dry_run: bool = False,
) -> MediaSidecarRepairReport:
    """Write missing occurrence sidecars and update media_items metadata.

    This does not dedupe or move media files. It repairs the per-occurrence JSON
    contract for rows that already point at a stored media artifact.
    """

    limit = max(1, min(int(limit or 500), 10_000))
    root = Path(vault_root) if vault_root else VAULT_ROOT
    report = MediaSidecarRepairReport(dry_run=dry_run)

    where = [
        """
        NOT (
            COALESCE(metadata, '{}'::jsonb) ? 'vault_sidecar'
            OR (
                COALESCE(metadata, '{}'::jsonb) ? 'vault_artifact'
                AND COALESCE(metadata->'vault_artifact'->>'sidecar_path', '') <> ''
            )
        )
        """,
        "file_path IS NOT NULL",
        "file_path <> ''",
        "content_id IS NOT NULL",
        "content_id <> ''",
    ]
    params: list[Any] = []
    if source:
        params.append(source)
        where.append(f"source = ${len(params)}")
    if since_hours is not None:
        params.append(str(max(1, int(since_hours))))
        where.append(f"collected_at >= now() - (${len(params)} || ' hours')::interval")
    params.append(limit)
    rows = await conn.fetch(
        f"""
        SELECT id, source, entity_id, entity_name, content_type, content_id,
               filename, file_path, file_size, width, height, sha256, source_url,
               metadata, ingest_path, kind, collected_at
        FROM media_items
        WHERE {" AND ".join(where)}
        ORDER BY collected_at DESC NULLS LAST
        LIMIT ${len(params)}
        """,
        *params,
    )

    for row in rows:
        report.scanned += 1
        d = dict(row)
        metadata = _coerce_metadata(d.get("metadata"))
        if dry_run:
            report.skipped += 1
            continue
        repaired_at = datetime.now(timezone.utc)
        metadata["sidecar_repair"] = {
            "repaired_at": repaired_at.isoformat(),
            "original_media_item_id": str(d.get("id")),
            "original_collected_at": _iso_or_none(d.get("collected_at")),
        }
        sidecar = write_media_sidecar(
            source=str(d.get("source") or ""),
            entity_id=str(d.get("entity_id") or ""),
            entity_name=str(d.get("entity_name") or ""),
            content_type=str(d.get("content_type") or "unknown"),
            content_id=str(d.get("content_id") or ""),
            filename=str(d.get("filename") or ""),
            file_path=str(d.get("file_path") or ""),
            file_size=d.get("file_size"),
            width=d.get("width"),
            height=d.get("height"),
            sha256=d.get("sha256"),
            source_url=d.get("source_url"),
            metadata=metadata,
            ingest_path=d.get("ingest_path"),
            kind=d.get("kind"),
            root=root,
        )
        sidecar_meta = {
            "vault_sidecar": {
                "enabled": sidecar.enabled,
                "ok": sidecar.ok,
                "path": sidecar.relative_path,
                "error": sidecar.error,
                "repaired": True,
                "repaired_at": repaired_at.isoformat(),
            }
        }
        try:
            await conn.execute(
                """
                UPDATE media_items
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb
                WHERE id = $1
                """,
                d.get("id"),
                json.dumps(sidecar_meta, default=str),
            )
            if sidecar.enabled and not sidecar.ok:
                report.failed += 1
                report.failures.append({
                    "id": str(d.get("id")),
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": sidecar.error,
                })
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    d.get("source"),
                    d.get("entity_id"),
                    d.get("content_id"),
                    f"vault sidecar repair failed: {sidecar.error}",
                )
            else:
                report.repaired += 1
        except Exception as exc:  # noqa: BLE001 - command should report all rows
            report.failed += 1
            report.failures.append({
                "id": str(d.get("id")),
                "source": d.get("source"),
                "content_id": d.get("content_id"),
                "error": str(exc),
            })
    return report


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None


def _coerce_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {"legacy_metadata": value}
        return dict(parsed) if isinstance(parsed, dict) else {"legacy_metadata": parsed}
    return {}
