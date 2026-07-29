"""Repair media_items rows missing source-occurrence vault sidecar metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.vault import (
    VAULT_ROOT,
    blob_path_for_sha256,
    relative_to_vault,
    write_artifact_sidecar,
    write_media_sidecar,
)


_SHA256_HEX_RE = r"^[a-fA-F0-9]{64}$"


@dataclass
class MediaSidecarRepairReport:
    scanned: int = 0
    repaired: int = 0
    failed: int = 0
    skipped: int = 0
    already_ok: int = 0
    would_repair: int = 0
    file_missing: int = 0
    size_mismatch: int = 0
    next_cursor: str | None = None
    dry_run: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "repaired": self.repaired,
            "failed": self.failed,
            "skipped": self.skipped,
            "already_ok": self.already_ok,
            "would_repair": self.would_repair,
            "file_missing": self.file_missing,
            "size_mismatch": self.size_mismatch,
            "next_cursor": self.next_cursor,
            "dry_run": self.dry_run,
            "failures": self.failures,
        }


async def repair_missing_media_sidecars(
    conn,
    *,
    source: str | None = None,
    limit: int = 500,
    since_hours: int | None = None,
    cursor_after: str | None = None,
    timeout: float | None = 10.0,
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
        params.append(str(cursor_after or ""))
        where.append(f"content_id > ${len(params)}")
    elif cursor_after:
        raise ValueError("cursor_after requires source for media sidecar repair")
    if since_hours is not None:
        params.append(str(max(1, int(since_hours))))
        where.append(f"collected_at >= now() - (${len(params)} || ' hours')::interval")
    params.append(limit)
    order_by = "content_id" if source else "source, content_id"
    rows = await conn.fetch(
        f"""
        SELECT id, source, entity_id, entity_name, content_type, content_id,
               filename, file_path, file_size, width, height, sha256, source_url,
               metadata, ingest_path, kind, collected_at
        FROM media_items
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        LIMIT ${len(params)}
        """,
        *params,
        timeout=timeout,
    )

    for row in rows:
        report.scanned += 1
        d = dict(row)
        content_id = str(d.get("content_id") or "")
        if content_id:
            report.next_cursor = content_id
        resolved_file_path = _resolve_media_path(d.get("file_path"), root)
        if not resolved_file_path or not resolved_file_path.is_file():
            report.file_missing += 1
            report.skipped += 1
            _append_failure(
                report,
                {
                    "id": str(d.get("id")),
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": "media file is missing",
                    "file_path": str(resolved_file_path) if resolved_file_path else str(d.get("file_path") or ""),
                },
            )
            continue
        expected_size = d.get("file_size")
        if expected_size is not None and int(expected_size) != int(resolved_file_path.stat().st_size):
            report.size_mismatch += 1
            report.failed += 1
            _append_failure(
                report,
                {
                    "id": str(d.get("id")),
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": (
                        f"size mismatch: db={expected_size} "
                        f"file={resolved_file_path.stat().st_size}"
                    ),
                    "file_path": str(resolved_file_path),
                },
            )
            continue
        metadata = _coerce_metadata(d.get("metadata"))
        if dry_run:
            report.would_repair += 1
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
            file_path=str(resolved_file_path),
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
                _append_failure(
                    report,
                    {
                        "id": str(d.get("id")),
                        "source": d.get("source"),
                        "content_id": d.get("content_id"),
                        "error": sidecar.error,
                    },
                )
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
            _append_failure(
                report,
                {
                    "id": str(d.get("id")),
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": str(exc),
                },
            )
    return report


async def repair_partial_vault_artifacts(
    conn,
    *,
    source: str | None = None,
    limit: int = 100,
    cursor_after: str | None = None,
    timeout: float | None = 10.0,
    vault_root: str | Path | None = None,
    dry_run: bool = False,
) -> MediaSidecarRepairReport:
    """Repair rows where the canonical blob exists but its artifact sidecar failed."""

    limit = max(1, min(int(limit or 100), 1_000))
    root = Path(vault_root) if vault_root else VAULT_ROOT
    report = MediaSidecarRepairReport(dry_run=dry_run)

    where = [
        """
        (
            COALESCE(metadata, '{}'::jsonb) ? 'vault_sidecar'
            AND metadata->'vault_sidecar'->>'ok' = 'false'
        ) OR (
            COALESCE(metadata, '{}'::jsonb) ? 'vault_artifact'
            AND (
                metadata->'vault_artifact'->>'ok' = 'false'
                OR metadata->'vault_artifact'->>'sidecar_ok' = 'false'
                OR metadata->'vault_artifact'->>'partial' = 'true'
            )
        )
        """,
        "file_path IS NOT NULL",
        "file_path <> ''",
    ]
    params: list[Any] = []
    if source:
        params.append(source)
        where.append(f"source = ${len(params)}")
        params.append(str(cursor_after or ""))
        where.append(f"content_id > ${len(params)}")
    elif cursor_after:
        raise ValueError("cursor_after requires source for partial artifact repair")
    params.append(limit)
    order_by = "content_id" if source else "source, content_id"
    rows = await conn.fetch(
        f"""
        SELECT id, source, content_id, filename, file_path, file_size, sha256,
               source_url, metadata, ingest_path, collected_at
        FROM media_items
        WHERE {" AND ".join(f"({part})" for part in where)}
        ORDER BY {order_by}
        LIMIT ${len(params)}
        """,
        *params,
        timeout=timeout,
    )

    for row in rows:
        report.scanned += 1
        d = dict(row)
        media_id = str(d.get("id"))
        content_id = str(d.get("content_id") or "")
        if content_id:
            report.next_cursor = content_id
        file_path = _resolve_media_path(d.get("file_path"), root)
        if not file_path:
            report.file_missing += 1
            report.failed += 1
            _append_failure(
                report,
                {
                    "id": media_id,
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": "media file path is empty",
                },
            )
            continue
        if dry_run:
            report.skipped += 1
            continue
        try:
            if not file_path.is_file():
                raise RuntimeError("media file is missing")
            stat = file_path.stat()
            expected_size = d.get("file_size")
            if expected_size is not None and int(expected_size) != int(stat.st_size):
                report.size_mismatch += 1
                raise RuntimeError(f"size mismatch: db={expected_size} file={stat.st_size}")
            actual_sha = _sha256_path(file_path)
            expected_sha = str(d.get("sha256") or "").lower()
            if expected_sha and expected_sha != actual_sha:
                raise RuntimeError(f"sha256 mismatch: db={expected_sha} file={actual_sha}")
        except Exception as exc:  # noqa: BLE001 - continue repairing other rows
            report.failed += 1
            if "media file is missing" in str(exc):
                report.file_missing += 1
            _append_failure(
                report,
                {
                    "id": media_id,
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": str(exc),
                    "file_path": str(file_path),
                },
            )
            continue

        repaired_at = datetime.now(timezone.utc)
        metadata = _coerce_metadata(d.get("metadata"))
        previous_artifact = metadata.get("vault_artifact") if isinstance(metadata.get("vault_artifact"), dict) else {}
        metadata["artifact_sidecar_repair"] = {
            "repaired_at": repaired_at.isoformat(),
            "original_media_item_id": media_id,
            "previous_error": previous_artifact.get("error"),
        }
        sidecar = write_artifact_sidecar(
            source=str(d.get("source") or ""),
            artifact_kind="media_blob",
            file_path=str(file_path),
            metadata={
                **metadata,
                "content_id": d.get("content_id"),
                "filename": d.get("filename"),
                "source_url": d.get("source_url"),
                "ingest_path": d.get("ingest_path"),
                "repaired_by": "media_sidecar_repair",
                "rebuild_target_tables": ["media_items"],
            },
            file_size=int(stat.st_size),
            sha256=actual_sha,
            root=root,
        )
        artifact_meta = {
            "ok": sidecar.ok,
            "partial": not sidecar.ok,
            "path": relative_to_vault(file_path, root),
            "blob_path": relative_to_vault(file_path, root),
            "sidecar_path": sidecar.relative_path if sidecar.ok else None,
            "duplicate_blob": bool(previous_artifact.get("duplicate_blob", True)),
            "error": sidecar.error,
            "repaired_by": "media_sidecar_repair",
            "repaired_at": repaired_at.isoformat(),
        }
        try:
            await conn.execute(
                """
                UPDATE media_items
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb
                WHERE id = $1
                """,
                d.get("id"),
                json.dumps({"vault_artifact": artifact_meta}, default=str),
            )
            if sidecar.ok:
                report.repaired += 1
            else:
                report.failed += 1
                _append_failure(
                    report,
                    {
                        "id": media_id,
                        "source": d.get("source"),
                        "content_id": d.get("content_id"),
                        "error": sidecar.error,
                    },
                )
                await conn.execute(
                    """
                    INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
                    VALUES ($1, $2, $3, $4)
                    """,
                    d.get("source"),
                    media_id,
                    d.get("content_id"),
                    f"vault artifact sidecar repair failed: {sidecar.error}",
                )
        except Exception as exc:  # noqa: BLE001 - command should report all rows
            report.failed += 1
            _append_failure(
                report,
                {
                    "id": media_id,
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": str(exc),
                },
            )
    return report


async def repair_media_file_paths_from_blobs(
    conn,
    *,
    source: str,
    limit: int = 100,
    cursor_after: str | None = None,
    timeout: float | None = 10.0,
    vault_root: str | Path | None = None,
    dry_run: bool = False,
) -> MediaSidecarRepairReport:
    """Point stale/missing media rows back at an existing canonical sha256 blob.

    This is local-only recovery. It never downloads from a platform. When a row's
    current file path is missing or size-mismatched, the command looks for the
    canonical vault blob derived from ``sha256`` and writes fresh occurrence
    sidecar metadata for that row.
    """

    if not source:
        raise ValueError("source is required for media file path repair")
    limit = max(1, min(int(limit or 100), 1_000))
    root = Path(vault_root) if vault_root else VAULT_ROOT
    cursor = str(cursor_after or "")
    report = MediaSidecarRepairReport(dry_run=dry_run)

    rows = await conn.fetch(
        f"""
        SELECT id, source, entity_id, entity_name, content_type, content_id,
               filename, file_path, file_size, width, height, sha256, source_url,
               metadata, ingest_path, kind, collected_at
        FROM media_items
        WHERE source = $1
          AND content_id > $2
          AND content_id IS NOT NULL
          AND content_id <> ''
          AND file_path IS NOT NULL
          AND file_path <> ''
          AND sha256 ~ $4
        ORDER BY content_id
        LIMIT $3
        """,
        source,
        cursor,
        limit,
        _SHA256_HEX_RE,
        timeout=timeout,
    )

    for row in rows:
        report.scanned += 1
        d = dict(row)
        content_id = str(d.get("content_id") or "")
        if content_id:
            report.next_cursor = content_id

        current_path = _resolve_media_path(d.get("file_path"), root)
        expected_size = d.get("file_size")
        if current_path and current_path.is_file():
            actual_size = current_path.stat().st_size
            if expected_size is None or int(expected_size) == int(actual_size):
                report.already_ok += 1
                report.skipped += 1
                continue

        blob_path = _existing_blob_path(
            str(d.get("sha256") or ""),
            filename=d.get("filename"),
            legacy_path=d.get("file_path"),
            root=root,
        )
        if not blob_path:
            report.file_missing += 1
            report.failed += 1
            _append_failure(
                report,
                {
                    "id": str(d.get("id")),
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": "canonical sha256 blob is missing",
                    "file_path": str(current_path) if current_path else str(d.get("file_path") or ""),
                },
            )
            continue

        blob_size = blob_path.stat().st_size
        if dry_run:
            report.would_repair += 1
            report.skipped += 1
            continue

        repaired_at = datetime.now(timezone.utc)
        metadata = _coerce_metadata(d.get("metadata"))
        metadata["file_path_repair"] = {
            "repaired_at": repaired_at.isoformat(),
            "original_media_item_id": str(d.get("id")),
            "legacy_path": str(d.get("file_path") or ""),
            "blob_path": relative_to_vault(blob_path, root),
            "reason": "legacy file_path missing or mismatched; canonical sha256 blob exists",
        }
        sidecar = write_media_sidecar(
            source=str(d.get("source") or ""),
            entity_id=str(d.get("entity_id") or ""),
            entity_name=str(d.get("entity_name") or ""),
            content_type=str(d.get("content_type") or "unknown"),
            content_id=str(d.get("content_id") or ""),
            filename=str(d.get("filename") or blob_path.name),
            file_path=str(blob_path),
            file_size=blob_size,
            width=d.get("width"),
            height=d.get("height"),
            sha256=str(d.get("sha256") or ""),
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
            },
            "file_path_repair": metadata["file_path_repair"],
        }
        try:
            await conn.execute(
                """
                UPDATE media_items
                SET file_path = $2,
                    file_size = $3,
                    metadata = COALESCE(metadata, '{}'::jsonb) || $4::jsonb
                WHERE id = $1
                """,
                d.get("id"),
                str(blob_path),
                blob_size,
                json.dumps(sidecar_meta, default=str),
            )
            if sidecar.enabled and not sidecar.ok:
                report.failed += 1
                _append_failure(
                    report,
                    {
                        "id": str(d.get("id")),
                        "source": d.get("source"),
                        "content_id": d.get("content_id"),
                        "error": sidecar.error,
                    },
                )
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
            _append_failure(
                report,
                {
                    "id": str(d.get("id")),
                    "source": d.get("source"),
                    "content_id": d.get("content_id"),
                    "error": str(exc),
                },
            )

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


def _resolve_media_path(value: Any, root: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized_root = root.resolve(strict=False)
    if text.startswith("/vault/") and normalized_root.as_posix() != "/vault":
        return normalized_root / text.removeprefix("/vault/")
    if text == "/vault" and normalized_root.as_posix() != "/vault":
        return normalized_root
    if text.startswith("/media/") and (normalized_root / "media").exists():
        return normalized_root / "media" / text.removeprefix("/media/")
    path = Path(text)
    if path.is_absolute():
        return path
    return normalized_root / path


def _existing_blob_path(
    sha256: str,
    *,
    filename: Any,
    legacy_path: Any,
    root: Path,
) -> Path | None:
    digest = str(sha256 or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return None
    suffixes: list[str | None] = []
    for raw in (filename, legacy_path):
        suffix = Path(str(raw or "")).suffix
        if suffix and suffix.lower() not in suffixes:
            suffixes.append(suffix.lower())
    suffixes.append(None)
    for suffix in suffixes:
        try:
            candidate = blob_path_for_sha256(digest, extension=suffix, root=root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    blob_dir = root / "media" / "blobs" / digest[:2] / digest[2:4]
    try:
        matches = sorted(blob_dir.glob(f"{digest}.*"))
    except OSError:
        matches = []
    for match in matches:
        if match.is_file():
            return match
    return None


def _append_failure(report: MediaSidecarRepairReport, failure: dict[str, Any]) -> None:
    if len(report.failures) < 50:
        report.failures.append(failure)


def _sha256_path(path: Path, chunk_size: int = 1024 * 1024) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
