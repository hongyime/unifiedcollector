from __future__ import annotations

import asyncpg

from .database import database


class NetworkBuilder:
    async def update_for_new_membership(self, user_jid: str, chat_jid: str, conn: asyncpg.Connection) -> int:
        updated = 0
        others = await database.list_other_chat_members(chat_jid, user_jid, conn)
        for other in others:
            await database.upsert_connection(user_jid, other, conn)
            updated += 1
        return updated


network_builder = NetworkBuilder()
