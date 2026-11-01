-- 2026-10-29 (security review): one github_readmes row per (repo_id, sha).
-- The collector used to plain-INSERT a README every cycle, so an unchanged
-- README accumulated one duplicate row per collection. Dedupe first (keep the
-- newest collected_at; tie-break on id), then enforce it with a unique index
-- that _upsert_readme's ON CONFLICT (repo_id, sha) targets.
-- Both statements are re-runnable: the DELETE is a no-op once deduped and the
-- index uses IF NOT EXISTS.

DELETE FROM github_readmes
WHERE id IN (
    SELECT id
    FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY repo_id, sha
                   ORDER BY collected_at DESC NULLS LAST, id DESC
               ) AS rn
        FROM github_readmes
    ) ranked
    WHERE ranked.rn > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS github_readmes_repo_sha_uniq
    ON github_readmes (repo_id, sha);
