"""Integration tests for DatabaseManager — schema, CRUD, and new save_cycle()"""
import pytest
import os
from db_manager import DatabaseManager


@pytest.fixture
def db(tmp_path):
    return DatabaseManager(str(tmp_path / "test.db"), str(tmp_path))


# Schema

def test_init_creates_all_tables(db):
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    expected = {"file_hashes", "settings", "websites_config", "websites", "links", "cycles"}
    assert expected.issubset(tables)


# Hash operations

def test_add_and_has_hash(db):
    db.add_hash("abc123", "sha256", "2026-01-01T00:00:00")
    assert db.has_hash("abc123")


def test_has_hash_returns_false_for_missing(db):
    assert not db.has_hash("nonexistent")


def test_add_hash_does_not_call_update_backup(db, monkeypatch):
    called = []
    monkeypatch.setattr(db, "update_backup", lambda: called.append(1))
    db.add_hash("xyz", "sha256", "2026-01-01")
    assert called == [], "update_backup must not be called per-hash"


def test_get_all_hashes(db):
    db.add_hash("h1", "sha256", "2026-01-01")
    db.add_hash("h2", "md5", "2026-01-02")
    assert set(db.get_all_hashes()) == {"h1", "h2"}
    assert db.get_all_hashes("sha256") == ["h1"]


# Settings

def test_save_and_get_settings(db):
    db.save_settings({"max_depth": 5, "timeout": 30})
    settings = db.get_settings()
    assert settings["max_depth"] == 5
    assert settings["timeout"] == 30


# Websites

def test_save_and_get_websites(db):
    sites = [{"name": "example", "url": "https://example.com", "enabled": True}]
    db.save_websites(sites)
    result = db.get_websites()
    assert len(result) == 1
    assert result[0]["name"] == "example"


def test_save_websites_replaces_existing(db):
    db.save_websites([{"name": "old", "url": "https://old.com", "enabled": True}])
    db.save_websites([{"name": "new", "url": "https://new.com", "enabled": True}])
    result = db.get_websites()
    assert len(result) == 1
    assert result[0]["name"] == "new"


# Cycle persistence (T-09)

def test_save_cycle_persists_row(db):
    db.save_cycle(
        cycle_id="20260101_120000",
        start_time="2026-01-01T12:00:00",
        end_time="2026-01-01T13:00:00",
        websites_processed=10,
        links_discovered=500,
        photos_downloaded=42,
        new_websites_added=3,
        status="completed",
    )
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute("SELECT * FROM cycles WHERE cycle_id = ?", ("20260101_120000",)).fetchone()
    assert row is not None
    assert row[8] == "completed"  # status column


def test_save_cycle_upserts_on_duplicate_id(db):
    kwargs = dict(
        cycle_id="dupe",
        start_time="2026-01-01T00:00:00",
        end_time="2026-01-01T01:00:00",
        websites_processed=1,
        links_discovered=10,
        photos_downloaded=0,
        new_websites_added=0,
    )
    db.save_cycle(**kwargs, status="interrupted")
    db.save_cycle(**kwargs, status="completed")
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        rows = conn.execute("SELECT status FROM cycles WHERE cycle_id = 'dupe'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "completed"


# Metrics reflect cycle data

def test_get_system_metrics_counts_photos_from_cycles(db):
    db.save_cycle("c1", "2026-01-01T00:00:00", "2026-01-01T01:00:00", 5, 100, 30, 1, "completed")
    db.save_cycle("c2", "2026-01-02T00:00:00", "2026-01-02T01:00:00", 5, 200, 15, 0, "completed")
    metrics = db.get_system_metrics()
    assert metrics["total_photos_downloaded"] == 45
