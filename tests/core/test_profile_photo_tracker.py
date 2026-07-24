import hashlib
import json

from src.core import profile_photo_tracker as tracker_mod
from src.core.profile_photo_tracker import ProfilePhotoTracker


def test_profile_photo_save_writes_vault_blob_and_artifact_sidecar(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(tracker_mod, "VAULT_ROOT", vault_root)
    tracker = ProfilePhotoTracker()
    data = b"\xff\xd8profile photo bytes"
    digest = hashlib.sha256(data).hexdigest()

    path = tracker._save(
        data,
        entity_id="123",
        source="github",
        save_dir=tmp_path / "legacy",
        url="https://avatars.githubusercontent.com/u/123?s=460",
    )

    assert path == vault_root / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert path.read_bytes() == data
    artifact_meta = tracker.last_artifact_metadata()
    assert artifact_meta["ok"] is True
    assert artifact_meta["partial"] is False
    assert artifact_meta["sha256"] == digest
    assert artifact_meta["blob_path"] == f"media/blobs/{digest[:2]}/{digest[2:4]}/{digest}.jpg"
    assert artifact_meta["sidecar_path"].startswith("sidecars/artifacts/github/")

    sidecar = next((vault_root / "sidecars" / "artifacts" / "github").rglob("*.json"))
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["artifact_kind"] == "media_blob"
    assert payload["metadata"]["content_type"] == "profile_photo"
    assert payload["metadata"]["content_id"] == "profile_123"
    assert payload["metadata"]["source_url"] == "https://avatars.githubusercontent.com/u/123?s=460"


def test_profile_photo_save_uses_png_extension(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(tracker_mod, "VAULT_ROOT", vault_root)
    tracker = ProfilePhotoTracker()
    data = b"\x89PNGprofile photo bytes"
    digest = hashlib.sha256(data).hexdigest()

    path = tracker._save(data, entity_id="123", source="instagram", save_dir=tmp_path / "legacy")

    assert path.name == f"{digest}.png"
    assert tracker.last_artifact_metadata()["blob_path"].endswith(f"{digest}.png")
