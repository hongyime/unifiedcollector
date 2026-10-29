from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.recovery import apply_task12_media_deletion as deletion


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    candidates = tmp_path / "proposal.tsv"
    candidates.write_text("media_id\tfile_path\tfile_size\tsha256\treason\n00000000-0000-0000-0000-000000000001\t/media/a.jpg\t1\t" + "a" * 64 + "\tmissing_canonical_path\n")
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({
        "mode": "proposal_only_no_database_or_filesystem_mutation",
        "inputs": {"plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest()},
        "candidate_tsv": {"path": str(candidates), "sha256": hashlib.sha256(candidates.read_bytes()).hexdigest(), "count": 1},
    }))
    monkeypatch.setattr(deletion, "EVIDENCE", tmp_path)
    monkeypatch.setattr(deletion, "PLAN", plan)
    monkeypatch.setattr(deletion, "PROPOSAL", proposal)
    return candidates


def test_proposal_verification_rejects_changed_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = _prepare(tmp_path, monkeypatch)
    path, count, _ = deletion.verify_proposal()
    assert path == candidates and count == 1
    candidates.write_text(candidates.read_text() + "x")
    with pytest.raises(deletion.DeletionError, match="hash-mismatched"):
        deletion.verify_proposal()


def test_transaction_requires_exact_before_images(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    sql = deletion._transaction_sql("media_id\tfile_path\tfile_size\tsha256\treason\n")
    assert "BEGIN;" in sql and "COMMIT;" in sql
    assert "before-image mismatch" in sql
    assert "media.file_path = candidate.file_path" in sql
    assert "media.file_size = candidate.file_size" in sql
    assert "media.sha256 = candidate.sha256" in sql


def test_transaction_does_not_insert_a_blank_copy_record_before_terminator() -> None:
    sql = deletion._transaction_sql("media_id\tfile_path\tfile_size\tsha256\treason\n")
    assert "reason\n\\.\nDO $$" in sql
    assert "reason\n\n\\.\nDO $$" not in sql


def test_transaction_recomputes_affected_rollups_once_without_row_trigger_cost() -> None:
    sql = deletion._transaction_sql("media_id\tfile_path\tfile_size\tsha256\treason\n")
    assert "ALTER TABLE media_items DISABLE TRIGGER trg_media_source_rollups" in sql
    assert "CREATE TEMP TABLE task12_affected_sources" in sql
    assert "GROUP BY media.source" in sql
    assert "ALTER TABLE media_items ENABLE TRIGGER trg_media_source_rollups" in sql
