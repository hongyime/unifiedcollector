from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from src.core import vault
from src.core.media_sidecar_repair import (
    repair_media_file_paths_from_blobs,
    repair_missing_media_sidecars,
    repair_partial_vault_artifacts,
)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_query = None
        self.fetch_args = None
        self.updates = []
        self.dlq = []

    async def fetch(self, query, *args, **kwargs):
        self.fetch_query = query
        self.fetch_args = args
        return self.rows

    async def execute(self, query, *args):
        if "UPDATE media_items" in query:
            self.updates.append(args)
        elif "INSERT INTO dead_letter_queue" in query:
            self.dlq.append(args)
        return "OK"


def _row(root):
    media = root / "media" / "telegram" / "photo.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"photo")
    return {
        "id": "media-1",
        "source": "telegram",
        "entity_id": "chat-1",
        "entity_name": "Chat One",
        "content_type": "image",
        "content_id": "msg-1",
        "filename": "photo.jpg",
        "file_path": str(media),
        "file_size": 5,
        "width": 10,
        "height": 20,
        "sha256": "a" * 64,
        "source_url": "https://example.com/message/1",
        "metadata": {"caption": "hello"},
        "ingest_path": "messaging",
        "kind": "message",
        "collected_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_repair_missing_media_sidecars_dry_run_does_not_write(tmp_path):
    conn = FakeConn([_row(tmp_path)])

    report = await repair_missing_media_sidecars(
        conn,
        source="telegram",
        limit=10,
        vault_root=tmp_path,
        dry_run=True,
    )

    assert report.scanned == 1
    assert report.skipped == 1
    assert report.would_repair == 1
    assert report.repaired == 0
    assert conn.updates == []
    assert "vault_artifact" in conn.fetch_query
    assert "sidecar_path" in conn.fetch_query
    assert "ORDER BY content_id" in conn.fetch_query
    assert conn.fetch_args == ("telegram", "", 10)


@pytest.mark.asyncio
async def test_repair_missing_media_sidecars_writes_sidecar_and_updates_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    conn = FakeConn([_row(tmp_path)])

    report = await repair_missing_media_sidecars(
        conn,
        source="telegram",
        limit=10,
        vault_root=tmp_path,
    )

    assert report.scanned == 1
    assert report.repaired == 1
    assert report.failed == 0
    assert len(conn.updates) == 1
    sidecar_meta = json.loads(conn.updates[0][1])
    assert sidecar_meta["vault_sidecar"]["ok"] is True
    assert sidecar_meta["vault_sidecar"]["repaired"] is True
    sidecar_path = tmp_path / sidecar_meta["vault_sidecar"]["path"]
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["source"] == "telegram"
    assert payload["content"]["id"] == "msg-1"
    assert payload["metadata"]["sidecar_repair"]["original_media_item_id"] == "media-1"
    assert report.next_cursor == "msg-1"


@pytest.mark.asyncio
async def test_repair_missing_media_sidecars_accepts_string_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    row = _row(tmp_path)
    row["metadata"] = json.dumps({"caption": "from legacy string"})
    conn = FakeConn([row])

    report = await repair_missing_media_sidecars(conn, vault_root=tmp_path)

    assert report.repaired == 1
    sidecar_meta = json.loads(conn.updates[0][1])
    payload = json.loads((tmp_path / sidecar_meta["vault_sidecar"]["path"]).read_text(encoding="utf-8"))
    assert payload["content"]["caption"] == "from legacy string"


@pytest.mark.asyncio
async def test_repair_missing_media_sidecars_skips_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    row = _row(tmp_path)
    (tmp_path / "media" / "telegram" / "photo.jpg").unlink()
    conn = FakeConn([row])

    report = await repair_missing_media_sidecars(conn, source="telegram", vault_root=tmp_path)

    assert report.scanned == 1
    assert report.repaired == 0
    assert report.skipped == 1
    assert report.file_missing == 1
    assert report.failures[0]["error"] == "media file is missing"
    assert conn.updates == []


@pytest.mark.asyncio
async def test_repair_missing_media_sidecars_rejects_cursor_without_source(tmp_path):
    conn = FakeConn([_row(tmp_path)])

    with pytest.raises(ValueError, match="cursor_after requires source"):
        await repair_missing_media_sidecars(conn, cursor_after="abc", vault_root=tmp_path)


@pytest.mark.asyncio
async def test_repair_partial_vault_artifacts_writes_artifact_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    row = _row(tmp_path)
    media = tmp_path / "media" / "telegram" / "photo.jpg"
    row["sha256"] = hashlib.sha256(media.read_bytes()).hexdigest()
    row["metadata"] = {
        "vault_artifact": {
            "ok": False,
            "partial": True,
            "error": "sidecar write failed: out of memory",
            "duplicate_blob": False,
        }
    }
    conn = FakeConn([row])

    report = await repair_partial_vault_artifacts(conn, vault_root=tmp_path)

    assert report.scanned == 1
    assert report.repaired == 1
    assert report.failed == 0
    assert len(conn.updates) == 1
    artifact_meta = json.loads(conn.updates[0][1])["vault_artifact"]
    assert artifact_meta["ok"] is True
    assert artifact_meta["partial"] is False
    assert artifact_meta["sidecar_path"]
    assert (tmp_path / artifact_meta["sidecar_path"]).is_file()
    assert report.next_cursor == "msg-1"


@pytest.mark.asyncio
async def test_repair_partial_vault_artifacts_dry_run_skips_hashing(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    row = _row(tmp_path)
    row["sha256"] = "b" * 64
    row["metadata"] = {"vault_artifact": {"ok": False, "partial": True}}
    conn = FakeConn([row])

    report = await repair_partial_vault_artifacts(conn, vault_root=tmp_path, dry_run=True)

    assert report.scanned == 1
    assert report.skipped == 1
    assert report.failed == 0
    assert conn.updates == []


@pytest.mark.asyncio
async def test_repair_partial_vault_artifacts_keeps_checksum_mismatch_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    row = _row(tmp_path)
    row["sha256"] = "b" * 64
    row["metadata"] = {"vault_artifact": {"ok": False, "partial": True}}
    conn = FakeConn([row])

    report = await repair_partial_vault_artifacts(conn, vault_root=tmp_path)

    assert report.scanned == 1
    assert report.repaired == 0
    assert report.failed == 1
    assert "sha256 mismatch" in report.failures[0]["error"]
    assert conn.updates == []


@pytest.mark.asyncio
async def test_repair_media_file_paths_from_blobs_updates_missing_legacy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "SIDECARS_ENABLED", True)
    digest = "c" * 64
    blob = tmp_path / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"blob")
    row = _row(tmp_path)
    row["file_path"] = str(tmp_path / "legacy" / "missing.jpg")
    row["file_size"] = 999
    row["sha256"] = digest
    conn = FakeConn([row])

    report = await repair_media_file_paths_from_blobs(
        conn,
        source="telegram",
        vault_root=tmp_path,
    )

    assert report.scanned == 1
    assert report.repaired == 1
    assert report.would_repair == 0
    assert report.already_ok == 0
    assert report.failed == 0
    assert report.next_cursor == "msg-1"
    assert len(conn.updates) == 1
    media_id, file_path, file_size, metadata_json = conn.updates[0]
    assert media_id == "media-1"
    assert file_path == str(blob)
    assert file_size == 4
    metadata = json.loads(metadata_json)
    assert metadata["vault_sidecar"]["ok"] is True
    assert metadata["file_path_repair"]["legacy_path"].endswith("missing.jpg")
    assert (tmp_path / metadata["vault_sidecar"]["path"]).is_file()


@pytest.mark.asyncio
async def test_repair_media_file_paths_from_blobs_reports_missing_blob(tmp_path):
    row = _row(tmp_path)
    row["file_path"] = str(tmp_path / "legacy" / "missing.jpg")
    row["sha256"] = "d" * 64
    conn = FakeConn([row])

    report = await repair_media_file_paths_from_blobs(
        conn,
        source="telegram",
        vault_root=tmp_path,
    )

    assert report.scanned == 1
    assert report.repaired == 0
    assert report.failed == 1
    assert report.file_missing == 1
    assert report.failures[0]["error"] == "canonical sha256 blob is missing"
    assert conn.updates == []


@pytest.mark.asyncio
async def test_repair_media_file_paths_from_blobs_requires_source(tmp_path):
    conn = FakeConn([_row(tmp_path)])

    with pytest.raises(ValueError, match="source is required"):
        await repair_media_file_paths_from_blobs(conn, source="", vault_root=tmp_path)


@pytest.mark.asyncio
async def test_repair_media_file_paths_from_blobs_skips_existing_good_path(tmp_path):
    row = _row(tmp_path)
    row["sha256"] = hashlib.sha256((tmp_path / "media" / "telegram" / "photo.jpg").read_bytes()).hexdigest()
    conn = FakeConn([row])

    report = await repair_media_file_paths_from_blobs(
        conn,
        source="telegram",
        vault_root=tmp_path,
    )

    assert report.scanned == 1
    assert report.repaired == 0
    assert report.already_ok == 1
    assert report.skipped == 1
    assert conn.updates == []
