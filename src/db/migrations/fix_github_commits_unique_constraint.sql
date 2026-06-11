-- Fix github_commits: deduplicate by (sha, repo_id) instead of just (sha).
-- A commit appearing in multiple forks should be tracked per-repo.

-- Drop the old sha-only unique constraints
ALTER TABLE github_commits DROP CONSTRAINT IF EXISTS unique_platform_commit_github;
ALTER TABLE github_commits DROP CONSTRAINT IF EXISTS github_commits_sha_key;

-- Add the new composite unique constraint
ALTER TABLE github_commits ADD CONSTRAINT unique_commit_repo_github UNIQUE (sha, repo_id);
