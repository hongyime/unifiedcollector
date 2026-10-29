#!/usr/bin/env python3
"""Fail-closed verifier for Task 49's value-free activation manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


class ActivationManifestError(ValueError):
    """The frozen activation input is no longer safe to use."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationManifestError(f"cannot load activation manifest: {exc}") from exc
    if not isinstance(document, dict):
        raise ActivationManifestError("activation manifest must be an object")
    return document


def _safe_file(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ActivationManifestError(f"{label} path is required")
    path = (root / value).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ActivationManifestError(f"{label} escapes repository root") from exc
    if not path.is_file():
        raise ActivationManifestError(f"{label} is missing")
    return path


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as exc:
        raise ActivationManifestError("Task 44 manifest escapes repository root") from exc


def verify(manifest: dict[str, Any], root: Path, task44: Path, rendered_compose: Path) -> dict[str, Any]:
    if manifest.get("schema_version") != 1 or manifest.get("task") != 49:
        raise ActivationManifestError("unsupported activation-manifest schema")
    if manifest.get("selected_branch") != "recovered" or manifest.get("expected_pgdata") != "unifiedcollector_pgdata_recovered":
        raise ActivationManifestError("activation manifest does not select Branch B's recovered volume")
    if manifest.get("task_44_manifest_path") != _relative(root, task44):
        raise ActivationManifestError("activation manifest is not bound to the selected Task 44 manifest path")
    if manifest.get("task_44_manifest_sha256") != _sha256(task44):
        raise ActivationManifestError("activation manifest is not bound to the current Task 44 manifest")
    wrapper = _safe_file(root, manifest.get("wrapper_path"), "wrapper")
    if manifest.get("wrapper_sha256") != _sha256(wrapper):
        raise ActivationManifestError("activation manifest wrapper digest is stale")
    if manifest.get("effective_compose_sha256") != _sha256(rendered_compose):
        raise ActivationManifestError("effective Compose render changed after activation freeze")
    env_files = manifest.get("env_files")
    if not isinstance(env_files, list) or not env_files:
        raise ActivationManifestError("activation manifest must contain sanitized env-file digests")
    paths: set[str] = set()
    for entry in env_files:
        if not isinstance(entry, dict):
            raise ActivationManifestError("env-file records must be objects")
        path_value = entry.get("path")
        if path_value in paths:
            raise ActivationManifestError("env-file paths must be unique")
        paths.add(path_value)
        env_file = _safe_file(root, path_value, "env file")
        if entry.get("sha256") != _sha256(env_file):
            raise ActivationManifestError(f"environment file changed after activation freeze: {path_value}")
    return {"schema_version": 1, "task": 49, "env_files": sorted(paths), "selected_branch": "recovered"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task44", type=Path, required=True)
    parser.add_argument("--rendered-compose", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(_load(args.manifest), args.root, args.task44, args.rendered_compose)
    except ActivationManifestError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
