from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.env import env_float
from src.core.vault import VAULT_ROOT, blob_path_for_sha256


DEFAULT_DB_COMPARE_TIMEOUT_SECONDS = 10.0

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
    blob_fallbacks_by_source: Counter[str] = field(default_factory=Counter)
    raw_payloads_by_source: Counter[str] = field(default_factory=Counter)
    invalid_files: list[str] = field(default_factory=list)
    checksum_verification: bool = False
    sidecar_scan_limit: int | None = None
    blob_scan_limit: int | None = None
    db_comparison_enabled: bool = False
    db_compare_error: str | None = None
    db_compare_timeout_seconds: float | None = None
    db_media_rows_scanned: int = 0
    sidecar_media_keys_scanned: int = 0
    blob_files_scanned: int = 0
    artifact_states_by_source: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    db_file_errors_by_source: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    artifact_samples_by_state: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

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
            "blob_fallbacks_by_source": dict(sorted(self.blob_fallbacks_by_source.items())),
            "raw_payloads_by_source": dict(sorted(self.raw_payloads_by_source.items())),
            "invalid_files": self.invalid_files[:100],
            "checksum_verification": self.checksum_verification,
            "sidecar_scan_limit": self.sidecar_scan_limit,
            "blob_scan_limit": self.blob_scan_limit,
            "artifact_reconciliation": {
                "enabled": self.db_comparison_enabled,
                "db_compare_error": self.db_compare_error,
                "db_compare_timeout_seconds": self.db_compare_timeout_seconds,
                "db_media_rows_scanned": self.db_media_rows_scanned,
                "sidecar_media_keys_scanned": self.sidecar_media_keys_scanned,
                "blob_files_scanned": self.blob_files_scanned,
                "states_by_source": {
                    source: dict(sorted(counter.items()))
                    for source, counter in sorted(self.artifact_states_by_source.items())
                },
                "db_file_errors_by_source": {
                    source: dict(sorted(counter.items()))
                    for source, counter in sorted(self.db_file_errors_by_source.items())
                },
                "samples_by_state": {
                    state: samples[:100]
                    for state, samples in sorted(self.artifact_samples_by_state.items())
                },
            },
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
        if self.blob_fallbacks_by_source:
            lines.append("")
            lines.append("Blob fallback references:")
            lines.extend(f"  {source}: {count}" for source, count in sorted(self.blob_fallbacks_by_source.items()))
        if self.raw_payloads_by_source:
            lines.append("")
            lines.append("Raw payload references:")
            lines.extend(f"  {source}: {count}" for source, count in sorted(self.raw_payloads_by_source.items()))
        if self.db_comparison_enabled:
            lines.append("")
            lines.append("Artifact reconciliation:")
            if self.db_compare_error:
                lines.append(f"  DB compare error: {self.db_compare_error}")
            if self.db_compare_timeout_seconds is not None:
                lines.append(f"  DB compare timeout: {self.db_compare_timeout_seconds:g}s")
            lines.append(f"  DB media rows scanned: {self.db_media_rows_scanned}")
            lines.append(f"  Media sidecar keys scanned: {self.sidecar_media_keys_scanned}")
            lines.append(f"  Canonical blob files scanned: {self.blob_files_scanned}")
            if self.artifact_states_by_source:
                lines.append("  States by source:")
                for source, counter in sorted(self.artifact_states_by_source.items()):
                    details = ", ".join(f"{state}={count}" for state, count in sorted(counter.items()))
                    lines.append(f"    {source}: {details}")
            if self.db_file_errors_by_source:
                lines.append("  DB file reference problems:")
                for source, counter in sorted(self.db_file_errors_by_source.items()):
                    details = ", ".join(f"{state}={count}" for state, count in sorted(counter.items()))
                    lines.append(f"    {source}: {details}")
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
    if payload.get("artifact_kind") in {"raw", "raw_payload", "json", "jsonl", "compressed_jsonl"}:
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
    errors, _ = file_reference_errors_with_blob_fallback(
        payload,
        root,
        verify_checksums=verify_checksums,
    )
    return errors


def file_reference_errors_with_blob_fallback(
    payload: dict[str, Any],
    root: Path,
    *,
    verify_checksums: bool = False,
) -> tuple[list[str], bool]:
    info = payload.get("file")
    if not isinstance(info, dict):
        return ["file_missing"], False
    raw_path = info.get("path") or info.get("absolute_path")
    if not raw_path:
        return ["file_path_missing"], False
    path = _vault_file_path(raw_path, root)
    used_blob_fallback = False
    if not path.exists() or not path.is_file():
        blob_path = info.get("blob_path") or info.get("blob_absolute_path")
        if not blob_path:
            return ["file_missing"], False
        candidate = _vault_file_path(blob_path, root)
        if not candidate.exists() or not candidate.is_file():
            return ["file_missing"], False
        path = candidate
        used_blob_fallback = True

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
    return errors, used_blob_fallback


def scan_sidecars(
    root: str | Path | None = None,
    *,
    verify_checksums: bool = False,
    sidecar_limit: int | None = None,
) -> RebuildReport:
    vault_root = Path(root).resolve() if root else VAULT_ROOT
    report = RebuildReport(
        root=vault_root,
        checksum_verification=verify_checksums,
        sidecar_scan_limit=sidecar_limit,
    )
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

        file_errors, used_blob_fallback = file_reference_errors_with_blob_fallback(
            payload,
            vault_root,
            verify_checksums=verify_checksums,
        )
        for error in file_errors:
            report.file_errors_by_source[source][error] += 1
        if used_blob_fallback and not file_errors:
            report.blob_fallbacks_by_source[source] += 1

        if not missing and not file_errors:
            for table in rebuild_target_tables(payload):
                report.reconstructable_tables[table] += 1

    return report


def db_compare_timeout_seconds() -> float:
    return env_float(
        "REBUILD_REPORT_DB_COMPARE_TIMEOUT_SECONDS",
        DEFAULT_DB_COMPARE_TIMEOUT_SECONDS,
        min_value=0.1,
    )


async def compare_db_media_artifacts(
    report: RebuildReport,
    conn,
    root: str | Path | None = None,
    *,
    verify_checksums: bool = False,
    limit: int | None = None,
    sidecar_limit: int | None = None,
    blob_limit: int | None = None,
    db_fetch_timeout: float | None = None,
) -> RebuildReport:
    """Compare media_items rows with vault sidecars/files/blobs.

    This is intentionally read-only. It reports inventory states so repair code
    can be built against a measured contract later.
    """
    vault_root = Path(root).resolve() if root else report.root
    report.db_comparison_enabled = True
    report.blob_scan_limit = blob_limit
    timeout = db_compare_timeout_seconds() if db_fetch_timeout is None else db_fetch_timeout
    report.db_compare_timeout_seconds = timeout

    try:
        db_rows = await _fetch_media_rows(conn, limit=limit, timeout=timeout)
    except TimeoutError:
        report.db_compare_error = "db_fetch_timeout"
        _note_artifact_state(
            report,
            "unknown",
            "db_fetch_timeout",
            f"media_items>{timeout:g}s",
        )
        return report

    report.db_media_rows_scanned = len(db_rows)
    sidecars, sidecar_blob_refs = _scan_media_sidecar_index(vault_root, limit=sidecar_limit)
    report.sidecar_media_keys_scanned = len(sidecars)
    blob_files = _scan_blob_files(vault_root, limit=blob_limit)
    report.blob_files_scanned = len(blob_files)

    db_keys: set[tuple[str, str]] = set()
    referenced_blobs = set(sidecar_blob_refs)

    for row in db_rows:
        source = str(_row_get(row, "source") or "unknown")
        content_id = str(_row_get(row, "content_id") or "")
        key = (source, content_id)
        if content_id:
            db_keys.add(key)
            if key not in sidecars:
                _note_artifact_state(report, source, "db_only", _format_media_key(source, content_id))

        blob = _db_row_blob_path(row, vault_root)
        if blob is not None:
            referenced_blobs.add(_norm_path(blob, vault_root))

        for error in db_media_file_reference_errors(row, vault_root, verify_checksums=verify_checksums):
            report.db_file_errors_by_source[source][error] += 1
            _note_artifact_state(report, source, error, _format_media_key(source, content_id))

    for source, content_id in sorted(set(sidecars) - db_keys):
        _note_artifact_state(report, source, "sidecar_only", _format_media_key(source, content_id))

    for blob in sorted(blob_files - referenced_blobs):
        _note_artifact_state(report, "unknown", "blob_only", _safe_relative(Path(blob), vault_root))

    return report


def db_media_file_reference_errors(
    row,
    root: Path,
    *,
    verify_checksums: bool = False,
) -> list[str]:
    raw_path = _row_get(row, "file_path")
    if not raw_path:
        return ["file_path_missing"]
    path = _vault_file_path(raw_path, root)
    if not path.exists() or not path.is_file():
        return ["file_missing"]

    errors: list[str] = []
    expected_size = _row_get(row, "file_size")
    if expected_size is not None:
        try:
            if path.stat().st_size != int(expected_size):
                errors.append("file_size_mismatch")
        except Exception:
            errors.append("file_size_invalid")

    expected_sha = _row_get(row, "sha256")
    if verify_checksums and expected_sha:
        actual = _sha256_file(path)
        if str(expected_sha).lower() != actual:
            errors.append("file_sha256_mismatch")
    return errors


async def _fetch_media_rows(conn, *, limit: int | None = None, timeout: float | None = None) -> list:
    sql = (
        "SELECT source, content_id, filename, file_path, file_size, sha256 "
        "FROM media_items"
    )
    if limit is not None and limit > 0:
        sql += f" LIMIT {int(limit)}"
    else:
        sql += " ORDER BY source, content_id"
    fetch = conn.fetch(sql)
    if timeout is not None and timeout > 0:
        return list(await asyncio.wait_for(fetch, timeout=timeout))
    return list(await fetch)


def _scan_media_sidecar_index(
    root: Path,
    *,
    limit: int | None = None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], set[str]]:
    sidecar_root = root / "sidecars"
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    blob_refs: set[str] = set()
    if not sidecar_root.exists():
        return entries, blob_refs
    scanned = 0
    for path in _iter_sidecar_paths(sidecar_root, limit=limit):
        if limit is not None and limit > 0 and scanned >= limit:
            break
        scanned += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict) or payload.get("artifact_kind") != "media":
            continue
        source = str(payload.get("source") or "unknown")
        content_id = nested_get(payload, ("content", "id"))
        if content_id:
            entries[(source, str(content_id))] = payload
        blob_path = nested_get(payload, ("file", "blob_path")) or nested_get(payload, ("file", "blob_absolute_path"))
        if blob_path:
            blob_refs.add(_norm_path(_vault_file_path(blob_path, root), root))
    return entries, blob_refs


def _scan_blob_files(root: Path, *, limit: int | None = None) -> set[str]:
    blob_root = root / "media" / "blobs"
    if not blob_root.exists():
        return set()
    paths: set[str] = set()
    for path in blob_root.rglob("*"):
        if not path.is_file():
            continue
        if limit is not None and limit > 0 and len(paths) >= limit:
            break
        paths.add(_norm_path(path, root))
    return paths


def _iter_sidecar_paths(sidecar_root: Path, *, limit: int | None = None):
    paths = sidecar_root.rglob("*.json")
    if limit is not None and limit > 0:
        return paths
    return sorted(paths)


def _db_row_blob_path(row, root: Path) -> Path | None:
    sha = _row_get(row, "sha256")
    if not sha:
        return None
    try:
        return blob_path_for_sha256(str(sha), extension=Path(str(_row_get(row, "filename") or "")).suffix, root=root)
    except ValueError:
        return None


def _row_get(row, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)


def _note_artifact_state(report: RebuildReport, source: str, state: str, sample: str) -> None:
    report.artifact_states_by_source[source][state] += 1
    samples = report.artifact_samples_by_state[state]
    if sample and len(samples) < 100:
        samples.append(sample)


def _format_media_key(source: str, content_id: str) -> str:
    return f"{source}:{content_id}" if content_id else f"{source}:<missing-content-id>"


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


def _vault_file_path(path: Any, root: Path) -> Path:
    raw = str(path)
    normalized = raw.replace("\\", "/")
    if normalized == "/vault" or normalized.startswith("/vault/"):
        suffix = normalized.removeprefix("/vault").lstrip("/")
        return root / suffix
    if normalized == "/media" or normalized.startswith("/media/"):
        suffix = normalized.removeprefix("/media").lstrip("/")
        if root.as_posix().rstrip("/") == "/media":
            return Path(normalized)
        return root / "media" / suffix
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = root / resolved
    return resolved


def _norm_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(_vault_file_path(path, root))
