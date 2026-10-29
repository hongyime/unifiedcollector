from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.recovery.capture_task53_cursor_queue import CursorQueueError, capture, validate


def _document() -> dict:
    return {
        "task": 53,
        "schema_version": 1,
        "owner_identity": "legal-data-use-owner",
        "selected_namespace": "production",
        "selected_branch": "recovered",
        "selected_volume": "unifiedcollector_pgdata_recovered",
        "captured_at_utc": "2026-10-29T00:00:00.000000Z",
        "service_cursors": [{"service": "whatsapp", "status": "idle"}],
        "browser_native_scrape_queue": [],
    }


def test_capture_writes_new_owner_only_private_export(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    output = tmp_path / "cursor-queue.json"
    report = capture(output, run=lambda *_args: json.dumps(_document()))

    assert report == {"service_cursors": 1, "browser_native_scrape_queue": 0}
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["owner_identity"] == "legal-data-use-owner"
    with pytest.raises(CursorQueueError, match="must be new"):
        capture(output, run=lambda *_args: json.dumps(_document()))


def test_validate_rejects_wrong_branch_or_non_array_state() -> None:
    bad = _document()
    bad["selected_branch"] = "baseline"
    with pytest.raises(CursorQueueError, match="ownership envelope"):
        validate(bad)
    bad = _document()
    bad["service_cursors"] = {}
    with pytest.raises(CursorQueueError, match="must be an array"):
        validate(bad)
