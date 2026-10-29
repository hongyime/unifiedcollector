from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.recovery.record_task53_snapshot import record


def _write_private(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def test_snapshot_binds_verified_dump_and_exact_private_exports(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    dump = tmp_path / "task-53-production-forward-db.dump"
    dump.write_bytes(b"dump")
    dump.chmod(0o600)
    _write_private(tmp_path / "task-53-production-forward-db.dump.sha256", f"{hashlib.sha256(b'dump').hexdigest()}  {dump}\n")
    _write_private(tmp_path / "task-53-production-forward-cursor-queue.json", json.dumps({
        "task": 53, "schema_version": 1, "owner_identity": "legal-data-use-owner",
        "selected_namespace": "production", "selected_branch": "recovered",
        "selected_volume": "unifiedcollector_pgdata_recovered", "captured_at_utc": "2026-10-29T00:00:00Z",
        "service_cursors": [{"service": "whatsapp"}], "browser_native_scrape_queue": [],
    }))
    _write_private(tmp_path / "task-53-production-forward-media-rows.csv", "id,file_path,file_size,sha256\n1,/media/a,1," + "a" * 64 + "\n")
    _write_private(tmp_path / "task-53-production-forward-capture-time.txt", "2026-10-29T00:00:00Z\n")

    snapshot = record(tmp_path)

    assert snapshot["media_items"] == 1
    assert snapshot["service_cursors"] == 1
    assert snapshot["browser_native_scrape_queue"] == 0
    assert (tmp_path / "task-53-production-forward-snapshot.json").stat().st_mode & 0o777 == 0o600
