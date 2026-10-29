#!/usr/bin/env python3
"""Verify the generated-artifact/image portion of Task 44 release evidence.

This deliberately verifies a supplied immutable runtime manifest rather than
constructing one.  The later Task 44 freeze step is responsible for building
images and recording their real IDs; this verifier makes both stale bind-mounted
bundles and changed images fail closed before a release can use that evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


class RuntimeManifestError(ValueError):
    """The runtime-artifact record cannot safely be used for release."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeManifestError(f"cannot load runtime manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeManifestError("runtime manifest must be a JSON object")
    return value


def _safe_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeManifestError(f"{label} path is required")
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeManifestError(f"{label} escapes verification root") from exc
    if not candidate.is_file():
        raise RuntimeManifestError(f"{label} file is missing: {value}")
    return candidate


def _digest_field(document: dict[str, Any], label: str) -> str:
    digest = document.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RuntimeManifestError(f"{label} sha256 must be a lowercase SHA-256 digest")
    return digest


def _image_id(reference: str) -> str:
    completed = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeManifestError(f"cannot inspect image {reference}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def verify(document: dict[str, Any], root: Path, *, verify_images: bool = False) -> dict[str, Any]:
    if document.get("schema_version") != 1 or document.get("task") != 44:
        raise RuntimeManifestError("unsupported runtime-artifact manifest schema")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeManifestError("runtime manifest must contain at least one artifact")

    names: set[str] = set()
    verified: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RuntimeManifestError("artifact records must be objects")
        name = artifact.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise RuntimeManifestError("artifact names must be unique nonempty strings")
        names.add(name)

        source = artifact.get("source")
        generated = artifact.get("generated")
        image = artifact.get("image")
        expected = artifact.get("expected_in_container")
        if not all(isinstance(value, dict) for value in (source, generated, image, expected)):
            raise RuntimeManifestError(f"{name}: source/generated/image/container records are required")
        source_path = _safe_path(root, source.get("path"), f"{name} source")
        generated_path = _safe_path(root, generated.get("path"), f"{name} generated artifact")
        source_digest = _digest_field(source, f"{name} source")
        generated_digest = _digest_field(generated, f"{name} generated artifact")
        if _sha256(source_path) != source_digest:
            raise RuntimeManifestError(f"{name}: source digest is stale")
        if _sha256(generated_path) != generated_digest:
            raise RuntimeManifestError(f"{name}: generated artifact digest is stale")

        reference = image.get("reference")
        image_id = image.get("id")
        if not isinstance(reference, str) or not reference or not isinstance(image_id, str) or not image_id:
            raise RuntimeManifestError(f"{name}: image reference and immutable ID are required")
        if expected.get("source_sha256") != source_digest or expected.get("artifact_sha256") != generated_digest:
            raise RuntimeManifestError(f"{name}: expected in-container hashes do not bind this artifact")
        if verify_images and _image_id(reference) != image_id:
            raise RuntimeManifestError(f"{name}: image ID does not match the manifest")
        verified.append(name)
    return {"schema_version": 1, "task": 44, "artifacts": sorted(verified)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path, help="repository root used for artifact paths")
    parser.add_argument("--verify-images", action="store_true", help="also compare each declared Docker image ID")
    args = parser.parse_args()
    try:
        report = verify(_load(args.manifest), args.root, verify_images=args.verify_images)
    except RuntimeManifestError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
