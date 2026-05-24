"""Unit tests for shared/config_store.py helper behavior."""

import time

from shared.config_store import ConfigStore, hash_value, mask_value


def test_mask_value_is_deterministic():
    assert mask_value("abcdef") == "a****f"
    assert mask_value("abcdefgh") == "ab****gh"
    assert mask_value("a") == "*"
    assert mask_value("") == ""
    assert mask_value(None) is None


def test_hash_value_is_stable_and_unique():
    h1 = hash_value("same")
    h2 = hash_value("same")
    h3 = hash_value("different")

    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64
    assert hash_value(None) is None


def test_get_setting_applies_cooldown_on_failure(monkeypatch):
    store = ConfigStore(retry_cooldown_seconds=120)
    calls = {"count": 0}

    def _boom():
        calls["count"] += 1
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store, "_connect", _boom)

    assert store.get_setting("X") is None
    first_next_retry = store._next_retry_ts
    assert first_next_retry > time.time()

    # Second call should be skipped by cooldown, not reconnecting.
    assert store.get_setting("X") is None
    assert calls["count"] == 1


def test_persist_setting_returns_failure_without_raise(monkeypatch):
    store = ConfigStore(retry_cooldown_seconds=1)

    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(store, "_connect", _boom)

    result = store.persist_setting(
        key="BOT_TOKEN",
        value="secret",
        group="platform",
        sensitive=True,
        changed_by="dashboard",
        source="dashboard",
        live_applied=False,
        restart_required=True,
        owners=("collector",),
    )

    assert not result.persisted
    assert result.error


class _FakeCursor:
    def __init__(self, table_exists=True, revision_count=0):
        self.table_exists = table_exists
        self.revision_count = revision_count
        self._last_query = ""

    def execute(self, query, params=None):
        self._last_query = query

    def fetchone(self):
        if "to_regclass" in self._last_query:
            return {"rel": "collector.config_revisions" if self.table_exists else None}
        if "SELECT COUNT(*) AS revision_count" in self._last_query:
            return {"revision_count": self.revision_count}
        return None

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


def test_revision_count_and_first_run_when_empty(monkeypatch):
    store = ConfigStore(retry_cooldown_seconds=1)
    fake_cursor = _FakeCursor(table_exists=True, revision_count=0)
    monkeypatch.setattr(store, "_connect", lambda: _FakeConn(fake_cursor))

    assert store.get_revision_count() == 0
    assert store.is_first_run() is True


def test_revision_count_and_first_run_when_existing(monkeypatch):
    store = ConfigStore(retry_cooldown_seconds=1)
    fake_cursor = _FakeCursor(table_exists=True, revision_count=3)
    monkeypatch.setattr(store, "_connect", lambda: _FakeConn(fake_cursor))

    assert store.get_revision_count() == 3
    assert store.is_first_run() is False


def test_first_run_when_revision_table_missing(monkeypatch):
    store = ConfigStore(retry_cooldown_seconds=1)
    fake_cursor = _FakeCursor(table_exists=False, revision_count=0)
    monkeypatch.setattr(store, "_connect", lambda: _FakeConn(fake_cursor))

    assert store.get_revision_count() == 0
    assert store.is_first_run() is True
