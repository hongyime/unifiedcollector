from __future__ import annotations

import json
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.vault import VAULT_ROOT


MEDIA_ITEM_FIELDS = {
    "source": ("source",),
    "entity_id": ("entity", "id"),
    "entity_name": ("entity", "name"),
    "content_type": ("content", "type"),
    "content_id": ("content", "id"),
    "filename": ("content", "filename"),
    "file_path": ("file", "path"),
    "file_size": ("file", "size"),
    "sha256": ("file", "sha256"),
}


@dataclass
class RebuildReport:
    root: Path
    sidecars_scanned: int = 0
    invalid_json: int = 0
    artifacts_by_kind: Counter[str] = field(default_factory=Counter)
    artifacts_by_source: Counter[str] = field(default_factory=Counter)
    reconstructable_tables: Counter[str] = field(default_factory=Counter)
    missing_fields_by_source: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    file_errors_by_source: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    raw_payloads_by_source: Counter[str] = field(default_factory=Counter)
    invalid_files: list[str] = field(default_factory=list)
    checksum_verification: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "sidecars_scanned": self.sidecars_scanned,
            "invalid_json": self.invalid_json,
            "artifacts_by_kind": dict(sorted(self.artifacts_by_kind.items())),
            "artifacts_by_source": dict(sorted(self.artifacts_by_source.items())),
            "reconstructable_tables": dict(sorted(self.reconstructable_tables.items())),
            "missing_fields_by_source": {
                source: dict(sorted(counter.items()))
                for source, counter in sorted(self.missing_fields_by_source.items())
            },
            "file_errors_by_source": {
                source: dict(sorted(counter.items()))
                for source, counter in sorted(self.file_errors_by_source.items())
            },
            "raw_payloads_by_source": dict(sorted(self.raw_payloads_by_source.items())),
            "invalid_files": self.invalid_files[:100],
            "checksum_verification": self.checksum_verification,
        }

    def to_text(self) -> str:
        lines = [
            f"Vault root: {self.root}",
            f"Sidecars scanned: {self.sidecars_scanned}",
            f"Invalid JSON: {self.invalid_json}",
            "",
            "Reconstructable tables:",
        ]
        if self.reconstructable_tables:
            lines.extend(f"  {table}: {count}" for table, count in sorted(self.reconstructable_tables.items()))
        else:
            lines.append("  none")
        lines.append("")
        lines.append("Artifacts by source:")
        if self.artifacts_by_source:
            lines.extend(f"  {source}: {count}" for source, count in sorted(self.artifacts_by_source.items()))
        else:
            lines.append("  none")
        if self.missing_fields_by_source:
            lines.append("")
            lines.append("Missing rebuild fields:")
            for source, counter in sorted(self.missing_fields_by_source.items()):
                details = ", ".join(f"{field}={count}" for field, count in sorted(counter.items()))
                lines.append(f"  {source}: {details}")
        if self.file_errors_by_source:
            lines.append("")
            lines.append("File reference problems:")
            for source, counter in sorted(self.file_errors_by_source.items()):
                details = ", ".join(f"{field}={count}" for field, count in sorted(counter.items()))
                lines.append(f"  {source}: {details}")
        if self.raw_payloads_by_source:
            lines.append("")
            lines.append("Raw payload references:")
            lines.extend(f"  {source}: {count}" for source, count in sorted(self.raw_payloads_by_source.items()))
        return "\n".join(lines)


def nested_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def missing_media_item_fields(payload: dict[str, Any]) -> list[str]:
    missing = []
    for field_name, path in MEDIA_ITEM_FIELDS.items():
        value = nested_get(payload, path)
        if value is None or value == "":
            missing.append(field_name)
    return missing


def missing_rebuild_required_fields(payload: dict[str, Any]) -> list[str]:
    rebuild = payload.get("rebuild")
    if not isinstance(rebuild, dict):
        return []
    fields = rebuild.get("required_fields")
    if not isinstance(fields, list):
        return []
    missing = []
    for field_name in fields:
        if not isinstance(field_name, str) or not field_name:
            continue
        value = nested_get(payload, tuple(field_name.split(".")))
        if value is None or value == "":
            missing.append(field_name)
    return missing


def rebuild_target_tables(payload: dict[str, Any]) -> list[str]:
    rebuild = payload.get("rebuild")
    if isinstance(rebuild, dict):
        tables = rebuild.get("target_tables") or rebuild.get("tables")
        if isinstance(tables, str):
            return [tables]
        if isinstance(tables, list):
            return [str(table) for table in tables if table]
    if payload.get("artifact_kind") == "media":
        return ["media_items"]
    return []


def raw_payload_reference_count(payload: dict[str, Any]) -> int:
    raw_payload = payload.get("raw_payload")
    count = 0
    if isinstance(raw_payload, dict):
        if raw_payload.get("inline") not in (None, "", {}, []):
            count += 1
        if raw_payload.get("path"):
            count += 1
    if payload.get("artifact_kind") in {"raw", "json", "jsonl", "compressed_jsonl"}:
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("raw_payload") is True:
            count += 1
    return count


def file_reference_errors(
    payload: dict[str, Any],
    root: Path,
    *,
    verify_checksums: bool = False,
) -> list[str]:
    info = payload.get("file")
    if not isinstance(info, dict):
        return ["file_missing"]
    raw_path = info.get("path") or info.get("absolute_path")
    if not raw_path:
        return ["file_path_missing"]
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = root / path
    if not path.exists() or not path.is_file():
        return ["file_missing"]

    errors: list[str] = []
    expected_size = info.get("size")
    if expected_size is not None:
        try:
            if path.stat().st_size != int(expected_size):
                errors.append("file_size_mismatch")
        except Exception:
            errors.append("file_size_invalid")
    expected_sha = info.get("sha256")
    if verify_checksums and expected_sha:
        actual = _sha256_file(path)
        if str(expected_sha).lower() != actual:
            errors.append("file_sha256_mismatch")
    return errors


def scan_sidecars(
    root: str | Path | None = None,
    *,
    verify_checksums: bool = False,
) -> RebuildReport:
    vault_root = Path(root).resolve() if root else VAULT_ROOT
    report = RebuildReport(root=vault_root, checksum_verification=verify_checksums)
    sidecar_root = vault_root / "sidecars"
    if not sidecar_root.exists():
        return report

    for path in sorted(sidecar_root.rglob("*.json")):
        report.sidecars_scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            report.invalid_json += 1
            report.invalid_files.append(_safe_relative(path, vault_root))
            continue
        if not isinstance(payload, dict):
            report.invalid_json += 1
            report.invalid_files.append(_safe_relative(path, vault_root))
            continue

        source = str(payload.get("source") or "unknown")
        kind = str(payload.get("artifact_kind") or "unknown")
        report.artifacts_by_source[source] += 1
        report.artifacts_by_kind[kind] += 1
        raw_count = raw_payload_reference_count(payload)
        if raw_count:
            report.raw_payloads_by_source[source] += raw_count

        missing = missing_rebuild_required_fields(payload)
        if kind == "media":
            missing.extend(field for field in missing_media_item_fields(payload) if field not in missing)

        for field_name in missing:
            report.missing_fields_by_source[source][field_name] += 1

        file_errors = file_reference_errors(payload, vault_root, verify_checksums=verify_checksums)
        for error in file_errors:
            report.file_errors_by_source[source][error] += 1

        if not missing and not file_errors:
            for table in rebuild_target_tables(payload):
                report.reconstructable_tables[table] += 1

    return report


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)
