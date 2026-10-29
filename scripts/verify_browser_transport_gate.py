#!/usr/bin/env python3
"""Validate the Task 45 browser-transport receipt before consumer activation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECEIPT = REPO_ROOT / ".omo/evidence/collector-production-recovery/task-45-browser.json"


def load_receipt(path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read browser transport receipt: {exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("unsupported browser transport receipt schema")
    return receipt


def verify_receipt(receipt: dict[str, Any], repo_root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    if receipt.get("task") != 45:
        errors.append("receipt must belong to task 45")
    decision = receipt.get("browser_transport_decision")
    if decision not in {"certified", "blocked"}:
        errors.append("browser_transport_decision must be certified or blocked")
    if not isinstance(receipt.get("exact_use"), str) or not receipt["exact_use"].strip():
        errors.append("exact_use is required")

    certification = receipt.get("certification_receipt")
    if not isinstance(certification, dict):
        return errors + ["certification_receipt is required"]
    source = certification.get("path")
    expected_hash = certification.get("sha256")
    if not isinstance(source, str) or not isinstance(expected_hash, str):
        return errors + ["certification receipt path and sha256 are required"]
    source_path = repo_root / source
    try:
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except OSError as exc:
        return errors + [f"cannot hash certification receipt: {exc}"]
    if actual_hash != expected_hash:
        errors.append("certification receipt sha256 does not match")

    task_50_rule = receipt.get("task_50_rule")
    if not isinstance(task_50_rule, dict):
        return errors + ["task_50_rule is required"]
    if decision == "blocked" and task_50_rule.get("blocked_prevents_start") is not True:
        errors.append("blocked transport must explicitly prevent task 50 start")
    if not isinstance(task_50_rule.get("required_before_start"), str):
        errors.append("task_50_rule must name certification required before start")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--require-certified", action="store_true")
    args = parser.parse_args()
    try:
        receipt = load_receipt(args.receipt)
        errors = verify_receipt(receipt)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    if errors:
        print("ERROR: " + "; ".join(errors))
        return 2
    decision = receipt["browser_transport_decision"]
    if args.require_certified and decision != "certified":
        print("BLOCKED: browser transport is not certified for the recorded exact use")
        return 3
    print(f"PASS: browser transport decision={decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
