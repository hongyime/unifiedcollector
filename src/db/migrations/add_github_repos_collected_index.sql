CREATE INDEX IF NOT EXISTS idx_github_repos_collected
    ON github_repos (collected_at DESC);
