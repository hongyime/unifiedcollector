#!/usr/bin/env python3
"""Build Task 53's complete baseline-plus-delta media coverage receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.media_reconciliation import canonical_media_path, parse_sha256_manifest, read_rows_csv


class CoverageError(RuntimeError):
    """A production forward capture cannot prove every media reference."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, document: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_coverage(*, rows_csv: Path, baseline_manifest: Path, root: Path, delta_dir: Path, output: Path) -> dict:
    if not rows_csv.is_file() or not baseline_manifest.is_file():
        raise CoverageError("rows CSV and baseline manifest are required")
    if output.exists() or not output.parent.is_dir() or os.stat(output.parent).st_mode & 0o077:
        raise CoverageError("coverage output must be new and private")
    if delta_dir.exists() and any(delta_dir.iterdir()):
        raise CoverageError("delta directory must be empty for this capture")
    root = root.resolve(strict=True)
    media_root = (root / "media").resolve(strict=True)
    delta_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.stat(delta_dir).st_mode & 0o077:
        raise CoverageError("delta directory must be owner-only")

    baseline = {entry.path: entry for entry in parse_sha256_manifest(baseline_manifest) if entry.path.is_relative_to(media_root)}
    candidate_count = baseline_count = delta_count = unaccounted_count = 0
    delta_entries: dict[Path, str] = {}
    source_hashes: dict[Path, str] = {}

    def source_hash(path: Path) -> str:
        if path not in source_hashes:
            source_hashes[path] = _sha256(path)
        return source_hashes[path]

    for row in read_rows_csv(rows_csv):
        candidate_count += 1
        try:
            source = canonical_media_path(row.file_path, root)
        except ValueError:
            unaccounted_count += 1
            continue
        entry = baseline.get(source)
        if entry and source.is_file() and not source.is_symlink() and row.file_size == entry.file_size and row.sha256 == entry.sha256 and source_hash(source) == entry.sha256:
            baseline_count += 1
            continue
        if row.file_size is None or row.sha256 is None or not source.is_file() or source.is_symlink():
            unaccounted_count += 1
            continue
        if source.stat().st_size != row.file_size or source_hash(source) != row.sha256:
            unaccounted_count += 1
            continue
        target = delta_dir / source.relative_to(media_root)
        previous = delta_entries.get(target)
        if previous is None:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if target.stat().st_size != row.file_size or _sha256(target) != row.sha256:
                raise CoverageError("delta copy checksum verification failed")
            delta_entries[target] = row.sha256
        elif previous != row.sha256:
            raise CoverageError("one delta path has conflicting database hashes")
        delta_count += 1

    delta_manifest = delta_dir / "task-53-delta.sha256"
    with delta_manifest.open("w", encoding="utf-8") as handle:
        for path, digest in sorted(delta_entries.items(), key=lambda item: os.fsencode(item[0])):
            handle.write(f"{digest}  {path}\n")
    os.chmod(delta_manifest, 0o600)
    report = {
        "task": 53,
        "schema_version": 1,
        "rows_csv_sha256": _sha256(rows_csv),
        "baseline_manifest_sha256": _sha256(baseline_manifest),
        "delta_manifest_sha256": _sha256(delta_manifest),
        "candidate_count": candidate_count,
        "baseline_count": baseline_count,
        "delta_count": delta_count,
        "unaccounted_count": unaccounted_count,
        "coverage_complete": unaccounted_count == 0,
    }
    _write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Task 53 media coverage from a quiesced DB export")
    parser.add_argument("--rows-csv", type=Path, required=True)
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--delta-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_coverage(**vars(args))
    except CoverageError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["coverage_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
