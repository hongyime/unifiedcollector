from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.finalize_task46_storage_evidence import Task46EvidenceError, build


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    manifest = tmp_path / "destination.sha256"
    manifest.write_text("a" * 64 + "  /media/sg-ultra/unifiedcollector/media/a.jpg\n", encoding="utf-8")
    readback = tmp_path / "readback.txt"
    readback.write_text(f"PASS: entries=1 manifest_sha256={_sha(manifest)}\n", encoding="utf-8")
    task11 = tmp_path / "task11.json"
    task11.write_text(json.dumps({"media_reconciliation": {"sample": {"historical_root": {
        "root": "/media/ultra-touch/seagate-backup-20261016/unifiedcollector",
        "owner_authorized_deletion_after_checksum_sync": True,
        "post_deletion_comparison_possible": False,
    }}}}), encoding="utf-8")
    task12 = tmp_path / "task12.md"
    task12.write_text("old Expansion was owner-authorized for discard; dated Ultra Touch was owner-deleted", encoding="utf-8")
    return manifest, readback, task11, task12


def test_build_binds_serial_readback_and_named_retention_exceptions(tmp_path: Path) -> None:
    manifest, readback, task11, task12 = _inputs(tmp_path)
    documents = build(manifest, readback, task11, task12)
    assert documents["destination"]["serial_readback"]["manifest_sha256"] == _sha(manifest)
    assert documents["report"]["unexplained_destination_integrity_differences"] == 0
    assert len(documents["source"]["preservation_exceptions"]) == 2
    assert documents["source"]["comparison_status"] == "post_deletion_source_enumeration_unavailable"


def test_rejects_readback_for_a_different_manifest(tmp_path: Path) -> None:
    manifest, readback, task11, task12 = _inputs(tmp_path)
    readback.write_text("PASS: entries=1 manifest_sha256=" + "0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(Task46EvidenceError, match="does not match"):
        build(manifest, readback, task11, task12)


def test_rejects_unapproved_or_ambiguous_deleted_source(tmp_path: Path) -> None:
    manifest, readback, task11, task12 = _inputs(tmp_path)
    task11.write_text(json.dumps({"media_reconciliation": {"sample": {"historical_root": {
        "root": "/media/ultra-touch/seagate-backup-20261016/unifiedcollector",
        "owner_authorized_deletion_after_checksum_sync": False,
        "post_deletion_comparison_possible": False,
    }}}}), encoding="utf-8")
    with pytest.raises(Task46EvidenceError, match="owner-authorized"):
        build(manifest, readback, task11, task12)
