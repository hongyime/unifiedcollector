"""One-off backfill: recover threads_posts.author_username from caption.

The browser-extension threads scraper sometimes dumps the whole post block into
`caption` and leaves author_username NULL. The block is "<handle>\n<relative
time>\n<real caption>", so line 1 is the author handle. We fill author_username
from line 1 ONLY when it's a clean handle (no spaces) — this excludes repost
headers like "x reposted 1h ago". Non-destructive: caption is left untouched and
only NULL author_username rows are updated, so it's safe to re-run / reverse.

The recurrence fix lives in src/bridges/ig_ingest.py::_save_posts (derives the
author at ingest time). This script just heals the existing rows.
"""
import asyncio, asyncpg, os

HANDLE_RE = r"^[A-Za-z0-9._]{2,30}$"


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=60)
    before = await c.fetchval("SELECT count(*) FROM threads_posts WHERE author_username IS NULL")
    res = await c.execute(
        "UPDATE threads_posts "
        "SET author_username = split_part(caption, E'\n', 1) "
        "WHERE author_username IS NULL AND caption IS NOT NULL "
        "AND split_part(caption, E'\n', 1) ~ $1",
        HANDLE_RE,
    )
    after = await c.fetchval("SELECT count(*) FROM threads_posts WHERE author_username IS NULL")
    print("backfill result:", res)
    print(f"null_author {before} -> {after} (filled {before - after})")
    # distinct authors recovered
    n_auth = await c.fetchval("SELECT count(DISTINCT author_username) FROM threads_posts")
    print("distinct threads authors now:", n_auth)
    await c.close()

asyncio.run(main())
