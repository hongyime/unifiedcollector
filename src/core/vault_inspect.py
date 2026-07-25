"""DB-free vault inspection helpers for media, sidecars, and raw references."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.vault import VAULT_ROOT, relative_to_vault


@dataclass
class VaultInspectReport:
    root: str
    sidecars_scanned: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    invalid_json: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "sidecars_scanned": self.sidecars_scanned,
            "artifact_count": len(self.artifacts),
            "invalid_json": self.invalid_json,
            "missing_files": self.missing_files,
            "artifacts": self.artifacts,
        }

    def to_text(self) -> str:
        lines = [
            f"Vault root: {self.root}",
            f"Sidecars scanned: {self.sidecars_scanned}",
            f"Artifacts returned: {len(self.artifacts)}",
        ]
        if self.invalid_json:
            lines.append(f"Invalid sidecars: {len(self.invalid_json)}")
        if self.missing_files:
            lines.append(f"Missing referenced files: {len(self.missing_files)}")
        for artifact in self.artifacts:
            file_info = artifact.get("file") or {}
            raw_refs = artifact.get("raw_payload_refs") or []
            lines.append(
                "- "
                + f"{artifact.get('source')}/{artifact.get('artifact_kind')} "
                + f"{artifact.get('artifact_id')} -> {file_info.get('path')}"
                + (" [missing]" if file_info.get("exists") is False else "")
            )
            if raw_refs:
                lines.append(f"  raw refs: {len(raw_refs)}")
        return "\n".join(lines)


def inspect_vault(
    root: str | Path | None = None,
    *,
    source: str | None = None,
    limit: int = 20,
) -> VaultInspectReport:
    """Inspect vault sidecars without collector DB or analyzer access."""
    vault_root = Path(root).resolve() if root else VAULT_ROOT
    source_filter = str(source).strip().lower() if source else None
    limit = max(1, min(int(limit or 20), 500))
    report = VaultInspectReport(root=str(vault_root))
    sidecar_root = vault_root / "sidecars"
    if not sidecar_root.exists():
        return report

    for path in sidecar_root.rglob("*.json"):
        if len(report.artifacts) >= limit:
            break
        report.sidecars_scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            report.invalid_json.append(relative_to_vault(path, vault_root) or str(path))
            continue
        if not isinstance(payload, dict):
            continue
        payload_source = str(payload.get("source") or "").lower()
        if source_filter and payload_source != source_filter:
            continue
        artifact = _artifact_summary(payload, path, vault_root)
        if artifact["file"].get("exists") is False and artifact["file"].get("path"):
            report.missing_files.append(artifact["sidecar_path"])
        report.artifacts.append(artifact)
    return report


def _artifact_summary(payload: dict[str, Any], sidecar_path: Path, root: Path) -> dict[str, Any]:
    file_info = payload.get("file") if isinstance(payload.get("file"), dict) else {}
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
    rebuild = payload.get("rebuild") if isinstance(payload.get("rebuild"), dict) else {}
    raw_payload = payload.get("raw_payload") if isinstance(payload.get("raw_payload"), dict) else {}

    path = file_info.get("path")
    blob_path = file_info.get("blob_path")
    raw_refs = _raw_refs(raw_payload)
    return {
        "sidecar_path": relative_to_vault(sidecar_path, root) or str(sidecar_path),
        "source": payload.get("source"),
        "artifact_kind": payload.get("artifact_kind"),
        "artifact_id": payload.get("artifact_id"),
        "ingest_path": payload.get("ingest_path"),
        "collection_priority": payload.get("collection_priority"),
        "entity": {
            "id": entity.get("id"),
            "name": entity.get("name"),
        },
        "content": {
            "id": content.get("id"),
            "type": content.get("type"),
            "filename": content.get("filename"),
            "source_url": content.get("source_url"),
        },
        "file": {
            "path": path,
            "exists": _vault_path_exists(path, root),
            "blob_path": blob_path,
            "blob_exists": _vault_path_exists(blob_path, root) if blob_path else None,
            "size": file_info.get("size"),
            "sha256": file_info.get("sha256"),
        },
        "raw_payload_refs": raw_refs,
        "rebuild": {
            "target_tables": rebuild.get("target_tables") or [],
            "required_fields": rebuild.get("required_fields") or [],
        },
        "provenance": payload.get("provenance") or {},
    }


def _raw_refs(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    path = raw_payload.get("path")
    if path:
        refs.append({
            "path": path,
            "sidecar_path": raw_payload.get("sidecar_path"),
            "artifact_id": raw_payload.get("artifact_id"),
        })
    extra = raw_payload.get("refs")
    if isinstance(extra, list):
        for ref in extra:
            if isinstance(ref, dict) and any(ref.get(key) for key in ("path", "sidecar_path", "artifact_id")):
                if ref not in refs:
                    refs.append(ref)
    return refs


def _vault_path_exists(path: Any, root: Path) -> bool | None:
    if not path:
        return None
    raw = str(path)
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/vault/"):
        p = root / normalized[len("/vault/"):]
    elif normalized.startswith("/media/"):
        p = root / "media" / normalized[len("/media/"):]
    else:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
    return p.exists()
