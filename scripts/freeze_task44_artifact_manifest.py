#!/usr/bin/env python3
"""Freeze Task 44's immutable release artifact record.

The command deliberately accepts already-produced evidence rather than trying
to infer release state.  In particular it refuses to overwrite a prior freeze,
requires a real review receipt, verifies the mutable source ledger only at the
predeployment stage, and delegates image/artifact identity validation to the
Task 44 runtime-manifest verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

from scripts.verify_runtime_artifact_manifest import RuntimeManifestError, verify as verify_runtime
from scripts.verify_source_acceptance import AcceptanceError, verify as verify_sources


class FreezeError(ValueError):
    """The supplied release inputs cannot be frozen safely."""


_BRANCH_FILES = (
    ".omo/evidence/collector-production-recovery/task-13-compose-baseline.yml",
    ".omo/evidence/collector-production-recovery/task-13-compose-baseline.yml.sha256",
    ".omo/evidence/collector-production-recovery/task-13-compose-recovered.yml",
    ".omo/evidence/collector-production-recovery/task-13-compose-recovered.yml.sha256",
    ".omo/evidence/collector-production-recovery/task-13-compose-rehearsal-baseline.yml",
    ".omo/evidence/collector-production-recovery/task-13-compose-rehearsal-baseline.yml.sha256",
    ".omo/evidence/collector-production-recovery/task-13-compose-rehearsal-recovered.yml",
    ".omo/evidence/collector-production-recovery/task-13-compose-rehearsal-recovered.yml.sha256",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise FreezeError(f"{label} must be a JSON object")
    return value


def _relative(root: Path, path: Path, label: str) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise FreezeError(f"{label} must be inside the repository root") from exc


def _required_digest(root: Path, relative: str, label: str) -> dict[str, str]:
    path = root / relative
    if not path.is_file():
        raise FreezeError(f"missing required {label}: {relative}")
    return {"path": relative, "sha256": _sha256(path)}


def _review_receipt(path: Path) -> dict[str, Any]:
    value = _load_json(path, "review receipt")
    for key in ("launch", "session", "round"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise FreezeError(f"review receipt requires nonempty {key}")
    return value


def _git_files(root: Path) -> list[str]:
    commands = (
        ["git", "-C", str(root), "ls-files", "-z"],
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
    )
    names: set[str] = set()
    for command in commands:
        completed = subprocess.run(command, check=False, capture_output=True)
        if completed.returncode:
            raise FreezeError("cannot enumerate tracked and dirty repository files")
        names.update(item.decode("utf-8", "surrogateescape") for item in completed.stdout.split(b"\0") if item)
    return sorted(name for name in names if name != ".env" and not name.startswith("docker/env/"))


def source_content_sha256(root: Path) -> str:
    """Hash current tracked source/config bytes with unambiguous path framing."""
    digest = hashlib.sha256()
    for relative in _git_files(root):
        path = root / relative
        if not path.is_file():
            raise FreezeError(f"tracked file is unavailable: {relative}")
        encoded = relative.encode("utf-8", "surrogateescape")
        data = path.read_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise FreezeError("cannot determine source HEAD")
    value = completed.stdout.strip()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise FreezeError("source HEAD is not a commit SHA")
    return value


def _migrations(root: Path) -> list[dict[str, str]]:
    directory = root / "src/db/migrations"
    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise FreezeError("no migration SQL files found")
    return [{"path": _relative(root, path, "migration"), "sha256": _sha256(path)} for path in paths]


def _mode(root: Path, path: Path, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise FreezeError(f"{label} must be an existing directory")
    return {"path": _relative(root, resolved, label) if resolved.is_relative_to(root.resolve()) else str(resolved),
            "mode": f"{os.stat(resolved).st_mode & 0o777:03o}"}


def _env_hashes(root: Path, paths: Iterable[Path]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[Path] = set()
    for raw in paths:
        path = raw.resolve()
        if path in seen:
            raise FreezeError("env-file paths must be unique")
        seen.add(path)
        if not path.is_file():
            raise FreezeError(f"env file is missing: {raw}")
        result.append({"path": _relative(root, path, "env file"), "sha256": _sha256(path)})
    if not result:
        raise FreezeError("at least one effective env file is required")
    return result


def _runtime_images(runtime: dict[str, Any]) -> list[dict[str, str]]:
    artifacts = runtime.get("artifacts")
    if not isinstance(artifacts, list):
        raise FreezeError("runtime manifest has no artifacts list")
    result: list[dict[str, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("image"), dict):
            raise FreezeError("runtime manifest artifact is malformed")
        image = artifact["image"]
        name, reference, image_id = artifact.get("name"), image.get("reference"), image.get("id")
        if not all(isinstance(value, str) and value for value in (name, reference, image_id)):
            raise FreezeError("runtime manifest image identity is malformed")
        result.append({"artifact": name, "reference": reference, "id": image_id})
    return sorted(result, key=lambda row: row["artifact"])


def build_manifest(
    root: Path,
    *,
    runtime_path: Path,
    contract_path: Path,
    ledger_path: Path,
    review_path: Path,
    rendered_compose: Path,
    env_paths: Iterable[Path],
    evidence_root: Path,
    recon_evidence_root: Path,
    selected_namespace: str,
    selected_branch: str,
    selected_volume: str,
    verify_images: bool,
    activation_manifest: str = "task-49-activation-manifest.json",
) -> dict[str, Any]:
    if selected_namespace != "production" or selected_branch not in {"baseline", "recovered"} or not selected_volume:
        raise FreezeError("selected namespace/branch/volume is invalid")
    if not activation_manifest or Path(activation_manifest).name != activation_manifest:
        raise FreezeError("activation manifest must be an evidence-root filename")
    root = root.resolve()
    runtime_path = runtime_path.resolve()
    contract_path = contract_path.resolve()
    ledger_path = ledger_path.resolve()
    rendered_compose = rendered_compose.resolve()
    for path, label in ((runtime_path, "runtime manifest"), (contract_path, "source contract"),
                        (ledger_path, "source ledger"), (rendered_compose, "rendered compose")):
        if not path.is_file():
            raise FreezeError(f"missing {label}")

    try:
        verify_runtime(_load_json(runtime_path, "runtime manifest"), root, verify_images=verify_images)
        acceptance = verify_sources(contract_path, ledger_path, stage="predeployment", selected=set(), since=None)
    except (RuntimeManifestError, AcceptanceError) as exc:
        raise FreezeError(str(exc)) from exc
    runtime = _load_json(runtime_path, "runtime manifest")

    branch_files = [_required_digest(root, name, "branch override") for name in _BRANCH_FILES]
    immutable = {
        "plan": _required_digest(root, ".omo/plans/collector-production-recovery.md", "plan"),
        "compose_wrapper": _required_digest(root, "scripts/recovery/compose_branch.sh", "Compose wrapper"),
        "branch_overrides": branch_files,
        "runtime_manifest": {"path": _relative(root, runtime_path, "runtime manifest"), "sha256": _sha256(runtime_path)},
        "source_contract": {"path": _relative(root, contract_path, "source contract"), "sha256": _sha256(contract_path)},
    }
    return {
        "schema_version": 1,
        "task": 44,
        "review": _review_receipt(review_path),
        "selected": {"namespace": selected_namespace, "branch": selected_branch, "volume": selected_volume},
        "immutable": immutable,
        "source_acceptance": {
            "contract_sha256": acceptance["contract_sha256"],
            "ledger_schema_version": 1,
            "activation_manifest": activation_manifest,
        },
        "source": {"head": _head(root), "content_sha256": source_content_sha256(root)},
        "predeployment": {
            "rendered_compose": {"path": _relative(root, rendered_compose, "rendered compose"), "sha256": _sha256(rendered_compose)},
            "env_files": _env_hashes(root, env_paths),
            "images": _runtime_images(runtime),
            "migrations": _migrations(root),
            "evidence_roots": [_mode(root, evidence_root, "evidence root"), _mode(root, recon_evidence_root, "recon evidence root")],
        },
    }


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FreezeError(f"refusing to overwrite immutable manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _output_path(root: Path, raw: Path) -> Path:
    path = raw.resolve()
    evidence = (root / ".omo/evidence/collector-production-recovery").resolve()
    try:
        path.relative_to(evidence)
    except ValueError as exc:
        raise FreezeError("Task 44 artifact manifest must be written under its evidence root") from exc
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--source-contract", type=Path, required=True)
    parser.add_argument("--source-ledger", type=Path, required=True)
    parser.add_argument("--review-receipt", type=Path, required=True)
    parser.add_argument("--rendered-compose", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--recon-evidence-root", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--activation-manifest", default="task-49-activation-manifest.json")
    args = parser.parse_args()
    try:
        manifest = build_manifest(
            args.root,
            runtime_path=args.runtime_manifest,
            contract_path=args.source_contract,
            ledger_path=args.source_ledger,
            review_path=args.review_receipt,
            rendered_compose=args.rendered_compose,
            env_paths=args.env_file,
            evidence_root=args.evidence_root,
            recon_evidence_root=args.recon_evidence_root,
            selected_namespace=args.namespace,
            selected_branch=args.branch,
            selected_volume=args.volume,
            verify_images=True,
            activation_manifest=args.activation_manifest,
        )
        _write_once(_output_path(args.root.resolve(), args.output), manifest)
    except FreezeError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({"task": 44, "manifest": str(args.output), "sha256": _sha256(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
