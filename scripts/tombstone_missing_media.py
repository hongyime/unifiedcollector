"""Tombstone media_items whose files are gone from disk (SYNC #38).

media_items rows can point at files deleted from disk (transient search/website
scrape caches, and lemon8/beeper media lost when Z was reformatted). There is no
status column, so we stamp metadata.missing_at instead — a non-destructive,
idempotent marker consumers can filter on. The analyzer's face worker has an
equivalent skip on its own side (analyzer SYNC #36).

Graceful-offline: we only tombstone when the media ROOT is mounted. If the whole
root is absent the drive is merely offline and files may return, so we abort
rather than mass-mark real files as gone.

Run on the host (has Z: mounted):
  MEDIA_BASE=Z:/unifiedcollector DATABASE_URL=postgresql://collector:...@localhost:5500/unifiedcollector \
    python scripts/tombstone_missing_media.py
file_path is stored as "/media/<source>/..." and resolves to MEDIA_BASE + file_path.
"""
import asyncio
import os

import asyncpg

MEDIA_BASE = os.getenv("MEDIA_BASE", "Z:/unifiedcollector")
CONFINEMENT_ROOT = os.path.join(MEDIA_BASE, "media")
BATCH = 5000


def _resolve(file_path: str) -> str:
    return os.path.normpath(MEDIA_BASE + file_path.replace("\\", "/"))


async def main() -> None:
    if not os.path.isdir(CONFINEMENT_ROOT):
        raise SystemExit(
            f"media root {CONFINEMENT_ROOT!r} is not mounted — aborting so an "
            f"offline drive is not mistaken for deleted files (graceful offline)."
        )

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, file_path FROM media_items WHERE file_path IS NOT NULL"
        )
        missing = [r["id"] for r in rows if not os.path.isfile(_resolve(r["file_path"]))]
        print(f"checked={len(rows)} missing={len(missing)}")

        tagged = 0
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            tagged += await conn.fetchval(
                """
                WITH upd AS (
                    UPDATE media_items
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                                 || jsonb_build_object('missing_at', now()::text)
                    WHERE id = ANY($1::uuid[])
                      AND NOT (COALESCE(metadata, '{}'::jsonb) ? 'missing_at')
                    RETURNING 1
                )
                SELECT count(*) FROM upd
                """,
                chunk,
            )
        print(f"tagged_missing_at={tagged}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
