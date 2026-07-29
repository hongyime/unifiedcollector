"""Clean-volume schema reproducibility test (P0-3).

Proves that the migration runner (src/db/migrate.apply_all) builds a COMPLETE
database from scratch — every table that the live production DB has must be
creatable on an empty database via the committed DDL alone.

This is the regression guard for the P0 schema-drift class of bug: 19 live,
code-referenced tables that existed only under db/migrations/ and were never
applied at boot, so a clean `docker compose up` produced a half-built DB.

Usage (inside the postgres container, against a THROWAWAY database):
    python /tmp/verify_clean_boot.py postgresql://user:pass@localhost/throwaway_db

Exit 0 = all expected tables present. Exit 1 = missing tables (DDL incomplete).
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

# The full set of tables a correct clean boot must produce. Derived from the
# live production DB table list (pg_tables, public schema) as of 2026-05-30.
# Add to this set whenever a new collector/table is introduced — CI will then
# fail until the DDL to create it is committed.
EXPECTED_TABLES = {
    # core
    "schema_migrations", "media_items", "service_cursors", "collection_targets",
    "collection_schedules", "collection_runs", "dead_letter_queue", "account_state",
    "dashboard_users", "profile_access_attempts", "profile_access_summary",
    "search_queries", "search_results", "account_quota_usage", "spider_queue",
    # telegram (4 base + 7 migration)
    "telegram_chats", "telegram_messages", "telegram_users", "telegram_spider_queue",
    "telegram_chat_members", "telegram_reactions", "telegram_reaction_counts",
    "telegram_polls", "telegram_discussion_visits", "telegram_user_accounts",
    "telegram_user_changes",
    # instagram
    "instagram_profiles", "instagram_posts", "instagram_comments",
    "instagram_spider_queue", "instagram_tls_state", "instagram_user_changes",
    # tiktok
    "tiktok_profiles", "tiktok_posts", "tiktok_comments", "tiktok_spider_queue",
    "tiktok_download_tracker",
    # youtube
    "youtube_channels", "youtube_videos", "youtube_comments", "youtube_transcripts",
    "youtube_spider_queue",
    # github
    "github_users", "github_repos", "github_commits", "github_issues",
    "github_readmes", "github_spider_queue", "github_issue_comments",
    "github_pr_reviews", "github_pr_review_comments", "github_edges",
    # strava
    "strava_athletes", "strava_activities", "strava_segments", "strava_gps_streams",
    "strava_day_coverage", "strava_spider_queue",
    # lemon8
    "lemon8_profiles", "lemon8_posts", "lemon8_discovered", "lemon8_spider_queue",
    # cross-platform
    "graph_edges", "source_health",
    # website / whatsapp / beeper / matrix
    "website_targets", "website_pages",
    "whatsapp_chats", "whatsapp_messages", "whatsapp_users",
    "wa_discovered_links",
    "beeper_shadow_accounts", "beeper_shadow_chats", "beeper_shadow_messages",
    "beeper_shadow_participants", "beeper_shadow_sync_state",
    "matrix_events", "matrix_sync_state", "matrix_backfill_state",
}


async def main(dsn: str) -> int:
    # Import the runner from the app source baked into the image.
    sys.path.insert(0, "/app")
    from src.db.migrate import apply_all

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        summary = await apply_all(pool)
        print(f"runner summary: {summary}")
        rows = await pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )
        present = {r["tablename"] for r in rows}
    finally:
        await pool.close()

    missing = EXPECTED_TABLES - present
    extra = present - EXPECTED_TABLES  # informational only

    print(f"tables present: {len(present)}  expected: {len(EXPECTED_TABLES)}")
    if extra:
        print(f"NOTE extra tables not in EXPECTED set (ok if new): {sorted(extra)}")
    if missing:
        print(f"FAIL missing {len(missing)} expected table(s): {sorted(missing)}")
        return 1
    print("PASS clean boot produced all expected tables")
    return 0


if __name__ == "__main__":
    dsn = sys.argv[1] if len(sys.argv) > 1 else \
        "postgresql://collector:collector@localhost:5432/_cleanboot_test"
    raise SystemExit(asyncio.run(main(dsn)))
