import os
from pathlib import Path

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
