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
    invalid_json: int = 0
    skipped_by_reason: Counter[str] = field(default_factory=Counter)
    inserted_by_source: Counter[str] = field(default_factory=Counter)
    sidecar_scan_limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "scratch_db": self.scratch_db,
            "sidecars_scanned": self.sidecars_scanned,
            "media_sidecars_seen": self.media_sidecars_seen,
            "media_rows_inserted": self.media_rows_inserted,
            "invalid_json": self.invalid_json,
            "skipped_by_reason": dict(sorted(self.skipped_by_reason.items())),
            "inserted_by_source": dict(sorted(self.inserted_by_source.items())),
            "sidecar_scan_limit": self.sidecar_scan_limit,
        }

    def to_text(self) -> str:
        lines = [
            f"Vault root: {self.root}",
            f"Scratch DB: {self.scratch_db}",
            f"Sidecars scanned: {self.sidecars_scanned}",
            f"Media sidecars seen: {self.media_sidecars_seen}",
            f"Media rows inserted: {self.media_rows_inserted}",
            f"Invalid JSON: {self.invalid_json}",
        ]
        if self.inserted_by_source:
            lines.append("")
            lines.append("Inserted by source:")
            lines.extend(
                f"  {source}: {count}"
                for source, count in sorted(self.inserted_by_source.items())
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
    verify_files: bool = True,
) -> MediaItemsRebuildRehearsal:
    """Materialize media sidecars into a scratch SQLite table.

    This is a production-safe rehearsal: it proves sidecars can become rows
    without connecting to or mutating the live collector database.
    """
    vault_root = Path(root).resolve() if root else VAULT_ROOT
    db_target = str(scratch_db) if scratch_db else ":memory:"
    report = MediaItemsRebuildRehearsal(
        root=vault_root,
        scratch_db=db_target,
        sidecar_scan_limit=sidecar_limit,
    )

    conn = sqlite3.connect(db_target)
    try:
        _create_media_items_table(conn)
        sidecar_root = vault_root / "sidecars"
        if not sidecar_root.exists():
            return report

        for path in _iter_sidecar_paths(sidecar_root, limit=sidecar_limit):
            if sidecar_limit is not None and sidecar_limit > 0 and report.sidecars_scanned >= sidecar_limit:
                break
            report.sidecars_scanned += 1
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
    errors.extend(file_reference_errors(payload, root, verify_checksums=verify_files))
    if errors:
        return {}, errors

    row = {
        field: nested_get(payload, path)
        for field, path in MEDIA_ITEM_FIELDS.items()
    }
    row["sidecar_path"] = str(sidecar_path)
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


def _iter_sidecar_paths(sidecar_root: Path, *, limit: int | None = None):
    roots = [
        path
        for path in sidecar_root.iterdir()
        if path.is_dir() and path.name != "artifacts"
    ]
    if not roots:
        roots = [sidecar_root]

    def generate():
        for root in sorted(roots):
            yield from root.rglob("*.json")

    paths = generate()
    if limit is not None and limit > 0:
        return paths
    return sorted(paths)
