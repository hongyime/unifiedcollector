"""Task 45 browser-transport receipt gates are fail-closed."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.verify_browser_transport_gate import REPO_ROOT, load_receipt, verify_receipt


RECEIPT_PATH = REPO_ROOT / ".omo/evidence/collector-production-recovery/task-45-browser.json"


def test_recorded_browser_no_go_is_hash_bound_and_blocks_task_50() -> None:
    receipt = load_receipt(RECEIPT_PATH)

    assert verify_receipt(receipt) == []
    assert receipt["browser_transport_decision"] == "blocked"
    assert receipt["task_50_rule"]["blocked_prevents_start"] is True


def test_receipt_rejects_a_blocked_transport_without_start_protection(tmp_path: Path) -> None:
    receipt = load_receipt(RECEIPT_PATH)
    receipt["task_50_rule"]["blocked_prevents_start"] = False
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")

    errors = verify_receipt(load_receipt(path))

    assert "blocked transport must explicitly prevent task 50 start" in errors
