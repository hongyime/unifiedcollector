from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.media_artifact_audit import audit_media_artifacts


class FakeConn:
    def __init__(self, *, rollups, rows_by_source, fail_sources=None):
        self.rollups = rollups
        self.rows_by_source = rows_by_source
        self.fail_sources = set(fail_sources or [])
        self.fetch_queries: list[str] = []
        self.fetch_args: list[tuple] = []

    async def fetchrow(self, query, *args, **kwargs):
        self.fetch_queries.append(query)
        self.fetch_args.append(args)
        wanted = args[0]
        for row in self.rollups:
            if row["source"] == wanted:
                return row
        return None

    async def fetch(self, query, *args, **kwargs):
        self.fetch_queries.append(query)
        self.fetch_args.append(args)
        if "FROM media_source_rollups" in query:
            return self.rollups
        if "FROM media_items" in query:
            source, cursor, limit = args
            if source in self.fail_sources:
                raise TimeoutError()
            rows = [
                row
                for row in self.rows_by_source.get(source, [])
                if str(row.get("content_id") or "") > cursor
            ]
            return rows[:limit]
        raise AssertionError(query)


def _rollup(source: str):
    return {
        "source": source,
        "total_media_items": 2,
        "total_media_bytes": 42,
        "latest_media_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_audit_media_artifacts_checks_files_and_sidecars(tmp_path):
    media = tmp_path / "media" / "telegram" / "photo.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"photo")
    sidecar = tmp_path / "sidecars" / "telegram" / "photo.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("{}", encoding="utf-8")
    conn = FakeConn(
        rollups=[_rollup("telegram")],
        rows_by_source={
            "telegram": [
                {
                    "content_id": "001",
                    "file_path": str(media),
                    "file_size": 5,
                    "occurrence_sidecar_path": "sidecars/telegram/photo.json",
                    "artifact_sidecar_path": None,
                },
                {
                    "content_id": "002",
                    "file_path": str(tmp_path / "media" / "telegram" / "missing.jpg"),
                    "file_size": 8,
                    "occurrence_sidecar_path": None,
                    "artifact_sidecar_path": None,
                },
            ],
        },
    )

    report = await audit_media_artifacts(conn, source="telegram", sample_per_source=10, vault_root=tmp_path)
    row = report.sources[0]

    assert report.mode == "keyset_sample_by_source_content_id"
    assert row.total_media_items == 2
    assert row.sampled == 2
    assert row.files_present == 1
    assert row.files_missing == 1
    assert row.sidecar_metadata_present == 1
    assert row.sidecar_metadata_missing == 1
    assert row.sidecar_files_present == 1
    assert row.next_cursor == "002"
    assert row.issue_count == 2
    assert {failure["kind"] for failure in row.failures} == {"file_missing", "sidecar_metadata_missing"}


@pytest.mark.asyncio
async def test_audit_media_artifacts_reports_size_and_sidecar_file_mismatches(tmp_path):
    media = tmp_path / "media" / "instagram" / "photo.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"real")
    conn = FakeConn(
        rollups=[_rollup("instagram")],
        rows_by_source={
            "instagram": [
                {
                    "content_id": "abc",
                    "file_path": str(media),
                    "file_size": 99,
                    "occurrence_sidecar_path": "sidecars/instagram/missing.json",
                    "artifact_sidecar_path": None,
                },
            ],
        },
    )

    report = await audit_media_artifacts(conn, source="instagram", sample_per_source=1, vault_root=tmp_path)
    row = report.sources[0]

    assert row.files_present == 1
    assert row.size_mismatches == 1
    assert row.sidecar_metadata_present == 1
    assert row.sidecar_files_missing == 1
    assert row.issue_count == 2
    assert {failure["kind"] for failure in row.failures} == {"size_mismatch", "sidecar_file_missing"}


@pytest.mark.asyncio
async def test_audit_media_artifacts_keeps_other_sources_when_one_times_out(tmp_path):
    ok_media = tmp_path / "media" / "website" / "page.png"
    ok_media.parent.mkdir(parents=True)
    ok_media.write_bytes(b"png")
    conn = FakeConn(
        rollups=[_rollup("telegram"), _rollup("website")],
        rows_by_source={
            "website": [
                {
                    "content_id": "001",
                    "file_path": str(ok_media),
                    "file_size": 3,
                    "occurrence_sidecar_path": None,
                    "artifact_sidecar_path": None,
                }
            ],
        },
        fail_sources={"telegram"},
    )

    report = await audit_media_artifacts(conn, sample_per_source=600, vault_root=tmp_path)

    assert report.sample_per_source == 500
    assert report.sources[0].source == "telegram"
    assert report.sources[0].query_error.startswith("TimeoutError")
    assert report.sources[1].source == "website"
    assert report.sources[1].sampled == 1
    sample_query = next(query for query in conn.fetch_queries if "FROM media_items" in query)
    assert "ORDER BY content_id" in sample_query
    assert "collected_at" not in sample_query
