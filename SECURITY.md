# Security Policy

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: cadence.linardi@gmail.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

You will receive a response within 48 hours. Please allow reasonable time to patch before public disclosure.

## Secrets Management

### Where secrets live

| Location | Purpose |
|---|---|
| `.env` (repo root, gitignored) | All runtime secrets — loaded by `docker-compose.yml` via `env_file` |
| `.env.example` | Template with all 107 keys, no values — copy to `.env` and fill in |
| `credentials/` (gitignored) | OAuth tokens, cookies, session files mounted into containers |
| `sessions/` (gitignored) | Telethon/WhatsApp session files |

### Backups

`.env.bak.*` files are stored **outside** the repo at `~/.unifiedcollector_env_backups/`.
Never create `.env.bak.*` files inside the repo directory.

### What's gitignored

`.env`, `.env.bak.*`, `credentials/`, `sessions/`, `*.session`, `*.pickle`,
`client_secret.json` — see `.gitignore` for the full list.

### Git history

Historical commits that contained secret-bearing files (`.env.bak.*`, archive
toolkit `.env.template`/`.env.example` files) were purged via `git filter-repo`
on 2026-06-07.

## Automated Security

- **TruffleHog** scans every push and PR for accidentally committed secrets
- **Dependabot** opens PRs for dependency updates daily (auto-merged)

## GH_PAT Security Model

### Scope

The GH_PAT (GitHub Personal Access Token) requires:
- repo — full control of private repositories
- workflow — update GitHub Actions workflows
- admin:repo_hook — manage repository hooks

### Blast Radius

The sync-repo-settings.yml workflow **propagates GH_PAT to every owned non-archived repository** as a repository secret. This means:

- If the PAT is compromised, an attacker has write access to ALL repositories
- The PAT is used with --admin flag in bot auto-merge workflows, bypassing all branch protection

### Auto-merge --admin Pattern

Bot PRs (Dependabot, Snyk, Sourcery, DeepSource, Copilot SWE) are merged with --admin to bypass branch protection. This is acceptable because:

1. These bots only modify dependency manifests and lockfiles
2. TruffleHog and CodeQL scan every commit regardless of merge method
3. The Build Check workflow validates the build before --admin merge is triggered
