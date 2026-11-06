import pytest

from src.core.messaging_members import list_messaging_members


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "beeper_shadow_participants"

    async def fetch(self, sql, *args):
        assert "whatsapp_group_members" in sql
        assert args == ("group@g.us", 100)
        return [{"member_id": "user@s.whatsapp.net", "pushname": "Member", "role": "admin"}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_whatsapp_member_roster_is_active_and_bounded():
    rows = await list_messaging_members(_Pool(), source="whatsapp", chat_id="group@g.us", limit=100)

    assert rows[0]["role"] == "admin"


@pytest.mark.asyncio
async def test_beeper_member_roster_is_bounded_and_uses_durable_participants():
    class _BeeperAcquire(_Acquire):
        async def fetch(self, sql, *args):
            assert "beeper_shadow_participants" in sql
            assert args == ("!room:beeper.local", 30)
            return [{"member_id": "@alice:beeper.local", "name": "Alice", "role": "member"}]

    class _BeeperPool:
        def acquire(self):
            return _BeeperAcquire()

    rows = await list_messaging_members(_BeeperPool(), source="beeper", chat_id="!room:beeper.local", limit=30)

    assert rows == [{"member_id": "@alice:beeper.local", "name": "Alice", "role": "member"}]
