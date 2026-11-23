import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-for-media-path-resolver")

from src.dashboard import api


def test_resolve_media_path_accepts_vault_media_blob(monkeypatch, tmp_path):
    drive_root = tmp_path / "drive"
    vault_root = tmp_path / "vault"
    blob = vault_root / "media" / "blobs" / "ab" / "cd" / "abcd.jpg"
    drive_root.mkdir()
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"image")

    monkeypatch.setattr("src.core.drive_check.DRIVE_PATH", str(drive_root))
    monkeypatch.setattr(api, "VAULT_ROOT", vault_root)

    assert api._resolve_media_path(str(blob)) == blob.resolve()


def test_resolve_media_path_rejects_paths_outside_allowed_roots(monkeypatch, tmp_path):
    drive_root = tmp_path / "drive"
    vault_root = tmp_path / "vault"
    outside = tmp_path / "outside" / "secret.jpg"
    drive_root.mkdir()
    (vault_root / "media").mkdir(parents=True)
    outside.parent.mkdir()
    outside.write_bytes(b"secret")

    monkeypatch.setattr("src.core.drive_check.DRIVE_PATH", str(drive_root))
    monkeypatch.setattr(api, "VAULT_ROOT", vault_root)

    with pytest.raises(HTTPException) as exc:
        api._resolve_media_path(str(outside))

    assert exc.value.status_code == 403


def test_resolve_media_path_rejects_missing_path():
    with pytest.raises(HTTPException) as exc:
        api._resolve_media_path("")

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_media_thumbnail_handles_rgba_image(monkeypatch, tmp_path):
    import uuid
    import io
    from PIL import Image
    from fastapi.responses import StreamingResponse
    from src.dashboard.api import media as media_module

    drive_root = tmp_path / "drive"
    vault_root = tmp_path / "vault"
    blob = vault_root / "media" / "blobs" / "test.png"
    drive_root.mkdir()
    blob.parent.mkdir(parents=True)

    # Save a transparent RGBA image
    im = Image.new("RGBA", (400, 400), (255, 0, 0, 128))
    im.save(blob, format="PNG")

    monkeypatch.setattr("src.core.drive_check.DRIVE_PATH", str(drive_root))
    monkeypatch.setattr(api, "VAULT_ROOT", vault_root)

    test_uuid = uuid.uuid4()

    class _FakeConn:
        async def fetchrow(self, query, *args):
            return {"file_path": str(blob), "content_type": "image"}

    class _FakePool:
        def acquire(self):
            return self
        async def __aenter__(self):
            return _FakeConn()
        async def __aexit__(self, *args):
            return False

    async def _fake_get_pool():
        return _FakePool()

    monkeypatch.setattr(media_module, "get_pool", _fake_get_pool)

    resp = await media_module.media_thumbnail(str(test_uuid))

    # Must be a StreamingResponse with JPEG thumbnail, NOT a FileResponse fallback
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "image/jpeg"

    # Verify the thumbnail body
    body = b"".join([chunk async for chunk in resp.body_iterator])
    thumb = Image.open(io.BytesIO(body))
    assert thumb.size == (300, 300)
    assert thumb.mode == "RGB"

