"""The Task 46 destination manifest verifier must be exhaustive and fail closed."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.verify_destination_manifest import verify_manifest


def _record(path: Path) -> str:
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}\n"


def test_verifies_sorted_manifest_and_content(tmp_path: Path) -> None:
    root = tmp_path / "sg-ultra" / "unifiedcollector"
    first = root / "media" / "a.bin"
    second = root / "sidecars" / "b.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(_record(first) + _record(second), encoding="utf-8")

    report = verify_manifest(manifest, [root])

    assert report.entries == 2
    assert len(report.manifest_sha256) == 64


def test_verifies_gnu_escaped_filename(tmp_path: Path) -> None:
    root = tmp_path / "sg-ultra" / "unifiedcollector"
    artifact = root / "media" / "line\nbreak.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"escaped")
    escaped_path = str(artifact).replace("\\", "\\\\").replace("\n", "\\n")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(f"\\{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {escaped_path}\n", encoding="utf-8")

    assert verify_manifest(manifest, [root]).entries == 1


@pytest.mark.parametrize("mode", ["tampered", "outside_root", "duplicate", "unsorted"])
def test_rejects_invalid_or_incomplete_manifest(tmp_path: Path, mode: str) -> None:
    root = tmp_path / "sg-ultra" / "unifiedcollector"
    first = root / "media" / "a.bin"
    second = root / "sidecars" / "b.json"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    manifest = tmp_path / "manifest.sha256"

    if mode == "tampered":
        manifest.write_text(_record(first), encoding="utf-8")
        first.write_bytes(b"changed")
    elif mode == "outside_root":
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"outside")
        manifest.write_text(_record(outside), encoding="utf-8")
    elif mode == "duplicate":
        manifest.write_text(_record(first) + _record(first), encoding="utf-8")
    else:
        manifest.write_text(_record(second) + _record(first), encoding="utf-8")

    with pytest.raises(ValueError):
        verify_manifest(manifest, [root])
