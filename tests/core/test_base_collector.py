import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.core import base_collector
from src.core.base_collector import BaseCollector


class _Conn:
    def __init__(self):
        self.execute_calls = []

    async def fetchval(self, *_args, **_kwargs):
        return 1

    async def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        return "INSERT 0 1"


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


class _TelegramCollector(BaseCollector):
    SOURCE_NAME = "telegram"

    async def collect(self, target=None):
        return None

    async def download_media(self, item):
        return None


def _collector(monkeypatch):
    monkeypatch.setattr(base_collector, "check_drive", lambda: True)
    coll = _Collector()
    coll.pool = _Pool()
    return coll


class _InsertConn:
    def __init__(self, row):
        self.row = row
        self.execute_calls = []

    async def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        return "INSERT 0 1"

    async def fetchrow(self, *_args, **_kwargs):
        return self.row


class _InsertPool:
    def __init__(self, row):
        self.conn = _InsertConn(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.mark.asyncio
async def test_run_queues_pause_when_drive_missing(monkeypatch):
    monkeypatch.setattr(base_collector, "check_drive", lambda: False)
    coll = _Collector()
    coll.pool = _Pool()

    with pytest.raises(RuntimeError, match="Drive not mounted"):
        await coll.run(["target"])

    assert coll.pool.conn.execute_calls
    args, _kwargs = coll.pool.conn.execute_calls[0]
    assert "dead_letter_queue" in args[0]
    assert args[1:] == (
        "github",
        "github",
        "__vault_unavailable__",
        "file-heavy collection paused: Drive not mounted. Pausing github.",
    )


@pytest.mark.asyncio
async def test_run_queues_pause_when_vault_write_check_fails(monkeypatch):
    monkeypatch.setattr(base_collector, "check_drive", lambda: True)

    def fail_write_check(*_args, **_kwargs):
        raise RuntimeError("media mirror missing")

    monkeypatch.setattr(base_collector, "assert_media_write_allowed", fail_write_check)
    coll = _Collector()
    coll.pool = _Pool()

    with pytest.raises(RuntimeError, match="Vault/media path not writable"):
        await coll.run(["target"])

    assert coll.pool.conn.execute_calls
    args, _kwargs = coll.pool.conn.execute_calls[0]
    assert "dead_letter_queue" in args[0]
    assert args[1:4] == ("github", "github", "__vault_unavailable__")
    assert "file-heavy collection paused: Vault/media path not writable. Pausing github: media mirror missing" == args[4]


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


@pytest.mark.asyncio
async def test_insert_media_item_records_vault_db_consistency_ok(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    media_file = vault_root / "media" / "blobs" / "aa" / "bb" / "asset.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"asset")
    digest = hashlib.sha256(b"asset").hexdigest()
    monkeypatch.setattr(base_collector, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(
        base_collector,
        "write_media_sidecar",
        lambda **_kwargs: SimpleNamespace(
            enabled=True,
            ok=True,
            relative_path="sidecars/media/telegram/asset.json",
            error=None,
        ),
    )
    row = {
        "file_path": str(media_file),
        "file_size": media_file.stat().st_size,
        "sha256": digest,
        "metadata": {
            "vault_sidecar": {
                "ok": True,
                "path": "sidecars/media/telegram/asset.json",
            }
        },
    }
    coll = _TelegramCollector()
    coll.pool = _InsertPool(row)

    inserted = await coll.insert_media_item(
        entity_id="chat1",
        entity_name="Chat One",
        content_type="photo",
        content_id="m1",
        filename="asset.jpg",
        file_path=str(media_file),
        file_size=media_file.stat().st_size,
        sha256=digest,
        metadata={},
    )

    assert inserted is True
    consistency_updates = [
        args for args, _kwargs in coll.pool.conn.execute_calls
        if "vault_artifact_db_consistency" in " ".join(map(str, args))
    ]
    assert consistency_updates
    assert not any("dead_letter_queue" in args[0] for args, _kwargs in coll.pool.conn.execute_calls)


@pytest.mark.asyncio
async def test_insert_media_item_queues_dlq_when_vault_db_consistency_fails(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    media_file = vault_root / "media" / "blobs" / "aa" / "bb" / "asset.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"asset")
    digest = hashlib.sha256(b"asset").hexdigest()
    monkeypatch.setattr(base_collector, "VAULT_ROOT", vault_root)
    monkeypatch.setattr(
        base_collector,
        "write_media_sidecar",
        lambda **_kwargs: SimpleNamespace(
            enabled=True,
            ok=True,
            relative_path="sidecars/media/telegram/asset.json",
            error=None,
        ),
    )
    coll = _TelegramCollector()
    coll.pool = _InsertPool(
        {
            "file_path": str(media_file),
            "file_size": media_file.stat().st_size,
            "sha256": "0" * 64,
            "metadata": {
                "vault_sidecar": {
                    "ok": True,
                    "path": "sidecars/media/telegram/asset.json",
                }
            },
        }
    )

    inserted = await coll.insert_media_item(
        entity_id="chat1",
        entity_name="Chat One",
        content_type="photo",
        content_id="m1",
        filename="asset.jpg",
        file_path=str(media_file),
        file_size=media_file.stat().st_size,
        sha256=digest,
        metadata={},
    )

    assert inserted is True
    dlq_calls = [
        args for args, _kwargs in coll.pool.conn.execute_calls
        if "dead_letter_queue" in args[0]
    ]
    assert dlq_calls
    assert "sha256 mismatch" in dlq_calls[-1][4]
