#!/usr/bin/env python3
"""Record Task 50's required fail-closed stop for an unapproved replay scope."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.checkpoint_replay import ReconciliationManifestError, validate_reconciliation_manifest


class GateError(ValueError):
    pass


def record(manifest_path: Path, output: Path) -> dict:
    if output.exists():
        raise GateError("refusing to overwrite Task 50 preflight receipt")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError("Task 12 reconciliation manifest is unavailable") from exc
    try:
        validate_reconciliation_manifest(manifest)
    except ReconciliationManifestError as exc:
        document = {
            "schema_version": 1,
            "task": 50,
            "status": "blocked_before_consumer_start",
            "reason": str(exc),
            "reconciliation_manifest": str(manifest_path),
            "consumer_started": False,
            "replay_started": False,
            "broker_or_redis_mutated": False,
            "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    else:
        raise GateError("Task 12 manifest is activation-approved; fail-closed receipt must not mask it")
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = record(args.manifest, args.output)
    except GateError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({"status": document["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
