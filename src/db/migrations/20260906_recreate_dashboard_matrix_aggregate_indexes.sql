-- Restores the dashboard source-matrix aggregate indexes previously introduced
-- by zz_add_dashboard_matrix_aggregate_indexes.sql (2026-08-01) whose file has
-- since been removed from the tree while its ledger row persisted. On a
-- clean-volume rebuild that combination silently produced a slower schema than
-- what production runs; this migration re-creates the physical indexes seen in
-- the live DB idempotently. See AUDIT.md DATA-001 for the ledger-drift context.
--
-- Verified against the live public.media_items and public.collection_runs
-- index catalog on the current pgvector container. All statements are
-- IDEMPOTENT (IF NOT EXISTS) so on a live DB this is a no-op, and on a fresh
-- volume it recreates the missing indexes so dashboard source-matrix queries
-- stay fast.

-- media_items: covering index for the dashboard source-matrix "count + sum
-- bytes + max(collected_at)" aggregate. INCLUDE(file_size) enables
-- index-only scans for SUM(file_size) by source.
CREATE INDEX IF NOT EXISTS idx_media_collected_source_recent_stats
    ON media_items USING btree (collected_at DESC, source)
    INCLUDE (file_size);

-- media_items: filters per-source ingest_path (headless / extension / messaging)
-- for the ingest-path summary card.
CREATE INDEX IF NOT EXISTS idx_media_ingest_path
    ON media_items USING btree (source, ingest_path)
    WHERE ingest_path IS NOT NULL;

-- collection_runs: latest-run-per-source lookup for the matrix status column.
-- idx_runs_source already exists on source alone; the composite adds the
-- ordering dimension so the planner can pick top-N per source without a sort.
CREATE INDEX IF NOT EXISTS idx_collection_runs_source_started
    ON collection_runs USING btree (source, started_at DESC);
