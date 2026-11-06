import pytest

from src.core.messaging_coverage import get_messaging_coverage


class _Acquire:
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return False
    async def fetchrow(self, *_args): return {"people": 2, "bots": 1}
    async def fetchval(self, sql):
        if "telegram_messages" in sql and "COUNT" in sql: return 3
        if "telegram_chats" in sql: return 1
        if "whatsapp_messages" in sql and "COUNT" in sql: return 4
        if "whatsapp_chats" in sql: return 2
        if "whatsapp_users" in sql: return 5
        return None
    async def fetch(self, sql):
        if "beeper_shadow_messages" in sql: return [{"network": "Discord", "messages": 6, "chats": 1, "last_message": None}]
        return []

class _Pool:
    def acquire(self): return _Acquire()

@pytest.mark.asyncio
async def test_messaging_coverage_keeps_native_and_beeper_counts_separate():
    rows = await get_messaging_coverage(_Pool())
    telegram = next(row for row in rows if row["network"] == "Telegram")
    discord = next(row for row in rows if row["network"] == "Discord")
    assert telegram["canonical_source"] == "native"
    assert telegram["native_messages"] == 3
    assert discord["canonical_source"] == "beeper"
    assert discord["beeper_messages"] == 6
