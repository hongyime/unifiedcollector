-- GitHub V2 Schema

CREATE TABLE IF NOT EXISTS github_repos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_repo_id BIGINT UNIQUE NOT NULL,
    name VARCHAR(255),
    full_name VARCHAR(500),
    description TEXT,
    homepage TEXT,
    language VARCHAR(100),
    stargazers_count INTEGER DEFAULT 0,
    watchers_count INTEGER DEFAULT 0,
    forks_count INTEGER DEFAULT 0,
    open_issues_count INTEGER DEFAULT 0,
    topics TEXT[],
    license VARCHAR(100),
    platform_created_at TIMESTAMP,
    platform_updated_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    CONSTRAINT unique_platform_repo_github UNIQUE (platform_repo_id)
);

CREATE TABLE IF NOT EXISTS github_readmes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    content TEXT,
    sha VARCHAR(50),
    size INTEGER,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_commits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    sha VARCHAR(40) NOT NULL,
    author_name VARCHAR(255),
    author_email VARCHAR(255),
    author_login VARCHAR(255),
    committer_name VARCHAR(255),
    committer_email VARCHAR(255),
    message TEXT,
    date TIMESTAMP,
    files_changed INTEGER,
    insertions INTEGER,
    deletions INTEGER,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_commit_repo_github UNIQUE (sha, repo_id)
);

CREATE TABLE IF NOT EXISTS github_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    platform_issue_id BIGINT UNIQUE NOT NULL,
    number INTEGER,
    title TEXT,
    body TEXT,
    state VARCHAR(20), -- 'open', 'closed'
    is_pull_request BOOLEAN DEFAULT FALSE,
    labels TEXT[],
    assignees TEXT[],
    milestone VARCHAR(255),
    comments_count INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    closed_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_issue_github UNIQUE (platform_issue_id)
);

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

CREATE TABLE IF NOT EXISTS github_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_user_id BIGINT UNIQUE NOT NULL,
    login VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255),
    company VARCHAR(255),
    blog TEXT,
    location VARCHAR(255),
    email VARCHAR(255),
    bio TEXT,
    public_repos_count INTEGER DEFAULT 0,
    public_gists_count INTEGER DEFAULT 0,
    followers_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_user_github UNIQUE (platform_user_id)
);

CREATE TABLE IF NOT EXISTS github_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_type VARCHAR(20), -- 'user', 'repo'
    target_identifier VARCHAR(500) NOT NULL, -- username or "owner/repo"
    source VARCHAR(50), -- 'followers', 'contributors', 'manual'
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_target_github UNIQUE (target_type, target_identifier)
);

CREATE INDEX IF NOT EXISTS idx_gh_commits_repo ON github_commits(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_issues_repo ON github_issues(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_issue_comments_repo ON github_issue_comments(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_pr_reviews_repo ON github_pr_reviews(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_pr_review_comments_repo ON github_pr_review_comments(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_edges_source ON github_edges(source_login, edge_type);
CREATE INDEX IF NOT EXISTS idx_gh_edges_target ON github_edges(target_login, edge_type);
CREATE INDEX IF NOT EXISTS idx_gh_edges_repo ON github_edges(repo_full_name, edge_type);
