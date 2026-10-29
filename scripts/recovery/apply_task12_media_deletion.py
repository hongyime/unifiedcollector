#!/usr/bin/env python3
"""Apply Task 12's hash-bound media-row proposal to a recovered database only."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".omo/evidence/collector-production-recovery"
PLAN = EVIDENCE / "task-11-media-reconciliation-plan.json"
PROPOSAL = EVIDENCE / "task-12-media-deletion-proposal.json"


class DeletionError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside_evidence(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(EVIDENCE.resolve())
    except ValueError as exc:
        raise DeletionError("proposal candidate file must remain under recovery evidence") from exc
    return resolved


def verify_proposal() -> tuple[Path, int, str]:
    try:
        receipt = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeletionError("Task 12 deletion proposal is unavailable or malformed") from exc
    if receipt.get("mode") != "proposal_only_no_database_or_filesystem_mutation":
        raise DeletionError("Task 12 receipt is not a deletion proposal")
    inputs, candidate = receipt.get("inputs"), receipt.get("candidate_tsv")
    if not isinstance(inputs, dict) or not isinstance(candidate, dict):
        raise DeletionError("Task 12 deletion proposal schema is incomplete")
    if inputs.get("plan_sha256") != _sha256(PLAN):
        raise DeletionError("Task 12 deletion proposal is not bound to the current Task 11 plan")
    raw_path, expected_sha, expected_count = candidate.get("path"), candidate.get("sha256"), candidate.get("count")
    if not isinstance(raw_path, str) or not isinstance(expected_sha, str) or not isinstance(expected_count, int) or expected_count <= 0:
        raise DeletionError("Task 12 deletion proposal has invalid candidate metadata")
    path = _inside_evidence(Path(raw_path))
    if not path.is_file() or _sha256(path) != expected_sha:
        raise DeletionError("Task 12 candidate TSV is missing or hash-mismatched")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"media_id", "file_path", "file_size", "sha256", "reason"}
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            raise DeletionError("Task 12 candidate TSV header is invalid")
        count = sum(1 for _ in reader)
    if count != expected_count:
        raise DeletionError("Task 12 candidate TSV count is mismatched")
    return path, count, expected_sha


def _transaction_sql(candidate_tsv: str) -> str:
    # psql treats a blank line in COPY's STDIN stream as a malformed record.
    # The CSV writer ends the approved TSV with a newline, so strip only that
    # framing before adding the required standalone ``\\.`` terminator.
    candidate_tsv = candidate_tsv.rstrip("\r\n")
    return f"""\\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE task12_media_candidates (
  media_id uuid PRIMARY KEY,
  file_path text NOT NULL,
  file_size bigint NOT NULL,
  sha256 text NOT NULL,
  reason text NOT NULL
) ON COMMIT DROP;
\\copy task12_media_candidates (media_id, file_path, file_size, sha256, reason) FROM STDIN WITH (FORMAT csv, HEADER true, DELIMITER E'\\t')
{candidate_tsv}
\\.
DO $$
DECLARE expected_count bigint; exact_count bigint; deleted_count bigint;
BEGIN
  SELECT count(*) INTO expected_count FROM task12_media_candidates;
  SELECT count(*) INTO exact_count
  FROM media_items AS media
  JOIN task12_media_candidates AS candidate ON candidate.media_id = media.id
  WHERE media.file_path = candidate.file_path
    AND media.file_size = candidate.file_size
    AND media.sha256 = candidate.sha256;
  IF exact_count <> expected_count THEN
    RAISE EXCEPTION 'Task 12 before-image mismatch: expected %, found %', expected_count, exact_count;
  END IF;
  CREATE TEMP TABLE task12_affected_sources (
    source text PRIMARY KEY
  ) ON COMMIT DROP;
  INSERT INTO task12_affected_sources (source)
  SELECT DISTINCT media.source
  FROM media_items AS media
  JOIN task12_media_candidates AS candidate ON candidate.media_id = media.id
  WHERE media.file_path = candidate.file_path
    AND media.file_size = candidate.file_size
    AND media.sha256 = candidate.sha256;
  ALTER TABLE media_items DISABLE TRIGGER trg_media_source_rollups;
  DELETE FROM media_items AS media
  USING task12_media_candidates AS candidate
  WHERE media.id = candidate.media_id
    AND media.file_path = candidate.file_path
    AND media.file_size = candidate.file_size
    AND media.sha256 = candidate.sha256;
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  IF deleted_count <> expected_count THEN
    RAISE EXCEPTION 'Task 12 deletion count mismatch: expected %, deleted %', expected_count, deleted_count;
  END IF;
  INSERT INTO media_source_rollups (
    source, total_media_items, total_media_bytes, latest_media_at, updated_at
  )
  SELECT media.source, count(*), COALESCE(sum(media.file_size), 0), max(media.collected_at), NOW()
  FROM media_items AS media
  JOIN task12_affected_sources AS affected ON affected.source = media.source
  GROUP BY media.source
  ON CONFLICT (source) DO UPDATE SET
    total_media_items = EXCLUDED.total_media_items,
    total_media_bytes = EXCLUDED.total_media_bytes,
    latest_media_at = EXCLUDED.latest_media_at,
    updated_at = EXCLUDED.updated_at;
  DELETE FROM media_source_rollups AS rollup
  USING task12_affected_sources AS affected
  WHERE rollup.source = affected.source
    AND NOT EXISTS (SELECT 1 FROM media_items AS media WHERE media.source = affected.source);
  ALTER TABLE media_items ENABLE TRIGGER trg_media_source_rollups;
END $$;
COMMIT;
"""


def apply(container: str, output: Path) -> dict[str, Any]:
    candidate, count, candidate_sha = verify_proposal()
    output = _inside_evidence(output)
    if output.exists():
        raise DeletionError("refusing to overwrite a Task 12 application receipt")
    sql = _transaction_sql(candidate.read_text(encoding="utf-8"))
    completed = subprocess.run(
        ["docker", "exec", "-i", "-u", "postgres", container, "psql", "-U", "postgres", "-d", "unifiedcollector"],
        input=sql, text=True, capture_output=True, check=False,
    )
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "psql transaction failed"
        raise DeletionError(f"Task 12 media deletion aborted: {message}")
    remaining = subprocess.run(
        ["docker", "exec", "-u", "postgres", container, "psql", "-U", "postgres", "-At", "-d", "unifiedcollector", "-c", "SELECT count(*) FROM media_items;"],
        text=True, capture_output=True, check=False,
    )
    if remaining.returncode or not remaining.stdout.strip().isdigit():
        raise DeletionError("cannot read back recovered media_items count")
    document = {
        "schema_version": 1,
        "task": 12,
        "mode": "recovered_database_transaction_only",
        "container": container,
        "proposal_sha256": _sha256(PROPOSAL),
        "candidate_tsv_sha256": candidate_sha,
        "deleted_media_items": count,
        "remaining_media_items": int(remaining.stdout.strip()),
        "parent_metadata_preserved": True,
        "unresolved_rows_or_orphan_files_mutated": False,
        "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = apply(args.container, args.output)
    except DeletionError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({"deleted_media_items": result["deleted_media_items"], "remaining_media_items": result["remaining_media_items"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
