import hashlib
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
