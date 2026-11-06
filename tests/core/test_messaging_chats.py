import pytest

from src.core.messaging_chats import list_messaging_chats


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "whatsapp_chats"

    async def fetch(self, sql, *_args):
        assert "whatsapp_chats" in sql
        return [{"platform_chat_id": "chat@g.us", "title": "Group", "type": "group", "members_count": 2}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_list_whatsapp_chats_normalizes_dashboard_shape():
    rows = await list_messaging_chats(_Pool(), source="whatsapp", limit=30)

    assert rows == [{"platform_chat_id": "chat@g.us", "title": "Group", "type": "group", "members_count": 2}]


@pytest.mark.asyncio
async def test_list_beeper_chats_preserves_dashboard_fields_and_filter():
    class _BeeperAcquire(_Acquire):
        async def fetch(self, sql, *args):
            assert "beeper_shadow_chats" in sql
            assert "WHERE c.network = $2" in sql
            assert args == (30, "Discord")
            return [{"chat_id": "!room:beeper.local", "title": "Room", "chat_type": "group", "platform_chat_id": "!room:beeper.local"}]

    class _BeeperPool:
        def acquire(self):
            return _BeeperAcquire()

    rows = await list_messaging_chats(_BeeperPool(), source="beeper", limit=30, network="Discord")

    assert rows[0]["chat_id"] == "!room:beeper.local"
