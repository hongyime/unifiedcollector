#!/usr/bin/env python3
"""Freeze Task 49's value-free activation inputs after Branch-B promotion."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".omo/evidence/collector-production-recovery"
VOLUME = "unifiedcollector_pgdata_recovered"
_TASK44_NAME = re.compile(r"task-44-artifact-manifest(?:-r[0-9]+)?\.json\Z")


class ActivationBuildError(ValueError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise ActivationBuildError("path escapes repository root") from exc


def build(root: Path, *, env_files: list[Path], rendered: Path, output: Path, task44_manifest: Path | None = None) -> dict:
    root, rendered, output = root.resolve(), rendered.resolve(), output.resolve()
    evidence = root / ".omo/evidence/collector-production-recovery"
    task44 = (task44_manifest or evidence / "task-44-artifact-manifest.json").resolve()
    task48 = root / ".omo/evidence/collector-production-recovery/task-48-forward-authority.json"
    wrapper = root / "scripts/recovery/compose_branch.sh"
    if output.exists() or rendered.exists():
        raise ActivationBuildError("refusing to overwrite activation evidence")
    try:
        promoted = json.loads(task48.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationBuildError("Task 48 promotion receipt is unavailable") from exc
    if promoted.get("selected_branch") != "recovered" or promoted.get("recovered_volume") != VOLUME:
        raise ActivationBuildError("Task 48 receipt does not select the recovered authority")
    if task44.parent != evidence.resolve() or not _TASK44_NAME.fullmatch(task44.name) or not task44.is_file() or not wrapper.is_file():
        raise ActivationBuildError("Task 44 artifact or branch wrapper is unavailable")
    rendered.parent.mkdir(parents=True, exist_ok=True)
    rendered_result = subprocess.run(
        [str(wrapper), "--namespace", "production", "--branch", "recovered", "--expect-volume", VOLUME, "--", "config", "--no-interpolate"],
        cwd=root, capture_output=True, check=False,
    )
    if rendered_result.returncode:
        raise ActivationBuildError("cannot render the recovered activation Compose configuration")
    rendered.write_bytes(rendered_result.stdout)
    records = []
    seen: set[str] = set()
    for raw in env_files:
        path = raw.resolve()
        relative = _relative(root, path)
        if relative in seen or not path.is_file():
            rendered.unlink(missing_ok=True)
            raise ActivationBuildError("activation env files must be unique existing repository files")
        seen.add(relative)
        records.append({"path": relative, "sha256": _sha(path)})
    if not records:
        rendered.unlink(missing_ok=True)
        raise ActivationBuildError("activation manifest requires effective env files")
    document = {
        "schema_version": 1,
        "task": 49,
        "selected_branch": "recovered",
        "expected_pgdata": VOLUME,
        "task_44_manifest_path": _relative(root, task44),
        "task_44_manifest_sha256": _sha(task44),
        "wrapper_path": _relative(root, wrapper),
        "wrapper_sha256": _sha(wrapper),
        "effective_compose_sha256": _sha(rendered),
        "env_files": sorted(records, key=lambda row: row["path"]),
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", required=True)
    parser.add_argument("--rendered-compose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task44-manifest", type=Path)
    args = parser.parse_args()
    try:
        document = build(
            args.root,
            env_files=args.env_file,
            rendered=args.rendered_compose,
            output=args.output,
            task44_manifest=args.task44_manifest,
        )
    except ActivationBuildError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({"task": 49, "env_file_count": len(document["env_files"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
