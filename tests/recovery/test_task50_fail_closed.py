from __future__ import annotations

import json
import subprocess
import sys

import pytest

from scripts.recovery.record_task50_fail_closed import GateError, record


def test_fail_closed_receipt_requires_task12_rejection(tmp_path) -> None:
    manifest = tmp_path / "task12.json"
    manifest.write_text(json.dumps({"schema_version": 1, "task": 12, "manifest_status": "preservation_only_not_activation_approved"}))
    receipt = record(manifest, tmp_path / "task50.json")
    assert receipt["status"] == "blocked_before_consumer_start"
    assert receipt["consumer_started"] is False


def test_fail_closed_receipt_refuses_an_activation_approved_manifest(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "task12.json"
    manifest.write_text("{}")
    monkeypatch.setattr("scripts.recovery.record_task50_fail_closed.validate_reconciliation_manifest", lambda _: None)
    with pytest.raises(GateError, match="must not mask"):
        record(manifest, tmp_path / "task50.json")


def test_fail_closed_recorder_is_directly_executable() -> None:
    result = subprocess.run([sys.executable, "scripts/recovery/record_task50_fail_closed.py", "--help"], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
