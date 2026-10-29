from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts.build_media_deletion_proposal import ProposalError, build


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = tmp_path / "rows.csv"
    rows.write_text("id,file_path,file_size,sha256\na,/media/a.jpg,1," + "a" * 64 + "\nb,/media/b.jpg,2," + "b" * 64 + "\n")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"mode": "read_only_planning", "inputs": {"rows_csv_sha256": _sha(rows), "destination_manifest_sha256": "c" * 64}, "delete_row_candidates": [{"media_id": "a", "reason": "missing_canonical_path"}]}))
    return plan, rows


def test_proposal_is_hash_bound_and_contains_exact_before_image(tmp_path: Path) -> None:
    plan, rows = _inputs(tmp_path)
    receipt = build(plan, rows, tmp_path / "proposal.json")
    candidate = tmp_path / "proposal.tsv"
    assert receipt["candidate_tsv"]["count"] == 1
    assert receipt["candidate_tsv"]["sha256"] == _sha(candidate)
    with candidate.open(newline="") as handle:
        assert list(csv.DictReader(handle, delimiter="\t")) == [{"media_id": "a", "file_path": "/media/a.jpg", "file_size": "1", "sha256": "a" * 64, "reason": "missing_canonical_path"}]


def test_proposal_rejects_changed_input_or_missing_candidate_before_image(tmp_path: Path) -> None:
    plan, rows = _inputs(tmp_path)
    rows.write_text(rows.read_text() + "c,/media/c.jpg,3," + "c" * 64 + "\n")
    with pytest.raises(ProposalError, match="does not match"):
        build(plan, rows, tmp_path / "changed.json")

    plan, rows = _inputs(tmp_path / "missing")
    payload = json.loads(plan.read_text())
    payload["delete_row_candidates"].append({"media_id": "missing", "reason": "missing_canonical_path"})
    plan.write_text(json.dumps(payload))
    with pytest.raises(ProposalError, match="missing a planned"):
        build(plan, rows, tmp_path / "missing.json")
