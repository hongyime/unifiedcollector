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
    sha VARCHAR(40) UNIQUE NOT NULL,
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
    CONSTRAINT unique_platform_commit_github UNIQUE (sha)
);

CREATE TABLE IF NOT EXISTS github_issues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    repo_id UUID REFERENCES github_repos(id) ON DELETE CASCADE,
    platform_issue_id INTEGER UNIQUE NOT NULL,
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
