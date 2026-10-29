from __future__ import annotations

import hashlib
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import scripts.verify_runtime_artifact_manifest as runtime_manifest
from scripts.verify_runtime_artifact_manifest import RuntimeManifestError, verify


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(source: Path, generated: Path) -> dict:
    source_digest = _digest(source)
    generated_digest = _digest(generated)
    return {
        "schema_version": 1,
        "task": 44,
        "artifacts": [{
            "name": "whatsapp-bridge",
            "source": {"path": str(source.name), "sha256": source_digest},
            "generated": {"path": str(generated.name), "sha256": generated_digest},
            "image": {"reference": "unifiedcollector-wa-bridge:release", "id": "sha256:fixture"},
            "expected_in_container": {
                "source_sha256": source_digest,
                "artifact_sha256": generated_digest,
            },
        }],
    }


def test_runtime_manifest_binds_source_generated_bundle_and_container_hashes(tmp_path: Path) -> None:
    source = tmp_path / "index.ts"
    generated = tmp_path / "bundle.js"
    source.write_bytes(b"source-v1")
    generated.write_bytes(b"generated-v1")
    assert verify(_manifest(source, generated), tmp_path) == {
        "schema_version": 1,
        "task": 44,
        "artifacts": ["whatsapp-bridge"],
    }


def test_runtime_manifest_rejects_a_stale_copied_generated_bundle(tmp_path: Path) -> None:
    source = tmp_path / "index.ts"
    generated = tmp_path / "bundle.js"
    source.write_bytes(b"source-v1")
    generated.write_bytes(b"generated-v1")
    manifest = _manifest(source, generated)
    generated.write_bytes(b"stale-copy")
    with pytest.raises(RuntimeManifestError, match="generated artifact digest is stale"):
        verify(manifest, tmp_path)


def test_runtime_manifest_rejects_an_unbound_in_container_digest(tmp_path: Path) -> None:
    source = tmp_path / "index.ts"
    generated = tmp_path / "bundle.js"
    source.write_bytes(b"source-v1")
    generated.write_bytes(b"generated-v1")
    manifest = _manifest(source, generated)
    manifest["artifacts"][0]["expected_in_container"]["artifact_sha256"] = "0" * 64
    with pytest.raises(RuntimeManifestError, match="do not bind"):
        verify(manifest, tmp_path)


def test_runtime_manifest_rejects_paths_outside_the_repository_root(tmp_path: Path) -> None:
    source = tmp_path / "index.ts"
    generated = tmp_path / "bundle.js"
    source.write_bytes(b"source-v1")
    generated.write_bytes(b"generated-v1")
    manifest = _manifest(source, generated)
    manifest["artifacts"][0]["generated"]["path"] = "../outside.js"
    with pytest.raises(RuntimeManifestError, match="escapes verification root"):
        verify(manifest, tmp_path)


def test_runtime_manifest_rejects_a_changed_image_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "index.ts"
    generated = tmp_path / "bundle.js"
    source.write_bytes(b"source-v1")
    generated.write_bytes(b"generated-v1")
    manifest = _manifest(source, generated)
    monkeypatch.setattr(
        runtime_manifest.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "sha256:other-image\n", ""),
    )
    with pytest.raises(RuntimeManifestError, match="image ID does not match"):
        verify(manifest, tmp_path, verify_images=True)
