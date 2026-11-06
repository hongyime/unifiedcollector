import pytest

from src.core.profile_status import list_profiles


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "lemon8_profiles"

    async def fetch(self, sql, *args):
        assert "lemon8_profiles" in sql
        assert args == (30,)
        return [{"username": "creator", "followers_count": 4, "posts_collected": 2}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_list_lemon8_profiles_uses_dashboard_query_shape():
    rows = await list_profiles(_Pool(), source="lemon8", limit=30)

    assert rows == [{"username": "creator", "followers_count": 4, "posts_collected": 2}]
