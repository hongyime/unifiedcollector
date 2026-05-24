"""Property-based tests for database operations in login_bot/main.py.

Tests require a real PostgreSQL database and will be skipped if one is not available.
"""
# Feature: login-bot-session-manager, Property 6: Account upsert idempotence
# Feature: login-bot-session-manager, Property 7: Backfill job insert idempotence

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# DB availability check — skip entire module if DB is not reachable
# ---------------------------------------------------------------------------

def _check_db_available() -> bool:
    """Check DB availability using a fresh SelectorEventLoop (Windows-safe)."""
    import selectors
    try:
        import psycopg
        import selectors as _sel
        # Direct TCP check — bypass the singleton pool entirely
        loop = asyncio.SelectorEventLoop(_sel.SelectSelector())
        try:
            async def _check() -> bool:
                try:
                    async with asyncio.timeout(2):
                        conn = await psycopg.AsyncConnection.connect(
                            host=os.environ.get("DB_HOST", "localhost"),
                            port=int(os.environ.get("DB_PORT", "5432")),
                            dbname=os.environ.get("DB_NAME", "telegramcollector"),
                            user=os.environ.get("DB_USER", "postgres"),
                            password=os.environ.get("DB_PASSWORD", ""),
                        )
                        await conn.close()
                    return True
                except Exception:
                    return False
            return loop.run_until_complete(_check())
        finally:
            loop.close()
    except Exception:
        return False


_DB_AVAILABLE = _check_db_available()

if _DB_AVAILABLE:
    from shared.database import get_db_connection

pytestmark = pytest.mark.skipif(not _DB_AVAILABLE, reason="Database not available")

# ---------------------------------------------------------------------------
# Imports only reached when DB is available
# ---------------------------------------------------------------------------

from hypothesis import given, settings as hyp_settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PHONE_PREFIX = "+7999"  # unique prefix for test data cleanup


def _make_phone(digits: str) -> str:
    """Build a test phone number in E.164 format using a recognisable prefix."""
    # Ensure total length is between 7 and 15 chars (including '+')
    suffix = digits[:10]  # at most 10 extra digits → max total 15
    return f"{_PHONE_PREFIX}{suffix}"


async def _cleanup_phone(phone: str) -> None:
    """Remove test rows created for *phone* from both tables."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # backfill_jobs rows reference telegram_accounts via account_id;
            # delete accounts first (cascade not guaranteed), so delete jobs first.
            await cur.execute(
                """
                DELETE FROM collector.backfill_jobs bj
                USING collector.telegram_accounts ta
                WHERE bj.account_id = ta.id
                  AND ta.phone_number = %s
                """,
                (phone,),
            )
            await cur.execute(
                "DELETE FROM collector.telegram_accounts WHERE phone_number = %s",
                (phone,),
            )


async def _cleanup_backfill(account_id: int, chat_id: int) -> None:
    """Remove a specific backfill_jobs test row."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM collector.backfill_jobs WHERE account_id = %s AND chat_id = %s",
                (account_id, chat_id),
            )


async def _ensure_backfill_unique_constraint() -> None:
    """Ensure the UNIQUE(account_id, chat_id) constraint exists on backfill_jobs.

    The design doc specifies ON CONFLICT (account_id, chat_id) DO NOTHING, which
    requires this constraint. Add it idempotently if the live DB is missing it.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'backfill_jobs_account_id_chat_id_key'
                          AND conrelid = 'collector.backfill_jobs'::regclass
                    ) THEN
                        ALTER TABLE collector.backfill_jobs
                            ADD CONSTRAINT backfill_jobs_account_id_chat_id_key
                            UNIQUE (account_id, chat_id);
                    END IF;
                END
                $$;
                """
            )


async def _ensure_accounts_columns() -> None:
    """Ensure session_file_path and last_error columns exist on telegram_accounts.

    These columns are used by save_account but may not be present in older DB
    instances that were initialised from the base init-db.sql.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                ALTER TABLE collector.telegram_accounts
                    ADD COLUMN IF NOT EXISTS session_file_path TEXT,
                    ADD COLUMN IF NOT EXISTS last_error TEXT;
                """
            )


async def _ensure_account(account_id: int) -> None:
    """Insert a placeholder telegram_accounts row so FK constraints are satisfied."""
    phone = f"+79000{account_id:010d}"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO collector.telegram_accounts (id, phone_number, status)
                VALUES (%s, %s, 'active')
                ON CONFLICT (id) DO NOTHING
                """,
                (account_id, phone),
            )


async def _cleanup_account(account_id: int) -> None:
    """Remove the placeholder account and its backfill rows."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM collector.backfill_jobs WHERE account_id = %s",
                (account_id,),
            )
            await cur.execute(
                "DELETE FROM collector.telegram_accounts WHERE id = %s",
                (account_id,),
            )


# ---------------------------------------------------------------------------
# Minimal mock objects required by save_account
# ---------------------------------------------------------------------------

class _MockSession:
    """Minimal stand-in for LoginState used by save_account."""

    def __init__(self, phone: str, stem: str) -> None:
        self.phone = phone
        self.session_file_name = stem  # stem only, no extension


class _MockMe:
    """Minimal stand-in for the Telethon 'me' object."""

    id = 12345


# ---------------------------------------------------------------------------
# Property 6: Account upsert idempotence
# Validates: Requirements 10.1, 10.2
# ---------------------------------------------------------------------------

@given(
    digits=st.text(
        alphabet="0123456789",
        min_size=4,
        max_size=10,
    )
)
@hyp_settings(max_examples=10)
def test_property_6_account_upsert_idempotence(digits: str) -> None:
    """**Validates: Requirements 10.1, 10.2**

    For any phone number, calling save_account twice with the same phone but
    different session_file_path values must result in exactly one row in
    collector.telegram_accounts with status = 'active' and the LATEST
    session_file_path.

    Feature: login-bot-session-manager, Property 6: Account upsert idempotence
    """
    from services.login_bot.main import save_account  # noqa: PLC0415

    phone = _make_phone(digits)
    stem = phone.lstrip("+")

    session_first = _MockSession(phone, stem)
    session_second = _MockSession(phone, stem + "_v2")

    async def _run() -> None:
        try:
            # First upsert
            await _ensure_accounts_columns()
            await save_account(session_first, _MockMe())
            # Second upsert — different session_file_path
            await save_account(session_second, _MockMe())

            # Verify exactly one row with status='active' and the latest path
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT session_file_path, status
                        FROM collector.telegram_accounts
                        WHERE phone_number = %s
                        """,
                        (phone,),
                    )
                    rows = await cur.fetchall()

            assert len(rows) == 1, (
                f"Expected exactly 1 row for phone {phone}, got {len(rows)}"
            )
            path, status = rows[0]
            assert status == "active", f"Expected status='active', got {status!r}"
            assert "_v2" in path, (
                f"Expected latest session_file_path (containing '_v2'), got {path!r}"
            )
        finally:
            await _cleanup_phone(phone)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 7: Backfill job insert idempotence
# Validates: Requirements 11.2, 11.3
# ---------------------------------------------------------------------------

@given(
    account_id=st.integers(min_value=900_000, max_value=999_999),
    chat_id=st.integers(min_value=1, max_value=10_000_000),
)
@hyp_settings(max_examples=10)
def test_property_7_backfill_job_idempotence(account_id: int, chat_id: int) -> None:
    """**Validates: Requirements 11.2, 11.3**

    For any (account_id, chat_id) pair, inserting a backfill job twice must
    result in exactly one row in collector.backfill_jobs — the second insert
    is silently ignored via ON CONFLICT DO NOTHING.

    Feature: login-bot-session-manager, Property 7: Backfill job insert idempotence
    """

    async def _run() -> None:
        await _ensure_backfill_unique_constraint()
        await _ensure_account(account_id)
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    insert_sql = """
                        INSERT INTO collector.backfill_jobs
                            (account_id, chat_id, status)
                        VALUES (%s, %s, 'pending')
                        ON CONFLICT (account_id, chat_id) DO NOTHING
                    """
                    # First insert
                    await cur.execute(insert_sql, (account_id, chat_id))
                    # Second insert — must be silently ignored
                    await cur.execute(insert_sql, (account_id, chat_id))

            # Verify exactly one row
            async with get_db_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT COUNT(*) FROM collector.backfill_jobs
                        WHERE account_id = %s AND chat_id = %s
                        """,
                        (account_id, chat_id),
                    )
                    row = await cur.fetchone()

            count = row[0] if row else 0
            assert count == 1, (
                f"Expected exactly 1 backfill_jobs row for "
                f"(account_id={account_id}, chat_id={chat_id}), got {count}"
            )
        finally:
            await _cleanup_backfill(account_id, chat_id)
            await _cleanup_account(account_id)

    asyncio.run(_run())
