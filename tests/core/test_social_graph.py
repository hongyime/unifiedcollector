import pytest

from src.core.social_graph import get_social_graph


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return False

    async def fetch(self, sql, *_args):
        if "graph_edges" in sql:
            return []
        return [{"source_user": "owner", "target_user": "friend", "edge_type": "following"}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_social_graph_falls_back_to_follow_edges():
    payload = await get_social_graph(_Pool(), source="instagram", limit=20)

    assert payload["nodes"] == [{"id": "friend"}, {"id": "owner"}]
    assert payload["edges"][0]["edge_type"] == "following"
