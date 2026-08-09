CREATE INDEX IF NOT EXISTS idx_github_edges_collected_at
    ON github_edges (collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_github_issues_collected_at
    ON github_issues (collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_github_issue_comments_collected_at
    ON github_issue_comments (collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_github_pr_reviews_collected_at
    ON github_pr_reviews (collected_at DESC);

CREATE INDEX IF NOT EXISTS idx_github_pr_review_comments_collected_at
    ON github_pr_review_comments (collected_at DESC);
