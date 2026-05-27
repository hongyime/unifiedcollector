"""Idempotent migration: add content_hashes table for unified dedup.

Run with:
    python -m src.migrations.add_content_hashes_table

Designed to be safe to re-run. Creates the table only if absent and
adds indexes only if absent.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)

DDL_TABLE = """
CREATE TABLE IF NOT EXISTS content_hashes (
    id            BIGSERIAL PRIMARY KEY,
    hash_kind     VARCHAR(16) NOT NULL,
    hash_value    VARCHAR(64) NOT NULL,
    source_table  VARCHAR(64) NOT NULL,
    source_id     UUID NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT content_hashes_kind_value_table_uniq
        UNIQUE (hash_kind, hash_value, source_table)
)
"""

DDL_INDEX_LOOKUP = """
CREATE INDEX IF NOT EXISTS idx_content_hashes_kind_value
  ON content_hashes (hash_kind, hash_value)
"""

DDL_INDEX_REVERSE = """
CREATE INDEX IF NOT EXISTS idx_content_hashes_source
  ON content_hashes (source_table, source_id)
"""


async def apply() -> None:
    from src.db.connection import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(DDL_TABLE)
            await conn.execute(DDL_INDEX_LOOKUP)
            await conn.execute(DDL_INDEX_REVERSE)
    logger.info("content_hashes table + indexes ready")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(apply())
    except Exception:
        logger.exception("migration failed")
        return 1
    print("OK: content_hashes migration applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
