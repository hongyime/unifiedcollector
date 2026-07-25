import json

from src.core.vault_inspect import inspect_vault


def test_inspect_vault_summarizes_sidecar_without_db(tmp_path):
    root = tmp_path / "vault"
    media = root / "media" / "instagram" / "p1.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    raw = root / "raw" / "instagram" / "p1.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"id":"p1"}', encoding="utf-8")
    sidecar = root / "sidecars" / "instagram" / "2026" / "07" / "p1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "artifact_id": "instagram:p1",
            "source": "instagram",
            "ingest_path": "extension",
            "collection_priority": "tier1",
            "entity": {"id": "u1", "name": "User One"},
            "content": {
                "id": "p1",
                "type": "photo",
                "filename": "p1.jpg",
                "source_url": "https://example.test/p/1",
            },
            "file": {
                "path": "media/instagram/p1.jpg",
                "size": 5,
                "sha256": "abc",
            },
            "raw_payload": {
                "path": "raw/instagram/p1.json",
                "refs": [{"path": "raw/instagram/p1.json", "artifact_id": "raw:p1"}],
            },
            "rebuild": {"target_tables": ["media_items"]},
            "provenance": {"collection_account": "bryan"},
        }),
        encoding="utf-8",
    )

    report = inspect_vault(root, source="instagram", limit=5)

    assert report.sidecars_scanned == 1
    assert len(report.artifacts) == 1
    artifact = report.artifacts[0]
    assert artifact["sidecar_path"] == "sidecars/instagram/2026/07/p1.json"
    assert artifact["file"]["exists"] is True
    assert artifact["raw_payload_refs"] == [
        {"path": "raw/instagram/p1.json", "sidecar_path": None, "artifact_id": None},
        {"path": "raw/instagram/p1.json", "artifact_id": "raw:p1"},
    ]
    assert artifact["rebuild"]["target_tables"] == ["media_items"]


def test_inspect_vault_maps_container_media_paths(tmp_path):
    root = tmp_path / "vault"
    media = root / "media" / "telegram" / "p1.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"image")
    sidecar = root / "sidecars" / "telegram" / "2026" / "07" / "p1.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "artifact_id": "telegram:p1",
            "source": "telegram",
            "file": {"path": "/media/telegram/p1.jpg"},
        }),
        encoding="utf-8",
    )

    report = inspect_vault(root, source="telegram", limit=5)

    assert report.artifacts[0]["file"]["exists"] is True
    assert report.missing_files == []
