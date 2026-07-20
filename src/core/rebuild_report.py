from __future__ import annotations

import json
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
    invalid_files: list[str] = field(default_factory=list)

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
            "invalid_files": self.invalid_files[:100],
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


def scan_sidecars(root: str | Path | None = None) -> RebuildReport:
    vault_root = Path(root).resolve() if root else VAULT_ROOT
    report = RebuildReport(root=vault_root)
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

        if kind == "media":
            missing = missing_media_item_fields(payload)
            if missing:
                for field_name in missing:
                    report.missing_fields_by_source[source][field_name] += 1
            else:
                report.reconstructable_tables["media_items"] += 1

    return report


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)
