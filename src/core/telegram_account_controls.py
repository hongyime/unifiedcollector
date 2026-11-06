"""Reversible Telegram account state controls shared by dashboard and bot."""
from __future__ import annotations


async def set_telegram_account_enabled(pool, name: str, *, enabled: bool) -> bool:
    """Enable or disable a durable account record; never purge its session."""
    async with pool.acquire() as conn:
        if enabled:
            result = await conn.execute(
                "UPDATE telegram_user_accounts SET status='active' "
                "WHERE name=$1 AND status='disabled'", name,
            )
            if result == "UPDATE 0":
                return False
            await conn.execute("SELECT pg_notify('telegram_account_added', $1)", name)
            return True
        result = await conn.execute(
            "UPDATE telegram_user_accounts SET status='disabled' WHERE name=$1", name,
        )
    return result != "UPDATE 0"
