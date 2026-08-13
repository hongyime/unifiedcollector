from __future__ import annotations

import json

import pytest


class FakeConn:
    def __init__(self, rows=None, *, delivery_table=True) -> None:
        self.rows = rows or []
        self.delivery_table = delivery_table
        self.executed: list[dict] = []

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return "realtime_media_deliveries" if self.delivery_table else None
        return None

    async def fetch(self, sql, *args):
        self.fetch_sql = sql
        self.fetch_args = args
        return self.rows

    async def execute(self, sql, *args):
        self.executed.append({"sql": sql, "args": args})
        return "INSERT 0 1"


class FakeRedis:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []
        self.closed = False

    async def rpush(self, key: str, value: str) -> int:
        self.pushed.append((key, value))
        return len(self.pushed)

    async def aclose(self) -> None:
        self.closed = True


def _media_row(source="threads", content_id="cid-1", content_type="photo"):
    return {
        "source": source,
        "entity_name": "profile",
        "content_id": content_id,
        "file_path": "/vault/blob.jpg",
        "source_url": "https://example.test/post",
        "sha256": "a" * 64,
        "metadata": {"caption": "stored media"},
        "kind": "image",
        "content_type": content_type,
        "file_size": 1234,
        "collected_at": "2026-08-12T12:00:00Z",
        "delivery_status": None,
    }


def test_parse_sources_rejects_private_without_flag():
    from src.notifications.realtime_backfill import parse_sources

    with pytest.raises(ValueError, match="private realtime source"):
        parse_sources("telegram")


@pytest.mark.asyncio
async def test_realtime_media_backfill_dry_run_selects_without_enqueue():
    from src.notifications.realtime_backfill import run_realtime_media_backfill

    conn = FakeConn(rows=[_media_row()])
    report = await run_realtime_media_backfill(
        conn,
        sources=["threads"],
        dry_run=True,
    )

    assert report["dry_run"] is True
    assert report["selected"] == 1
    assert report["enqueued"] == 0
    assert report["by_source"]["threads"]["selected"] == 1
    assert conn.executed == []


@pytest.mark.asyncio
async def test_realtime_media_backfill_handles_missing_delivery_table():
    from src.notifications.realtime_backfill import fetch_candidates

    conn = FakeConn(rows=[_media_row()], delivery_table=False)

    rows = await fetch_candidates(
        conn,
        sources=["threads"],
        since_hours=12,
        limit=5,
        per_source_limit=2,
    )

    assert len(rows) == 1
    assert "realtime_media_deliveries" not in conn.fetch_sql
    assert "$4::bool" in conn.fetch_sql


@pytest.mark.asyncio
async def test_realtime_media_backfill_enqueues_and_records(monkeypatch):
    from src.notifications import realtime_backfill

    redis = FakeRedis()

    async def open_redis():
        return redis

    monkeypatch.setattr(realtime_backfill, "_open_redis_client", open_redis)
    conn = FakeConn(rows=[_media_row(source="instagram", content_id="ig-1")])

    report = await realtime_backfill.run_realtime_media_backfill(
        conn,
        sources=["instagram"],
        dry_run=False,
        sleep_seconds=0,
    )

    assert report["enqueued"] == 1
    assert redis.closed is True
    assert len(redis.pushed) == 1
    payload = json.loads(redis.pushed[0][1])
    assert payload["source"] == "instagram"
    assert payload["content_id"] == "ig-1"
    assert conn.executed


@pytest.mark.asyncio
async def test_realtime_media_backfill_records_profile_skip(monkeypatch):
    from src.notifications import realtime_backfill

    redis = FakeRedis()

    async def open_redis():
        return redis

    monkeypatch.setattr(realtime_backfill, "_open_redis_client", open_redis)
    conn = FakeConn(rows=[_media_row(content_id="pfp", content_type="profile_photo")])

    report = await realtime_backfill.run_realtime_media_backfill(
        conn,
        sources=["threads"],
        include_profiles=True,
        dry_run=False,
        sleep_seconds=0,
    )

    assert report["skipped"] == 1
    assert redis.pushed == []
    assert conn.executed
