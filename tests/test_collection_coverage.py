import asyncio
from datetime import datetime, timedelta, timezone

from src.core.collection_coverage import build_collection_coverage_snapshot


class Conn:
    def __init__(self):
        self.inserted = []
        self.now = datetime.now(timezone.utc)

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return True
        if "source_health" in sql:
            return "running"
        if "rate_limit_events" in sql:
            return 1
        return None

    async def fetch(self, sql, *args):
        if "FROM source_health" in sql:
            return [{"source": "telegram"}]
        return []

    async def fetchrow(self, sql, *args):
        if "FROM media_items" in sql:
            return {"latest_data_at": self.now - timedelta(hours=1), "media_24h": 42}
        if "FROM collection_runs" in sql:
            return {"latest_run_at": self.now - timedelta(hours=1), "errors_24h": 0}
        return {}

    async def executemany(self, sql, rows):
        assert "collection_coverage_snapshots" in sql
        self.inserted.extend(rows)


def test_collection_coverage_snapshot_writes_digest():
    conn = Conn()

    report = asyncio.run(build_collection_coverage_snapshot(conn, expected_cadence_hours=24))

    assert report["summary"]["fresh"] == 1
    assert report["summary"]["digest"].startswith("Coverage: 1/1 sources fresh")
    assert len(conn.inserted) == 1


def test_collection_coverage_snapshot_dry_run_does_not_write():
    conn = Conn()

    report = asyncio.run(build_collection_coverage_snapshot(conn, write=False))

    assert report["written"] == 0
    assert conn.inserted == []
