#!/usr/bin/env python3
"""Capture the production cursor and native queue before Task 53 writer resume."""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


OWNER = "legal-data-use-owner"
CONTAINER = "unifiedcollector_postgres"
QUERY = """
SELECT json_build_object(
  'task', 53,
  'schema_version', 1,
  'owner_identity', 'legal-data-use-owner',
  'selected_namespace', 'production',
  'selected_branch', 'recovered',
  'selected_volume', 'unifiedcollector_pgdata_recovered',
  'captured_at_utc', to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'service_cursors', COALESCE((SELECT json_agg(to_jsonb(cursor) ORDER BY cursor.service) FROM service_cursors AS cursor), '[]'::json),
  'browser_native_scrape_queue', COALESCE((SELECT json_agg(to_jsonb(queue) ORDER BY queue.id) FROM browser_native_scrape_queue AS queue), '[]'::json)
)::text;
"""


class CursorQueueError(RuntimeError):
    """The private cursor/queue capture is not safe to use."""


def _run(*command: str) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise CursorQueueError("production cursor/queue export command failed")
    return result.stdout


def validate(document: object) -> dict:
    if not isinstance(document, dict):
        raise CursorQueueError("cursor/queue export must be a JSON object")
    required = {
        "task": 53,
        "schema_version": 1,
        "owner_identity": OWNER,
        "selected_namespace": "production",
        "selected_branch": "recovered",
        "selected_volume": "unifiedcollector_pgdata_recovered",
    }
    if any(document.get(key) != value for key, value in required.items()):
        raise CursorQueueError("cursor/queue ownership envelope is invalid")
    if not isinstance(document.get("captured_at_utc"), str):
        raise CursorQueueError("cursor/queue capture timestamp is missing")
    for key in ("service_cursors", "browser_native_scrape_queue"):
        if not isinstance(document.get(key), list):
            raise CursorQueueError(f"{key} must be an array")
    return document


def capture(output: Path, *, run: Callable[..., str] = _run) -> dict:
    if output.exists() or not output.parent.is_dir() or stat.S_IMODE(output.parent.stat().st_mode) != 0o700:
        raise CursorQueueError("cursor/queue output must be new and inside owner-only evidence")
    raw = run("docker", "exec", "-u", "postgres", CONTAINER, "psql", "-U", "postgres", "-d", "unifiedcollector", "-Atq", "-c", QUERY)
    try:
        document = validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise CursorQueueError("production cursor/queue export was not valid JSON") from exc
    document["captured_by_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "service_cursors": len(document["service_cursors"]),
        "browser_native_scrape_queue": len(document["browser_native_scrape_queue"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Task 53 production cursor/queue state privately")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        counts = capture(args.output)
    except CursorQueueError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
