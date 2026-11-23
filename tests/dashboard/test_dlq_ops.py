from fastapi.testclient import TestClient

from src.dashboard.api import app
from src.dashboard.api import auth as dashboard_auth


class _FakeConn:
    def __init__(self, execute_result="UPDATE 1"):
        self.execute_result = execute_result
        self.execute_calls = []

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return self.execute_result


class _FakePool:
    def __init__(self, execute_result="UPDATE 1"):
        self.conn = _FakeConn(execute_result)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


def test_dlq_retry_item_endpoint(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_AUTH_DISABLED", True)
    pool = _FakePool("UPDATE 1")
    async def _fake_get_pool():
        return pool
    monkeypatch.setattr("src.dashboard.api.ops._get_pool", _fake_get_pool)

    client = TestClient(app)
    resp = client.post("/dlq/42/retry")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 42, "status": "pending"}
    assert len(pool.conn.execute_calls) == 1
    query, args = pool.conn.execute_calls[0]
    assert "status = 'pending'" in query
    assert args == (42,)


def test_dlq_retry_item_not_found(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_AUTH_DISABLED", True)
    pool = _FakePool("UPDATE 0")
    async def _fake_get_pool():
        return pool
    monkeypatch.setattr("src.dashboard.api.ops._get_pool", _fake_get_pool)

    client = TestClient(app)
    resp = client.post("/dlq/999/retry")

    assert resp.status_code == 404


def test_dlq_redrive_pending_endpoint(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_AUTH_DISABLED", True)
    pool = _FakePool("UPDATE 5")
    async def _fake_get_pool():
        return pool
    monkeypatch.setattr("src.dashboard.api.ops._get_pool", _fake_get_pool)

    client = TestClient(app)
    resp = client.post("/dlq/redrive-pending?source=telegram")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "redriven": 5}
    query, args = pool.conn.execute_calls[0]
    assert "status = 'pending' AND source = $1" in query
    assert args == ("telegram",)


def test_dlq_archive_failed_endpoint(monkeypatch):
    monkeypatch.setattr(dashboard_auth, "_AUTH_DISABLED", True)
    pool = _FakePool("UPDATE 6664")
    async def _fake_get_pool():
        return pool
    monkeypatch.setattr("src.dashboard.api.ops._get_pool", _fake_get_pool)

    client = TestClient(app)
    resp = client.post("/dlq/archive-failed")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "action": "archived", "count": 6664}
    query, args = pool.conn.execute_calls[0]
    assert "SET status = 'archived' WHERE status = 'failed'" in query
