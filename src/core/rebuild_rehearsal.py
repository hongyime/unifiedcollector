from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.rebuild_report import (
    MEDIA_ITEM_FIELDS,
    file_reference_errors,
    missing_media_item_fields,
    nested_get,
)
from src.core.vault import VAULT_ROOT


@dataclass
class MediaItemsRebuildRehearsal:
    root: Path
    scratch_db: str
    sidecars_scanned: int = 0
    media_sidecars_seen: int = 0
    media_rows_inserted: int = 0
    raw_payload_sidecars_seen: int = 0
    raw_payload_rows_inserted: int = 0
    invalid_json: int = 0
    skipped_by_reason: Counter[str] = field(default_factory=Counter)
    inserted_by_source: Counter[str] = field(default_factory=Counter)
    raw_payloads_by_source: Counter[str] = field(default_factory=Counter)
    sidecar_scan_limit: int | None = None
    raw_payload_scan_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "scratch_db": self.scratch_db,
            "sidecars_scanned": self.sidecars_scanned,
            "media_sidecars_seen": self.media_sidecars_seen,
            "media_rows_inserted": self.media_rows_inserted,
            "raw_payload_sidecars_seen": self.raw_payload_sidecars_seen,
            "raw_payload_rows_inserted": self.raw_payload_rows_inserted,
            "invalid_json": self.invalid_json,
            "skipped_by_reason": dict(sorted(self.skipped_by_reason.items())),
            "inserted_by_source": dict(sorted(self.inserted_by_source.items())),
            "raw_payloads_by_source": dict(sorted(self.raw_payloads_by_source.items())),
            "sidecar_scan_limit": self.sidecar_scan_limit,
            "raw_payload_scan_limit": self.raw_payload_scan_limit,
        }

    def to_text(self) -> str:
        lines = [
            f"Vault root: {self.root}",
            f"Scratch DB: {self.scratch_db}",
            f"Sidecars scanned: {self.sidecars_scanned}",
            f"Media sidecars seen: {self.media_sidecars_seen}",
            f"Media rows inserted: {self.media_rows_inserted}",
            f"Raw payload sidecars seen: {self.raw_payload_sidecars_seen}",
            f"Raw payload rows inserted: {self.raw_payload_rows_inserted}",
            f"Invalid JSON: {self.invalid_json}",
        ]
        if self.inserted_by_source:
            lines.append("")
            lines.append("Inserted by source:")
            lines.extend(
                f"  {source}: {count}"
                for source, count in sorted(self.inserted_by_source.items())
            )
        if self.raw_payloads_by_source:
            lines.append("")
            lines.append("Raw payloads by source:")
            lines.extend(
                f"  {source}: {count}"
                for source, count in sorted(self.raw_payloads_by_source.items())
            )
        if self.skipped_by_reason:
            lines.append("")
            lines.append("Skipped:")
            lines.extend(
                f"  {reason}: {count}"
                for reason, count in sorted(self.skipped_by_reason.items())
            )
        return "\n".join(lines)


def rehearse_media_items_rebuild(
    root: str | Path | None = None,
    *,
    scratch_db: str | Path | None = None,
    sidecar_limit: int | None = None,
    raw_payload_limit: int | None = None,
    verify_files: bool = True,
) -> MediaItemsRebuildRehearsal:
    """Materialize media and raw-payload sidecars into scratch SQLite tables.

    This is a production-safe rehearsal: it proves sidecars can become rows
    without connecting to or mutating the live collector database.
    """
    vault_root = Path(root).resolve() if root else VAULT_ROOT
    db_target = str(scratch_db) if scratch_db else ":memory:"
    report = MediaItemsRebuildRehearsal(
        root=vault_root,
        scratch_db=db_target,
        sidecar_scan_limit=sidecar_limit,
        raw_payload_scan_limit=sidecar_limit if raw_payload_limit is None else raw_payload_limit,
    )

    conn = sqlite3.connect(db_target)
    try:
        _create_media_items_table(conn)
        _create_raw_payloads_table(conn)
        sidecar_root = vault_root / "sidecars"
        if not sidecar_root.exists():
            return report

        media_scanned = 0
        for path in _iter_media_sidecar_paths(sidecar_root, limit=sidecar_limit):
            if _scan_limit_reached(media_scanned, sidecar_limit):
                break
            report.sidecars_scanned += 1
            media_scanned += 1
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                report.invalid_json += 1
                report.skipped_by_reason["invalid_json"] += 1
                continue
            if not isinstance(payload, dict) or payload.get("artifact_kind") != "media":
                continue
            report.media_sidecars_seen += 1
            row, errors = _media_item_row_from_sidecar(
                payload,
                vault_root,
                verify_files=verify_files,
                sidecar_path=path,
            )
            if errors:
                for error in errors:
                    report.skipped_by_reason[error] += 1
                continue
            inserted = _insert_media_item_row(conn, row)
            if inserted:
                report.media_rows_inserted += 1
                report.inserted_by_source[str(row["source"])] += 1
            else:
                report.skipped_by_reason["duplicate_source_content_id"] += 1

        artifacts_root = sidecar_root / "artifacts"
        raw_limit = sidecar_limit if raw_payload_limit is None else raw_payload_limit
        if artifacts_root.exists():
            raw_scanned = 0
            for path in _iter_raw_payload_sidecar_paths(artifacts_root, limit=raw_limit):
                if _scan_limit_reached(raw_scanned, raw_limit):
                    break
                report.sidecars_scanned += 1
                raw_scanned += 1
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    report.invalid_json += 1
                    report.skipped_by_reason["invalid_json"] += 1
                    continue
                if not isinstance(payload, dict) or payload.get("artifact_kind") != "raw_payload":
                    continue
                report.raw_payload_sidecars_seen += 1
                row, errors = _raw_payload_row_from_sidecar(
                    payload,
                    vault_root,
                    verify_files=verify_files,
                    sidecar_path=path,
                )
                if errors:
                    for error in errors:
                        report.skipped_by_reason[error] += 1
                    continue
                inserted = _insert_raw_payload_row(conn, row)
                if inserted:
                    report.raw_payload_rows_inserted += 1
                    report.raw_payloads_by_source[str(row["source"])] += 1
                else:
                    report.skipped_by_reason["duplicate_raw_payload"] += 1
        conn.commit()
    finally:
        conn.close()
    return report


def _create_media_items_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_items (
            source TEXT NOT NULL,
            entity_id TEXT,
            entity_name TEXT NOT NULL,
            content_type TEXT NOT NULL,
            content_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            sidecar_path TEXT NOT NULL,
            PRIMARY KEY (source, content_id)
        )
        """
    )


def _create_raw_payloads_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_payloads (
            source TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            sidecar_path TEXT NOT NULL,
            target_tables TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            collected_at TEXT,
            PRIMARY KEY (source, artifact_id, file_path)
        )
        """
    )


def _media_item_row_from_sidecar(
    payload: dict[str, Any],
    root: Path,
    *,
    verify_files: bool,
    sidecar_path: Path,
) -> tuple[dict[str, Any], list[str]]:
    errors = []
    missing = missing_media_item_fields(payload)
    errors.extend(f"missing_{field}" for field in missing)
    if verify_files:
        errors.extend(file_reference_errors(payload, root, verify_checksums=True))
    if errors:
        return {}, errors

    row = {
        field: nested_get(payload, path)
        for field, path in MEDIA_ITEM_FIELDS.items()
    }
    row["sidecar_path"] = str(sidecar_path)
    return row, []


def _raw_payload_row_from_sidecar(
    payload: dict[str, Any],
    root: Path,
    *,
    verify_files: bool,
    sidecar_path: Path,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    rebuild = payload.get("rebuild") if isinstance(payload.get("rebuild"), dict) else {}
    row = {
        "source": payload.get("source"),
        "artifact_id": metadata.get("artifact_id") or payload.get("artifact_id"),
        "artifact_kind": payload.get("artifact_kind"),
        "file_path": nested_get(payload, ("file", "path")),
        "file_size": nested_get(payload, ("file", "size")),
        "sha256": nested_get(payload, ("file", "sha256")),
        "sidecar_path": str(sidecar_path),
        "target_tables": rebuild.get("target_tables") if isinstance(rebuild.get("target_tables"), list) else [],
        "metadata_json": metadata,
        "collected_at": nested_get(payload, ("timestamps", "collected_at")),
    }
    required = ("source", "artifact_id", "artifact_kind", "file_path", "file_size", "sha256")
    for field in required:
        value = row.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing_{field}")
    if verify_files:
        errors.extend(file_reference_errors(payload, root, verify_checksums=True))
    if errors:
        return {}, errors
    return row, []


def _insert_media_item_row(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO media_items (
            source, entity_id, entity_name, content_type, content_id,
            filename, file_path, file_size, sha256, sidecar_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(row["source"]),
            None if row.get("entity_id") is None else str(row["entity_id"]),
            str(row["entity_name"]),
            str(row["content_type"]),
            str(row["content_id"]),
            str(row["filename"]),
            str(row["file_path"]),
            int(row["file_size"]),
            str(row["sha256"]),
            str(row["sidecar_path"]),
        ),
    )
    return cur.rowcount > 0


def _insert_raw_payload_row(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO raw_payloads (
            source, artifact_id, artifact_kind, file_path, file_size,
            sha256, sidecar_path, target_tables, metadata_json, collected_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(row["source"]),
            str(row["artifact_id"]),
            str(row["artifact_kind"]),
            str(row["file_path"]),
            int(row["file_size"]),
            str(row["sha256"]),
            str(row["sidecar_path"]),
            json.dumps(row["target_tables"], sort_keys=True),
            json.dumps(row["metadata_json"], sort_keys=True, default=str),
            None if row.get("collected_at") is None else str(row["collected_at"]),
        ),
    )
    return cur.rowcount > 0


def _scan_limit_reached(scanned: int, limit: int | None) -> bool:
    return limit is not None and limit > 0 and scanned >= limit


def _iter_media_sidecar_paths(sidecar_root: Path, *, limit: int | None = None):
    roots = [
        path
        for path in sidecar_root.iterdir()
        if path.is_dir() and path.name != "artifacts"
    ]

    def generate():
        if roots:
            for root in sorted(roots):
                yield from root.rglob("*.json")
            return
        yield from sidecar_root.glob("*.json")

    paths = generate()
    if limit is not None and limit > 0:
        return paths
    return sorted(paths)


def _iter_raw_payload_sidecar_paths(artifacts_root: Path, *, limit: int | None = None):
    paths = artifacts_root.rglob("*.json")
    if limit is not None and limit > 0:
        return paths
    return sorted(paths)
