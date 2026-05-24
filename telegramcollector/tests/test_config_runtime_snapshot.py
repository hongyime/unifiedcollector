"""Tests for DB-snapshot runtime hydration in shared/config.py."""

import os
import types

import shared.config as config_module


class _FakeCursor:
    def __init__(self, rows=None, table_exists=True):
        self.rows = rows or []
        self.table_exists = table_exists
        self._last_query = ""

    def execute(self, query, params=None):
        self._last_query = query

    def fetchone(self):
        if "to_regclass" in self._last_query:
            return ("collector.config_settings",) if self.table_exists else (None,)
        return None

    def fetchall(self):
        if "FROM collector.config_settings" in self._last_query:
            return self.rows
        return []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_load_db_config_snapshot_excludes_bootstrap_keys(monkeypatch):
    rows = [
        ("FACE_BATCH_SIZE", "42"),
        ("BOT_TOKEN", "123:secret"),
        ("DB_HOST", "db-from-store"),
    ]

    fake_cursor = _FakeCursor(rows=rows, table_exists=True)
    fake_psycopg = types.SimpleNamespace(connect=lambda **kwargs: _FakeConn(fake_cursor))

    monkeypatch.setattr(config_module, "_psycopg", fake_psycopg)
    monkeypatch.setenv("DB_PASSWORD", "bootstrap-password")

    snapshot = config_module.load_db_config_snapshot()

    assert snapshot["FACE_BATCH_SIZE"] == "42"
    assert snapshot["BOT_TOKEN"] == "123:secret"
    assert "DB_HOST" not in snapshot


def test_load_db_config_snapshot_returns_empty_when_table_missing(monkeypatch):
    fake_cursor = _FakeCursor(rows=[], table_exists=False)
    fake_psycopg = types.SimpleNamespace(connect=lambda **kwargs: _FakeConn(fake_cursor))

    monkeypatch.setattr(config_module, "_psycopg", fake_psycopg)
    monkeypatch.setenv("DB_PASSWORD", "bootstrap-password")

    assert config_module.load_db_config_snapshot() == {}


def test_apply_db_config_snapshot_sets_environment(monkeypatch):
    monkeypatch.setattr(
        config_module,
        "load_db_config_snapshot",
        lambda: {"FACE_BATCH_SIZE": "9", "RUN_MODE": "realtime"},
    )

    monkeypatch.delenv("FACE_BATCH_SIZE", raising=False)
    monkeypatch.delenv("RUN_MODE", raising=False)

    count = config_module.apply_db_config_snapshot()

    assert count == 2
    assert os.environ["FACE_BATCH_SIZE"] == "9"
    assert os.environ["RUN_MODE"] == "realtime"
