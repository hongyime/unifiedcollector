"""Disposable acceptance test for the one-shot schema-init contract."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.db.migrate import apply_all


class _SingleConnectionPool:
    """Minimal asyncpg-pool shape for an already-owned disposable connection."""

    def __init__(self, connection) -> None:
        self._connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


@pytest.mark.asyncio
async def test_schema_init_empty_db_then_second_boot_preserves_row_bytes(pg_conn) -> None:
    """The committed runner initializes once and never replays ledgered DDL."""
    pool = _SingleConnectionPool(pg_conn)

    first = await apply_all(pool)
    assert not first["deferred"], first
    assert first["schemas"] > 0
    assert first["migrations_applied"], first

    media_id = await pg_conn.fetchval(
        """
        INSERT INTO media_items(
            source, entity_id, entity_name, content_type, content_id, filename,
            file_path, file_size, metadata, source_url
        )
        VALUES (
            'schema-init-fixture', 'fixture-entity', 'Fixture entity', 'photo',
            'schema-init-fixture-media', 'fixture.jpg', '/fixture/fixture.jpg',
            17, '{"preserved":true}'::jsonb, 'https://example.invalid/fixture'
        )
        RETURNING id
        """
    )
    before = await pg_conn.fetchval(
        "SELECT row_to_json(m)::text FROM media_items m WHERE id=$1", media_id
    )
    ledger_before = await pg_conn.fetch(
        "SELECT filename, checksum, applied_at FROM schema_migrations ORDER BY filename"
    )

    second = await apply_all(pool)
    assert not second["deferred"], second
    assert second["migrations_applied"] == [], second
    assert await pg_conn.fetchval(
        "SELECT row_to_json(m)::text FROM media_items m WHERE id=$1", media_id
    ) == before
    assert await pg_conn.fetch(
        "SELECT filename, checksum, applied_at FROM schema_migrations ORDER BY filename"
    ) == ledger_before
