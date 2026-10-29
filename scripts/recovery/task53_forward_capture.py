#!/usr/bin/env python3
"""Fail-closed Task 53 forward-capture preflight.

This intentionally performs no dump, stop, or restore.  It proves that a
private capture root can hold the current recovered PGDATA plus a fixed safety
reserve before a separate, controlled capture window is opened.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".omo/evidence/collector-production-recovery"
RECOVERED_VOLUME = "unifiedcollector_pgdata_recovered"
RESERVE_BYTES = 20 * 1024**3
REQUIRED_RECEIPTS = (
    "task-14-collector-production-recovery.json",
    "task-46-destination-manifest.json",
    "task-46-destination-readback.txt",
    "task-47-collector-production-recovery.json",
    "task-48-forward-authority.json",
    "task-49-activation-manifest.json",
    "task-50-runtime-verification.json",
    "task-52-collector-production-recovery.md",
    "task-55-domain-remediation.json",
)


class CapturePreflightError(RuntimeError):
    """A forward capture must not begin."""


def _run(*args: str) -> str:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode:
        raise CapturePreflightError(f"command failed: {' '.join(args[:3])}")
    return result.stdout.strip()


def _tree_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink())


def preflight(
    *,
    evidence: Path = EVIDENCE,
    volume: str = RECOVERED_VOLUME,
    reserve_bytes: int = RESERVE_BYTES,
    run: Callable[..., str] = _run,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> dict[str, int | str]:
    if reserve_bytes <= 0:
        raise CapturePreflightError("reserve must be positive")
    if not evidence.is_dir() or os.stat(evidence).st_mode & 0o077:
        raise CapturePreflightError("capture evidence root must be owner-only")
    missing = [name for name in REQUIRED_RECEIPTS if not (evidence / name).is_file()]
    if missing:
        raise CapturePreflightError("required receipts are missing: " + ",".join(missing))
    mountpoint = run("docker", "volume", "inspect", volume, "--format", "{{.Mountpoint}}")
    try:
        pgdata_bytes = int(run("sudo", "-n", "du", "-sb", "--", mountpoint).split()[0])
    except (IndexError, ValueError) as exc:
        raise CapturePreflightError("recovered volume size is unreadable") from exc
    available = disk_usage(evidence).free
    required = pgdata_bytes + reserve_bytes
    if available < required:
        raise CapturePreflightError(
            f"insufficient private headroom: available={available} required={required}"
        )
    return {
        "selected_volume": volume,
        "pgdata_bytes": pgdata_bytes,
        "reserve_bytes": reserve_bytes,
        "available_bytes": available,
        "minimum_before_capture_bytes": required,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Task 53 private forward-capture capacity")
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    parser.add_argument("--reserve-gib", type=int, default=20)
    args = parser.parse_args()
    try:
        report = preflight(evidence=args.evidence, reserve_bytes=args.reserve_gib * 1024**3)
    except CapturePreflightError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
