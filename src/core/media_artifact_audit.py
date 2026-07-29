"""Bounded media/file/sidecar audit helpers.

This module is deliberately read-only. It samples media rows by
``(source, content_id)`` keyset order so operators can inspect vault health
without triggering the expensive historical sidecar scans used by rebuild tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.vault import VAULT_ROOT


MAX_SAMPLE_PER_SOURCE = 500


@dataclass
class MediaArtifactSourceAudit:
    source: str
    total_media_items: int | None = None
    total_media_bytes: int | None = None
    latest_media_at: Any = None
    cursor_after: str = ""
    next_cursor: str | None = None
    sample_limit: int = 0
    sampled: int = 0
    files_present: int = 0
    files_missing: int = 0
    size_mismatches: int = 0
    sidecar_metadata_present: int = 0
    sidecar_metadata_missing: int = 0
    sidecar_files_present: int = 0
    sidecar_files_missing: int = 0
    rows_with_occurrence_sidecar: int = 0
    rows_with_artifact_sidecar: int = 0
    query_error: str | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def issue_count(self) -> int:
        return (
            self.files_missing
            + self.size_mismatches
            + self.sidecar_metadata_missing
            + self.sidecar_files_missing
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total_media_items": self.total_media_items,
            "total_media_bytes": self.total_media_bytes,
            "latest_media_at": _json_time(self.latest_media_at),
            "cursor_after": self.cursor_after,
            "next_cursor": self.next_cursor,
            "sample_limit": self.sample_limit,
            "sampled": self.sampled,
            "files_present": self.files_present,
            "files_missing": self.files_missing,
            "size_mismatches": self.size_mismatches,
            "sidecar_metadata_present": self.sidecar_metadata_present,
            "sidecar_metadata_missing": self.sidecar_metadata_missing,
            "sidecar_files_present": self.sidecar_files_present,
            "sidecar_files_missing": self.sidecar_files_missing,
            "rows_with_occurrence_sidecar": self.rows_with_occurrence_sidecar,
            "rows_with_artifact_sidecar": self.rows_with_artifact_sidecar,
            "issue_count": self.issue_count,
            "query_error": self.query_error,
            "failures": self.failures,
        }


@dataclass
class MediaArtifactAuditReport:
    checked_at: datetime
    mode: str
    sample_per_source: int
    timeout_seconds: float | None
    vault_root: str
    sources: list[MediaArtifactSourceAudit] = field(default_factory=list)
    source_error: str | None = None

    @property
    def total_sampled(self) -> int:
        return sum(source.sampled for source in self.sources)

    @property
    def total_issues(self) -> int:
        return sum(source.issue_count for source in self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "mode": self.mode,
            "sample_per_source": self.sample_per_source,
            "timeout_seconds": self.timeout_seconds,
            "vault_root": self.vault_root,
            "total_sampled": self.total_sampled,
            "total_issues": self.total_issues,
            "source_error": self.source_error,
            "sources": [source.to_dict() for source in self.sources],
        }


async def audit_media_artifacts(
    conn,
    *,
    source: str | None = None,
    sample_per_source: int = 100,
    cursor_after: str | None = None,
    timeout: float | None = 5.0,
    vault_root: str | Path | None = None,
) -> MediaArtifactAuditReport:
    """Return a bounded, read-only media artifact audit.

    The audit uses ``media_source_rollups`` for totals and a keyset sample over
    ``media_items(source, content_id)`` for file/sidecar checks. It does not scan
    the sidecar tree and does not select full JSON metadata payloads.
    """

    root = Path(vault_root) if vault_root else VAULT_ROOT
    sample_limit = max(1, min(int(sample_per_source or 100), MAX_SAMPLE_PER_SOURCE))
    cursor = str(cursor_after or "")
    report = MediaArtifactAuditReport(
        checked_at=datetime.now(timezone.utc),
        mode="keyset_sample_by_source_content_id",
        sample_per_source=sample_limit,
        timeout_seconds=timeout,
        vault_root=str(root),
    )

    try:
        rollups = await _fetch_rollups(conn, source=source, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - audit should report degraded state
        report.source_error = f"{exc.__class__.__name__}: {exc}"
        if source:
            rollups = [{"source": source}]
        else:
            return report

    for rollup in rollups:
        source_name = str(rollup.get("source") or "").strip()
        if not source_name:
            continue
        source_audit = MediaArtifactSourceAudit(
            source=source_name,
            total_media_items=_int_or_none(rollup.get("total_media_items")),
            total_media_bytes=_int_or_none(rollup.get("total_media_bytes")),
            latest_media_at=rollup.get("latest_media_at"),
            cursor_after=cursor,
            sample_limit=sample_limit,
        )
        try:
            rows = await _fetch_sample_rows(
                conn,
                source=source_name,
                cursor_after=cursor,
                limit=sample_limit,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - keep other sources auditable
            source_audit.query_error = f"{exc.__class__.__name__}: {exc}"
            report.sources.append(source_audit)
            continue

        _audit_rows(source_audit, rows, vault_root=root)
        report.sources.append(source_audit)

    return report


async def _fetch_rollups(conn, *, source: str | None, timeout: float | None) -> list[dict[str, Any]]:
    if source:
        row = await conn.fetchrow(
            """
            SELECT source, total_media_items, total_media_bytes, latest_media_at
            FROM media_source_rollups
            WHERE source = $1
            """,
            source,
            timeout=timeout,
        )
        if row:
            return [dict(row)]
        return [{"source": source}]

    rows = await conn.fetch(
        """
        SELECT source, total_media_items, total_media_bytes, latest_media_at
        FROM media_source_rollups
        ORDER BY source
        """,
        timeout=timeout,
    )
    return [dict(row) for row in rows]


async def _fetch_sample_rows(
    conn,
    *,
    source: str,
    cursor_after: str,
    limit: int,
    timeout: float | None,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT content_id, file_path, file_size,
               metadata->'vault_sidecar'->>'path' AS occurrence_sidecar_path,
               metadata->'vault_artifact'->>'sidecar_path' AS artifact_sidecar_path
        FROM media_items
        WHERE source = $1
          AND content_id > $2
          AND file_path IS NOT NULL
          AND file_path <> ''
        ORDER BY content_id
        LIMIT $3
        """,
        source,
        cursor_after,
        limit,
        timeout=timeout,
    )
    return [dict(row) for row in rows]


def _audit_rows(
    source_audit: MediaArtifactSourceAudit,
    rows: list[dict[str, Any]],
    *,
    vault_root: Path,
) -> None:
    for row in rows:
        source_audit.sampled += 1
        content_id = str(row.get("content_id") or "")
        if content_id:
            source_audit.next_cursor = content_id

        media_path = _resolve_artifact_path(row.get("file_path"), vault_root)
        if media_path and media_path.is_file():
            source_audit.files_present += 1
            expected_size = _int_or_none(row.get("file_size"))
            if expected_size is not None:
                actual_size = media_path.stat().st_size
                if int(actual_size) != int(expected_size):
                    source_audit.size_mismatches += 1
                    _append_failure(
                        source_audit,
                        "size_mismatch",
                        content_id,
                        str(media_path),
                        f"db={expected_size} file={actual_size}",
                    )
        else:
            source_audit.files_missing += 1
            _append_failure(
                source_audit,
                "file_missing",
                content_id,
                str(media_path) if media_path else str(row.get("file_path") or ""),
            )

        occurrence_sidecar = _clean_text(row.get("occurrence_sidecar_path"))
        artifact_sidecar = _clean_text(row.get("artifact_sidecar_path"))
        if occurrence_sidecar:
            source_audit.rows_with_occurrence_sidecar += 1
        if artifact_sidecar:
            source_audit.rows_with_artifact_sidecar += 1

        sidecar_paths = [p for p in (occurrence_sidecar, artifact_sidecar) if p]
        if not sidecar_paths:
            source_audit.sidecar_metadata_missing += 1
            _append_failure(source_audit, "sidecar_metadata_missing", content_id, "")
            continue

        source_audit.sidecar_metadata_present += 1
        found_sidecar = False
        missing_sidecars: list[str] = []
        for sidecar_path in sidecar_paths:
            resolved = _resolve_artifact_path(sidecar_path, vault_root)
            if resolved and resolved.is_file():
                found_sidecar = True
            else:
                missing_sidecars.append(str(resolved) if resolved else sidecar_path)
        if found_sidecar:
            source_audit.sidecar_files_present += 1
        else:
            source_audit.sidecar_files_missing += 1
            _append_failure(
                source_audit,
                "sidecar_file_missing",
                content_id,
                ", ".join(missing_sidecars),
            )


def _resolve_artifact_path(value: Any, root: Path) -> Path | None:
    text = _clean_text(value)
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


def _append_failure(
    source_audit: MediaArtifactSourceAudit,
    kind: str,
    content_id: str,
    path: str,
    detail: str | None = None,
) -> None:
    if len(source_audit.failures) >= 20:
        return
    failure = {
        "kind": kind,
        "content_id": content_id,
        "path": path,
    }
    if detail:
        failure["detail"] = detail
    source_audit.failures.append(failure)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_time(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)
