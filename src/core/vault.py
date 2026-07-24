"""Collector vault helpers.

The collector DB is the live index, but the Z: vault is the durable evidence
store. This module centralizes path handling and per-artifact sidecar writes so
collectors do not each invent their own recovery metadata format.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SIDECARS_ENABLED = _env_bool("COLLECTOR_SIDECARS_ENABLED", True)


def _default_vault_root() -> Path:
    configured = os.getenv("COLLECTOR_VAULT_ROOT")
    if configured:
        return Path(configured).resolve()
    container_root = Path("/vault")
    if container_root.exists() and container_root.is_dir():
        return container_root.resolve()
    drive_path = Path(os.getenv("COLLECTOR_DRIVE_PATH", "Z:/unifiedcollector/media"))
    # Host default is Z:/unifiedcollector/media, so sidecars belong one level up.
    # Container default is /media, where only that directory is mounted; keep
    # sidecars under /media until the compose root-vault mount lands everywhere.
    if drive_path.as_posix() not in {"/media", "media"} and drive_path.name.lower() == "media":
        return drive_path.parent.resolve()
    return drive_path.resolve()


VAULT_ROOT = _default_vault_root()


@dataclass(frozen=True)
class VaultHealth:
    root: Path
    available: bool
    writable: bool
    free_bytes: int | None = None
    total_bytes: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class SidecarResult:
    enabled: bool
    ok: bool
    path: Path | None = None
    relative_path: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RawPayloadResult:
    ok: bool
    path: Path | None = None
    relative_path: str | None = None
    sidecar: SidecarResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class AtomicArtifactResult:
    ok: bool
    partial: bool
    source: str
    artifact_id: str
    artifact_kind: str
    sha256: str | None = None
    file_size: int | None = None
    path: Path | None = None
    relative_path: str | None = None
    blob_path: Path | None = None
    blob_relative_path: str | None = None
    sidecar: SidecarResult | None = None
    duplicate_blob: bool = False
    db_recorded: bool = False
    error: str | None = None


_SIDECAR_REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version",
    "artifact_kind",
    "artifact_id",
    "source",
    "ingest_path",
    "collection_priority",
    "entity",
    "content",
    "file",
    "timestamps",
    "raw_payload",
    "provenance",
    "metadata",
)

_SIDECAR_REQUIRED_NESTED_FIELDS = {
    "entity": ("id", "name"),
    "content": ("type", "kind", "id", "filename", "source_url", "caption", "text"),
    "file": ("path", "absolute_path", "size", "width", "height", "sha256"),
    "timestamps": ("collected_at", "posted_at", "discovered_at"),
    "raw_payload": ("inline", "path"),
    "provenance": (
        "platform_ids",
        "collection_account",
        "scrape_run_id",
        "extension_version",
        "request_url",
        "http_status",
        "rate_limit_scope",
        "partial",
    ),
}

_SIDECAR_REQUIRED_OBJECT_FIELDS = (*_SIDECAR_REQUIRED_NESTED_FIELDS.keys(), "metadata")

_SIDECAR_REQUIRED_VALUE_FIELDS = (
    "schema_version",
    "artifact_kind",
    "artifact_id",
    "source",
    "ingest_path",
    "entity.id",
    "content.type",
    "content.kind",
    "content.id",
    "content.filename",
    "file.path",
    "timestamps.collected_at",
)

_MEDIA_REBUILD_REQUIRED_FIELDS = (
    "source",
    "entity.id",
    "entity.name",
    "content.type",
    "content.id",
    "content.filename",
    "file.path",
    "file.size",
    "file.sha256",
)

_MISSING = object()


def _is_missing_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _sidecar_value(payload: Mapping[str, Any], field_path: str) -> Any:
    current: Any = payload
    for part in field_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def validate_sidecar_payload(payload: Any) -> list[str]:
    """Return schema errors for required collector vault sidecar fields."""
    if not isinstance(payload, Mapping):
        return ["sidecar payload must be a JSON object"]

    errors: list[str] = []
    missing = [field for field in _SIDECAR_REQUIRED_TOP_LEVEL_FIELDS if field not in payload]
    errors.extend(missing)

    for field in _SIDECAR_REQUIRED_OBJECT_FIELDS:
        if field in payload and not isinstance(payload[field], Mapping):
            errors.append(f"{field} must be an object")

    for section, fields in _SIDECAR_REQUIRED_NESTED_FIELDS.items():
        value = payload.get(section)
        if not isinstance(value, Mapping):
            continue
        errors.extend(f"{section}.{field}" for field in fields if field not in value)

    for field_path in _SIDECAR_REQUIRED_VALUE_FIELDS:
        if field_path in errors:
            continue
        value = _sidecar_value(payload, field_path)
        if value is _MISSING:
            continue
        if _is_missing_value(value):
            errors.append(f"{field_path} is required")

    schema_version = payload.get("schema_version")
    if "schema_version" not in missing and not _is_missing_value(schema_version) and schema_version != 1:
        errors.append("schema_version must be 1")

    artifact_kind = payload.get("artifact_kind")
    if "artifact_kind" not in missing and not _is_missing_value(artifact_kind) and artifact_kind != "media":
        errors.append("artifact_kind must be media")

    return errors


def validate_sidecar_file(path: str | os.PathLike[str]) -> list[str]:
    """Load a JSON sidecar file and return required-field schema errors."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"sidecar file unreadable: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"sidecar file invalid JSON: {exc.msg}"]
    return validate_sidecar_payload(payload)


def vault_health(root: Path = VAULT_ROOT) -> VaultHealth:
    """Return mount/writability health for the canonical vault root."""
    if not root.exists() or not root.is_dir():
        return VaultHealth(root=root, available=False, writable=False, error="vault root missing")
    free_bytes = None
    total_bytes = None
    try:
        usage = shutil.disk_usage(root)
        free_bytes = int(usage.free)
        total_bytes = int(usage.total)
    except OSError:
        pass
    probe = root / f".vault_check.{os.getpid()}.{time.time_ns()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return VaultHealth(root=root, available=True, writable=True, free_bytes=free_bytes, total_bytes=total_bytes)
    except OSError as exc:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return VaultHealth(
            root=root,
            available=True,
            writable=False,
            free_bytes=free_bytes,
            total_bytes=total_bytes,
            error=str(exc),
        )


async def vault_artifact_counts(conn, *, timeout: float | None = 10.0) -> dict[str, int]:
    """DB-backed artifact health counts used by dashboards and Telegram."""
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT COUNT(*)::int
           FROM dead_letter_queue
           WHERE error_message LIKE 'vault sidecar write failed:%') AS sidecar_failures,
          (SELECT COUNT(*)::int
           FROM dead_letter_queue
           WHERE status IN ('pending', 'in_progress')
             AND error_message LIKE 'vault sidecar write failed:%') AS artifacts_queued,
          (SELECT COUNT(*)::int
           FROM media_items
           WHERE metadata ? 'vault_sidecar'
             AND metadata->'vault_sidecar'->>'ok' = 'false') AS artifacts_partial
        """,
        timeout=timeout,
    )
    return {
        "sidecar_failures": int(row["sidecar_failures"] or 0),
        "artifacts_queued": int(row["artifacts_queued"] or 0),
        "artifacts_partial": int(row["artifacts_partial"] or 0),
    }


def ensure_vault_available(root: Path = VAULT_ROOT) -> None:
    health = vault_health(root)
    if not health.available or not health.writable:
        raise RuntimeError(f"collector vault unavailable: {health.error or root}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _media_root_from_env(root: Path = VAULT_ROOT) -> Path:
    return Path(os.getenv("COLLECTOR_DRIVE_PATH", str(root / "media"))).resolve(strict=False)


def _assert_media_root_mirrors_vault(media_root: Path, vault_media: Path) -> None:
    token = f"{os.getpid()}:{time.time_ns()}"
    name = f".vault_media_check.{os.getpid()}.{time.time_ns()}"
    media_probe = media_root / name
    vault_probe = vault_media / name
    try:
        media_probe.write_text(token, encoding="utf-8")
        if not vault_probe.exists() or vault_probe.read_text(encoding="utf-8") != token:
            raise RuntimeError(
                f"collector media root {media_root} is not linked to vault media {vault_media}"
            )
    finally:
        for probe in {media_probe, vault_probe}:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def assert_media_write_allowed(
    dest_path: str | os.PathLike[str],
    *,
    root: Path = VAULT_ROOT,
    media_root: str | os.PathLike[str] | None = None,
) -> None:
    """Fail closed before media/artifact writes.

    The container normally has both /vault (Z:/unifiedcollector) and /media
    (Z:/unifiedcollector/media) mounted. A healthy /vault alone is not enough:
    if /media is missing Docker/Python can create a local directory and the
    collector would falsely appear to be collecting. This guard verifies the
    destination, media root, and vault mirror relationship before file writes.
    """
    ensure_vault_available(root)

    root_path = root.resolve(strict=False)
    media_path = Path(media_root).resolve(strict=False) if media_root is not None else _media_root_from_env(root_path)
    if not media_path.exists() or not media_path.is_dir():
        raise RuntimeError(f"collector media root missing or not a directory: {media_path}")

    dest = Path(dest_path)
    if not dest.is_absolute():
        dest = media_path / dest
    dest = dest.resolve(strict=False)
    if not _is_relative_to(dest, media_path):
        raise RuntimeError(f"collector write destination escapes media root: {dest} not under {media_path}")

    if _is_relative_to(media_path, root_path):
        return

    vault_media = (root_path / "media").resolve(strict=False)
    if not vault_media.exists() or not vault_media.is_dir():
        raise RuntimeError(f"vault media mirror missing or not a directory: {vault_media}")
    _assert_media_root_mirrors_vault(media_path, vault_media)


def relative_to_vault(path: str | os.PathLike[str] | None, root: Path = VAULT_ROOT) -> str | None:
    """Best-effort vault-relative path for stable DB/sidecar references."""
    if not path:
        return None
    try:
        p = Path(path).resolve()
        return p.relative_to(root).as_posix()
    except Exception:
        return str(path)


_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.=-]+")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _safe_part(value: Any, *, fallback: str = "unknown", limit: int = 96) -> str:
    text = str(value or fallback).strip() or fallback
    text = _SAFE_PART_RE.sub("_", text)
    return text[:limit].strip("._") or fallback


def sidecar_path_for_media(
    *,
    source: str,
    content_id: str,
    collected_at: datetime | None = None,
    root: Path = VAULT_ROOT,
) -> Path:
    ts = collected_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (
        root
        / "sidecars"
        / _safe_part(source)
        / f"{ts:%Y}"
        / f"{ts:%m}"
        / f"{_safe_part(content_id, limit=140)}.json"
    )


def sidecar_path_for_artifact(
    *,
    source: str,
    artifact_id: str,
    artifact_kind: str,
    collected_at: datetime | None = None,
    root: Path = VAULT_ROOT,
) -> Path:
    ts = collected_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    digest = hashlib.sha256(artifact_id.encode("utf-8")).hexdigest()[:16]
    leaf = f"{digest}_{_safe_part(Path(artifact_id).name, fallback=artifact_kind, limit=96)}.json"
    return (
        root
        / "sidecars"
        / "artifacts"
        / _safe_part(source)
        / f"{ts:%Y}"
        / f"{ts:%m}"
        / leaf
    )


def raw_payload_path(
    *,
    source: str,
    artifact_id: str,
    extension: str = "json",
    collected_at: datetime | None = None,
    root: Path = VAULT_ROOT,
) -> Path:
    ts = collected_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    safe_ext = _safe_part(extension, fallback="json", limit=16).lstrip(".") or "json"
    digest = hashlib.sha256(f"{source}:{artifact_id}".encode("utf-8")).hexdigest()[:16]
    leaf = f"{digest}_{_safe_part(Path(artifact_id).name, fallback='payload', limit=96)}.{safe_ext}"
    return (
        root
        / "raw"
        / _safe_part(source)
        / f"{ts:%Y}"
        / f"{ts:%m}"
        / leaf
    )


def blob_path_for_sha256(
    sha256: str,
    *,
    extension: str | None = None,
    root: Path = VAULT_ROOT,
) -> Path:
    """Canonical future location for a physical media blob keyed by sha256.

    Source occurrences keep their own sidecars and DB rows. This path is only
    the shared physical-file target, so provenance is not lost when multiple
    sources resolve to the same bytes.
    """
    digest = str(sha256 or "").strip().lower()
    if not _SHA256_RE.match(digest):
        raise ValueError("sha256 must be a 64-character hex digest")

    suffix = ""
    if extension:
        clean_ext = _safe_part(str(extension).lower(), fallback="", limit=16).lstrip(".")
        if clean_ext:
            suffix = f".{clean_ext}"

    return root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}{suffix}"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            if content and not content.endswith("\n"):
                f.write("\n")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2, default=str)
            f.write("\n")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _raw_payload_text(payload: Any, *, extension: str) -> str:
    if extension == "jsonl":
        rows = payload if isinstance(payload, list) else [payload]
        return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n" for row in rows)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"


def _rebuild_contract(
    metadata: dict[str, Any],
    *,
    default_target_tables: list[str] | None = None,
    default_required_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    configured = metadata.get("rebuild")
    if isinstance(configured, Mapping):
        target_tables = configured.get("target_tables") or configured.get("tables")
        required_fields = configured.get("required_fields")
    else:
        target_tables = metadata.get("rebuild_target_tables") or metadata.get("rebuild_tables")
        required_fields = metadata.get("rebuild_required_fields")

    if isinstance(target_tables, str):
        target_tables = [target_tables]
    if not isinstance(target_tables, list):
        target_tables = default_target_tables or []
    target_tables = [str(table) for table in target_tables if table]

    if isinstance(required_fields, str):
        required_fields = [required_fields]
    if not isinstance(required_fields, list):
        required_fields = list(default_required_fields)
    required_fields = [str(field) for field in required_fields if field]

    return {
        "target_tables": target_tables,
        "required_fields": required_fields,
    }


def write_atomic_artifact(
    *,
    source: str,
    artifact_id: str,
    artifact_kind: str,
    data: bytes,
    extension: str | None = None,
    metadata: dict | None = None,
    expected_sha256: str | None = None,
    root: Path = VAULT_ROOT,
    db_writer=None,
) -> AtomicArtifactResult:
    """Write a file-backed artifact as one logical vault operation.

    This is the shared Phase-2 primitive for new migrations: bytes go to a temp
    file under the vault, the hash and size are verified, the file moves to the
    canonical sha256 blob path, a sidecar is written, and an optional DB callback
    records the occurrence. Any failure after a blob write is reported as
    ``partial=True`` so a repair queue can safely pick it up.
    """
    metadata = dict(metadata or {})
    try:
        ensure_vault_available(root)
    except Exception as exc:
        return AtomicArtifactResult(
            ok=False,
            partial=False,
            source=source,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            error=str(exc),
        )

    if not isinstance(data, (bytes, bytearray)):
        return AtomicArtifactResult(
            ok=False,
            partial=False,
            source=source,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            error="artifact data must be bytes",
        )

    payload = bytes(data)
    size = len(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 and expected_sha256.lower() != digest:
        return AtomicArtifactResult(
            ok=False,
            partial=False,
            source=source,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            sha256=digest,
            file_size=size,
            error="checksum mismatch before write",
        )

    try:
        blob_path = blob_path_for_sha256(digest, extension=extension, root=root)
    except Exception as exc:
        return AtomicArtifactResult(
            ok=False,
            partial=False,
            source=source,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            sha256=digest,
            file_size=size,
            error=str(exc),
        )

    tmp_dir = root / "media" / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{_safe_part(artifact_kind)}.", suffix=".tmp", dir=str(tmp_dir))
    tmp = Path(tmp_name)
    duplicate_blob = False
    blob_written = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if tmp.stat().st_size != size or _sha256_path(tmp) != digest:
            raise RuntimeError("checksum mismatch after temp write")

        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if blob_path.exists():
            duplicate_blob = True
            if blob_path.stat().st_size != size or _sha256_path(blob_path) != digest:
                raise RuntimeError("existing blob checksum mismatch")
            tmp.unlink(missing_ok=True)
        else:
            tmp.replace(blob_path)
            blob_written = True

        if blob_path.stat().st_size != size or _sha256_path(blob_path) != digest:
            raise RuntimeError("checksum mismatch after blob move")

        sidecar_meta = {
            **metadata,
            "original_artifact_id": artifact_id,
            "blob_path": relative_to_vault(blob_path, root),
            "sha256": digest,
            "file_size": size,
        }
        sidecar = write_artifact_sidecar(
            source=source,
            artifact_kind=artifact_kind,
            file_path=str(blob_path),
            metadata=sidecar_meta,
            root=root,
        )
        if not sidecar.ok:
            return AtomicArtifactResult(
                ok=False,
                partial=True,
                source=source,
                artifact_id=artifact_id,
                artifact_kind=artifact_kind,
                sha256=digest,
                file_size=size,
                path=blob_path,
                relative_path=relative_to_vault(blob_path, root),
                blob_path=blob_path,
                blob_relative_path=relative_to_vault(blob_path, root),
                sidecar=sidecar,
                duplicate_blob=duplicate_blob,
                error=f"sidecar write failed: {sidecar.error}",
            )

        result = AtomicArtifactResult(
            ok=True,
            partial=False,
            source=source,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            sha256=digest,
            file_size=size,
            path=blob_path,
            relative_path=relative_to_vault(blob_path, root),
            blob_path=blob_path,
            blob_relative_path=relative_to_vault(blob_path, root),
            sidecar=sidecar,
            duplicate_blob=duplicate_blob,
        )
        if db_writer is None:
            return result
        try:
            db_writer(result)
        except Exception as exc:
            return replace(result, ok=False, partial=True, error=f"db write failed: {exc}")
        return replace(result, db_recorded=True)
    except Exception as exc:
        return AtomicArtifactResult(
            ok=False,
            partial=blob_written or blob_path.exists(),
            source=source,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            sha256=digest,
            file_size=size,
            path=blob_path if blob_path.exists() else None,
            relative_path=relative_to_vault(blob_path, root) if blob_path.exists() else None,
            blob_path=blob_path if blob_path.exists() else None,
            blob_relative_path=relative_to_vault(blob_path, root) if blob_path.exists() else None,
            duplicate_blob=duplicate_blob,
            error=str(exc),
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def write_artifact_sidecar(
    *,
    source: str,
    artifact_kind: str,
    file_path: str,
    metadata: dict | None = None,
    root: Path = VAULT_ROOT,
) -> SidecarResult:
    """Write a sidecar for non-media artifacts such as raw/metadata JSON."""
    if not SIDECARS_ENABLED:
        return SidecarResult(enabled=False, ok=True)
    try:
        metadata = metadata or {}
        ensure_vault_available(root)
        path = Path(file_path)
        stat = path.stat()
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        rel = relative_to_vault(path, root)
        artifact_id = f"{source}:{rel or path.name}"
        now = datetime.now(timezone.utc)
        sidecar_path = sidecar_path_for_artifact(
            source=source,
            artifact_kind=artifact_kind,
            artifact_id=artifact_id,
            collected_at=now,
            root=root,
        )
        payload = {
            "schema_version": 1,
            "artifact_kind": artifact_kind,
            "artifact_id": artifact_id,
            "source": source,
            "file": {
                "path": rel,
                "absolute_path": str(path),
                "size": int(stat.st_size),
                "sha256": sha,
            },
            "timestamps": {
                "collected_at": now.isoformat(),
            },
            "metadata": metadata,
            "rebuild": _rebuild_contract(
                metadata,
                default_required_fields=("source", "artifact_kind", "file.path", "file.size", "file.sha256"),
            ),
        }
        _atomic_write_json(sidecar_path, payload)
        return SidecarResult(
            enabled=True,
            ok=True,
            path=sidecar_path,
            relative_path=relative_to_vault(sidecar_path, root),
        )
    except Exception as exc:
        return SidecarResult(enabled=True, ok=False, error=str(exc))


def write_raw_payload(
    *,
    source: str,
    artifact_id: str,
    payload: Any,
    metadata: dict | None = None,
    target_tables: list[str] | None = None,
    extension: str = "json",
    root: Path = VAULT_ROOT,
) -> RawPayloadResult:
    """Persist a raw API/browser/message/route payload under the vault raw tree."""
    try:
        ensure_vault_available(root)
        now = datetime.now(timezone.utc)
        ext = _safe_part(extension, fallback="json", limit=16).lstrip(".") or "json"
        path = raw_payload_path(
            source=source,
            artifact_id=artifact_id,
            extension=ext,
            collected_at=now,
            root=root,
        )
        _atomic_write_text(path, _raw_payload_text(payload, extension=ext))
        meta = dict(metadata or {})
        meta["raw_payload"] = True
        if target_tables is not None:
            meta["rebuild_target_tables"] = target_tables
        meta.setdefault(
            "rebuild_required_fields",
            ["source", "artifact_kind", "file.path", "file.size", "file.sha256"],
        )
        meta["artifact_id"] = artifact_id
        sidecar = write_artifact_sidecar(
            source=source,
            artifact_kind="raw_payload",
            file_path=str(path),
            metadata=meta,
            root=root,
        )
        return RawPayloadResult(
            ok=sidecar.ok,
            path=path,
            relative_path=relative_to_vault(path, root),
            sidecar=sidecar,
            error=sidecar.error,
        )
    except Exception as exc:
        return RawPayloadResult(ok=False, error=str(exc))


def write_media_sidecar(
    *,
    source: str,
    entity_id: str,
    entity_name: str,
    content_type: str,
    content_id: str,
    filename: str,
    file_path: str,
    file_size: int | None,
    width: int | None,
    height: int | None,
    sha256: str | None,
    source_url: str | None,
    metadata: dict | None,
    ingest_path: str | None,
    kind: str | None,
    root: Path = VAULT_ROOT,
) -> SidecarResult:
    """Write one JSON sidecar for a stored media artifact.

    Sidecars are intentionally source-occurrence records, not physical blob
    dedupe records. If the same sha256 appears through three sources, all three
    occurrences can have their own sidecar while sharing one physical blob.
    """
    if not SIDECARS_ENABLED:
        return SidecarResult(enabled=False, ok=True)
    try:
        metadata = metadata or {}
        ensure_vault_available(root)
        now = datetime.now(timezone.utc)
        sidecar_path = sidecar_path_for_media(source=source, content_id=content_id, collected_at=now, root=root)
        blob_path = None
        if sha256 and _SHA256_RE.match(str(sha256).strip()):
            blob_path = blob_path_for_sha256(sha256, extension=Path(filename or "").suffix, root=root)
        payload = {
            "schema_version": 1,
            "artifact_kind": "media",
            "artifact_id": f"{source}:{content_id}",
            "source": source,
            "ingest_path": ingest_path,
            "collection_priority": metadata.get("collection_priority"),
            "entity": {
                "id": entity_id,
                "name": entity_name,
            },
            "content": {
                "type": content_type,
                "kind": kind or "post",
                "id": content_id,
                "filename": filename,
                "source_url": source_url,
                "caption": metadata.get("caption"),
                "text": metadata.get("text"),
            },
            "file": {
                "path": relative_to_vault(file_path, root),
                "absolute_path": str(file_path) if file_path else None,
                "size": file_size,
                "width": width,
                "height": height,
                "sha256": sha256,
                "blob_path": relative_to_vault(blob_path, root) if blob_path else None,
                "blob_absolute_path": str(blob_path) if blob_path else None,
            },
            "timestamps": {
                "collected_at": now.isoformat(),
                "posted_at": metadata.get("posted_at") or metadata.get("timestamp"),
                "discovered_at": metadata.get("discovered_at"),
            },
            "raw_payload": {
                "inline": metadata.get("raw"),
                "path": metadata.get("raw_payload_path"),
            },
            "provenance": {
                "platform_ids": metadata.get("platform_ids"),
                "collection_account": metadata.get("collection_account"),
                "scrape_run_id": metadata.get("scrape_run_id"),
                "extension_version": metadata.get("extension_version"),
                "request_url": metadata.get("request_url"),
                "http_status": metadata.get("http_status"),
                "rate_limit_scope": metadata.get("rate_limit_scope"),
                "partial": False,
            },
            "metadata": metadata,
            "rebuild": _rebuild_contract(
                metadata,
                default_target_tables=["media_items"],
                default_required_fields=_MEDIA_REBUILD_REQUIRED_FIELDS,
            ),
        }
        validation_errors = validate_sidecar_payload(payload)
        if validation_errors:
            raise ValueError(f"invalid sidecar payload: {', '.join(validation_errors)}")
        _atomic_write_json(sidecar_path, payload)
        return SidecarResult(
            enabled=True,
            ok=True,
            path=sidecar_path,
            relative_path=relative_to_vault(sidecar_path, root),
        )
    except Exception as exc:
        return SidecarResult(enabled=True, ok=False, error=str(exc))
