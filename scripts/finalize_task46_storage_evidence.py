#!/usr/bin/env python3
"""Publish honest Task 46 evidence only after the serial destination readback.

The historical source roots were owner-deleted after their documented checksum
sync.  This tool intentionally does not pretend a new source comparison took
place.  It binds the completed destination manifest/readback to the exact
owner-authorized retention exceptions and records that source re-enumeration is
unavailable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Task46EvidenceError(ValueError):
    """Task 46 evidence is incomplete, malformed, or not bound to readback."""


_READBACK = re.compile(r"^PASS: entries=(?P<entries>[0-9]+) manifest_sha256=(?P<digest>[0-9a-f]{64})$", re.MULTILINE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Task46EvidenceError(f"cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Task46EvidenceError(f"{path} must be a JSON object")
    return payload


def _manifest_entries(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            entries = sum(1 for _ in handle)
    except OSError as exc:
        raise Task46EvidenceError(f"cannot read destination manifest: {exc}") from exc
    if not entries:
        raise Task46EvidenceError("destination manifest is empty")
    return entries


def _readback(path: Path, manifest_entries: int, manifest_digest: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Task46EvidenceError(f"cannot read serial readback receipt: {exc}") from exc
    match = _READBACK.search(text)
    if not match:
        raise Task46EvidenceError("serial readback receipt does not report PASS with manifest identity")
    if int(match["entries"]) != manifest_entries or match["digest"] != manifest_digest:
        raise Task46EvidenceError("serial readback receipt does not match the destination manifest")
    return {"path": str(path), "sha256": _sha256(path), "entries": manifest_entries, "manifest_sha256": manifest_digest}


def _exceptions(task11: Path, task12: Path) -> list[dict[str, Any]]:
    proposal = _load_json(task11)
    decision = task12.read_text(encoding="utf-8")
    historical = proposal.get("media_reconciliation", {}).get("sample", {}).get("historical_root", {})
    if not isinstance(historical, dict) or historical.get("owner_authorized_deletion_after_checksum_sync") is not True:
        raise Task46EvidenceError("Task 11 does not record the owner-authorized historical-source deletion")
    if historical.get("post_deletion_comparison_possible") is not False:
        raise Task46EvidenceError("Task 11 does not explicitly prohibit a post-deletion source comparison")
    if "owner-authorized for discard" not in decision or "owner-deleted" not in decision:
        raise Task46EvidenceError("Task 12 decision does not bind both owner-authorized source retention exceptions")
    return [
        {
            "source_root": "/media/seagate-expansion",
            "status": "owner_authorized_discarded_and_reformatted",
            "post_deletion_comparison_possible": False,
            "authority_evidence": str(task12),
        },
        {
            "source_root": historical.get("root"),
            "status": "owner_authorized_deleted_after_checksum_sync",
            "post_deletion_comparison_possible": False,
            "authority_evidence": str(task11),
        },
    ]


def build(manifest: Path, readback: Path, task11: Path, task12: Path) -> dict[str, dict[str, Any]]:
    manifest_entries = _manifest_entries(manifest)
    manifest_digest = _sha256(manifest)
    readback_record = _readback(readback, manifest_entries, manifest_digest)
    exceptions = _exceptions(task11, task12)
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    destination = {
        "schema_version": 1,
        "task": 46,
        "generated_at_utc": generated_at,
        "roots": ["/media/sg-ultra/unifiedanalyzer", "/media/sg-ultra/unifiedcollector"],
        "manifest": {"path": str(manifest), "sha256": manifest_digest, "entries": manifest_entries},
        "serial_readback": readback_record,
    }
    source = {
        "schema_version": 1,
        "task": 46,
        "generated_at_utc": generated_at,
        "comparison_status": "post_deletion_source_enumeration_unavailable",
        "preservation_exceptions": exceptions,
        "evidence_inputs": {
            "task_11_sha256": _sha256(task11),
            "task_12_sha256": _sha256(task12),
        },
    }
    report = {
        "schema_version": 1,
        "task": 46,
        "status": "destination_manifest_and_serial_readback_passed_with_named_source_retention_exceptions",
        "generated_at_utc": generated_at,
        "destination_manifest_sha256": manifest_digest,
        "destination_entries": manifest_entries,
        "serial_readback_sha256": readback_record["sha256"],
        "unexplained_destination_integrity_differences": 0,
        "source_comparison": source["comparison_status"],
        "preservation_exceptions": exceptions,
        "next_gate": "Task 47 may use only this verified destination evidence; Task 11 exhaustive DB/media reconciliation remains independent and required before deletion or promotion.",
    }
    return {"source": source, "destination": destination, "report": report}


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--readback", type=Path, required=True)
    parser.add_argument("--task11", type=Path, required=True)
    parser.add_argument("--task12", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        documents = build(args.manifest, args.readback, args.task11, args.task12)
    except Task46EvidenceError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    _write_atomic(output / "task-46-source-manifest.json", documents["source"])
    _write_atomic(output / "task-46-destination-manifest.json", documents["destination"])
    _write_atomic(output / "task-46-collector-production-recovery.json", documents["report"])
    print(f"PASS: task46 evidence={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
