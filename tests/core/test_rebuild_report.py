from __future__ import annotations

import json
import hashlib

from src.core.rebuild_report import (
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
