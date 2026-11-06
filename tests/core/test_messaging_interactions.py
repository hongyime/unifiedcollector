import pytest

from src.core.messaging_interactions import get_messaging_interactions


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetch(self, sql, *_args):
        if "whatsapp_reactions" in sql:
            return [{"message_id": "message", "actor": "actor", "emoji": "👍", "removed": False}]
        return [{"message_id": "poll", "question": "Lunch?", "total_voters": None, "is_closed": False}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_whatsapp_interactions_return_canonical_rows():
    payload = await get_messaging_interactions(_Pool(), source="whatsapp")

    assert payload["reactions"][0]["emoji"] == "👍"
    assert payload["polls"][0]["question"] == "Lunch?"
