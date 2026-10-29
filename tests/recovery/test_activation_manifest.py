from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.recovery.verify_activation_manifest import ActivationManifestError, verify


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[dict, Path, Path, Path]:
    env = tmp_path / "docker" / "env" / "common.env"
    wrapper = tmp_path / "scripts" / "recovery" / "compose_branch.sh"
    task44 = tmp_path / "task-44.json"
    rendered = tmp_path / "compose.json"
    for path, value in ((env, b"SAFE=1\n"), (wrapper, b"#!/bin/sh\n"), (task44, b"{}\n"), (rendered, b"{\"services\":{}}\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    manifest = {
        "schema_version": 1,
        "task": 49,
        "selected_branch": "recovered",
        "expected_pgdata": "unifiedcollector_pgdata_recovered",
        "task_44_manifest_path": "task-44.json",
        "task_44_manifest_sha256": _sha(task44),
        "wrapper_path": "scripts/recovery/compose_branch.sh",
        "wrapper_sha256": _sha(wrapper),
        "effective_compose_sha256": _sha(rendered),
        "env_files": [{"path": "docker/env/common.env", "sha256": _sha(env)}],
    }
    return manifest, task44, rendered, env


def test_activation_manifest_binds_frozen_branch_env_wrapper_and_compose(tmp_path: Path) -> None:
    manifest, task44, rendered, _ = _fixture(tmp_path)
    assert verify(manifest, tmp_path, task44, rendered)["selected_branch"] == "recovered"


@pytest.mark.parametrize("mutate", ["env", "compose", "task44"])
def test_activation_manifest_rejects_changed_frozen_inputs(tmp_path: Path, mutate: str) -> None:
    manifest, task44, rendered, env = _fixture(tmp_path)
    {"env": env, "compose": rendered, "task44": task44}[mutate].write_bytes(b"changed\n")
    with pytest.raises(ActivationManifestError):
        verify(manifest, tmp_path, task44, rendered)


def test_activation_manifest_rejects_wrong_branch_volume_or_path_escape(tmp_path: Path) -> None:
    manifest, task44, rendered, _ = _fixture(tmp_path)
    manifest["selected_branch"] = "baseline"
    with pytest.raises(ActivationManifestError, match="Branch B"):
        verify(manifest, tmp_path, task44, rendered)
    manifest, task44, rendered, _ = _fixture(tmp_path)
    manifest["env_files"][0]["path"] = "../outside.env"
    with pytest.raises(ActivationManifestError, match="escapes"):
        verify(manifest, tmp_path, task44, rendered)


def test_activation_manifest_rejects_a_different_task44_path(tmp_path: Path) -> None:
    manifest, task44, rendered, _ = _fixture(tmp_path)
    manifest["task_44_manifest_path"] = "different.json"
    with pytest.raises(ActivationManifestError, match="manifest path"):
        verify(manifest, tmp_path, task44, rendered)
