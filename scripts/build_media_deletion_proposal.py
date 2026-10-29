#!/usr/bin/env python3
"""Turn a read-only Task 11 plan into a hash-bound deletion proposal.

The proposal contains exact before-images for a later transaction.  This tool
never opens PostgreSQL or changes a vault path.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProposalError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_plan(path: Path, rows_csv: Path) -> tuple[dict[str, str], str]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalError(f"cannot read reconciliation plan: {exc}") from exc
    inputs = plan.get("inputs")
    if plan.get("mode") != "read_only_planning" or not isinstance(inputs, dict):
        raise ProposalError("unsupported reconciliation-plan schema")
    if inputs.get("rows_csv_sha256") != _sha256(rows_csv):
        raise ProposalError("rows CSV does not match the reconciliation plan")
    candidates = plan.get("delete_row_candidates")
    if not isinstance(candidates, list):
        raise ProposalError("reconciliation plan has no deletion candidates")
    reasons: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ProposalError("malformed deletion candidate")
        media_id, reason = candidate.get("media_id"), candidate.get("reason")
        if not isinstance(media_id, str) or not media_id or not isinstance(reason, str) or not reason:
            raise ProposalError("deletion candidate requires media_id and reason")
        if media_id in reasons:
            raise ProposalError("duplicate media_id in deletion candidates")
        reasons[media_id] = reason
    return reasons, str(inputs.get("destination_manifest_sha256") or "")


def build(plan_path: Path, rows_csv: Path, output: Path) -> dict[str, Any]:
    """Write the immutable candidate TSV and its small proposal receipt."""
    output = output.resolve()
    candidate_tsv = output.with_suffix(".tsv")
    if output.exists() or candidate_tsv.exists():
        raise ProposalError("refusing to overwrite a deletion proposal")
    reasons, destination_manifest_sha256 = _load_plan(plan_path, rows_csv)
    if len(destination_manifest_sha256) != 64:
        raise ProposalError("plan lacks its destination-manifest digest")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=candidate_tsv.name + ".", dir=output.parent)
    seen: set[str] = set()
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle, rows_csv.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            required = {"id", "file_path", "file_size", "sha256"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ProposalError("rows CSV must have id,file_path,file_size,sha256 headers")
            writer = csv.DictWriter(handle, fieldnames=["media_id", "file_path", "file_size", "sha256", "reason"], delimiter="\t")
            writer.writeheader()
            for row in reader:
                media_id = row["id"]
                reason = reasons.get(media_id)
                if reason is None:
                    continue
                if media_id in seen:
                    raise ProposalError("rows CSV contains duplicate deletion-candidate media_id")
                if not row["file_path"] or not row["file_size"] or not row["sha256"]:
                    raise ProposalError("deletion candidate lacks its exact before-image")
                seen.add(media_id)
                writer.writerow({"media_id": media_id, "file_path": row["file_path"], "file_size": row["file_size"], "sha256": row["sha256"], "reason": reason})
        if seen != set(reasons):
            raise ProposalError("rows CSV is missing a planned deletion candidate")
        temporary = Path(temporary_name)
        tsv_sha256 = _sha256(temporary)
        receipt = {
            "schema_version": 1,
            "mode": "proposal_only_no_database_or_filesystem_mutation",
            "created_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "inputs": {
                "plan": str(plan_path),
                "plan_sha256": _sha256(plan_path),
                "rows_csv": str(rows_csv),
                "rows_csv_sha256": _sha256(rows_csv),
                "destination_manifest_sha256": destination_manifest_sha256,
            },
            "candidate_tsv": {"path": str(candidate_tsv), "sha256": tsv_sha256, "count": len(seen)},
            "application_preconditions": [
                "revalidate every candidate before-image against the recovered database in one transaction",
                "delete only media_items rows; preserve parent source/post/message metadata",
                "do not mutate unresolved rows or unreferenced files",
                "require the exact Task 12 Branch-B decision and this proposal digest",
            ],
        }
        temporary.replace(candidate_tsv)
        receipt_temporary = output.with_suffix(output.suffix + ".tmp")
        receipt_temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt_temporary.replace(output)
        os.chmod(candidate_tsv, 0o600)
        os.chmod(output, 0o600)
        return receipt
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--rows-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = build(args.plan, args.rows_csv, args.output)
    except ProposalError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({"candidate_count": receipt["candidate_tsv"]["count"], "status": "proposal_written"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
