from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.recovery.create_task49_activation_manifest import ActivationBuildError, VOLUME, build


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    evidence = root / ".omo/evidence/collector-production-recovery"
    evidence.mkdir(parents=True)
    (root / "docker/env").mkdir(parents=True)
    (root / "scripts/recovery").mkdir(parents=True)
    env = root / "docker/env/common.env"
    env.write_text("SECRET=unprinted\n")
    wrapper = root / "scripts/recovery/compose_branch.sh"
    wrapper.write_text("#!/bin/sh\n")
    (evidence / "task-44-artifact-manifest.json").write_text("{}\n")
    (evidence / "task-48-forward-authority.json").write_text(json.dumps({"selected_branch": "recovered", "recovered_volume": VOLUME}))
    monkeypatch.setattr("scripts.recovery.create_task49_activation_manifest.subprocess.run", lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": b"services: {}\n"})())
    return root, env, evidence


def test_activation_builder_binds_promoted_authority_render_and_env_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, env, evidence = _fixture(tmp_path, monkeypatch)
    rendered, output = evidence / "rendered.yml", evidence / "manifest.json"
    document = build(root, env_files=[env], rendered=rendered, output=output)
    assert document["selected_branch"] == "recovered"
    assert document["env_files"] == [{"path": "docker/env/common.env", "sha256": __import__("hashlib").sha256(env.read_bytes()).hexdigest()}]


def test_activation_builder_binds_revisioned_task44_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, env, evidence = _fixture(tmp_path, monkeypatch)
    task44 = evidence / "task-44-artifact-manifest-r46.json"
    task44.write_text("{}\n")
    document = build(
        root,
        env_files=[env],
        rendered=evidence / "rendered-r46.yml",
        output=evidence / "manifest-r46.json",
        task44_manifest=task44,
    )
    assert document["task_44_manifest_path"].endswith("task-44-artifact-manifest-r46.json")
    assert document["task_44_manifest_sha256"] == __import__("hashlib").sha256(task44.read_bytes()).hexdigest()


def test_activation_builder_rejects_non_task44_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, env, evidence = _fixture(tmp_path, monkeypatch)
    not_task44 = evidence / "other.json"
    not_task44.write_text("{}\n")
    with pytest.raises(ActivationBuildError, match="Task 44 artifact"):
        build(
            root,
            env_files=[env],
            rendered=evidence / "rendered-other.yml",
            output=evidence / "manifest-other.json",
            task44_manifest=not_task44,
        )


def test_activation_builder_rejects_missing_or_wrong_promotion_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, env, evidence = _fixture(tmp_path, monkeypatch)
    (evidence / "task-48-forward-authority.json").write_text("{}")
    with pytest.raises(ActivationBuildError, match="recovered authority"):
        build(root, env_files=[env], rendered=evidence / "rendered.yml", output=evidence / "manifest.json")
