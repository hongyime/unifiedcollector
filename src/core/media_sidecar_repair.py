"""Repair media_items rows missing source-occurrence vault sidecar metadata."""

from __future__ import annotations

import json
import asyncio
import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from src.core import media_filter
from src.core.vault import (
    VAULT_ROOT,
    blob_path_for_sha256,
    relative_to_vault,
    write_atomic_artifact,
    write_artifact_sidecar,
    write_media_sidecar,
)


_SHA256_HEX_RE = r"^[a-fA-F0-9]{64}$"
_SAFE_DIRECT_RECOVERY_SOURCES = {"search", "website", "youtube"}
_PLATFORM_BACKFILL_ONLY_SOURCES = {
    "beeper": "beeper attachment URLs require authenticated asset-serve backfill",
    "tiktok": "tiktok media URLs are signed/ephemeral; rerun platform backfill",
}
MISSING_MEDIA_RECOVERY_SOURCES = tuple(sorted(_SAFE_DIRECT_RECOVERY_SOURCES | set(_PLATFORM_BACKFILL_ONLY_SOURCES)))
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic"}
_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
_RECOVERY_USER_AGENT = "UnifiedCollector missing-media-recovery/1.0"


@dataclass
class _ValidatedDownload:
    ok: bool
    url: str
    data: bytes = b""
    kind: str | None = None
    media_type: str | None = None
    content_type: str | None = None
    error: str | None = None
    status_code: int | None = None
    final_url: str | None = None


@dataclass
class MediaSidecarRepairReport:
    scanned: int = 0
    repaired: int = 0
    redownloaded: int = 0
    canonical_blob_available: int = 0
    failed: int = 0
    skipped: int = 0
    already_ok: int = 0
    would_repair: int = 0
    file_missing: int = 0
    size_mismatch: int = 0
    unsafe_response: int = 0
    platform_backfill_required: int = 0
    no_direct_url: int = 0
    queued_backfill: int = 0
    next_cursor: str | None = None
    dry_run: bool = False
    sources: dict[str, dict[str, int]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "repaired": self.repaired,
            "redownloaded": self.redownloaded,
            "canonical_blob_available": self.canonical_blob_available,
            "failed": self.failed,
            "skipped": self.skipped,
            "already_ok": self.already_ok,
            "would_repair": self.would_repair,
            "file_missing": self.file_missing,
            "size_mismatch": self.size_mismatch,
            "unsafe_response": self.unsafe_response,
            "platform_backfill_required": self.platform_backfill_required,
            "no_direct_url": self.no_direct_url,
            "queued_backfill": self.queued_backfill,
            "next_cursor": self.next_cursor,
            "dry_run": self.dry_run,
            "sources": self.sources,
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
        """
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


async def recover_missing_media_files(
    conn,
    *,
    source: str | None = None,
    limit: int = 25,
    cursor_after: str | None = None,
    timeout: float | None = 10.0,
    vault_root: str | Path | None = None,
    dry_run: bool = False,
    max_bytes: int = 50 * 1024 * 1024,
    request_timeout: float = 20.0,
    delay_seconds: float = 0.25,
    queue_platform_backfill: bool = False,
    client_factory: Callable[..., Any] | None = None,
) -> MediaSidecarRepairReport:
    """Recover missing/stale media files only through source-aware safe paths.

    This command is intentionally narrow. It never performs a generic untrusted
    GET and never writes bytes unless both the response content-type and magic
    bytes validate as image/video media. Beeper and TikTok rows are report-only
    because their attachment/media URLs are authenticated, signed, or ephemeral.
    """

    limit = max(1, min(int(limit or 25), 500))
    max_bytes = max(1, int(max_bytes or 1))
    root = Path(vault_root) if vault_root else VAULT_ROOT
    report = MediaSidecarRepairReport(dry_run=dry_run)

    where = [
        "content_id IS NOT NULL",
        "content_id <> ''",
        "file_path IS NOT NULL",
        "file_path <> ''",
    ]
    params: list[Any] = []
    if source:
        params.append(source)
        where.append(f"source = ${len(params)}")
        params.append(str(cursor_after or ""))
        where.append(f"content_id > ${len(params)}")
        order_by = "content_id"
    elif cursor_after:
        raise ValueError("cursor_after requires source for missing media recovery")
    else:
        params.append(list(MISSING_MEDIA_RECOVERY_SOURCES))
        where.append(f"source = ANY(${len(params)}::text[])")
        order_by = "source, content_id"
    params.append(limit)

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
        source_name = str(d.get("source") or "")
        _bump_source(report, source_name, "scanned")
        content_id = str(d.get("content_id") or "")
        if content_id:
            report.next_cursor = content_id

        current_path = _resolve_media_path(d.get("file_path"), root)
        if current_path and current_path.is_file():
            expected_size = d.get("file_size")
            actual_size = current_path.stat().st_size
            if expected_size is None or int(expected_size) == int(actual_size):
                report.already_ok += 1
                report.skipped += 1
                _bump_source(report, source_name, "already_ok")
                _bump_source(report, source_name, "skipped")
                continue
            report.size_mismatch += 1
            _bump_source(report, source_name, "size_mismatch")
        else:
            report.file_missing += 1
            _bump_source(report, source_name, "file_missing")

        blob_path = _existing_valid_blob_path(d, root)
        if blob_path:
            report.canonical_blob_available += 1
            report.skipped += 1
            _bump_source(report, source_name, "canonical_blob_available")
            _bump_source(report, source_name, "skipped")
            continue

        if source_name in _PLATFORM_BACKFILL_ONLY_SOURCES:
            await _record_platform_backfill(
                conn,
                report,
                d,
                _PLATFORM_BACKFILL_ONLY_SOURCES[source_name],
                dry_run=dry_run,
                queue=queue_platform_backfill,
            )
            continue

        if source_name not in _SAFE_DIRECT_RECOVERY_SOURCES:
            _record_unrecoverable_skip(report, d, "unsupported recovery source")
            continue

        expected_media_type = _expected_recovery_media_type(d)
        if not expected_media_type:
            _record_no_direct_url(report, d, f"unsupported content_type {d.get('content_type') or ''}".strip())
            continue

        candidates = _recovery_url_candidates(d)
        if not candidates:
            platform_reason = _platform_backfill_reason(source_name, d)
            if platform_reason:
                await _record_platform_backfill(
                    conn,
                    report,
                    d,
                    platform_reason,
                    dry_run=dry_run,
                    queue=queue_platform_backfill,
                )
            else:
                _record_no_direct_url(report, d, "no source-specific direct media URL")
            continue

        if dry_run:
            report.would_repair += 1
            report.skipped += 1
            _bump_source(report, source_name, "would_repair")
            _bump_source(report, source_name, "skipped")
            continue

        recovered = False
        last_error = "no candidate attempted"
        attempted_network = False
        for candidate_url in candidates:
            attempted_network = True
            fetched = await _fetch_validated_media(
                candidate_url,
                expected_media_type=expected_media_type,
                max_bytes=max_bytes,
                request_timeout=request_timeout,
                client_factory=client_factory,
            )
            if not fetched.ok:
                last_error = fetched.error or f"HTTP {fetched.status_code}"
                continue
            payload_data, payload_kind, width, height, prepare_error = _prepare_recovered_payload(d, fetched)
            if prepare_error:
                last_error = prepare_error
                continue
            actual_sha = hashlib.sha256(payload_data).hexdigest()
            expected_sha = str(d.get("sha256") or "").strip().lower()
            if _is_sha256_hex(expected_sha) and expected_sha != actual_sha:
                last_error = f"sha256 mismatch after recovery: db={expected_sha} download={actual_sha}"
                continue
            try:
                sidecar_ok = await _write_recovered_media_row(
                    conn,
                    d,
                    data=payload_data,
                    kind=payload_kind,
                    media_type=str(fetched.media_type or expected_media_type),
                    width=width,
                    height=height,
                    request_url=fetched.final_url or fetched.url,
                    content_type=fetched.content_type,
                    root=root,
                )
            except Exception as exc:  # noqa: BLE001 - report remaining rows
                report.failed += 1
                _bump_source(report, source_name, "failed")
                _append_failure(
                    report,
                    {
                        "id": str(d.get("id")),
                        "source": source_name,
                        "content_id": d.get("content_id"),
                        "error": str(exc),
                        "request_url": candidate_url,
                    },
                )
                recovered = True
                break
            if sidecar_ok:
                report.repaired += 1
                report.redownloaded += 1
                _bump_source(report, source_name, "repaired")
                _bump_source(report, source_name, "redownloaded")
            else:
                report.failed += 1
                _bump_source(report, source_name, "failed")
            recovered = True
            break
        if attempted_network and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        if not recovered:
            report.unsafe_response += 1
            report.skipped += 1
            _bump_source(report, source_name, "unsafe_response")
            _bump_source(report, source_name, "skipped")
            _append_failure(
                report,
                {
                    "id": str(d.get("id")),
                    "source": source_name,
                    "content_id": d.get("content_id"),
                    "error": last_error,
                    "candidates": candidates[:5],
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


def _existing_valid_blob_path(row: dict[str, Any], root: Path) -> Path | None:
    digest = str(row.get("sha256") or "").strip().lower()
    blob_path = _existing_blob_path(
        digest,
        filename=row.get("filename"),
        legacy_path=row.get("file_path"),
        root=root,
    )
    if not blob_path:
        return None
    if not _is_sha256_hex(digest):
        return None
    try:
        return blob_path if _sha256_path(blob_path) == digest else None
    except OSError:
        return None


def _bump_source(report: MediaSidecarRepairReport, source: str, key: str, amount: int = 1) -> None:
    source_key = source or "unknown"
    bucket = report.sources.setdefault(source_key, {})
    bucket[key] = int(bucket.get(key, 0)) + amount


def _record_unrecoverable_skip(report: MediaSidecarRepairReport, row: dict[str, Any], error: str) -> None:
    source = str(row.get("source") or "")
    report.skipped += 1
    report.no_direct_url += 1
    _bump_source(report, source, "skipped")
    _bump_source(report, source, "no_direct_url")
    _append_failure(
        report,
        {
            "id": str(row.get("id")),
            "source": source,
            "content_id": row.get("content_id"),
            "error": error,
        },
    )


def _record_no_direct_url(report: MediaSidecarRepairReport, row: dict[str, Any], error: str) -> None:
    _record_unrecoverable_skip(report, row, error)


async def _record_platform_backfill(
    conn,
    report: MediaSidecarRepairReport,
    row: dict[str, Any],
    reason: str,
    *,
    dry_run: bool,
    queue: bool,
) -> None:
    source = str(row.get("source") or "")
    report.platform_backfill_required += 1
    report.skipped += 1
    _bump_source(report, source, "platform_backfill_required")
    _bump_source(report, source, "skipped")
    _append_failure(
        report,
        {
            "id": str(row.get("id")),
            "source": source,
            "content_id": row.get("content_id"),
            "error": reason,
            "action": "platform_backfill_required",
        },
    )
    if dry_run or not queue:
        return
    message = f"missing media file requires platform-specific backfill: {reason}"
    try:
        status = await conn.execute(
            """
            INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
            SELECT $1::varchar(20), $2::varchar(100), $3::varchar(100), $4::text
            WHERE NOT EXISTS (
                SELECT 1
                FROM dead_letter_queue
                WHERE source = $1::varchar(20)
                  AND content_id = $3::varchar(100)
                  AND status IN ('pending', 'in_progress')
                  AND error_message = $4::text
            )
            """,
            source,
            row.get("entity_id"),
            row.get("content_id"),
            message,
        )
        if not isinstance(status, str) or status.endswith(" 1"):
            report.queued_backfill += 1
            _bump_source(report, source, "queued_backfill")
    except Exception as exc:  # noqa: BLE001 - queueing is best-effort
        report.failed += 1
        _bump_source(report, source, "failed")
        _append_failure(
            report,
            {
                "id": str(row.get("id")),
                "source": source,
                "content_id": row.get("content_id"),
                "error": f"platform backfill queue failed: {exc}",
            },
        )


def _expected_recovery_media_type(row: dict[str, Any]) -> str | None:
    content_type = str(row.get("content_type") or "").strip().lower()
    if content_type in {"image", "thumbnail", "profile_photo", "photo", "avatar"}:
        return "image"
    if content_type == "video":
        return "video"
    return None


def _platform_backfill_reason(source: str, row: dict[str, Any]) -> str | None:
    if source in _PLATFORM_BACKFILL_ONLY_SOURCES:
        return _PLATFORM_BACKFILL_ONLY_SOURCES[source]
    if source == "youtube" and str(row.get("content_type") or "").lower() == "video":
        return "youtube videos require yt-dlp/platform backfill"
    return None


def _recovery_url_candidates(row: dict[str, Any]) -> list[str]:
    source = str(row.get("source") or "")
    metadata = _coerce_metadata(row.get("metadata"))
    candidates: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            url = value.strip()
            if _is_http_url(url) and not _is_ephemeral_recovery_url(source, url) and url not in candidates:
                candidates.append(url)

    for value in _metadata_url_values(metadata):
        add(value)

    if source == "youtube":
        add(row.get("source_url"))
        for value in _youtube_thumbnail_url_values(row, metadata):
            add(value)
        if str(row.get("content_type") or "").lower() == "video":
            for value in _youtube_direct_video_url_values(metadata):
                add(value)
    else:
        add(row.get("source_url"))

    return candidates


def _metadata_url_values(metadata: dict[str, Any]) -> list[Any]:
    keys = ("request_url", "url", "media_url", "image_url", "video_url", "thumbnail_url", "download_url")
    values: list[Any] = [metadata.get(key) for key in keys]
    raw = metadata.get("raw")
    if isinstance(raw, dict):
        values.extend(raw.get(key) for key in keys)
        values.extend(_youtube_raw_thumbnail_urls(raw))
    artifact = metadata.get("vault_artifact")
    if isinstance(artifact, dict):
        values.extend(artifact.get(key) for key in keys)
    return values


def _youtube_thumbnail_url_values(row: dict[str, Any], metadata: dict[str, Any]) -> list[Any]:
    content_type = str(row.get("content_type") or "").lower()
    if content_type not in {"thumbnail", "image", "profile_photo"}:
        return []
    values: list[Any] = []
    raw = metadata.get("raw")
    if isinstance(raw, dict):
        values.extend(_youtube_raw_thumbnail_urls(raw))
    if content_type == "thumbnail":
        video_id = str(row.get("content_id") or "").strip()
        if video_id and "/" not in video_id and "\\" not in video_id:
            values.extend(
                f"https://i.ytimg.com/vi/{video_id}/{name}.jpg"
                for name in ("maxresdefault", "hqdefault", "mqdefault", "default")
            )
    return values


def _youtube_raw_thumbnail_urls(raw: dict[str, Any]) -> list[Any]:
    thumbs = raw.get("snippet", {}).get("thumbnails", {}) if isinstance(raw.get("snippet"), dict) else {}
    if not isinstance(thumbs, dict):
        return []
    values: list[Any] = []
    for key in ("maxres", "standard", "high", "medium", "default"):
        item = thumbs.get(key)
        if isinstance(item, dict):
            values.append(item.get("url"))
    return values


def _youtube_direct_video_url_values(metadata: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for key in ("request_url", "url", "media_url", "video_url", "download_url"):
        value = metadata.get(key)
        if isinstance(value, str) and _url_extension_type(value) == "video":
            values.append(value)
    raw = metadata.get("raw")
    if isinstance(raw, dict):
        for key in ("request_url", "url", "media_url", "video_url", "download_url"):
            value = raw.get(key)
            if isinstance(value, str) and _url_extension_type(value) == "video":
                values.append(value)
    return values


async def _fetch_validated_media(
    url: str,
    *,
    expected_media_type: str,
    max_bytes: int,
    request_timeout: float,
    client_factory: Callable[..., Any] | None,
) -> _ValidatedDownload:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return _ValidatedDownload(ok=False, url=url, error="unsupported URL scheme")

    factory = client_factory or httpx.AsyncClient
    headers = {"User-Agent": _RECOVERY_USER_AGENT, "Accept": "image/*,video/*;q=0.9,*/*;q=0.1"}
    try:
        async with factory(timeout=request_timeout, follow_redirects=True) as client:
            try:
                head = await client.head(url, headers=headers)
                if 200 <= head.status_code < 300:
                    cl_error = _content_length_error(head.headers.get("content-length"), max_bytes)
                    if cl_error:
                        return _ValidatedDownload(ok=False, url=url, error=cl_error, status_code=head.status_code)
                    ct_error = _content_type_error(
                        head.headers.get("content-type"),
                        expected_media_type=expected_media_type,
                        url=url,
                        require_present=False,
                    )
                    if ct_error:
                        return _ValidatedDownload(ok=False, url=url, error=ct_error, status_code=head.status_code)
                elif head.status_code not in {403, 405}:
                    return _ValidatedDownload(ok=False, url=url, error=f"HEAD status {head.status_code}", status_code=head.status_code)
            except Exception:
                pass

            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return _ValidatedDownload(ok=False, url=url, error=f"GET status {resp.status_code}", status_code=resp.status_code)
                cl_error = _content_length_error(resp.headers.get("content-length"), max_bytes)
                if cl_error:
                    return _ValidatedDownload(ok=False, url=url, error=cl_error, status_code=resp.status_code)
                content_type = resp.headers.get("content-type", "")
                ct_error = _content_type_error(
                    content_type,
                    expected_media_type=expected_media_type,
                    url=url,
                    require_present=True,
                )
                if ct_error:
                    return _ValidatedDownload(ok=False, url=url, error=ct_error, status_code=resp.status_code)
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        return _ValidatedDownload(
                            ok=False,
                            url=url,
                            error=f"response exceeds cap {max_bytes}",
                            status_code=resp.status_code,
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                ok, kind, media_type, reason = media_filter.inspect(data, content_type)
                if not ok:
                    return _ValidatedDownload(ok=False, url=url, error=reason, status_code=resp.status_code)
                if media_type not in {"image", "video"}:
                    return _ValidatedDownload(ok=False, url=url, error=f"type {media_type or kind} not recoverable")
                if media_type != expected_media_type:
                    return _ValidatedDownload(
                        ok=False,
                        url=url,
                        error=f"media type mismatch: expected {expected_media_type}, got {media_type}",
                        status_code=resp.status_code,
                    )
                header_type = _content_type_media_type(content_type)
                if header_type and header_type != media_type:
                    return _ValidatedDownload(
                        ok=False,
                        url=url,
                        error=f"content-type mismatch: header {header_type}, bytes {media_type}",
                        status_code=resp.status_code,
                    )
                return _ValidatedDownload(
                    ok=True,
                    url=url,
                    data=data,
                    kind=kind,
                    media_type=media_type,
                    content_type=content_type,
                    status_code=resp.status_code,
                    final_url=str(resp.url),
                )
    except Exception as exc:  # noqa: BLE001 - network recovery reports errors per row
        return _ValidatedDownload(ok=False, url=url, error=str(exc))


def _prepare_recovered_payload(row: dict[str, Any], fetched: _ValidatedDownload) -> tuple[bytes, str, int | None, int | None, str | None]:
    data = fetched.data
    kind = str(fetched.kind or "bin")
    width = row.get("width")
    height = row.get("height")
    if row.get("source") == "search" and fetched.media_type == "image":
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=95)
                data = out.getvalue()
                kind = "jpg"
        except Exception as exc:  # noqa: BLE001 - keep row unrecovered
            return b"", kind, None, None, f"search image normalization failed: {exc}"
    return data, kind, _int_or_none(width), _int_or_none(height), None


async def _write_recovered_media_row(
    conn,
    row: dict[str, Any],
    *,
    data: bytes,
    kind: str,
    media_type: str,
    width: int | None,
    height: int | None,
    request_url: str,
    content_type: str | None,
    root: Path,
) -> bool:
    source = str(row.get("source") or "")
    content_id = str(row.get("content_id") or "")
    repaired_at = datetime.now(timezone.utc)
    digest = hashlib.sha256(data).hexdigest()
    filename = _filename_with_extension(row.get("filename"), content_id=content_id, extension=kind)
    metadata = _coerce_metadata(row.get("metadata"))
    recovery_meta = {
        "repaired_at": repaired_at.isoformat(),
        "original_media_item_id": str(row.get("id")),
        "legacy_path": str(row.get("file_path") or ""),
        "request_url": request_url,
        "content_type": content_type,
        "media_type": media_type,
        "kind": kind,
        "original_sha256": row.get("sha256"),
        "method": "source_specific_validated_redownload",
    }
    artifact = write_atomic_artifact(
        source=source,
        artifact_id=content_id,
        artifact_kind="media_blob",
        data=data,
        extension=kind,
        expected_sha256=digest,
        metadata={
            **metadata,
            "missing_media_recovery": recovery_meta,
            "filename": filename,
            "request_url": request_url,
            "source_url": row.get("source_url") or request_url,
            "rebuild_target_tables": _rebuild_tables_for_source(source),
        },
        root=root,
    )
    if not artifact.path:
        raise RuntimeError(f"vault artifact write failed: {artifact.error}")
    artifact_meta = {
        "ok": artifact.ok,
        "partial": artifact.partial,
        "path": artifact.relative_path,
        "blob_path": artifact.blob_relative_path,
        "sidecar_path": artifact.sidecar.relative_path if artifact.sidecar else None,
        "duplicate_blob": artifact.duplicate_blob,
        "error": artifact.error,
        "repaired_by": "media_sidecar_repair",
        "repaired_at": repaired_at.isoformat(),
    }
    metadata["missing_media_recovery"] = recovery_meta
    metadata["vault_artifact"] = artifact_meta
    sidecar = write_media_sidecar(
        source=source,
        entity_id=str(row.get("entity_id") or ""),
        entity_name=str(row.get("entity_name") or ""),
        content_type=str(row.get("content_type") or media_type),
        content_id=content_id,
        filename=filename,
        file_path=str(artifact.path),
        file_size=artifact.file_size,
        width=width,
        height=height,
        sha256=artifact.sha256,
        source_url=row.get("source_url") or request_url,
        metadata=metadata,
        ingest_path=row.get("ingest_path"),
        kind=row.get("kind"),
        root=root,
    )
    patch = {
        "vault_artifact": artifact_meta,
        "vault_sidecar": {
            "enabled": sidecar.enabled,
            "ok": sidecar.ok,
            "path": sidecar.relative_path,
            "error": sidecar.error,
            "repaired": True,
            "repaired_at": repaired_at.isoformat(),
        },
        "missing_media_recovery": recovery_meta,
    }
    await conn.execute(
        """
        UPDATE media_items
        SET filename = $2,
            file_path = $3,
            file_size = $4,
            width = COALESCE($5, width),
            height = COALESCE($6, height),
            sha256 = $7,
            metadata = COALESCE(metadata, '{}'::jsonb) || $8::jsonb
        WHERE id = $1
        """,
        row.get("id"),
        filename,
        str(artifact.path),
        artifact.file_size,
        width,
        height,
        artifact.sha256,
        json.dumps(patch, default=str),
    )
    ok = True
    if artifact.partial:
        ok = False
        await conn.execute(
            """
            INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
            VALUES ($1, $2, $3, $4)
            """,
            source,
            row.get("entity_id"),
            content_id,
            f"vault artifact partial during missing media recovery: {artifact.error}",
        )
    if sidecar.enabled and not sidecar.ok:
        ok = False
        await conn.execute(
            """
            INSERT INTO dead_letter_queue (source, entity_id, content_id, error_message)
            VALUES ($1, $2, $3, $4)
            """,
            source,
            row.get("entity_id"),
            content_id,
            f"vault sidecar repair failed: {sidecar.error}",
        )
    return ok


def _content_length_error(value: str | None, max_bytes: int) -> str | None:
    if not value:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    if length > max_bytes:
        return f"content-length exceeds cap {max_bytes}"
    return None


def _content_type_error(
    content_type: str | None,
    *,
    expected_media_type: str,
    url: str,
    require_present: bool,
) -> str | None:
    base = (content_type or "").split(";")[0].strip().lower()
    if not base:
        return "missing content-type" if require_present else None
    header_type = _content_type_media_type(base)
    if header_type == "unsupported":
        return f"unsafe content-type {base}"
    if header_type and header_type != expected_media_type:
        return f"content-type mismatch: expected {expected_media_type}, got {header_type}"
    if header_type is None and _url_extension_type(url) != expected_media_type:
        return f"ambiguous content-type {base} without direct {expected_media_type} URL"
    return None


def _content_type_media_type(content_type: str | None) -> str | None:
    base = (content_type or "").split(";")[0].strip().lower()
    if not base:
        return None
    if base.startswith("image/"):
        return "image"
    if base.startswith("video/"):
        return "video"
    if base in {"application/octet-stream", "binary/octet-stream"}:
        return None
    return "unsupported"


def _url_extension_type(url: str) -> str | None:
    suffix = Path(urlparse(url).path.lower()).suffix
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    return None


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_ephemeral_recovery_url(source: str, url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if source in _PLATFORM_BACKFILL_ONLY_SOURCES:
        return True
    if source == "youtube" and ("googlevideo.com" in host or "youtube.com" in host and "/watch" in urlparse(url).path):
        return True
    return False


def _filename_with_extension(filename: Any, *, content_id: str, extension: str) -> str:
    clean_ext = str(extension or "bin").strip().lower().lstrip(".") or "bin"
    name = Path(str(filename or "")).name
    if not name:
        name = f"{content_id or 'media'}.{clean_ext}"
    current = Path(name)
    return f"{current.stem or content_id or 'media'}.{clean_ext}"


def _rebuild_tables_for_source(source: str) -> list[str]:
    tables = {
        "search": ["media_items", "search_results", "search_queries"],
        "website": ["media_items", "website_pages", "website_targets"],
        "youtube": ["media_items", "youtube_videos", "youtube_channels"],
    }
    return tables.get(source, ["media_items"])


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_sha256_hex(value: str) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


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
