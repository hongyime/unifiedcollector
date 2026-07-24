import hashlib
import json
from contextlib import asynccontextmanager

import pytest

from src.core import base_collector
from src.core.base_collector import BaseCollector


class _Conn:
    async def fetchval(self, *_args, **_kwargs):
        return 1


class _Pool:
    def __init__(self):
        self.conn = _Conn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _Collector(BaseCollector):
    SOURCE_NAME = "github"

    async def collect(self, target=None):
        return None

    async def download_media(self, item):
        return None


def _collector(monkeypatch):
    monkeypatch.setattr(base_collector, "check_drive", lambda: True)
    coll = _Collector()
    coll.pool = _Pool()
    return coll


def test_save_file_writes_canonical_vault_blob(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    drive_root = tmp_path / "media"
    vault_root.mkdir()
    monkeypatch.setattr(base_collector, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(base_collector, "DRIVE_PATH", str(drive_root))
    monkeypatch.setattr(base_collector, "assert_media_write_allowed", lambda *args, **kwargs: None)
    coll = _collector(monkeypatch)
    data = b"base collector media"
    digest = hashlib.sha256(data).hexdigest()
    filename = coll.build_filename("u1", "User One", "image", "post123", extension="jpg")

    path = coll.save_file(data, filename, metadata={"raw": {"id": "post123"}})

    assert path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert path.read_bytes() == data
    assert "post123" in coll._known_ids
    assert not (drive_root / "github" / filename).exists()
    sidecar = next((vault_root / "sidecars" / "artifacts" / "github").rglob("*.json"))
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["metadata"]["filename"] == filename
    assert payload["metadata"]["raw"] == {"id": "post123"}
    assert payload["metadata"]["legacy_path"].endswith(filename)


@pytest.mark.asyncio
async def test_duplicate_skip_keeps_canonical_vault_blob(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    monkeypatch.setattr(base_collector, "VAULT_ROOT", vault_root)
    coll = _collector(monkeypatch)
    data = b"shared blob"
    digest = hashlib.sha256(data).hexdigest()
    blob = vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(data)

    inserted = await coll.insert_media_item(
        entity_id="u1",
        entity_name="User One",
        content_type="image",
        content_id="dup-1",
        filename=blob.name,
        file_path=str(blob),
        file_size=len(data),
        sha256=digest,
    )

    assert inserted is False
    assert blob.read_bytes() == data


@pytest.mark.asyncio
async def test_duplicate_skip_can_remove_legacy_occurrence_file(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    monkeypatch.setattr(base_collector, "VAULT_ROOT", vault_root)
    coll = _collector(monkeypatch)
    data = b"legacy duplicate"
    digest = hashlib.sha256(data).hexdigest()
    legacy = tmp_path / "media" / "github" / "dup.jpg"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(data)

    inserted = await coll.insert_media_item(
        entity_id="u1",
        entity_name="User One",
        content_type="image",
        content_id="dup-2",
        filename=legacy.name,
        file_path=str(legacy),
        file_size=len(data),
        sha256=digest,
    )

    assert inserted is False
    assert not legacy.exists()
