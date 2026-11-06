import pytest

from src.core.source_controls import is_manually_paused, list_source_controls, set_manual_pause


class _Acquire:
    def __init__(self):
        self.executed = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "source_controls"

    async def fetch(self, sql, *args):
        assert "FROM source_controls" in sql
        assert args == (30,)
        return [{"source_name": "whatsapp", "state": "running", "enabled": True,
                 "last_success": None, "last_attempt": None, "blocked_until": None,
                 "stop_reason": None, "consecutive_failures": 0, "updated_at": None}]

    async def fetchrow(self, *_args):
        return {"state": "PAUSED", "enabled": False, "stop_reason": "operator pause"}

    async def execute(self, sql, *args):
        self.executed = (sql, args)


class _Pool:
    def __init__(self):
        self.conn = _Acquire()

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_source_controls_are_bounded_and_serializable():
    rows = await list_source_controls(_Pool(), limit=30)

    assert rows == [{"source_name": "whatsapp", "state": "running", "enabled": True,
                     "last_success": None, "last_attempt": None, "blocked_until": None,
                     "stop_reason": None, "consecutive_failures": 0, "updated_at": None}]


@pytest.mark.asyncio
async def test_manual_pause_is_distinct_and_requires_explicit_resume():
    pool = _Pool()
    assert await is_manually_paused(pool, "whatsapp") == "operator pause"
    await set_manual_pause(pool, "whatsapp", paused=True)
    assert "state='PAUSED'" in pool.conn.executed[0]
    await set_manual_pause(pool, "whatsapp", paused=False)
    assert "state='HEALTHY'" in pool.conn.executed[0]
