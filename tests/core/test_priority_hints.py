import asyncio

from src.core import priority_hints


class _FakeConn:
    def __init__(self):
        self.executemany_calls = []

    async def executemany(self, sql, records):
        self.executemany_calls.append((sql, records))


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return _AcquireContext(self.conn)


def _hint(source="instagram", target_id="123", username="Alice", confidence=97.0, **extra):
    row = {
        "id": "hint-1",
        "source": source,
        "target_id": target_id,
        "target_username": username,
        "priority": 1,
        "confidence": confidence,
        "hint_type": "identity_priority",
        "entity_id": "entity-a",
        "candidate_entity_id": "entity-b",
        "relationship_id": "rel-1",
        "evidence": {"score": confidence},
        "updated_at": "2026-07-20T10:00:00+00:00",
    }
    row.update(extra)
    return row


def test_collector_target_for_hint_maps_usernames_and_native_ids(monkeypatch):
    monkeypatch.setenv("COLLECTOR_PRIORITY_HINTS_TARGET_PRIORITY", "7")

    instagram, reason = priority_hints.collector_target_for_hint(_hint("instagram", "123", "@Alice"))
    strava, strava_reason = priority_hints.collector_target_for_hint(_hint("strava", "72101656", None))
    telegram, telegram_reason = priority_hints.collector_target_for_hint(_hint("telegram", "9988", "SomeUser"))
    youtube, youtube_reason = priority_hints.collector_target_for_hint(_hint("youtube", "UC123", "Channel"))

    assert reason is None
    assert instagram.source == "instagram"
    assert instagram.target_id == "alice"
    assert instagram.priority == 7
    assert instagram.metadata["analyzer_priority_hint"]["source_target_id"] == "123"

    assert strava_reason is None
    assert strava.target_id == "72101656"

    assert telegram_reason is None
    assert telegram.target_id == "someuser"

    assert youtube_reason is None
    assert youtube.target_id == "UC123"


def test_build_targets_skips_bad_hints_and_dedupes_by_best_confidence():
    rows = [
        _hint("x", "1", "xuser"),
        _hint("instagram", "1", None),
        _hint("instagram", "2", "A" * 101),
        _hint("instagram", "3", "alice", confidence=96),
        _hint("instagram", "4", "alice", confidence=99, id="hint-best"),
    ]

    targets, skipped = priority_hints.build_collector_priority_targets(rows)

    assert skipped == {
        "missing_target": 1,
        "target_too_long": 1,
        "unsupported_source": 1,
    }
    assert len(targets) == 1
    assert targets[0].target_id == "alice"
    assert targets[0].metadata["analyzer_priority_hint"]["hint_id"] == "hint-best"


def test_upsert_collection_targets_raises_priority_without_resetting_status():
    pool = _FakePool()
    target = priority_hints.CollectorPriorityTarget(
        source="instagram",
        target_id="alice",
        target_name="alice",
        priority=6,
        metadata={"analyzer_priority_hint": {"hint_id": "h1"}},
    )

    written = asyncio.run(priority_hints._upsert_collection_targets(pool, [target]))

    assert written == 1
    sql, records = pool.conn.executemany_calls[0]
    assert "GREATEST(collection_targets.priority, EXCLUDED.priority)" in sql
    update_clause = sql.split("DO UPDATE SET", 1)[1]
    assert "status =" not in update_clause
    assert records[0][:4] == ("instagram", "alice", "alice", 6)


def test_refresh_collector_priority_hints_fetches_analyzer_and_writes(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(priority_hints, "_LAST_REFRESH", 0.0)
    monkeypatch.setattr(priority_hints, "analyzer_database_url", lambda: "postgres://analyzer")

    async def fake_fetch(dsn):
        assert dsn == "postgres://analyzer"
        return [_hint("instagram", "123", "alice")]

    monkeypatch.setattr(priority_hints, "_fetch_active_analyzer_hints", fake_fetch)

    result = asyncio.run(priority_hints.refresh_collector_priority_hints(pool, force=True))

    assert result["fetched"] == 1
    assert result["targets"] == 1
    assert result["written"] == 1
    assert pool.conn.executemany_calls
