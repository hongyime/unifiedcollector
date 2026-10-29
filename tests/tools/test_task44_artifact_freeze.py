from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.freeze_task44_artifact_manifest import FreezeError, _output_path, _write_once, build_manifest, source_content_sha256


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else value.encode())


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Task 44 tests")
    _write(root / "src/app.py", "print('ok')\n")
    _write(root / ".env", "SECRET=ignored\n")
    _write(root / "docker/env/common.env", "COMMON=ignored\n")
    _write(root / ".omo/plans/collector-production-recovery.md", "plan\n")
    _write(root / "scripts/recovery/compose_branch.sh", "#!/bin/sh\n")
    for name in (
        "task-13-compose-baseline.yml", "task-13-compose-baseline.yml.sha256",
        "task-13-compose-recovered.yml", "task-13-compose-recovered.yml.sha256",
        "task-13-compose-rehearsal-baseline.yml", "task-13-compose-rehearsal-baseline.yml.sha256",
        "task-13-compose-rehearsal-recovered.yml", "task-13-compose-rehearsal-recovered.yml.sha256",
    ):
        _write(root / ".omo/evidence/collector-production-recovery" / name, name + "\n")
    _write(root / "src/db/migrations/001.sql", "select 1;\n")
    source = root / "src/source.py"
    generated = root / "build/bundle.js"
    _write(source, "source\n")
    _write(generated, "bundle\n")
    runtime = root / "runtime.json"
    runtime.write_text(json.dumps({
        "schema_version": 1, "task": 44,
        "artifacts": [{
            "name": "bridge", "source": {"path": "src/source.py", "sha256": _sha(source)},
            "generated": {"path": "build/bundle.js", "sha256": _sha(generated)},
            "image": {"reference": "example:latest", "id": "sha256:example"},
            "expected_in_container": {"source_sha256": _sha(source), "artifact_sha256": _sha(generated)},
        }],
    }))
    contract = root / "contract.json"
    contract.write_text(json.dumps({"schema_version": 1, "sources": [
        {"source": "website", "kind": "active", "durable": {"table": "pages", "key": "id"},
         "cohort": {"source": "website"}, "required_fields": ["id"], "media": "required",
         "telemetry": "expected", "paid_evidence": "forbidden"},
        {"source": "human", "kind": "human_blocked", "durable": {"table": "accounts", "key": "id"},
         "cohort": {"source": "human"}, "required_fields": ["id"], "media": "not_applicable",
         "telemetry": "not_expected", "paid_evidence": "forbidden"},
    ]}))
    ledger = root / "ledger.json"
    ledger.write_text(json.dumps({"schema_version": 1, "contract_sha256": _sha(contract), "entries": [
        {"source": "website", "status": "pending_live_verification"},
        {"source": "human", "status": "blocked"},
    ]}))
    review = root / "review.json"
    review.write_text(json.dumps({"launch": "review-launch", "session": "review-session", "round": "1"}))
    rendered = root / "rendered.json"
    _write(rendered, "{\"services\":{}}\n")
    evidence = root / ".omo/evidence/collector-production-recovery"
    recon = root / "recon-evidence"
    recon.mkdir()
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    return {"root": root, "runtime": runtime, "contract": contract, "ledger": ledger, "review": review,
            "rendered": rendered, "evidence": evidence, "recon": recon}


def _manifest(paths: dict[str, Path]) -> dict:
    return build_manifest(
        paths["root"], runtime_path=paths["runtime"], contract_path=paths["contract"], ledger_path=paths["ledger"],
        review_path=paths["review"], rendered_compose=paths["rendered"],
        env_paths=[paths["root"] / ".env", paths["root"] / "docker/env/common.env"],
        evidence_root=paths["evidence"], recon_evidence_root=paths["recon"], selected_namespace="production",
        selected_branch="recovered", selected_volume="unifiedcollector_pgdata_recovered", verify_images=False,
    )


def test_freeze_binds_required_immutable_inputs(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    manifest = _manifest(paths)
    assert manifest["task"] == 44
    assert manifest["selected"]["branch"] == "recovered"
    assert manifest["source_acceptance"]["activation_manifest"] == "task-49-activation-manifest.json"
    assert len(manifest["immutable"]["branch_overrides"]) == 8
    assert manifest["predeployment"]["images"] == [{"artifact": "bridge", "reference": "example:latest", "id": "sha256:example"}]


def test_freeze_binds_a_revisioned_activation_manifest(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    manifest = build_manifest(
        paths["root"], runtime_path=paths["runtime"], contract_path=paths["contract"], ledger_path=paths["ledger"],
        review_path=paths["review"], rendered_compose=paths["rendered"],
        env_paths=[paths["root"] / ".env", paths["root"] / "docker/env/common.env"],
        evidence_root=paths["evidence"], recon_evidence_root=paths["recon"], selected_namespace="production",
        selected_branch="recovered", selected_volume="unifiedcollector_pgdata_recovered", verify_images=False,
        activation_manifest="task-49-activation-manifest-r46.json",
    )
    assert manifest["source_acceptance"]["activation_manifest"] == "task-49-activation-manifest-r46.json"
    with pytest.raises(FreezeError, match="evidence-root filename"):
        build_manifest(
            paths["root"], runtime_path=paths["runtime"], contract_path=paths["contract"], ledger_path=paths["ledger"],
            review_path=paths["review"], rendered_compose=paths["rendered"],
            env_paths=[paths["root"] / ".env"], evidence_root=paths["evidence"], recon_evidence_root=paths["recon"],
            selected_namespace="production", selected_branch="recovered", selected_volume="unifiedcollector_pgdata_recovered",
            verify_images=False, activation_manifest="../task-49.json",
        )


def test_source_content_excludes_activation_env_but_binds_dirty_tracked_bytes(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    before = source_content_sha256(paths["root"])
    _write(paths["root"] / ".env", "SECRET=changed\n")
    assert source_content_sha256(paths["root"]) == before
    _write(paths["root"] / "src/app.py", "print('changed')\n")
    assert source_content_sha256(paths["root"]) != before
    after_tracked_change = source_content_sha256(paths["root"])
    _write(paths["root"] / "src/untracked.py", "print('also bound')\n")
    assert source_content_sha256(paths["root"]) != after_tracked_change


def test_freeze_rejects_bad_predeployment_ledger(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    ledger = json.loads(paths["ledger"].read_text())
    ledger["entries"][1]["status"] = "pending_live_verification"
    paths["ledger"].write_text(json.dumps(ledger))
    with pytest.raises(FreezeError, match="human-blocked"):
        _manifest(paths)


def test_manifest_write_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    _write_once(path, {"task": 44})
    with pytest.raises(FreezeError, match="refusing to overwrite"):
        _write_once(path, {"task": 44})


def test_output_must_be_task44_evidence(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    allowed = paths["evidence"] / "task-44-artifact-manifest.json"
    assert _output_path(paths["root"], allowed) == allowed.resolve()
    with pytest.raises(FreezeError, match="evidence root"):
        _output_path(paths["root"], tmp_path / "manifest.json")
