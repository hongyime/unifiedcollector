import pytest

from src.core.dlq_status import list_dead_letter_queue


class _Acquire:
    def __init__(self, expected_sql_contains=None, expected_args=None, return_rows=None):
        self.expected_sql_contains = expected_sql_contains or []
        self.expected_args = expected_args
        self.return_rows = return_rows or [{"source": "telegram", "status": "pending", "retry_count": 1}]
        self.last_query = None
        self.last_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "dead_letter_queue"

    async def fetch(self, sql, *args):
        self.last_query = sql
        self.last_args = args
        for token in self.expected_sql_contains:
            assert token in sql
        if self.expected_args is not None:
            assert args == self.expected_args
        return self.return_rows


class _Pool:
    def __init__(self, acquire=None):
        self._acquire = acquire or _Acquire(
            expected_sql_contains=["SELECT id, source", "WHERE source=$1"],
            expected_args=("telegram", 30),
        )

    def acquire(self):
        return self._acquire


@pytest.mark.asyncio
async def test_dlq_status_bounds_and_filters_source():
    rows = await list_dead_letter_queue(_Pool(), source="Telegram", limit=30)

    assert rows == [{"source": "telegram", "status": "pending", "retry_count": 1}]


@pytest.mark.asyncio
async def test_dlq_status_filters_status_and_source():
    acquire = _Acquire(
        expected_sql_contains=["WHERE source=$1 AND status=$2", "LIMIT $3"],
        expected_args=("telegram", "pending", 25),
    )
    rows = await list_dead_letter_queue(_Pool(acquire), source="telegram", status="pending", limit=25)
    assert rows == [{"source": "telegram", "status": "pending", "retry_count": 1}]


@pytest.mark.asyncio
async def test_dlq_status_filters_status_only():
    acquire = _Acquire(
        expected_sql_contains=["WHERE status=$1", "LIMIT $2"],
        expected_args=("failed", 50),
    )
    rows = await list_dead_letter_queue(_Pool(acquire), status="failed", limit=50)
    assert rows == [{"source": "telegram", "status": "pending", "retry_count": 1}]


@pytest.mark.asyncio
async def test_dlq_status_rejects_invalid_status():
    with pytest.raises(ValueError, match="Invalid status"):
        await list_dead_letter_queue(_Pool(), status="not_a_real_status")

