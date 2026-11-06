import pytest

from src.core.ephemeral_media_status import get_ephemeral_media_status


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchrow(self, *_args):
        return {"stories": 2, "highlights": 1, "accounts": 1, "sources": 1}

    async def fetch(self, _sql, limit):
        assert limit == 30
        return [{"source": "instagram", "entity_name": "creator", "story_count": 2, "highlight_count": 1}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_ephemeral_media_status_is_bounded_and_aggregate_only():
    payload = await get_ephemeral_media_status(_Pool(), limit=30)

    assert payload["stats"]["stories"] == 2
    assert payload["accounts"][0]["entity_name"] == "creator"
