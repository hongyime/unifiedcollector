from __future__ import annotations

import json
import hashlib

import pytest

from src.core.rebuild_report import (
    compare_db_media_artifacts,
    file_reference_errors,
    missing_media_item_fields,
    scan_sidecars,
)


def test_missing_media_item_fields_accepts_complete_media_payload():
    payload = {
        "source": "instagram",
        "entity": {"id": "u1", "name": "User One"},
        "content": {"type": "photo", "id": "p1", "filename": "p1.jpg"},
        "file": {"path": "media/instagram/p1.jpg", "size": 10, "sha256": "abc"},
    }

    assert missing_media_item_fields(payload) == []


def test_scan_sidecars_reports_reconstructable_media_items(tmp_path):
    media = tmp_path / "media" / "instagram" / "p1.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"0123456789")
    sidecar = tmp_path / "sidecars" / "instagram" / "2026" / "07" / "p1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "instagram",
            "entity": {"id": "u1", "name": "User One"},
            "content": {"type": "photo", "id": "p1", "filename": "p1.jpg"},
            "file": {"path": "media/instagram/p1.jpg", "size": 10, "sha256": "abc"},
            "raw_payload": {"inline": {"id": "p1"}},
        }),
        encoding="utf-8",
    )

    report = scan_sidecars(tmp_path)

    assert report.sidecars_scanned == 1
    assert report.reconstructable_tables["media_items"] == 1
    assert report.artifacts_by_source["instagram"] == 1
    assert report.raw_payloads_by_source["instagram"] == 1


def test_scan_sidecars_reports_missing_fields_and_bad_json(tmp_path):
    sidecar_root = tmp_path / "sidecars" / "telegram" / "2026" / "07"
    sidecar_root.mkdir(parents=True)
    (sidecar_root / "partial.json").write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "telegram",
            "entity": {"id": "u1"},
            "content": {"type": "photo", "id": "p1"},
            "file": {"path": "media/telegram/p1.jpg"},
        }),
        encoding="utf-8",
    )
    (sidecar_root / "bad.json").write_text("{", encoding="utf-8")

    report = scan_sidecars(tmp_path)

    assert report.sidecars_scanned == 2
    assert report.invalid_json == 1
    assert report.reconstructable_tables["media_items"] == 0
    assert report.missing_fields_by_source["telegram"]["entity_name"] == 1
    assert report.missing_fields_by_source["telegram"]["filename"] == 1


def test_scan_sidecars_limit_stops_early_and_is_reported(tmp_path):
    sidecar_root = tmp_path / "sidecars" / "telegram" / "2026" / "07"
    sidecar_root.mkdir(parents=True)
    for idx in range(3):
        (sidecar_root / f"{idx}.json").write_text(
            json.dumps({"artifact_kind": "raw_payload", "source": "telegram"}),
            encoding="utf-8",
        )

    report = scan_sidecars(tmp_path, sidecar_limit=2)

    assert report.sidecars_scanned == 2
    assert report.to_dict()["sidecar_scan_limit"] == 2


def test_scan_sidecars_does_not_count_missing_files_as_reconstructable(tmp_path):
    sidecar = tmp_path / "sidecars" / "telegram" / "2026" / "07" / "p1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "telegram",
            "entity": {"id": "u1", "name": "User One"},
            "content": {"type": "photo", "id": "p1", "filename": "p1.jpg"},
            "file": {"path": "media/telegram/p1.jpg", "size": 10, "sha256": "abc"},
        }),
        encoding="utf-8",
    )

    report = scan_sidecars(tmp_path)

    assert report.reconstructable_tables["media_items"] == 0
    assert report.file_errors_by_source["telegram"]["file_missing"] == 1


def test_scan_sidecars_uses_canonical_blob_when_occurrence_file_is_missing(tmp_path):
    data = b"same-bytes"
    digest = hashlib.sha256(data).hexdigest()
    blob = tmp_path / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(data)
    sidecar = tmp_path / "sidecars" / "instagram" / "2026" / "07" / "p1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "instagram",
            "entity": {"id": "u1", "name": "User One"},
            "content": {"type": "photo", "id": "p1", "filename": "p1.jpg"},
            "file": {
                "path": "media/instagram/missing.jpg",
                "blob_path": f"media/blobs/{digest[:2]}/{digest[2:4]}/{digest}.jpg",
                "size": len(data),
                "sha256": digest,
            },
        }),
        encoding="utf-8",
    )

    report = scan_sidecars(tmp_path, verify_checksums=True)

    assert report.reconstructable_tables["media_items"] == 1
    assert report.blob_fallbacks_by_source["instagram"] == 1
    assert not report.file_errors_by_source


def test_scan_sidecars_counts_artifact_rebuild_table_hints(tmp_path):
    artifact = tmp_path / "raw" / "strava" / "route-1.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"activity_id":"a1"}', encoding="utf-8")
    sidecar = tmp_path / "sidecars" / "artifacts" / "strava" / "2026" / "07" / "route-1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "raw_payload",
            "artifact_id": "strava:route-1",
            "source": "strava",
            "file": {
                "path": "raw/strava/route-1.json",
                "size": artifact.stat().st_size,
                "sha256": "not-checked-by-default",
            },
            "metadata": {"raw_payload": True},
            "rebuild": {
                "target_tables": ["strava_activity_streams"],
                "required_fields": ["source", "artifact_id", "file.path", "file.size"],
            },
        }),
        encoding="utf-8",
    )

    report = scan_sidecars(tmp_path)

    assert report.reconstructable_tables["strava_activity_streams"] == 1
    assert report.raw_payloads_by_source["strava"] == 1


def test_file_reference_errors_can_verify_checksum(tmp_path):
    media = tmp_path / "media" / "instagram" / "p1.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"real-bytes")
    payload = {
        "file": {
            "path": "media/instagram/p1.jpg",
            "size": media.stat().st_size,
            "sha256": "0" * 64,
        },
    }

    assert file_reference_errors(payload, tmp_path) == []
    assert file_reference_errors(payload, tmp_path, verify_checksums=True) == [
        "file_sha256_mismatch",
    ]


def test_file_reference_errors_verifies_checksum_against_blob_fallback(tmp_path):
    data = b"blob-bytes"
    digest = hashlib.sha256(data).hexdigest()
    blob = tmp_path / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(data)
    payload = {
        "file": {
            "path": "media/instagram/missing.jpg",
            "blob_path": f"media/blobs/{digest[:2]}/{digest[2:4]}/{digest}.jpg",
            "size": len(data),
            "sha256": digest,
        },
    }

    assert file_reference_errors(payload, tmp_path, verify_checksums=True) == []


@pytest.mark.asyncio
async def test_compare_db_media_artifacts_reports_inventory_states(tmp_path):
    sidecar_media = tmp_path / "media" / "instagram" / "sidecar-only.jpg"
    sidecar_media.parent.mkdir(parents=True)
    sidecar_media.write_bytes(b"sidecar")
    sidecar = tmp_path / "sidecars" / "instagram" / "2026" / "07" / "sidecar-only.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "instagram",
            "entity": {"id": "u1", "name": "User One"},
            "content": {"type": "photo", "id": "sidecar-only", "filename": "sidecar-only.jpg"},
            "file": {
                "path": "media/instagram/sidecar-only.jpg",
                "size": sidecar_media.stat().st_size,
                "sha256": hashlib.sha256(sidecar_media.read_bytes()).hexdigest(),
            },
        }),
        encoding="utf-8",
    )

    db_only_file = tmp_path / "media" / "instagram" / "db-only.jpg"
    db_only_file.write_bytes(b"db-only")
    size_mismatch_file = tmp_path / "media" / "telegram" / "size.jpg"
    size_mismatch_file.parent.mkdir(parents=True)
    size_mismatch_file.write_bytes(b"size")
    hash_mismatch_file = tmp_path / "media" / "telegram" / "hash.jpg"
    hash_mismatch_file.write_bytes(b"hash")
    blob_only = tmp_path / "media" / "blobs" / "aa" / "bb" / ("a" * 64 + ".jpg")
    blob_only.parent.mkdir(parents=True)
    blob_only.write_bytes(b"unreferenced")

    rows = [
        {
            "source": "instagram",
            "content_id": "db-only",
            "filename": "db-only.jpg",
            "file_path": str(db_only_file),
            "file_size": db_only_file.stat().st_size,
            "sha256": hashlib.sha256(db_only_file.read_bytes()).hexdigest(),
        },
        {
            "source": "telegram",
            "content_id": "missing-file",
            "filename": "missing.jpg",
            "file_path": str(tmp_path / "media" / "telegram" / "missing.jpg"),
            "file_size": 10,
            "sha256": None,
        },
        {
            "source": "telegram",
            "content_id": "size-mismatch",
            "filename": "size.jpg",
            "file_path": str(size_mismatch_file),
            "file_size": 999,
            "sha256": hashlib.sha256(size_mismatch_file.read_bytes()).hexdigest(),
        },
        {
            "source": "telegram",
            "content_id": "hash-mismatch",
            "filename": "hash.jpg",
            "file_path": str(hash_mismatch_file),
            "file_size": hash_mismatch_file.stat().st_size,
            "sha256": "0" * 64,
        },
    ]

    class FakeConn:
        async def fetch(self, _sql):
            return rows

    report = scan_sidecars(tmp_path)
    await compare_db_media_artifacts(report, FakeConn(), tmp_path, verify_checksums=True)
    payload = report.to_dict()["artifact_reconciliation"]

    assert payload["enabled"] is True
    assert payload["db_media_rows_scanned"] == 4
    assert payload["sidecar_media_keys_scanned"] == 1
    assert payload["blob_files_scanned"] == 1
    assert payload["states_by_source"]["instagram"]["db_only"] == 1
    assert payload["states_by_source"]["instagram"]["sidecar_only"] == 1
    assert payload["states_by_source"]["telegram"]["file_missing"] == 1
    assert payload["states_by_source"]["telegram"]["file_size_mismatch"] == 1
    assert payload["states_by_source"]["telegram"]["file_sha256_mismatch"] == 1
    assert payload["states_by_source"]["unknown"]["blob_only"] == 1
    assert payload["db_file_errors_by_source"]["telegram"]["file_missing"] == 1
    assert "instagram:db-only" in payload["samples_by_state"]["db_only"]
