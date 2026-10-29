from __future__ import annotations

import hashlib

import pytest

from scripts.create_task44_runtime_manifest import ARTIFACTS, Artifact, ManifestError, build


def test_runtime_manifest_binds_local_hashes_and_immutable_image_id(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "src/source.txt").write_text("source\n")
    (tmp_path / "build/bundle.txt").write_text("bundle\n")
    output = tmp_path / "manifest.json"
    document = build(tmp_path, output, artifacts=(Artifact("bridge", "src/source.txt", "build/bundle.txt", "bridge:latest"),), image_id=lambda _: "sha256:real")
    row = document["artifacts"][0]
    assert row["image"]["id"] == "sha256:real"
    assert row["source"]["sha256"] == hashlib.sha256(b"source\n").hexdigest()
    assert row["expected_in_container"]["artifact_sha256"] == row["generated"]["sha256"]
    with pytest.raises(ManifestError, match="overwrite"):
        build(tmp_path, output, artifacts=(), image_id=lambda _: "")


def test_runtime_manifest_refuses_missing_generated_artifact(tmp_path) -> None:
    (tmp_path / "source.txt").write_text("source\n")
    with pytest.raises(ManifestError, match="missing"):
        build(tmp_path, tmp_path / "manifest.json", artifacts=(Artifact("bridge", "source.txt", "missing.txt", "bridge:latest"),), image_id=lambda _: "sha256:real")


def test_runtime_manifest_distinguishes_guard_and_worker_sources_sharing_an_image() -> None:
    artifacts = {artifact.name: artifact for artifact in ARTIFACTS}
    assert artifacts["recon-egress-guard"].image == artifacts["spiderfoot"].image
    assert artifacts["recon-egress-guard"].source != artifacts["spiderfoot"].source
