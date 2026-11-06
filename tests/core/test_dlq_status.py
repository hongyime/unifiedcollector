import pytest

from src.core.dlq_status import list_dead_letter_queue


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "dead_letter_queue"

    async def fetch(self, sql, *args):
        assert "SELECT id, source" in sql
        assert args == ("telegram", 30)
        return [{"source": "telegram", "status": "pending", "retry_count": 1}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_dlq_status_bounds_and_filters_source():
    rows = await list_dead_letter_queue(_Pool(), source="Telegram", limit=30)

    assert rows == [{"source": "telegram", "status": "pending", "retry_count": 1}]
