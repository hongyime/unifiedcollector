from datetime import datetime, timedelta, timezone

import pytest

from src.core.seen_targets import (
    SeenTarget,
    collect_seen_target_records,
    merge_seen_targets,
    upsert_seen_targets,
)


class _FakeConn:
    def __init__(self):
        self.inserted = []
        self.now = datetime.now(timezone.utc)

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return args[0] in {"social_users", "collection_targets", "instagram_profiles", "collector_seen_targets"}
        return None

    async def fetch(self, sql, *args):
        if "FROM social_users" in sql:
            return [{
                "platform": "instagram",
                "uid": "123",
                "platform_user_id": "123",
                "username": "Alice",
                "display_name": "Alice A",
                "first_seen": self.now - timedelta(days=2),
                "last_seen": self.now - timedelta(hours=1),
                "times_seen": 3,
                "contexts": ["follow"],
                "profile_photo_url": "https://example.test/a.jpg",
                "metadata": {},
            }]
        if "FROM collection_targets" in sql:
            return [{
                "source": "instagram",
                "target_type": "user",
                "target_id": "alice",
                "target_name": "Alice",
                "priority": 7,
                "status": "pending",
                "collection_count": 0,
                "last_collection_at": None,
                "created_at": self.now - timedelta(days=2),
                "metadata": {},
            }]
        if "FROM instagram_profiles" in sql:
            return [{
                "target_key": "alice",
                "target_display": "Alice A",
                "source_record_id": "profile-row",
                "first_seen_at": self.now - timedelta(days=2),
                "last_seen_at": self.now - timedelta(minutes=15),
                "last_backfill_at": self.now - timedelta(minutes=15),
                "evidence_count": 12,
                "metadata": {"platform_user_id": "123"},
                "raw_status": None,
            }]
        return []

    async def executemany(self, sql, rows):
        self.inserted.extend(rows)


def test_merge_seen_targets_prefers_backfilled_profile_over_pending_queue():
    now = datetime.now(timezone.utc)
    rows = merge_seen_targets([
        SeenTarget("Instagram", "user", "@Alice", status="pending", priority=4, first_seen_at=now - timedelta(days=1)),
        SeenTarget("instagram", "profile", "alice", status="fresh", priority=2, evidence_count=9, last_backfill_at=now),
    ])

    assert len(rows) == 1
    assert rows[0].platform == "instagram"
    assert rows[0].target_type == "user"
    assert rows[0].target_key == "alice"
    assert rows[0].status == "fresh"
    assert rows[0].evidence_count == 9


@pytest.mark.asyncio
async def test_collect_seen_target_records_reads_existing_sources():
    conn = _FakeConn()

    records = await collect_seen_target_records(conn, source="instagram", limit_per_source=50)

    assert len(records) == 1
    record = records[0]
    assert record.platform == "instagram"
    assert record.target_key == "alice"
    assert record.status == "fresh"
    assert record.source_table == "instagram_profiles"


@pytest.mark.asyncio
async def test_upsert_seen_targets_writes_registry_rows():
    conn = _FakeConn()

    written = await upsert_seen_targets(conn, [
        SeenTarget("website", "domain", "Example.COM", status="pending", source_table="website_targets"),
    ])

    assert written == 1
    assert conn.inserted[0][0] == "website"
    assert conn.inserted[0][1] == "domain"
    assert conn.inserted[0][2] == "example.com"
