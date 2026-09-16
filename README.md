# unifiedcollector

## Overview

Read-only ingestion service that scrapes 14 social platforms
(github, youtube, strava, search, website, tiktok, lemon8, whatsapp,
telegram, instagram, beeper/matrix, threads, facebook, x) and writes
into one shared Postgres database. Runs as a 26-service Docker Compose
stack on a single Windows host. Feeds a downstream `unifiedanalyzer`
(separate repository) that does identity resolution and timelines.

The stack observes and archives; it never writes back to any source
platform. That is enforced at three layers: a static tripwire test, a
runtime `ReadOnlyTelegramClient` wrapper, and the general absence of
send/reply/react primitives in the codebase.

## Prerequisites

- **Operating system**: Windows 10 or 11 with WSL2 + Docker Desktop 4.30+.
  Alternatively Linux with Docker Engine 24+ (untested by the
  maintainer, path-mapping in compose assumes Windows drive letters).
- **Runtime**: Docker Compose v2. Python 3.12 for host-side scripts.
  Node.js 20 for the Chrome extension bundler (auto-installed inside
  the wa-bridge image via the setup_20.x deb).
- **System libraries** (installed automatically by Dockerfiles):
  Playwright Chromium, libolm for Matrix E2EE, sqlite3, ffmpeg.
- **External accounts required** (all optional per collector; disable
  by unsetting the credential):
  - Telegram: `api_id` + `api_hash` + 1–4 session strings via
    onboarding bot.
  - WhatsApp: Baileys-scan QR through the bridge on first launch.
  - GitHub: personal access token (comma-separated rotation supported).
  - YouTube: API key + OAuth cookie file.
  - Instagram: 1–6 accounts with username/password (Instaloader mode)
    or browser-cookie mode.
  - Strava, TikTok, Lemon8: session cookies exported from browser.
  - Search: Serper or DuckDuckGo (via Tor SOCKS5).
  - Beeper (optional): Beeper Desktop Local API token.
  - GHunt (optional recon): burner Google account.
- **Host storage**: Z-drive (or another external volume) mounted for
  the media vault at `z:/unifiedcollector`. Compose bind-mounts
  `z:/unifiedcollector/media` and `z:/unifiedcollector` into most
  collector containers.
- **Chrome (host-side)**: Chrome for Testing or Playwright Chromium at
  `%LOCALAPPDATA%\Google\Chrome for Testing\Application\chrome.exe`
  (auto-detected by `scripts/start-scraper-chrome-cdp.ps1`). The
  UnifiedCollector Bridge extension is loaded manually into this Chrome
  profile.

## Installation

```powershell
# Clone
git clone https://github.com/hongyime/unifiedcollector.git
cd unifiedcollector

# Environment
Copy-Item .env.example .env
# Edit .env with real values (see "Environment Configuration" below).

# Per-service env split (SEC-003) — copy each template and populate.
# The stack expects docker/env/*.env to exist alongside docker/env/*.env.example.
Get-ChildItem docker/env/*.env.example | ForEach-Object {
    Copy-Item $_ ($_.FullName -replace '\.example$','')
}
# Then edit each docker/env/<service>.env with the platform-scoped values
# from your monolithic .env. Only the fields relevant to that service.

# Chrome-side (one-time on host)
pwsh scripts/start-scraper-chrome-cdp.ps1
# Load the extension: Chrome → chrome://extensions → Developer mode →
# "Load unpacked" → select the extension/ directory. Copy the
# extension ID that appears and set UC_EXTENSION_ID in .env.

# Supply the existing restricted Telegram account allowlist from the private host environment.
# Compose refuses to start if TELEGRAM_SPIDER_ACCOUNTS is unset or empty.
# Bring up the stack
docker compose -f docker/docker-compose.yml up -d

# Optional: enable the OSS enrichment pipeline
docker compose -f docker/docker-compose.yml --profile recon up -d

# Register host-side scheduled tasks (browser autorecover + tab maintenance)
pwsh scripts/register-browser-autorecover-task.ps1
pwsh scripts/register-browser-maintenance-task.ps1
```

Success looks like `docker compose ps` reporting all 26 services `Up`
with `(healthy)` on the 22 that define healthchecks, and
`curl http://localhost:8700/health` returning `{"status":"ok",...}`.

## Environment Configuration

Environment variables are defined in three places:

1. `.env` at repo root — the monolithic file historically holding
   everything. Kept for backward compatibility with services that read
   `env_file: ../.env`; today no compose service references it (SEC-003
   subtractive removed those entries).
2. `docker/env/*.env` — per-service scoped files (gitignored). Each
   service in `docker/docker-compose.yml` sources exactly its own scope.
3. `docker/env/*.env.example` — tracked templates with variable names +
   purpose comments but no values.

To create the runtime env from templates:

```powershell
Get-ChildItem docker/env/*.env.example |
    ForEach-Object { Copy-Item $_ ($_.FullName -replace '\.example$','') }
# Then edit each docker/env/<service>.env.
```

The full variable catalogue (names, purposes) lives in the tracked
`.env.example` files. Selected required variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | Yes | none | Postgres credentials |
| `POSTGRES_HOST_PORT` | No | `5433` | Host-side port for Postgres |
| `DATABASE_URL` | Yes | none | Full DSN for asyncpg (services reconstruct from POSTGRES_*) |
| `COLLECTOR_VAULT_ROOT` | Yes | `z:/unifiedcollector` | Media / sidecar mount root |
| `RABBITMQ_USER`, `RABBITMQ_PASSWORD` | Yes | none | RabbitMQ credentials for WhatsApp bridge queues |
| `REDIS_PASSWORD` | Yes | none | Redis auth for realtime feed |
| `DASHBOARD_ADMIN_USERNAME` | Yes | none | Dashboard login user |
| `DASHBOARD_ADMIN_PASSWORD` | Yes | none | Dashboard login password (bcrypt-hashed at boot) |
| `DASHBOARD_JWT_SECRET` | Yes | none | JWT signing key (generate: `python -c "import secrets;print(secrets.token_urlsafe(48))"`) |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Only if telegram | none | Telethon MTProto credentials |
| `WHATSAPP_MEDIA_BRIDGE_SECRET` | Only if whatsapp | none | Shared secret between bridge and consumer |
| `NOTIFY_TELEGRAM_BOT_TOKEN` | For alerts | none | Bot that posts operational alerts |
| `CHROME_CDP_URL` | Yes | `http://host.docker.internal:9336` | Browser cookie vault + extension coordination |
| `UC_EXTENSION_ID` | For extension path | none | Installed extension ID (from `chrome://extensions`) |
| `UC_EXTENSION_EXPECTED_VERSION` | For dashboard | current manifest version | Dashboard flags mismatch |
| `WA_CONTACT_BATCH_MAX` | No | `100` | Contact-event batch size |
| `WA_CONTACT_BATCH_TIMEOUT_MS` | No | `250` | Contact batch flush timeout |
| `WA_CONTACT_ARCHIVE_RAW` | No | `0` | `1` to re-enable raw-payload archive for contacts |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` | No | `300000` | Postgres reaps IIT sessions past this |
| `RECON_ALLOWLIST` | No | empty | Comma-separated domains in scope for enrichment |
| `RECON_ALLOW_UNSCOPED` | No | `0` | `1` to allow OSINT outside allowlist (use with care) |
| `COMPOSE_PROFILES` | No | empty | Set to `recon` to bring up spiderfoot alongside core |

The full list (240+ variables) is in `.env.example` +
`docker/env/*.env.example`.

## Running

**Development mode** (all services, code-mount live-reload):

```powershell
docker compose -f docker/docker-compose.yml up -d
```

Ports bound on the host:
- `${POSTGRES_HOST_PORT:-5433}` → postgres:5432
- `8700` → dashboard (also mapped :8001)
- `8765` → ig_ingest (Chrome extension endpoint)
- `8790` → browser_cookie_vault
- `3011`, `3012` → wa-bridge-1/2 (management + QR scan)

Expected outputs:
- Dashboard at `http://localhost:8700` (login with
  `DASHBOARD_ADMIN_USERNAME` / `_PASSWORD`).
- `curl http://localhost:8765/health` → `{"ok": true, "db_pool": true, ...}`.

**Production mode**: same command with `.env` populated. No separate
config layer. Cron-style tasks (backup, scheduler) run inside their
containers on `restart: unless-stopped`.

**Enrichment (recon) mode**:

```powershell
docker compose -f docker/docker-compose.yml --profile recon up -d
```

This adds `collector_spiderfoot`. Requires `RECON_ALLOWLIST` to be
populated unless `RECON_ALLOW_UNSCOPED=1`.

## Usage

**Query collector health from CLI:**

```powershell
docker exec unifiedcollector_dashboard sh -c `
    'wget -qO- http://localhost:8700/health'
```

Sample output:

```json
{"status":"ok","database":"healthy","drive":"skipped","database_status":"ok",
 "vault":{"available":null,"writable":null,"mode":"skipped_by_config",...}}
```

**Trigger a bounded recon-seed run (dry-run first):**

```powershell
docker exec unifiedcollector_scheduler `
    python -m src.main recon-seed --limit 200 --dry-run
docker exec unifiedcollector_scheduler `
    python -m src.main recon-seed --limit 200
```

**Restore the latest DB backup into a scratch DB and verify** (bounded,
non-destructive):

```powershell
docker exec unifiedcollector_collector `
    python -m src.main restore-drill --dry-run
```

**Run one WhatsApp phone-OSINT backfill batch** (offline
`phonenumbers` library only, no network):

```powershell
docker exec unifiedcollector_collector `
    python -m src.core.wa_phone_intel --limit 20000
```

**Regenerate the maigret false-positive blocklist** (inside the recon
container, requires `--profile recon`):

```powershell
docker exec unifiedcollector_spiderfoot `
    python -m src.recon_maigret_fp_refresh --controls 10 --force
```

Before updating an existing deployment, copy its current `TELEGRAM_SPIDER_ACCOUNTS`
allowlist into the private host environment or an explicit Compose `--env-file`.
A service-level `env_file` alone does not supply Compose interpolation. Preserve
the same account names: an empty list in the collector means all accounts, so
Compose requires a non-empty value and no longer hard-codes personal identities.
See [Docker interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

## Testing

**Python suite** (tracked under `tests/`):

```powershell
docker exec unifiedcollector_collector sh -c `
    'cd /app && python -m pytest tests/ -q --ignore=tests/verify_clean_boot.py --ignore=tests/verify_production.py'
```

`.github/workflows/python-ci.yml` runs Ruff lint (F + E9), the full
Python suite with external sockets blocked, and a clean-volume schema
verifier against `pgvector:pg16`. Node 24 executes pure extension host
validation fixtures without contacting the browser or upstream providers.
The schema verifier checks an empty database and a second boot: existing
row bytes and migration checksums must survive, and the media rollup
trigger must keep its narrowed update columns. Migration prerequisites
are declared in `src/db/migrate.py`; applied SQL files retain their names
and checksums.

**Frontend smoke test** (Vitest + jsdom, `AppShell` render):

```powershell
Push-Location src/dashboard/frontend
npm ci --ignore-scripts
npm test
Pop-Location
```

Passes 2/2 tests in ~13 s on Windows (the config forces
`pool: "threads"` because `forks` times out on this platform).

**Environment split verifier** (SEC-003 CI check):

```powershell
python scripts/verify_env_split.py
```

Runs also in CI on any PR that touches compose, `docker/env/**`,
`src/**/*.py`, or the script itself.

**What is not covered:**
- Frontend beyond `AppShell` render — no route-by-route coverage.
- WhatsApp bridge (TypeScript) has no unit tests.
- End-to-end integration tests requiring a live RabbitMQ +
  Chrome-with-extension are not automated; they were run manually
  during the audit.

## Project Structure

```
unifiedcollector/
├── AGENTS.md                        Cross-agent conventions (skills, state, sync)
├── AUDIT.md                         Full audit output from the LIVE-scope pass
├── PRD.md                           This document's sibling — product requirements
├── README.md                        You are here
├── REPO_MAP.md                      Deep repo cartography
├── SECURITY.md                      Reporting + Dependabot policy
├── .env.example                     Monolithic template with all env names
├── docker/                          Compose stack + Dockerfiles
│   ├── docker-compose.yml           26 services
│   ├── Dockerfile*                  5 build targets (collector, dashboard, spiderfoot, backup, whatsapp-bridge)
│   ├── env/                         Per-service env templates (SEC-003)
│   ├── patches/                     Runtime patches for GHunt + SpiderFoot
│   └── postgres/                    postgres.conf overlay
├── src/                             Python codebase
│   ├── main.py                      argparse dispatcher (thin, delegates to src/cli/commands/)
│   ├── cli/commands/                10 subcommand modules
│   ├── collectors/                  13 source-specific collectors (mixin-composed telegram, package-composed dashboard/api & ig_ingest)
│   ├── core/                        Cross-cutting helpers (vault, dedupe, rate limit, base collector, recon)
│   ├── bridges/
│   │   ├── ig_ingest/               aiohttp bridge package, 15 modules
│   │   └── whatsapp/                TypeScript Baileys bridge (own package.json)
│   ├── dashboard/
│   │   ├── api/                     FastAPI package, 14 modules
│   │   ├── websocket.py             WebSocket routes
│   │   └── frontend/                React 19 + Vite SPA (moved from top-level dashboard/)
│   ├── scheduler/
│   │   ├── __init__.py              183 LOC dispatcher
│   │   └── handlers/                12 periodic handler classes
│   ├── watchdog/freshness.py        Restart-on-stale safety net
│   ├── notifications/               Realtime feed + alerts + Telegram send
│   ├── db/
│   │   ├── connection.py            asyncpg pool factory (with IIT timeout)
│   │   ├── migrate.py               Ledger-tracked migration runner
│   │   ├── schemas/                 14 base schema files
│   │   └── migrations/              124 migration files (+ _archive/ subdir)
│   ├── backup/                      pg_dump wrapper + restore-drill
│   ├── bots/onboard_bot.py          Telegram onboarding bot
│   ├── tools/browser_cookie_vault.py CDP snapshot loop
│   └── worker/__init__.py           WorkerService supervisor
├── extension/                       Chrome MV3 extension (esbuild + IIFE bundle)
│   ├── src/                         Typed source (background, content, inject, popup, tabs + 6 shared modules)
│   └── dist/                        Bundled output (committed for reproducibility)
├── config/sources/                  Per-source .targets / .env overlays (bind-mounted RO)
├── scripts/                         Windows PS1 + Python operator scripts (48+ files)
├── tests/                           110 test files mirroring src/ layout
├── docs/
│   ├── README.md                    Docs manifest
│   ├── KNOWN_ISSUES.md              Resolved + open architectural concerns
│   ├── enrichment.md                Deep reference for the OSS enrichment layer
│   ├── contracts/                   COLLECTION_SPEC.md + IDENTITY_KEYS.md
│   ├── audits/                      Historical audit outputs (collector_audit.md, feature_gap_analysis.md, SYNC_PROGRESS.md, etc.)
│   ├── handoff/                     Session-handoff docs
│   └── plans/                       Bucket A refactor plans + drain-acceleration research
├── tools/                           telegram_login, browser_tab_audit, optional_rollout_monitor
├── .github/
│   ├── workflows/                   16 CI workflows (python-ci, codeql, bandit, semgrep, trufflehog, scorecard, lfs-guard, extension-build, env-split-verify)
│   ├── dependabot.yml
│   └── labels.yml
└── models/dlib/                     Placeholder (.gitkeep + README); model files never committed
```

## Troubleshooting

Failure modes hit during the audit / hardening pass and their fixes:

**1. `ig_ingest` reports `db_pool: false` after a container restart.**
Old symptom: `startup DB pool timed out` at boot when Postgres was slow
to accept connections after the host wake-up. Fixed 2026-09-06 by
extending startup retry to 300 s and by making `/health` return 503
when db_pool is absent AND startup is not pending. If it still happens,
`docker compose restart ig_ingest` and watch
`docker logs unifiedcollector_ig_ingest --tail 20`.

**2. Chrome extension stops POSTing to `ig_ingest`.**
Diagnosis: `curl http://localhost:8790/health` (browser cookie vault)
also fails, and `Test-NetConnection 127.0.0.1 -Port 9336` returns
False. Fix: close all Chrome windows on the host, then re-run
`pwsh scripts/start-scraper-chrome-cdp.ps1`. Confirm the `UnifiedCollector
Bridge` extension is enabled with an active service worker at
`chrome://extensions`.

**3. WhatsApp bridge disconnects — `bridge_unpaired` 503 in collector
logs.** One or both Baileys sessions have lost credentials. Fix:
`docker exec unifiedcollector_wa_bridge_1 sh -c 'wget -qO- http://localhost:3001/qr'`
returns a QR payload; scan it from the WhatsApp mobile app under Linked
Devices. Alert handler `bridge_unpaired_alert` will notify Telegram if
it recurs.

**4. RabbitMQ `contacts` queue backlog growing.** Historic bug: the
consumer's `raise` inside `async with message.process()` triggered a
`ChannelInvalidStateError` cascade → 60 s reconnect cycles → net
negative drain. Fixed 2026-09-07 by explicit `message.nack(requeue=True)`
without re-raise. Batch consumer (default `WA_CONTACT_BATCH_MAX=100`)
peaks at ~63 events/s across 4 parallel consumers.

**5. Idle-in-transaction sessions accumulating in Postgres.** Source is
the analyzer repo's `build_timeline` (out of this repo). Mitigation:
Postgres server-side `idle_in_transaction_session_timeout=5min` reaps
them automatically. Alert handler `postgres_idle_txn_alert` posts to
Telegram if any session stays IIT >10 min. Full fix requires an
analyzer PR.

**6. Vitest `Timeout waiting for worker to respond` on Windows.** The
default `forks` pool doesn't work reliably on Windows/Docker Desktop.
`src/dashboard/frontend/vite.config.ts` forces `pool: "threads"` which
resolved this.

**Database startup recovery limits.** `DB_CONNECT_RETRY_TIMEOUT_SECONDS`
(default `180`) covers pool initialization and retry sleeps together. A stalled
connection cannot extend a positive budget. Set it to `0` for one connection
attempt without retries; the driver's connection timeout still applies.
`DB_CONNECT_RETRY_INITIAL_SECONDS` defaults to `5` and
`DB_CONNECT_RETRY_MAX_SECONDS` to `30`; the maximum also caps the first delay.
Malformed or non-finite values use the defaults. Failed or cancelled pool
initialization cancels unfinished sibling attempts and closes initialized
connections before returning. Concurrent callers still share one pool.
These controls change startup recovery, not query limits, pool size, collection
cadence, retention or storage location.

**7. TypeScript 7 rejects `moduleResolution: "node10"` in the WhatsApp
bridge.** Dependabot bumped `typescript` from `^5.9.3` to `^7.0.2` in
`src/bridges/whatsapp/package.json`. TS 7 hard-errors on CommonJS
importing Baileys' ESM package. Fix: pin back to `~5.9.3` — the bridge
uses CommonJS output and TS 5.9 keeps the CJS/ESM compat this bridge
relies on.

**8. Docker Desktop 500 errors during `docker compose restart`.**
Symptom: `request returned 500 Internal Server Error for API route ...
/containers/.../json`. This is a Docker Desktop transient — retry the
restart, or `docker restart <name>` directly. Not caused by the stack.

**9. `browser-autorecover` scheduled task not running.** The registrar
fell back to the Startup folder (elevated `Register-ScheduledTask`
denied). It only runs `AtLogOn`, which means Chrome doesn't
auto-recover while the host screen is locked. Re-run
`pwsh scripts/register-browser-autorecover-task.ps1` after granting
admin, or accept the current behavior on this single-user host.

**10. Verify a fresh clone runs.** After cloning + populating `.env`
and `docker/env/*.env`, run
`python scripts/verify_env_split.py` before `docker compose up -d`.
The script fails loudly if any referenced env var is not documented in
the templates.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
