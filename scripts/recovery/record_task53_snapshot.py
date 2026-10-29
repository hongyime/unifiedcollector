#!/usr/bin/env python3
"""Bind completed Task 53 forward-capture artifacts without exposing payloads."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.recovery.capture_task53_cursor_queue import validate


class SnapshotError(RuntimeError):
    """The forward capture cannot be made into a durable snapshot receipt."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(evidence: Path) -> dict:
    if not evidence.is_dir() or stat.S_IMODE(evidence.stat().st_mode) != 0o700:
        raise SnapshotError("evidence root must be owner-only")
    dump = evidence / "task-53-production-forward-db.dump"
    checksum = dump.with_suffix(dump.suffix + ".sha256")
    cursor = evidence / "task-53-production-forward-cursor-queue.json"
    rows = evidence / "task-53-production-forward-media-rows.csv"
    captured = evidence / "task-53-production-forward-capture-time.txt"
    output = evidence / "task-53-production-forward-snapshot.json"
    if output.exists():
        raise SnapshotError("snapshot receipt already exists")
    for path in (dump, checksum, cursor, rows, captured):
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise SnapshotError(f"required private artifact is missing or has wrong mode: {path.name}")
    line = checksum.read_text(encoding="utf-8").strip().split()
    if len(line) != 2 or line[0] != _sha256(dump):
        raise SnapshotError("dump checksum is invalid")
    try:
        cursor_document = validate(json.loads(cursor.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SnapshotError("cursor/queue export is invalid") from exc
    with rows.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != ["id", "file_path", "file_size", "sha256"]:
            raise SnapshotError("media rows CSV header is invalid")
        media_items = sum(1 for _ in reader)
    captured_at = captured.read_text(encoding="utf-8").strip()
    if not captured_at.endswith("Z"):
        raise SnapshotError("capture timestamp is invalid")
    document = {
        "task": 53,
        "schema_version": 1,
        "selected_namespace": "production",
        "selected_branch": "recovered",
        "selected_volume": "unifiedcollector_pgdata_recovered",
        "captured_at_utc": captured_at,
        "dump_sha256": line[0],
        "cursor_queue_sha256": _sha256(cursor),
        "media_rows_sha256": _sha256(rows),
        "media_items": media_items,
        "service_cursors": len(cursor_document["service_cursors"]),
        "browser_native_scrape_queue": len(cursor_document["browser_native_scrape_queue"]),
        "writer_resume_order": [
            "wa-bridge-1", "wa-bridge-2", "collector_whatsapp", "collector_paypal",
            "recon_egress_guard", "collector_spiderfoot",
        ],
    }
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description="Record Task 53 private production forward snapshot")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = record(args.evidence)
    except SnapshotError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps({key: result[key] for key in ("media_items", "service_cursors", "browser_native_scrape_queue")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
