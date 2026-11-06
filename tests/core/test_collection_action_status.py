import pytest

from src.core.collection_action_queue import list_collection_actions


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "collection_action_queue"

    async def fetch(self, sql, *args):
        assert "WHERE status=$1" in sql
        assert args == ("open", 30)
        return [{"source": "whatsapp", "action_type": "review", "priority": 1, "reason": "check"}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_action_status_is_bounded_read_only():
    payload = await list_collection_actions(_Pool(), status="open", limit=30)

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["actions"][0]["source"] == "whatsapp"
