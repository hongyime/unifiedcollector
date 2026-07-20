import json

from src.core import vault


def _valid_sidecar_payload():
    return {
        "schema_version": 1,
        "artifact_kind": "media",
        "artifact_id": "instagram:post_123",
        "source": "instagram",
        "ingest_path": "extension",
        "collection_priority": None,
        "entity": {
            "id": "u1",
            "name": "User One",
        },
        "content": {
            "type": "image",
            "kind": "post",
            "id": "post_123",
            "filename": "a.jpg",
            "source_url": "https://example.com/p/123",
            "caption": "hello",
            "text": None,
        },
        "file": {
            "path": "media/instagram/a.jpg",
            "absolute_path": "Z:/unifiedcollector/media/instagram/a.jpg",
            "size": 3,
            "width": 10,
            "height": 20,
            "sha256": "abc123",
        },
        "timestamps": {
            "collected_at": "2026-07-20T00:00:00+00:00",
            "posted_at": None,
            "discovered_at": None,
        },
        "raw_payload": {
            "inline": {"id": "123"},
            "path": None,
        },
        "provenance": {
            "platform_ids": None,
            "collection_account": "bryan",
            "scrape_run_id": None,
            "extension_version": None,
            "request_url": None,
            "http_status": None,
            "rate_limit_scope": None,
            "partial": False,
        },
        "metadata": {},
    }


def test_relative_to_vault_uses_stable_relative_paths(tmp_path):
    root = tmp_path / "vault"
    media = root / "media" / "instagram" / "a.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")

    assert vault.relative_to_vault(media, root) == "media/instagram/a.jpg"


def test_validate_sidecar_payload_and_file_accept_valid_payload(tmp_path):
    payload = _valid_sidecar_payload()

    assert vault.validate_sidecar_payload(payload) == []

    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    assert vault.validate_sidecar_file(sidecar) == []


def test_validate_sidecar_payload_reports_missing_required_fields():
    payload = _valid_sidecar_payload()
    del payload["source"]
    del payload["content"]["id"]
    del payload["file"]["path"]

    errors = vault.validate_sidecar_payload(payload)

    assert "source" in errors
    assert "content.id" in errors
    assert "file.path" in errors


def test_write_media_sidecar_records_rebuild_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    media = root / "media" / "instagram" / "a.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"abc")

    result = vault.write_media_sidecar(
        source="instagram",
        entity_id="u1",
        entity_name="User One",
        content_type="image",
        content_id="post/123",
        filename="a.jpg",
        file_path=str(media),
        file_size=3,
        width=10,
        height=20,
        sha256="abc123",
        source_url="https://example.com/p/123",
        metadata={
            "caption": "hello",
            "raw": {"id": "123"},
            "collection_account": "bryan",
        },
        ingest_path="extension",
        kind="post",
        root=root,
    )

    assert result.ok is True
    assert result.relative_path is not None
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert payload["artifact_id"] == "instagram:post/123"
    assert payload["file"]["path"] == "media/instagram/a.jpg"
    assert payload["content"]["caption"] == "hello"
    assert payload["raw_payload"]["inline"] == {"id": "123"}
    assert payload["provenance"]["collection_account"] == "bryan"


def test_write_media_sidecar_rejects_invalid_payload_before_write(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    media = root / "media" / "instagram" / "a.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"abc")

    result = vault.write_media_sidecar(
        source="",
        entity_id="u1",
        entity_name="User One",
        content_type="image",
        content_id="1",
        filename="a.jpg",
        file_path=str(media),
        file_size=3,
        width=10,
        height=20,
        sha256="abc123",
        source_url=None,
        metadata={},
        ingest_path="headless",
        kind=None,
        root=root,
    )

    assert result.enabled is True
    assert result.ok is False
    assert "source is required" in result.error
    assert list(root.rglob("*.json")) == []


def test_write_media_sidecar_reports_missing_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    missing = tmp_path / "missing"
    result = vault.write_media_sidecar(
        source="instagram",
        entity_id="u1",
        entity_name="User One",
        content_type="image",
        content_id="1",
        filename="a.jpg",
        file_path=str(tmp_path / "a.jpg"),
        file_size=None,
        width=None,
        height=None,
        sha256=None,
        source_url=None,
        metadata={},
        ingest_path="headless",
        kind=None,
        root=missing,
    )

    assert result.enabled is True
    assert result.ok is False
    assert "vault" in result.error.lower()
