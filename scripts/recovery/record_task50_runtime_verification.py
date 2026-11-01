#!/usr/bin/env python3
"""Write a value-free Task 50 runtime identity receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class RuntimeVerificationError(ValueError):
    """A running service cannot be bound to the frozen release evidence."""


@dataclass(frozen=True)
class ServiceSpec:
    service: str
    container: str
    artifact: str | None = None
    source_path: str | None = None
    generated_path: str | None = None


SPECS = (
    ServiceSpec("postgres", "unifiedcollector_postgres"),
    ServiceSpec("rabbitmq", "unifiedcollector_rabbitmq"),
    ServiceSpec("redis", "unifiedcollector_redis"),
    ServiceSpec("dashboard", "unifiedcollector_dashboard", "dashboard", "/app/src/dashboard/frontend/src/main.tsx", "/app/src/dashboard/frontend/dist/index.html"),
    ServiceSpec("browser_ingest", "unifiedcollector_browser_ingest", "collector", "/app/src/main.py", "/app/config/task-37-admission-policy.json"),
    ServiceSpec("scheduler", "unifiedcollector_scheduler", "scheduler", "/app/src/main.py", "/app/config/task-37-admission-policy.json"),
    ServiceSpec("collector_paypal", "unifiedcollector_collector_paypal", "collector", "/app/src/main.py", "/app/config/task-37-admission-policy.json"),
    ServiceSpec("collector_whatsapp", "unifiedcollector_collector_whatsapp", "collector", "/app/src/main.py", "/app/config/task-37-admission-policy.json"),
    ServiceSpec("recon_egress_guard", "unifiedcollector_recon_egress_guard", "recon-egress-guard", "/app/src/core/recon_egress_guard.py", "/app/config/task-37-admission-policy.json"),
    ServiceSpec("collector_spiderfoot", "unifiedcollector_spiderfoot", "spiderfoot", "/app/src/recon_spiderfoot_service.py", "/app/config/task-37-admission-policy.json"),
    ServiceSpec("wa-bridge-1", "unifiedcollector_wa_bridge_1", "wa-bridge-1", "/app/src/index.ts", "/app/build/index.js"),
    ServiceSpec("wa-bridge-2", "unifiedcollector_wa_bridge_2", "wa-bridge-2", "/app/src/index.ts", "/app/build/index.js"),
)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeVerificationError(f"cannot load {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeVerificationError(f"{path.name} must be an object")
    return value


def _inspect(container: str) -> dict[str, Any]:
    result = subprocess.run(["docker", "inspect", container], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeVerificationError(f"cannot inspect {container}")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeVerificationError(f"invalid inspect result for {container}") from exc
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeVerificationError(f"unexpected inspect result for {container}")
    return rows[0]


def _container_sha(container: str, path: str) -> str:
    result = subprocess.run(["docker", "exec", container, "sha256sum", path], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeVerificationError(f"cannot hash {path} in {container}")
    digest = result.stdout.split(maxsplit=1)[0] if result.stdout else ""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RuntimeVerificationError(f"invalid digest for {path} in {container}")
    return digest


def _digest(value: Any) -> str:
    return _sha_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def collect(
    runtime: dict[str, Any],
    activation: dict[str, Any],
    *,
    baseline_runtime: dict[str, Any] | None = None,
    refreshed_services: frozenset[str] = frozenset(),
    inspect: Callable[[str], dict[str, Any]] = _inspect,
    container_sha: Callable[[str, str], str] = _container_sha,
    specs: tuple[ServiceSpec, ...] = SPECS,
) -> dict[str, Any]:
    if runtime.get("schema_version") != 1 or runtime.get("task") != 44:
        raise RuntimeVerificationError("unsupported Task 44 runtime manifest")
    if activation.get("schema_version") != 1 or activation.get("task") != 49:
        raise RuntimeVerificationError("unsupported Task 49 activation manifest")
    if baseline_runtime is not None and (baseline_runtime.get("schema_version") != 1 or baseline_runtime.get("task") != 44):
        raise RuntimeVerificationError("unsupported baseline Task 44 runtime manifest")
    artifacts = runtime.get("artifacts")
    baseline_artifacts = baseline_runtime.get("artifacts") if baseline_runtime else None
    if not isinstance(artifacts, list) or (baseline_runtime is not None and not isinstance(baseline_artifacts, list)):
        raise RuntimeVerificationError("Task 44 runtime manifest has no artifacts")
    expected = {row.get("name"): row for row in artifacts if isinstance(row, dict)}
    baseline_expected = {row.get("name"): row for row in (baseline_artifacts or []) if isinstance(row, dict)}
    services: list[dict[str, Any]] = []
    for spec in specs:
        row = inspect(spec.container)
        state, config = row.get("State"), row.get("Config")
        if not isinstance(state, dict) or state.get("Running") is not True or not isinstance(config, dict):
            raise RuntimeVerificationError(f"{spec.service} is not running")
        record: dict[str, Any] = {
            "service": spec.service,
            "container": spec.container,
            "image": row.get("Image"),
            "mounts": [
                {key: mount.get(key) for key in ("Destination", "Source", "RW", "Type")}
                for mount in row.get("Mounts", []) if isinstance(mount, dict)
            ],
            "env_sha256": _digest(sorted(config.get("Env") or [])),
            "config_sha256": _digest({key: config.get(key) for key in ("Cmd", "Entrypoint", "User", "WorkingDir")}),
        }
        if spec.artifact is not None:
            manifest = expected if spec.service in refreshed_services or baseline_runtime is None else baseline_expected
            artifact = manifest.get(spec.artifact)
            if not isinstance(artifact, dict):
                raise RuntimeVerificationError(f"missing {spec.artifact} artifact")
            image = artifact.get("image")
            expected_hashes = artifact.get("expected_in_container")
            if not isinstance(image, dict) or row.get("Image") != image.get("id"):
                raise RuntimeVerificationError(f"{spec.service} image does not match Task 44")
            if not isinstance(expected_hashes, dict) or not spec.source_path or not spec.generated_path:
                raise RuntimeVerificationError(f"{spec.service} artifact hash contract is incomplete")
            source_sha = container_sha(spec.container, spec.source_path)
            generated_sha = container_sha(spec.container, spec.generated_path)
            if source_sha != expected_hashes.get("source_sha256") or generated_sha != expected_hashes.get("artifact_sha256"):
                raise RuntimeVerificationError(f"{spec.service} in-container artifact hashes are stale")
            record["artifact"] = spec.artifact
            record["release"] = "refreshed" if baseline_runtime is None or spec.service in refreshed_services else "baseline"
            record["in_container"] = {"source_sha256": source_sha, "artifact_sha256": generated_sha}
        services.append(record)
    return {"schema_version": 1, "task": 50, "selected_branch": activation.get("selected_branch"), "services": services}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--activation-manifest", type=Path, required=True)
    parser.add_argument("--baseline-runtime-manifest", type=Path)
    parser.add_argument("--refreshed-service", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        print("BLOCKED: refusing to overwrite runtime verification receipt")
        return 2
    try:
        baseline_runtime = _load(args.baseline_runtime_manifest) if args.baseline_runtime_manifest else None
        refreshed_services = frozenset(args.refreshed_service)
        if baseline_runtime is None and refreshed_services:
            raise RuntimeVerificationError("refreshed services require a baseline runtime manifest")
        receipt = collect(
            _load(args.runtime_manifest), _load(args.activation_manifest),
            baseline_runtime=baseline_runtime, refreshed_services=refreshed_services,
        )
    except RuntimeVerificationError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    receipt["task_44_runtime_manifest_sha256"] = _sha_bytes(args.runtime_manifest.read_bytes())
    receipt["task_49_activation_manifest_sha256"] = _sha_bytes(args.activation_manifest.read_bytes())
    if args.baseline_runtime_manifest:
        receipt["baseline_task_44_runtime_manifest_sha256"] = _sha_bytes(args.baseline_runtime_manifest.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"task": 50, "services": [row["service"] for row in receipt["services"]]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
