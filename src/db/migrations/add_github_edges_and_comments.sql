ALTER TABLE github_issues
    ALTER COLUMN platform_issue_id TYPE BIGINT;

CREATE TABLE IF NOT EXISTS github_issue_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    issue_id UUID REFERENCES github_issues(id) ON DELETE CASCADE,
    platform_comment_id BIGINT UNIQUE NOT NULL,
    issue_number INTEGER,
    author_login VARCHAR(255),
    body TEXT,
    html_url TEXT,
    platform_created_at TIMESTAMP,
    platform_updated_at TIMESTAMP,
    metadata JSONB,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_pr_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    platform_review_id BIGINT UNIQUE NOT NULL,
    pr_number INTEGER,
    author_login VARCHAR(255),
    state VARCHAR(50),
    body TEXT,
    html_url TEXT,
    platform_submitted_at TIMESTAMP,
    metadata JSONB,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_pr_review_comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    platform_comment_id BIGINT UNIQUE NOT NULL,
    pr_number INTEGER,
    author_login VARCHAR(255),
    body TEXT,
    html_url TEXT,
    path TEXT,
    position INTEGER,
    platform_created_at TIMESTAMP,
    platform_updated_at TIMESTAMP,
    metadata JSONB,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_login VARCHAR(255) NOT NULL,
    target_login VARCHAR(255) NOT NULL,
    repo_full_name VARCHAR(500),
    edge_type VARCHAR(80) NOT NULL,
    strength INTEGER NOT NULL DEFAULT 50,
    evidence_url TEXT,
    evidence_id VARCHAR(500) NOT NULL,
    raw_payload JSONB,
    first_seen TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW(),
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_github_edge_evidence UNIQUE (
        source_login, target_login, repo_full_name, edge_type, evidence_id
    )
);

CREATE INDEX IF NOT EXISTS idx_gh_issue_comments_repo ON github_issue_comments(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_pr_reviews_repo ON github_pr_reviews(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_pr_review_comments_repo ON github_pr_review_comments(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_edges_source ON github_edges(source_login, edge_type);
CREATE INDEX IF NOT EXISTS idx_gh_edges_target ON github_edges(target_login, edge_type);
CREATE INDEX IF NOT EXISTS idx_gh_edges_repo ON github_edges(repo_full_name, edge_type);
