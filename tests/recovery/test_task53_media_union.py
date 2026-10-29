from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest

from scripts.recovery.verify_task53_media_union import UnionError, verify_union


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "file_path", "file_size", "sha256"])
        writer.writeheader()
        writer.writerows(rows)


def test_union_stages_references_without_copying_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    baseline = root / "media" / "baseline.bin"
    current_delta = root / "media" / "delta.bin"
    baseline.parent.mkdir(parents=True)
    baseline.write_bytes(b"baseline")
    current_delta.write_bytes(b"delta")
    manifest = tmp_path / "baseline.sha256"
    manifest.write_text(f"{_sha256(baseline)}  {baseline}\n", encoding="utf-8")
    rows = tmp_path / "rows.csv"
    _rows(rows, [
        {"id": "1", "file_path": "/media/baseline.bin", "file_size": str(baseline.stat().st_size), "sha256": _sha256(baseline)},
        {"id": "2", "file_path": "/media/delta.bin", "file_size": str(current_delta.stat().st_size), "sha256": _sha256(current_delta)},
    ])
    delta_dir = tmp_path / "delta"
    delta_dir.mkdir()
    copied_delta = delta_dir / "delta.bin"
    copied_delta.write_bytes(current_delta.read_bytes())
    stage = tmp_path / "stage"

    report = verify_union(rows_csv=rows, baseline_manifest=manifest, root=root, delta_dir=delta_dir, stage=stage)

    assert report == {"baseline_references": 1, "delta_references": 1, "references": 2}
    assert (stage / "media" / "baseline.bin").is_symlink()
    assert (stage / "media" / "delta.bin").is_symlink()
    assert (stage / "media" / "baseline.bin").read_bytes() == b"baseline"
    assert (stage / "media" / "delta.bin").read_bytes() == b"delta"


def test_union_rejects_a_same_size_corrupt_delta(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "media" / "delta.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"delta")
    manifest = tmp_path / "baseline.sha256"
    manifest.write_text("", encoding="utf-8")
    rows = tmp_path / "rows.csv"
    _rows(rows, [{"id": "1", "file_path": "/media/delta.bin", "file_size": "5", "sha256": _sha256(source)}])
    delta_dir = tmp_path / "delta"
    delta_dir.mkdir()
    (delta_dir / "delta.bin").write_bytes(b"other")

    with pytest.raises(UnionError, match="delta checksum"):
        verify_union(rows_csv=rows, baseline_manifest=manifest, root=root, delta_dir=delta_dir, stage=tmp_path / "stage")
