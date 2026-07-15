"""Backfill instagram_posts.profile_id for the ~19,317 posts left NULL by the
collector (recurring NULL-FK bug — see analyzer VISION_PLAN gap-round-2 +
memory "NULL FK hides data").

Background
----------
`instagram_posts.platform_post_id` has the shape `<media_id>_<author_user_id>`.
That trailing id equals `instagram_profiles.platform_user_id`. When the collector
inserts a post without resolving/setting `profile_id`, every consumer that
attributes a post via `instagram_posts.profile_id -> instagram_profiles.id`
(the analyzer timeline builder's IG block AND the /geo endpoint) silently drops
it. GAP-4 (ig_geo_resolver) repaired only the 4,202 geo posts; ~19,317 non-geo
posts are still NULL and therefore invisible in IG timelines.

This backfill recovers the author uid from platform_post_id, maps it to the
instagram_profiles row, and UPDATEs profile_id. Only rows whose uid maps to an
existing profile are repaired; posts with no embedded uid (bare media id) or an
author we never collected a profile for are left NULL (genuinely unrecoverable).

Safety
------
* Idempotent — only touches rows still `profile_id IS NULL`; re-running is a
  no-op once repaired.
* Batched with `SET lock_timeout` + retry, because instagram_posts is a
  live-written table (the IG collector inserts concurrently). Each batch is a
  short, id-bounded UPDATE so it never holds a long lock.
* Read-only on instagram_profiles.

Run inside the collector container (any collector shares the same DB):
  docker exec unifiedcollector_collector_instagram \
    python /app/scripts/backfill_instagram_post_profile_id.py
"""
import asyncio
import os

import asyncpg

BATCH_SIZE = 2000
LOCK_TIMEOUT = "5s"
MAX_RETRIES = 5


# One batch: repair up to BATCH_SIZE NULL rows whose embedded uid maps to a
# profile. Bounded by ctid so each statement is short. Returns rows updated.
_BATCH_SQL = """
    WITH cand AS (
        SELECT p.ctid, ipf.id AS profile_id
        FROM instagram_posts p
        JOIN instagram_profiles ipf
          ON ipf.platform_user_id = split_part(p.platform_post_id, '_', 2)
        WHERE p.profile_id IS NULL
          AND split_part(p.platform_post_id, '_', 2) ~ '^[0-9]+$'
        LIMIT $1
    )
    UPDATE instagram_posts p
    SET profile_id = cand.profile_id
    FROM cand
    WHERE p.ctid = cand.ctid
"""


async def _run_batch(pool) -> int:
    """Run one batch with lock_timeout + retry. Returns rows updated."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                    res = await conn.execute(_BATCH_SQL, BATCH_SIZE)
            # res like "UPDATE 2000"
            try:
                return int(res.split()[-1])
            except (ValueError, IndexError):
                return 0
        except asyncpg.exceptions.LockNotAvailableError:
            wait = attempt * 2
            print(f"  lock busy, retry {attempt}/{MAX_RETRIES} in {wait}s")
            await asyncio.sleep(wait)
    print("  batch gave up after retries (will resume on next run)")
    return 0


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        before = await pool.fetchval(
            "SELECT count(*) FROM instagram_posts WHERE profile_id IS NULL"
        )
        print(f"NULL profile_id before: {before}")

        total = 0
        while True:
            updated = await _run_batch(pool)
            total += updated
            if updated:
                print(f"  repaired batch: {updated} (cumulative {total})")
            if updated < BATCH_SIZE:
                # last partial batch (or a lock give-up returning 0) — done for now
                break

        after = await pool.fetchval(
            "SELECT count(*) FROM instagram_posts WHERE profile_id IS NULL"
        )
        # Break down what remains, so an operator knows it's unrecoverable, not a bug.
        remaining = await pool.fetchrow("""
            SELECT
              count(*) FILTER (WHERE split_part(platform_post_id,'_',2) !~ '^[0-9]+$')
                AS no_embedded_uid,
              count(*) FILTER (
                WHERE split_part(platform_post_id,'_',2) ~ '^[0-9]+$'
                  AND NOT EXISTS (
                    SELECT 1 FROM instagram_profiles i
                    WHERE i.platform_user_id = split_part(instagram_posts.platform_post_id,'_',2)
                  )) AS uid_without_profile_row
            FROM instagram_posts WHERE profile_id IS NULL
        """)
        print(f"repaired this run: {total}")
        print(f"NULL profile_id after: {after}")
        print(f"  remaining unrecoverable — no embedded uid: {remaining['no_embedded_uid']}")
        print(f"  remaining unrecoverable — uid without profile row: {remaining['uid_without_profile_row']}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
