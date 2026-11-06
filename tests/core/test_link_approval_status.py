import pytest

from src.core.link_approval_status import list_link_approvals


class _Conn:
    async def fetchval(self, *_args):
        return "link_approvals"

    async def fetch(self, sql, *args):
        assert "last_error" in sql
        assert "completed_at" in sql
        assert args == ("telegram", 500)
        return [{"source": "telegram", "state": "collection_active"}]


class _Acquire:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_link_approval_status_is_bounded_and_source_scoped():
    rows = await list_link_approvals(_Pool(), source="Telegram", limit=999)

    assert rows == [{"source": "telegram", "state": "collection_active"}]
