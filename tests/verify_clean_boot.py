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
    "youtube_spider_queue", "youtube_community_posts", "youtube_edges",
    "youtube_profile_queue",
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
        assert not await pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public')"
        ), "Clean-boot verification requires an empty fixture database"
        summary = await apply_all(pool)
        assert not summary["deferred"], "Fresh schema application was deferred"
        print(f"runner summary: {summary}")
        rows = await pool.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )
        present = {r["tablename"] for r in rows}
        expected_indexes = {
            "idx_facebook_profiles_updated_at", "idx_x_profiles_updated_at",
            "idx_beeper_shadow_attachment_chat_ts", "idx_beeper_messages_network_message",
            "idx_beeper_shadow_ingested_network", "idx_browser_ingest_content_platform_created",
            "idx_threads_posts_collected", "idx_facebook_posts_collected", "idx_x_posts_collected",
            "idx_media_ig_tagged_entity_owner_media", "idx_media_ingest_path",
        }
        indexes = {r["indexname"] for r in await pool.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
        )}
        assert not expected_indexes - indexes, expected_indexes - indexes
        # A table-only check misses a trigger migration applied in the wrong
        # order, and replay can appear healthy while destroying existing rows.
        trigger_columns_sql = """
            SELECT array_agg(a.attname ORDER BY a.attname)
            FROM pg_trigger t
            CROSS JOIN LATERAL unnest(t.tgattr::smallint[]) n(attnum)
            JOIN pg_attribute a ON a.attrelid=t.tgrelid AND a.attnum=n.attnum
            WHERE t.tgrelid='public.media_items'::regclass AND t.tgname='trg_media_source_rollups'
        """
        trigger_columns = await pool.fetchval(trigger_columns_sql)
        assert trigger_columns == ["collected_at", "file_size", "source"], trigger_columns
        ledger_before = await pool.fetch("SELECT filename,checksum,applied_at FROM schema_migrations ORDER BY filename")
        sample_id = await pool.fetchval("""
            INSERT INTO media_items(source,entity_id,entity_name,content_type,content_id,filename,file_path,file_size,metadata,source_url)
            VALUES ('fixture','fixture','Synthetic fixture','photo','clean-boot-fixture','fixture.jpg','/fixture/fixture.jpg',17,'{"preserved":true}'::jsonb,'https://example.invalid/fixture')
            RETURNING id
        """)
        sample_before = await pool.fetchval("SELECT row_to_json(m)::text FROM media_items m WHERE id=$1",sample_id)
        lemon8_id = await pool.fetchval("""
            INSERT INTO lemon8_posts(platform_post_id,username,post_url,metadata)
            VALUES ('fixture','fixture_owner','https://example.invalid/post','{"preserved":true}'::jsonb)
            RETURNING id
        """)
        lemon8_before = await pool.fetchval("SELECT row_to_json(p)::text FROM lemon8_posts p WHERE id=$1", lemon8_id)
        replay = await apply_all(pool)
        assert not replay["deferred"] and not replay["migrations_applied"], replay
        assert await pool.fetchval(trigger_columns_sql) == trigger_columns
        assert await pool.fetch("SELECT filename,checksum,applied_at FROM schema_migrations ORDER BY filename") == ledger_before
        assert await pool.fetchval("SELECT row_to_json(m)::text FROM media_items m WHERE id=$1",sample_id) == sample_before
        assert await pool.fetchval("SELECT row_to_json(p)::text FROM lemon8_posts p WHERE id=$1", lemon8_id) == lemon8_before
        assert await pool.fetchval("SELECT total_media_bytes FROM media_source_rollups WHERE source='fixture'") == 17
        await pool.execute("UPDATE media_items SET file_size=23 WHERE id=$1",sample_id)
        assert await pool.fetchval("SELECT total_media_bytes FROM media_source_rollups WHERE source='fixture'") == 23
        print("PASS second boot preserves row bytes and migration ledger; narrowed rollup trigger remains functional")
        # A deployment may contain both the new composite key and an old
        # SHA-only key, or just the old key. Neither upgrade may drop rows.
        github_id = await pool.fetchval("INSERT INTO github_commits(sha,message) VALUES ('fixture-sha','Synthetic fixture') RETURNING id")
        github_before = await pool.fetchval("SELECT row_to_json(c)::text FROM github_commits c WHERE id=$1", github_id)
        constraint_oid = await pool.fetchval("SELECT conindid FROM pg_constraint WHERE conname='unique_commit_repo_github'")
        for old_only in [False, True]:
            if old_only:
                await pool.execute("ALTER TABLE github_commits DROP CONSTRAINT unique_commit_repo_github")
            await pool.execute("ALTER TABLE github_commits ADD CONSTRAINT github_commits_sha_key UNIQUE(sha)")
            await pool.execute("DELETE FROM schema_migrations WHERE filename='fix_github_commits_unique_constraint.sql'")
            upgraded = await apply_all(pool)
            assert upgraded["migrations_applied"] == ["fix_github_commits_unique_constraint.sql"], upgraded
            assert await pool.fetchval("SELECT row_to_json(c)::text FROM github_commits c WHERE id=$1", github_id) == github_before
            assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='github_commits_sha_key')")
            if not old_only:
                assert await pool.fetchval("SELECT conindid FROM pg_constraint WHERE conname='unique_commit_repo_github'") == constraint_oid
        print("PASS legacy and mixed GitHub constraints upgrade without changing rows; existing composite index is preserved")
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
