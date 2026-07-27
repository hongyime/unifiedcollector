import gzip
import hashlib
import json

import pytest

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


def test_blob_path_for_sha256_uses_sharded_media_blob_path(tmp_path):
    root = tmp_path / "vault"
    digest = "ab" + "cd" + ("1" * 60)

    path = vault.blob_path_for_sha256(digest, extension=".jpg", root=root)

    assert path == root / "media" / "blobs" / "ab" / "cd" / f"{digest}.jpg"


def test_blob_path_for_sha256_rejects_invalid_digest(tmp_path):
    with pytest.raises(ValueError, match="64-character hex"):
        vault.blob_path_for_sha256("abc123", extension="jpg", root=tmp_path)


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
    assert payload["rebuild"]["target_tables"] == ["media_items"]
    assert "file.sha256" in payload["rebuild"]["required_fields"]


def test_write_media_sidecar_records_canonical_blob_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    media = root / "media" / "instagram" / "a.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"abc")
    digest = "ab" + "cd" + ("2" * 60)

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
        sha256=digest,
        source_url="https://example.com/p/123",
        metadata={},
        ingest_path="extension",
        kind="post",
        root=root,
    )

    assert result.ok is True
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert payload["file"]["path"] == "media/instagram/a.jpg"
    assert payload["file"]["blob_path"] == f"media/blobs/ab/cd/{digest}.jpg"
    assert payload["file"]["blob_absolute_path"] == str(root / "media" / "blobs" / "ab" / "cd" / f"{digest}.jpg")


def test_write_atomic_artifact_success_writes_blob_sidecar_and_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    root.mkdir()
    db_seen = []

    result = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="chat/1/message/2/photo",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        metadata={"collection_account": "bryan"},
        root=root,
        db_writer=db_seen.append,
    )

    assert result.ok is True
    assert result.partial is False
    assert result.db_recorded is True
    assert result.duplicate_blob is False
    assert result.path.exists()
    assert result.path.read_bytes() == b"hello"
    assert result.blob_relative_path.startswith("media/blobs/")
    assert result.sidecar.ok is True
    sidecar = json.loads(result.sidecar.path.read_text(encoding="utf-8"))
    assert sidecar["metadata"]["original_artifact_id"] == "chat/1/message/2/photo"
    assert sidecar["metadata"]["collection_account"] == "bryan"
    assert sidecar["provenance"]["collection_account"] == "bryan"
    assert sidecar["provenance"]["partial"] is False
    assert db_seen[0].sha256 == result.sha256


def test_write_atomic_artifact_records_raw_payload_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    root.mkdir()

    result = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="chat/1/message/2/photo",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        metadata={
            "raw_payload_path": "raw/telegram/2026/07/message-2.json",
            "raw_payload_sidecar_path": "sidecars/artifacts/telegram/message-2.json",
            "raw_payload_artifact_id": "chat/1/message/2",
        },
        root=root,
    )

    assert result.ok is True
    sidecar = json.loads(result.sidecar.path.read_text(encoding="utf-8"))
    assert sidecar["raw_payload"]["path"] == "raw/telegram/2026/07/message-2.json"
    assert sidecar["raw_payload"]["sidecar_path"] == "sidecars/artifacts/telegram/message-2.json"
    assert sidecar["raw_payload"]["artifact_id"] == "chat/1/message/2"
    assert sidecar["raw_payload"]["refs"] == [
        {
            "path": "raw/telegram/2026/07/message-2.json",
            "sidecar_path": "sidecars/artifacts/telegram/message-2.json",
            "artifact_id": "chat/1/message/2",
        }
    ]


def test_write_atomic_artifact_reports_sidecar_failure_as_partial(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()

    def fail_sidecar(**_kwargs):
        return vault.SidecarResult(enabled=True, ok=False, error="disk full")

    monkeypatch.setattr(vault, "write_artifact_sidecar", fail_sidecar)

    result = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="message/1",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        root=root,
    )

    assert result.ok is False
    assert result.partial is True
    assert result.path.exists()
    assert "sidecar write failed" in result.error


def test_write_atomic_artifact_rejects_expected_checksum_mismatch(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()

    result = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="message/1",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        expected_sha256="0" * 64,
        root=root,
    )

    assert result.ok is False
    assert result.partial is False
    assert "checksum mismatch" in result.error
    assert not list((root / "media").rglob("*")) if (root / "media").exists() else True


def test_write_atomic_artifact_reports_missing_vault(tmp_path):
    result = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="message/1",
        artifact_kind="media_blob",
        data=b"hello",
        root=tmp_path / "missing",
    )

    assert result.ok is False
    assert result.partial is False
    assert "vault" in result.error.lower()


def test_write_atomic_artifact_reuses_duplicate_blob(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    first = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="message/1",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        root=root,
    )
    second = vault.write_atomic_artifact(
        source="whatsapp",
        artifact_id="message/2",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        root=root,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.duplicate_blob is True
    assert second.path == first.path
    assert len(list((root / "media" / "blobs").rglob("*.jpg"))) == 1
    assert len(list((root / "sidecars" / "artifacts").rglob("*.json"))) == 2


def test_write_atomic_artifact_from_path_moves_temp_file(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    temp_dir = root / "media" / ".tmp"
    temp_dir.mkdir(parents=True)
    source_path = temp_dir / "video.part"
    data = b"streamed video bytes"
    source_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    result = vault.write_atomic_artifact_from_path(
        source="website",
        artifact_id="video/1",
        artifact_kind="media_blob",
        source_path=source_path,
        extension=".mp4",
        metadata={"source_url": "https://example.com/v.mp4"},
        expected_sha256=digest,
        root=root,
        delete_source=True,
    )

    assert result.ok is True
    assert result.partial is False
    assert result.path == root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.mp4"
    assert result.path.read_bytes() == data
    assert source_path.exists() is False
    assert result.sidecar.ok is True
    sidecar = json.loads(result.sidecar.path.read_text(encoding="utf-8"))
    assert sidecar["metadata"]["original_artifact_id"] == "video/1"
    assert sidecar["metadata"]["source_url"] == "https://example.com/v.mp4"


def test_write_atomic_artifact_from_path_records_raw_payload_reference(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    source_path = tmp_path / "photo.part"
    source_path.write_bytes(b"photo bytes")

    result = vault.write_atomic_artifact_from_path(
        source="whatsapp",
        artifact_id="chat/1/message/2/photo",
        artifact_kind="media_blob",
        source_path=source_path,
        extension=".webp",
        metadata={"raw_payload_path": "raw/whatsapp/2026/07/message-2.jsonl"},
        root=root,
        delete_source=True,
    )

    assert result.ok is True
    sidecar = json.loads(result.sidecar.path.read_text(encoding="utf-8"))
    assert sidecar["raw_payload"]["path"] == "raw/whatsapp/2026/07/message-2.jsonl"
    assert sidecar["raw_payload"]["refs"][0]["path"] == "raw/whatsapp/2026/07/message-2.jsonl"


def test_write_atomic_artifact_from_path_reuses_duplicate_blob(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    data = b"same bytes"
    first_path = tmp_path / "one.part"
    second_path = tmp_path / "two.part"
    first_path.write_bytes(data)
    second_path.write_bytes(data)

    first = vault.write_atomic_artifact_from_path(
        source="website",
        artifact_id="video/1",
        artifact_kind="media_blob",
        source_path=first_path,
        extension=".mp4",
        root=root,
        delete_source=True,
    )
    second = vault.write_atomic_artifact_from_path(
        source="search",
        artifact_id="video/2",
        artifact_kind="media_blob",
        source_path=second_path,
        extension=".mp4",
        root=root,
        delete_source=True,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.duplicate_blob is True
    assert second.path == first.path
    assert second_path.exists() is False
    assert len(list((root / "media" / "blobs").rglob("*.mp4"))) == 1
    assert len(list((root / "sidecars" / "artifacts").rglob("*.json"))) == 2


def test_write_atomic_artifact_from_path_can_keep_source_file(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    source_path = tmp_path / "keep.part"
    data = b"keep source"
    source_path.write_bytes(data)

    result = vault.write_atomic_artifact_from_path(
        source="website",
        artifact_id="video/keep",
        artifact_kind="media_blob",
        source_path=source_path,
        extension=".mp4",
        root=root,
        delete_source=False,
    )

    assert result.ok is True
    assert source_path.exists() is True
    assert source_path.read_bytes() == data
    assert result.path.read_bytes() == data


def test_write_atomic_artifact_reports_db_failure_as_partial(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()

    def fail_db(_result):
        raise RuntimeError("db down")

    result = vault.write_atomic_artifact(
        source="telegram",
        artifact_id="message/1",
        artifact_kind="media_blob",
        data=b"hello",
        extension=".jpg",
        root=root,
        db_writer=fail_db,
    )

    assert result.ok is False
    assert result.partial is True
    assert result.db_recorded is False
    assert result.path.exists()
    assert "db write failed" in result.error


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


@pytest.mark.asyncio
async def test_verify_media_item_db_consistency_accepts_vault_artifact_sidecar(tmp_path):
    media = tmp_path / "media" / "telegram" / "photo.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"photo")

    class Conn:
        async def fetchrow(self, *_args, **_kwargs):
            return {
                "file_path": str(media),
                "file_size": 5,
                "sha256": "a" * 64,
                "metadata": {
                    "vault_artifact": {
                        "ok": True,
                        "sidecar_path": "sidecars/artifacts/telegram/photo.json",
                    }
                },
            }

    result = await vault.verify_media_item_db_consistency(
        Conn(),
        source="telegram",
        content_id="msg-1",
        file_path=media,
        file_size=5,
        sha256="a" * 64,
        sidecar_path="sidecars/artifacts/telegram/photo.json",
    )

    assert result.ok is True
    assert result.errors == ()


@pytest.mark.asyncio
async def test_vault_artifact_counts_checks_both_sidecar_metadata_shapes():
    class Conn:
        async def fetchrow(self, query, **_kwargs):
            assert "vault_sidecar" in query
            assert "vault_artifact" in query
            assert "idx_media_missing_occurrence_sidecar" in query
            assert "to_regclass" in query
            return {
                "sidecar_failures": 0,
                "artifacts_queued": 0,
                "artifacts_partial": 2,
                "artifacts_missing_sidecar": 3,
            }

    counts = await vault.vault_artifact_counts(Conn())

    assert counts["artifacts_partial"] == 2
    assert counts["artifacts_missing_sidecar"] == 3
    assert counts["artifacts_missing_sidecar_estimated"] is True


def test_write_artifact_sidecar_records_json_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    artifact = root / "media" / "strava" / "clubs" / "clubs_1.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"clubs":[]}', encoding="utf-8")

    result = vault.write_artifact_sidecar(
        source="strava",
        artifact_kind="json",
        file_path=str(artifact),
        metadata={
            "purpose": "clubs",
            "ingest_path": "api",
            "collection_account": "bryan",
            "request_url": "https://www.strava.com/api/v3/athlete/clubs",
            "http_status": 200,
        },
        root=root,
    )

    assert result.ok is True
    assert result.relative_path is not None
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert payload["artifact_kind"] == "json"
    assert payload["source"] == "strava"
    assert payload["ingest_path"] == "api"
    assert payload["file"]["path"] == "media/strava/clubs/clubs_1.json"
    assert payload["file"]["size"] == artifact.stat().st_size
    assert len(payload["file"]["sha256"]) == 64
    assert payload["metadata"]["purpose"] == "clubs"
    assert payload["provenance"]["collection_account"] == "bryan"
    assert payload["provenance"]["request_url"].endswith("/athlete/clubs")
    assert payload["provenance"]["http_status"] == 200
    assert payload["rebuild"]["target_tables"] == []
    assert "file.sha256" in payload["rebuild"]["required_fields"]


def test_write_raw_payload_records_raw_file_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    root.mkdir()

    result = vault.write_raw_payload(
        source="telegram",
        artifact_id="chat/123/message/456",
        payload={"id": 456, "text": "hello"},
        metadata={"collection_account": "bryan"},
        target_tables=["telegram_messages"],
        root=root,
    )

    assert result.ok is True
    assert result.relative_path is not None
    assert result.relative_path.startswith("raw/telegram/")
    raw_payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert raw_payload == {"id": 456, "text": "hello"}
    sidecar = json.loads(result.sidecar.path.read_text(encoding="utf-8"))
    assert sidecar["artifact_kind"] == "raw_payload"
    assert sidecar["file"]["path"] == result.relative_path
    assert sidecar["metadata"]["raw_payload"] is True
    assert sidecar["metadata"]["artifact_id"] == "chat/123/message/456"
    assert sidecar["provenance"]["collection_account"] == "bryan"
    assert sidecar["rebuild"]["target_tables"] == ["telegram_messages"]


def test_write_raw_payload_can_store_compressed_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    root = tmp_path / "vault"
    root.mkdir()

    result = vault.write_raw_payload(
        source="search",
        artifact_id="crawl/example",
        payload=[{"url": "https://example.com/a"}, {"url": "https://example.com/b"}],
        metadata={"collection_priority": "lower_tier"},
        target_tables=["search_results"],
        extension="jsonl.gz",
        root=root,
    )

    assert result.ok is True
    assert result.relative_path.endswith(".jsonl.gz")
    with gzip.open(result.path, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert rows == [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]
    sidecar = json.loads(result.sidecar.path.read_text(encoding="utf-8"))
    assert sidecar["file"]["path"] == result.relative_path
    assert sidecar["file"]["size"] == result.path.stat().st_size
    assert sidecar["metadata"]["compression"] == "gzip"
    with gzip.open(result.path, "rb") as handle:
        decompressed = handle.read()
    assert sidecar["metadata"]["uncompressed_size"] == len(decompressed)
    assert sidecar["rebuild"]["target_tables"] == ["search_results"]


def test_write_raw_payload_reports_missing_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)

    result = vault.write_raw_payload(
        source="telegram",
        artifact_id="message/1",
        payload={"id": 1},
        root=tmp_path / "missing",
    )

    assert result.ok is False
    assert "vault" in result.error.lower()


def test_ensure_vault_available_raises_for_missing_vault(tmp_path):
    with pytest.raises(RuntimeError, match="collector vault unavailable"):
        vault.ensure_vault_available(tmp_path / "missing")


def test_assert_media_write_allowed_accepts_media_inside_vault(tmp_path):
    root = tmp_path / "vault"
    media = root / "media"
    media.mkdir(parents=True)

    vault.assert_media_write_allowed(media / "instagram" / "a.jpg", root=root, media_root=media)


def test_assert_media_write_allowed_rejects_missing_media_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()

    with pytest.raises(RuntimeError, match="media root missing"):
        vault.assert_media_write_allowed(root / "media" / "a.jpg", root=root, media_root=root / "media")


def test_assert_media_write_allowed_rejects_dest_outside_media_root(tmp_path):
    root = tmp_path / "vault"
    media = root / "media"
    media.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="escapes media root"):
        vault.assert_media_write_allowed(tmp_path / "other" / "a.jpg", root=root, media_root=media)


def test_assert_media_write_allowed_rejects_unlinked_media_mount(tmp_path):
    root = tmp_path / "vault"
    (root / "media").mkdir(parents=True)
    media = tmp_path / "detached_media"
    media.mkdir()

    with pytest.raises(RuntimeError, match="not linked to vault media"):
        vault.assert_media_write_allowed(media / "instagram" / "a.jpg", root=root, media_root=media)
