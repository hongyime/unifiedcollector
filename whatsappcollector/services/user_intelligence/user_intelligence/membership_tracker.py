from __future__ import annotations

import asyncpg

from .database import database


class MembershipTracker:
    async def record_membership(self, user_jid: str, chat_jid: str, conn: asyncpg.Connection) -> bool:
        return await database.upsert_membership(user_jid, chat_jid, conn)


membership_tracker = MembershipTracker()
