from __future__ import annotations

import json

from src.core import dm_purge


def test_purge_direct_message_artifacts_requires_valid_dm_sidecar(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    sidecars = root / "sidecars"
    sidecars.mkdir(parents=True)
    raw = root / "raw" / "tiktok" / "dm.json"
    raw.parent.mkdir(parents=True)
    raw.write_text("dm", encoding="utf-8")
    ordinary = root / "raw" / "tiktok" / "post.json"
    ordinary.write_text("post", encoding="utf-8")
    (sidecars / "dm.json").write_text(json.dumps({
        "artifact_kind": "raw_payload",
        "metadata": {"endpoint": "dm_sample"},
        "file": {"absolute_path": str(raw)},
    }), encoding="utf-8")
    (sidecars / "ordinary.json").write_text(json.dumps({
        "artifact_kind": "raw_payload",
        "metadata": {"endpoint": "posts"},
        "file": {"absolute_path": str(ordinary)},
    }), encoding="utf-8")
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "tiktok_frame.bin").write_bytes(b"x")

    report = dm_purge.purge_direct_message_artifacts(root, samples)

    assert report == {"vault_artifacts": 1, "vault_sidecars": 1, "samples": 1, "failures": 0}
    assert not raw.exists()
    assert not (sidecars / "dm.json").exists()
    assert ordinary.exists()
    assert (sidecars / "ordinary.json").exists()
