#!/usr/bin/env python3
"""Create Task 44 runtime evidence from local artifacts and immutable image IDs."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Artifact:
    name: str
    source: str
    generated: str
    image: str


ARTIFACTS = (
    Artifact("collector", "src/main.py", "config/task-37-admission-policy.json", "unifiedcollector-collector:latest"),
    Artifact("dashboard", "src/dashboard/frontend/src/main.tsx", "src/dashboard/frontend/dist/index.html", "unifiedcollector-dashboard"),
    Artifact("scheduler", "src/main.py", "config/task-37-admission-policy.json", "unifiedcollector-scheduler"),
    Artifact("onboard-bot", "src/main.py", "config/task-37-admission-policy.json", "unifiedcollector-onboard_bot"),
    Artifact("recon-egress-guard", "src/core/recon_egress_guard.py", "config/task-37-admission-policy.json", "unifiedcollector-spiderfoot:latest"),
    Artifact("spiderfoot", "src/recon_spiderfoot_service.py", "config/task-37-admission-policy.json", "unifiedcollector-spiderfoot:latest"),
    Artifact("wa-bridge-1", "src/bridges/whatsapp/src/index.ts", "src/bridges/whatsapp/build/index.js", "unifiedcollector-wa-bridge-1"),
    Artifact("wa-bridge-2", "src/bridges/whatsapp/src/index.ts", "src/bridges/whatsapp/build/index.js", "unifiedcollector-wa-bridge-2"),
)


class ManifestError(ValueError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _image_id(reference: str) -> str:
    completed = subprocess.run(["docker", "image", "inspect", reference, "--format", "{{.Id}}"], text=True, capture_output=True, check=False)
    if completed.returncode or not completed.stdout.strip():
        raise ManifestError(f"missing built image: {reference}")
    return completed.stdout.strip()


def build(root: Path, output: Path, *, artifacts: tuple[Artifact, ...] = ARTIFACTS, image_id: Callable[[str], str] = _image_id) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ManifestError("refusing to overwrite runtime artifact manifest")
    rows = []
    for artifact in artifacts:
        source, generated = root / artifact.source, root / artifact.generated
        if not source.is_file() or not generated.is_file():
            raise ManifestError(f"{artifact.name}: required source or generated artifact is missing")
        source_sha, generated_sha = _sha(source), _sha(generated)
        rows.append({
            "name": artifact.name,
            "source": {"path": artifact.source, "sha256": source_sha},
            "generated": {"path": artifact.generated, "sha256": generated_sha},
            "image": {"reference": artifact.image, "id": image_id(artifact.image)},
            "expected_in_container": {"source_sha256": source_sha, "artifact_sha256": generated_sha},
        })
    document = {"schema_version": 1, "task": 44, "artifacts": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = build(args.root, args.output)
    except ManifestError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({"artifacts": [item["name"] for item in document["artifacts"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
