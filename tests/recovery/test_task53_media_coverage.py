from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from scripts.recovery.build_task53_media_coverage import build_coverage


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "file_path", "file_size", "sha256"])
        writer.writeheader()
        writer.writerows(rows)


def test_coverage_classifies_baseline_and_copies_only_delta(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    baseline_file = root / "media" / "baseline.bin"
    delta_file = root / "media" / "delta.bin"
    baseline_file.parent.mkdir(parents=True)
    baseline_file.write_bytes(b"baseline")
    delta_file.write_bytes(b"delta")
    manifest = tmp_path / "baseline.sha256"
    manifest.write_text(f"{_sha256(baseline_file)}  {baseline_file}\n", encoding="utf-8")
    rows = tmp_path / "rows.csv"
    _rows(rows, [
        {"id": "1", "file_path": "/media/baseline.bin", "file_size": str(baseline_file.stat().st_size), "sha256": _sha256(baseline_file)},
        {"id": "2", "file_path": "/media/delta.bin", "file_size": str(delta_file.stat().st_size), "sha256": _sha256(delta_file)},
    ])
    delta_dir = tmp_path / "delta"
    delta_dir.mkdir(mode=0o700)
    output = tmp_path / "coverage.json"

    report = build_coverage(rows_csv=rows, baseline_manifest=manifest, root=root, delta_dir=delta_dir, output=output)

    assert report["candidate_count"] == 2
    assert report["baseline_count"] == 1
    assert report["delta_count"] == 1
    assert report["unaccounted_count"] == 0
    assert (delta_dir / "delta.bin").read_bytes() == b"delta"
    assert json.loads(output.read_text())["coverage_complete"] is True


def test_coverage_reports_unaccounted_reference_without_copying_it(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "media").mkdir(parents=True)
    manifest = tmp_path / "baseline.sha256"
    manifest.write_text("", encoding="utf-8")
    rows = tmp_path / "rows.csv"
    _rows(rows, [{"id": "1", "file_path": "/media/missing.bin", "file_size": "1", "sha256": "a" * 64}])
    delta_dir = tmp_path / "delta"
    delta_dir.mkdir(mode=0o700)
    output = tmp_path / "coverage.json"

    report = build_coverage(rows_csv=rows, baseline_manifest=manifest, root=root, delta_dir=delta_dir, output=output)

    assert report["unaccounted_count"] == 1
    assert report["coverage_complete"] is False
    assert list(delta_dir.iterdir()) == [delta_dir / "task-53-delta.sha256"]


def test_coverage_rejects_a_same_size_changed_baseline_file(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "media" / "changed.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"before")
    original_sha256 = _sha256(source)
    manifest = tmp_path / "baseline.sha256"
    manifest.write_text(f"{original_sha256}  {source}\n", encoding="utf-8")
    source.write_bytes(b"after!")
    rows = tmp_path / "rows.csv"
    _rows(rows, [{"id": "1", "file_path": "/media/changed.bin", "file_size": "6", "sha256": original_sha256}])
    delta_dir = tmp_path / "delta"
    delta_dir.mkdir(mode=0o700)
    output = tmp_path / "coverage.json"

    report = build_coverage(rows_csv=rows, baseline_manifest=manifest, root=root, delta_dir=delta_dir, output=output)

    assert report["baseline_count"] == 0
    assert report["unaccounted_count"] == 1
