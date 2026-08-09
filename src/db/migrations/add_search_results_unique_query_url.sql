-- Search results are source evidence, but the same query+URL can be seen on
-- every crawl. Keep one canonical occurrence per query URL so repeated runs are
-- idempotent and do not inflate progress/row counts.
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY query_id, url
            ORDER BY collected_at ASC NULLS LAST, id ASC
        ) AS rn
    FROM search_results
)
DELETE FROM search_results sr
USING ranked r
WHERE sr.id = r.id
  AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_search_results_query_url_unique
    ON search_results(query_id, url);
