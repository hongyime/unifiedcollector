import pytest

from src.core.messaging_messages import get_messaging_messages


class _Acquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def fetchval(self, *_args):
        return "whatsapp_chats"

    async def fetchrow(self, *_args):
        return {"platform_chat_id": "group@g.us", "name": "Group"}

    async def fetch(self, sql, *args):
        assert "FROM whatsapp_messages" in sql
        assert args == ("group@g.us", 30)
        return [{"platform_message_id": "1", "text": "hello", "sender_pushname": "Member"}]


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_whatsapp_messages_are_bounded_and_chronological():
    payload = await get_messaging_messages(_Pool(), source="whatsapp", chat_id="group@g.us", limit=30)

    assert payload["chat"]["name"] == "Group"
    assert payload["messages"][0]["text"] == "hello"


@pytest.mark.asyncio
async def test_beeper_messages_are_bounded_and_do_not_return_media_url():
    class _BeeperAcquire(_Acquire):
        async def fetchrow(self, *_args):
            return {"chat_id": "!room:beeper.local", "title": "Room"}

        async def fetch(self, sql, *args):
            if "beeper_shadow_messages" in sql:
                assert args == ("!room:beeper.local", 30)
                return [{"message_id": "m1", "text": "hello", "media_url": None}]
            assert "media_items" in sql
            assert args == (["m1"],)
            return []

    class _BeeperPool:
        def acquire(self):
            return _BeeperAcquire()

    payload = await get_messaging_messages(_BeeperPool(), source="beeper", chat_id="!room:beeper.local", limit=30)

    assert payload["chat"]["title"] == "Room"
    assert payload["messages"] == [{"message_id": "m1", "text": "hello", "media_url": None, "media_item_id": None}]
