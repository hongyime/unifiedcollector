CREATE TABLE IF NOT EXISTS github_repos (
    repo_id VARCHAR(100) PRIMARY KEY,
    full_name VARCHAR(500) NOT NULL,
    description TEXT,
    star_count INT,
    fork_count INT,
    last_scraped TIMESTAMP,
    spider_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_gh_repos_status ON github_repos(spider_status);
