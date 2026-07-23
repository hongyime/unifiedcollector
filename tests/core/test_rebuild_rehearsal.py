from __future__ import annotations

import hashlib
import json
import sqlite3

from src.core.rebuild_rehearsal import rehearse_media_items_rebuild


def _write_media_sidecar(root, source, content_id, data=b"bytes"):
    media = root / "media" / source / f"{content_id}.jpg"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(data)
    sidecar = root / "sidecars" / source / "2026" / "07" / f"{content_id}.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": source,
            "entity": {"id": "u1", "name": "User One"},
            "content": {
                "type": "photo",
                "id": content_id,
                "filename": media.name,
            },
            "file": {
                "path": f"media/{source}/{content_id}.jpg",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }),
        encoding="utf-8",
    )
    return sidecar


def test_rebuild_rehearsal_materializes_media_sidecars_into_scratch_db(tmp_path):
    _write_media_sidecar(tmp_path, "instagram", "p1")
    scratch = tmp_path / "scratch.sqlite"

    report = rehearse_media_items_rebuild(tmp_path, scratch_db=scratch)

    assert report.media_sidecars_seen == 1
    assert report.media_rows_inserted == 1
    assert report.inserted_by_source["instagram"] == 1
    with sqlite3.connect(scratch) as conn:
        row = conn.execute(
            "SELECT source, content_id, entity_name FROM media_items"
        ).fetchone()
    assert row == ("instagram", "p1", "User One")


def test_rebuild_rehearsal_reports_skips_without_touching_live_db(tmp_path):
    _write_media_sidecar(tmp_path, "telegram", "ok")
    missing = tmp_path / "sidecars" / "telegram" / "2026" / "07" / "bad.json"
    missing.write_text(
        json.dumps({
            "artifact_kind": "media",
            "source": "telegram",
            "entity": {"id": "u1"},
            "content": {"type": "photo", "id": "bad"},
            "file": {"path": "media/telegram/missing.jpg"},
        }),
        encoding="utf-8",
    )

    report = rehearse_media_items_rebuild(tmp_path, sidecar_limit=2)

    assert report.sidecars_scanned == 2
    assert report.media_rows_inserted == 1
    assert report.skipped_by_reason["missing_entity_name"] == 1
    assert report.skipped_by_reason["missing_filename"] == 1
