"""Read-only planning for recovery-time media reconciliation.

This module deliberately produces an auditable plan only.  It never removes a
database row or a filesystem path.  A later, separately authorized transaction
must consume the plan after checking its input digests and current state.
"""
from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator


_HEX = frozenset(b"0123456789abcdef")


@dataclass(frozen=True)
class MediaRow:
    media_id: str
    file_path: str
    file_size: int | None
    sha256: str | None


@dataclass(frozen=True)
class ManifestEntry:
    path: Path
    file_size: int
    sha256: str


@dataclass(frozen=True)
class ReconciliationPlan:
    matched: tuple[str, ...]
    delete_row_candidates: tuple[dict[str, str], ...]
    unresolved_rows: tuple[dict[str, str], ...]
    unreferenced_files: tuple[str, ...]

    def summary(self) -> dict[str, int]:
        return {
            "matched": len(self.matched),
            "delete_row_candidates": len(self.delete_row_candidates),
            "unresolved_rows": len(self.unresolved_rows),
            "unreferenced_files": len(self.unreferenced_files),
        }


def canonical_media_path(file_path: str, root: Path) -> Path:
    """Map only established container media paths to an sg-ultra host path."""
    root = root.resolve(strict=False)
    normalized = file_path.replace("\\", "/")
    if normalized == "/vault/media" or normalized.startswith("/vault/media/"):
        suffix = normalized.removeprefix("/vault/media").lstrip("/")
        candidate = root / "media" / suffix
    elif normalized == "/media" or normalized.startswith("/media/"):
        suffix = normalized.removeprefix("/media").lstrip("/")
        candidate = root / "media" / suffix
    else:
        raise ValueError("file_path is outside the established /media or /vault/media mappings")
    resolved = candidate.resolve(strict=False)
    media_root = (root / "media").resolve(strict=False)
    if not resolved.is_relative_to(media_root) or resolved == media_root:
        raise ValueError("file_path escapes the established media root")
    return resolved


def parse_sha256_manifest(manifest: Path) -> Iterator[ManifestEntry]:
    """Parse GNU sha256sum output without reading or hashing payload files."""
    for line_number, line in enumerate(manifest.read_bytes().splitlines(), start=1):
        escaped = line.startswith(b"\\")
        record = line[1:] if escaped else line
        if len(record) < 67 or record[64:66] not in {b"  ", b" *"} or any(byte not in _HEX for byte in record[:64]):
            raise ValueError(f"malformed sha256sum record at line {line_number}")
        raw_path = _unescape_filename(record[66:]) if escaped else record[66:]
        if not raw_path:
            raise ValueError(f"empty manifest path at line {line_number}")
        path = Path(os.fsdecode(raw_path)).resolve(strict=False)
        yield ManifestEntry(path=path, file_size=path.stat().st_size if path.exists() else -1, sha256=record[:64].decode("ascii"))


def build_reconciliation_plan(rows: Iterable[MediaRow], entries: Iterable[ManifestEntry], root: Path) -> ReconciliationPlan:
    """Classify DB references against an already-built destination manifest.

    The manifest hash is the evidence for file bytes.  This function does not
    re-read the target files, so callers should use it only after Task 46's
    serial manifest/readback succeeds.
    """
    root = root.resolve(strict=False)
    media_root = (root / "media").resolve(strict=False)
    manifest_by_path: dict[Path, ManifestEntry] = {}
    for entry in entries:
        if not entry.path.is_relative_to(media_root):
            continue
        if entry.path in manifest_by_path:
            raise ValueError(f"duplicate manifest path: {entry.path}")
        manifest_by_path[entry.path] = entry

    referenced: set[Path] = set()
    matched: list[str] = []
    deletion_candidates: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    for row in rows:
        try:
            target = canonical_media_path(row.file_path, root)
        except ValueError as exc:
            unresolved.append({"media_id": row.media_id, "reason": str(exc)})
            continue
        if target in referenced:
            unresolved.append({"media_id": row.media_id, "reason": "multiple media rows map to the same canonical path"})
            continue
        referenced.add(target)
        entry = manifest_by_path.get(target)
        if entry is None:
            deletion_candidates.append({"media_id": row.media_id, "reason": "missing_canonical_path"})
        elif row.file_size is None or row.sha256 is None:
            unresolved.append({"media_id": row.media_id, "reason": "database row lacks size or sha256"})
        elif entry.file_size != row.file_size:
            deletion_candidates.append({"media_id": row.media_id, "reason": "file_size_mismatch"})
        elif entry.sha256 != row.sha256:
            deletion_candidates.append({"media_id": row.media_id, "reason": "sha256_mismatch"})
        else:
            matched.append(row.media_id)

    # A file not referenced by a media_items row is never a delete instruction.
    # It may be a sidecar, raw capture, or candidate whose provenance requires
    # review before the owner-approved Branch-B disposition can be applied.
    unreferenced = sorted(str(path) for path in manifest_by_path if path not in referenced)
    return ReconciliationPlan(
        matched=tuple(sorted(matched)),
        delete_row_candidates=tuple(sorted(deletion_candidates, key=lambda item: item["media_id"])),
        unresolved_rows=tuple(sorted(unresolved, key=lambda item: item["media_id"])),
        unreferenced_files=tuple(unreferenced),
    )


def read_rows_csv(rows_csv: Path) -> Iterator[MediaRow]:
    with rows_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"id", "file_path", "file_size", "sha256"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("rows CSV must have id,file_path,file_size,sha256 headers")
        for row in reader:
            size = row["file_size"]
            yield MediaRow(
                media_id=row["id"],
                file_path=row["file_path"],
                file_size=int(size) if size else None,
                sha256=row["sha256"] or None,
            )


def write_plan(plan: ReconciliationPlan, output: Path, *, rows_csv: Path, manifest: Path, root: Path) -> None:
    """Atomically write the full inventory plan and hashes of its exact inputs."""
    import json

    payload = {
        "schema_version": 1,
        "mode": "read_only_planning",
        "root": str(root.resolve(strict=False)),
        "inputs": {
            "rows_csv": str(rows_csv),
            "rows_csv_sha256": _sha256_file(rows_csv),
            "destination_manifest": str(manifest),
            "destination_manifest_sha256": _sha256_file(manifest),
        },
        "summary": plan.summary(),
        "matched_media_ids": list(plan.matched),
        "delete_row_candidates": list(plan.delete_row_candidates),
        "unresolved_rows": list(plan.unresolved_rows),
        "unreferenced_files": list(plan.unreferenced_files),
        "safety": {
            "database_mutated": False,
            "filesystem_mutated": False,
            "unreferenced_files_are_not_delete_instructions": True,
        },
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unescape_filename(value: bytes) -> bytes:
    output = bytearray()
    escapes = {ord("\\"): ord("\\"), ord("n"): ord("\n"), ord("r"): ord("\r")}
    index = 0
    while index < len(value):
        byte = value[index]
        if byte != ord("\\"):
            output.append(byte)
            index += 1
            continue
        if index + 1 == len(value) or value[index + 1] not in escapes:
            raise ValueError("invalid GNU sha256sum filename escape")
        output.append(escapes[value[index + 1]])
        index += 2
    return bytes(output)
