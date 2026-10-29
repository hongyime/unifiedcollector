#!/usr/bin/env python3
"""Stage and verify Task 53's baseline-plus-delta media reference union."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.media_reconciliation import canonical_media_path, parse_sha256_manifest, read_rows_csv


class UnionError(RuntimeError):
    """The captured media references cannot be reconstructed safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_union(*, rows_csv: Path, baseline_manifest: Path, root: Path, delta_dir: Path, stage: Path) -> dict[str, int]:
    if stage.exists() or not rows_csv.is_file() or not baseline_manifest.is_file() or not delta_dir.is_dir():
        raise UnionError("union inputs must exist and the disposable stage must be new")
    root = root.resolve(strict=True)
    media_root = (root / "media").resolve(strict=True)
    delta_root = delta_dir.resolve(strict=True)
    baseline = {entry.path: entry for entry in parse_sha256_manifest(baseline_manifest) if entry.path.is_relative_to(media_root)}
    stage_media = stage / "media"
    stage_media.mkdir(mode=0o700, parents=True)
    hashes: dict[Path, str] = {}
    staged: dict[Path, tuple[Path, int, str]] = {}
    baseline_references = delta_references = 0

    def digest(path: Path) -> str:
        if path not in hashes:
            hashes[path] = _sha256(path)
        return hashes[path]

    try:
        for row in read_rows_csv(rows_csv):
            if row.file_size is None or row.sha256 is None:
                raise UnionError("captured media row lacks size or sha256")
            try:
                source = canonical_media_path(row.file_path, root)
            except ValueError as exc:
                raise UnionError("captured media path is outside the media root") from exc
            relative = source.relative_to(media_root)
            entry = baseline.get(source)
            if entry and source.is_file() and not source.is_symlink() and row.file_size == entry.file_size and row.sha256 == entry.sha256 and digest(source) == entry.sha256:
                selected = source
                baseline_references += 1
            else:
                selected = (delta_root / relative).resolve(strict=False)
                if not selected.is_relative_to(delta_root) or not selected.is_file() or selected.is_symlink():
                    raise UnionError("captured media reference is absent from baseline and delta")
                if selected.stat().st_size != row.file_size or digest(selected) != row.sha256:
                    raise UnionError("captured delta checksum does not match its media row")
                delta_references += 1
            target = stage_media / relative
            selected = selected.resolve(strict=True)
            metadata = (selected, row.file_size, row.sha256)
            if target in staged:
                if staged[target] != metadata:
                    raise UnionError("one staged path has conflicting media rows")
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.symlink_to(selected)
            if target.stat().st_size != row.file_size or digest(target) != row.sha256:
                raise UnionError("staged media union checksum verification failed")
            staged[target] = metadata
    except Exception:
        import shutil

        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {
        "references": baseline_references + delta_references,
        "baseline_references": baseline_references,
        "delta_references": delta_references,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Task 53 media union in a disposable reference tree")
    parser.add_argument("--rows-csv", type=Path, required=True)
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--delta-dir", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_union(**vars(args)), sort_keys=True))
    except UnionError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
