import pytest

from src.core.whatsapp_users import get_whatsapp_user_history, list_whatsapp_users


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetch(self, sql, *args):
        self.sql, self.args = sql, args
        return [{"platform_user_id": "person@s.whatsapp.net", "text": "hello"}]


class _Pool:
    def __init__(self):
        self.conn = _Acquire()

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_whatsapp_users_escapes_search_wildcards_and_bounds_limit():
    pool = _Pool()

    rows = await list_whatsapp_users(pool, search="a_%", limit=999)

    assert rows[0]["platform_user_id"] == "person@s.whatsapp.net"
    assert "ILIKE" in pool.conn.sql
    assert pool.conn.args == ("%a\\_\\%%", 500)


@pytest.mark.asyncio
async def test_whatsapp_user_history_is_bounded():
    pool = _Pool()

    rows = await get_whatsapp_user_history(pool, jid="person@s.whatsapp.net", limit=9999)

    assert rows[0]["text"] == "hello"
    assert "JOIN whatsapp_users" in pool.conn.sql
    assert pool.conn.args == ("person@s.whatsapp.net", 1000)
