-- 2026-07-03 (P2 review §3): DB-enforced content dedup for the scraped sources.
-- The old dedup was a racy app-level SELECT-then-INSERT (two concurrent tasks
-- could both miss and both store the same bytes under different content_ids —
-- 8659 such redundant rows had accumulated). This partial UNIQUE index makes it
-- atomic: base_collector now does INSERT ... ON CONFLICT DO NOTHING, which the DB
-- rejects on a duplicate (source, sha256).
--
-- PARTIAL / scoped on purpose:
--   * sha256 IS NOT NULL — rows without a hash are not deduped.
--   * source IN (...) — ONLY the OSINT/social scrapers. Messaging sources
--     (telegram/whatsapp/beeper) are EXCLUDED: the same media legitimately
--     appears in multiple chats there and must NOT be collapsed.
--
-- On the LIVE db this was applied out-of-band with CREATE UNIQUE INDEX
-- CONCURRENTLY (after deleting the 8659 pre-existing dups, snapshotted to
-- media_items_sha256_dup_backup) so it did not lock the hot table. This file uses
-- a plain CREATE for clean-rebuild parity; IF NOT EXISTS makes it a no-op where the
-- index already exists. (migrate.py runs migrations in a txn, where CONCURRENTLY is
-- illegal — a fresh/empty rebuild has no dups, so the plain build is safe there.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_source_sha256
    ON media_items(source, sha256)
    WHERE sha256 IS NOT NULL
      AND source IN ('instagram','tiktok','lemon8','threads','facebook','x','search','website','github','strava');
