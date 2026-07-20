from __future__ import annotations

import json

from src.core.rebuild_report import missing_media_item_fields, scan_sidecars


def test_missing_media_item_fields_accepts_complete_media_payload():
    payload = {
        "source": "instagram",
        "entity": {"id": "u1", "name": "User One"},
        "content": {"type": "photo", "id": "p1", "filename": "p1.jpg"},
        "file": {"path": "media/instagram/p1.jpg", "size": 10, "sha256": "abc"},
    }

    assert missing_media_item_fields(payload) == []


def test_scan_sidecars_reports_reconstructable_media_items(tmp_path):
    sidecar = tmp_path / "sidecars" / "instagram" / "2026" / "07" / "p1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "instagram",
            "entity": {"id": "u1", "name": "User One"},
            "content": {"type": "photo", "id": "p1", "filename": "p1.jpg"},
            "file": {"path": "media/instagram/p1.jpg", "size": 10, "sha256": "abc"},
        }),
        encoding="utf-8",
    )

    report = scan_sidecars(tmp_path)

    assert report.sidecars_scanned == 1
    assert report.reconstructable_tables["media_items"] == 1
    assert report.artifacts_by_source["instagram"] == 1


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
