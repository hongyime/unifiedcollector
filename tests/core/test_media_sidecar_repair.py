from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.core import vault
from src.core.media_sidecar_repair import repair_missing_media_sidecars


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_query = None
        self.fetch_args = None
        self.updates = []
        self.dlq = []

    async def fetch(self, query, *args):
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
    assert report.repaired == 0
    assert conn.updates == []
    assert "vault_artifact" in conn.fetch_query
    assert "sidecar_path" in conn.fetch_query


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
