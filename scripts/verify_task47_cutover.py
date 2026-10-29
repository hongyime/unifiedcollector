#!/usr/bin/env python3
"""Record the selected Branch-B mount/bind state without starting services."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = Path("/media/sg-ultra/unifiedcollector")
EXPECTED_UUID = "2C30-310F"
EVIDENCE = ROOT / ".omo/evidence/collector-production-recovery/task-47-collector-production-recovery.json"


def _run(*command: str) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"command failed ({' '.join(command[:3])}): {completed.stderr.strip()}")
    return completed.stdout


def _render_recovered_compose() -> dict[str, Any]:
    output = _run(
        str(ROOT / "scripts/recovery/compose_branch.sh"),
        "--namespace", "production",
        "--branch", "recovered",
        "--expect-volume", "unifiedcollector_pgdata_recovered",
        "--",
        "config",
        "--format",
        "json",
    )
    return json.loads(output)


def verify() -> dict[str, Any]:
    # The default JSON column set omits UUID.  Request it explicitly so the
    # cutover guard proves the selected sg-ultra device rather than treating a
    # missing JSON field as an identity mismatch.
    mount = _run("findmnt", "-J", "-o", "SOURCE,FSTYPE,UUID,TARGET", "-T", str(VAULT_ROOT))
    filesystem = json.loads(mount)["filesystems"][0]
    if filesystem.get("uuid") != EXPECTED_UUID or filesystem.get("fstype") != "exfat":
        raise RuntimeError("sg-ultra mount UUID/filesystem does not match the selected destination")

    compose = _render_recovered_compose()
    if compose.get("volumes", {}).get("pgdata", {}).get("name") != "unifiedcollector_pgdata_recovered":
        raise RuntimeError("rendered recovered branch does not select recovered pgdata")
    bad_binds: list[dict[str, str]] = []
    for service_name, service in compose.get("services", {}).items():
        for volume in service.get("volumes", []):
            if not isinstance(volume, dict):
                continue
            target = volume.get("target")
            source = volume.get("source")
            if target == "/media" and source != str(VAULT_ROOT / "media"):
                bad_binds.append({"service": service_name, "target": target, "source": str(source)})
            if target == "/vault" and source != str(VAULT_ROOT):
                bad_binds.append({"service": service_name, "target": target, "source": str(source)})
    if bad_binds:
        raise RuntimeError(f"rendered vault binds do not select sg-ultra: {bad_binds}")

    original = subprocess.run(
        ["docker", "inspect", "unifiedcollector_postgres", "--format", "{{.State.Running}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if original.returncode == 0 and original.stdout.strip() == "true":
        raise RuntimeError("production PostgreSQL is running during Task 47")
    running = _run("docker", "ps", "--format", "{{.Names}}").splitlines()
    forbidden = [name for name in running if name.startswith("unifiedcollector_")]
    if forbidden:
        raise RuntimeError(f"collector-stack containers are running during Task 47: {forbidden}")

    volume = subprocess.run(
        ["docker", "volume", "inspect", "unifiedcollector_pgdata_recovered"],
        text=True,
        capture_output=True,
        check=False,
    )
    volume_state: dict[str, Any] = {"present": volume.returncode == 0}
    if volume.returncode == 0:
        volume_state["inspect"] = json.loads(volume.stdout)[0]
    return {
        "schema_version": 1,
        "task": 47,
        "recorded_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "selected_branch": "recovered",
        "expected_pgdata": "unifiedcollector_pgdata_recovered",
        "mount": {
            "source": filesystem.get("source"),
            "fstype": filesystem.get("fstype"),
            "uuid": filesystem.get("uuid"),
            "target": filesystem.get("target"),
        },
        "rendered_bind_root": str(VAULT_ROOT),
        "recovered_volume": volume_state,
        "running_collector_stack_containers": forbidden,
        "production_postgres_running": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    document = verify()
    output = args.output.resolve()
    if output.parent != EVIDENCE.parent.resolve():
        raise SystemExit("output must be written directly under the private recovery evidence root")
    if os.stat(output.parent).st_mode & 0o077:
        raise SystemExit("recovery evidence root must remain owner-only")
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"PASS: task47 evidence={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
