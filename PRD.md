# Product Requirements Document

Reflects the repository state as of commit `1c1d532c` on branch `main`,
after the drain-acceleration sprint. All statements below are grounded in
tracked code, docker-compose service definitions, or live-database
observations. Anything the maintainer could not verify is prefixed
`[unverified]`.

## 1. Executive Summary

Unified ingestion plane for social-platform content. Collects public and
semi-public content from 14 source platforms (github, youtube, strava,
search, website, tiktok, lemon8, whatsapp, telegram, instagram,
beeper/matrix, threads, facebook, x) and writes it into one shared
Postgres database. Read-only by design — the service observes and
archives, it never writes back to a source. Feeds a downstream
`unifiedanalyzer` (separate repository) that performs identity
resolution, timelines, and cross-platform clustering. Operator model is
single-tenant on a Windows host running Docker Compose; there is no
multi-user or SaaS component.

## 2. System Architecture

**Runtime:** Docker Compose, 26 services on one shared Postgres 16
(`pgvector/pgvector:pg16`), Redis 7, and RabbitMQ 3.13. Every container
bind-mounts `../src:/app/src` so code changes apply on
`docker compose restart <svc>` without image rebuild.

**Three collection paths:**

| Path | Provenance tag | Services |
|---|---|---|
| Headless collectors (server-side scraping) | `ingest_path='headless'` | `collector_youtube`, `collector_tiktok`, `collector_instagram`, `collector_lemon8`, `collector_website`, `collector_exposure`, `collector_lowrisk` (github+strava+search merged) |
| Browser extension bridge | `ingest_path='extension'` | Chrome MV3 extension (host-side) posts to `ig_ingest:8765` — aiohttp bridge that persists into the same DB |
| Realtime messaging | `ingest_path='messaging'` | `collector_telegram` (Telethon), `collector_whatsapp` (consumes RabbitMQ from `wa-bridge-1/2` Baileys), `collector_beeper` (Matrix) |

**Support services:** `dashboard` (FastAPI at :8700 + React 19 SPA),
`watchdog` (freshness safety net that restarts stale realtime
containers via Docker socket), `scheduler` (12 periodic handlers),
`realtime_feed` (drains Redis list to Telegram alerts),
`browser_cookie_vault` (5-min Chrome CDP cookie snapshots), `backup`
(daily pg_dump with verified atomic rename), `onboard_bot`,
`collector_spiderfoot` (OSINT enrichment, compose profile `recon`).

**State:** Postgres 16 + pgvector holds all structured state (145 public
tables). Redis holds the realtime post-feed queue and anti-ban dedupe
cache. RabbitMQ (`whatsapp.events` exchange) carries WhatsApp bridge
events. Filesystem vault on `z:/unifiedcollector` holds ~887k media
files under sha256-blob paths.

**Trust boundaries:** All services run in one Docker network. Postgres
is not exposed to host by default (compose maps only `${POSTGRES_HOST_PORT:-5433}`).
`dashboard` binds `:8700`, `ig_ingest` `:8765`, `browser_cookie_vault`
`:8790`. Everything else is internal. `credentials/` and GHunt
`creds.m` mount as read-only. Per-service `.env` files scope credentials
so `collector_spiderfoot` no longer sees Instagram/Telegram credentials
(SEC-003 subtractive complete as of commit `6500878f`).

## 3. Feature Matrix

| Feature | Module/Path | Status | Notes |
|---|---|---|---|
| GitHub collector | `src/collectors/github/` | Implemented | Merged into `collector_lowrisk` container |
| Strava collector | `src/collectors/strava/` | Implemented | In `collector_lowrisk` |
| Search collector | `src/collectors/search/` | Implemented | In `collector_lowrisk` |
| Website spider | `src/collectors/website/` | Implemented | Own container |
| Exposure (dorking) collector | `src/collectors/exposure/` | Implemented | 13th collector, own container |
| Instagram collector (headless) | `src/collectors/instagram/` | Implemented | Multi-account instaloader |
| Instagram DM subsystem | `src/collectors/instagram_dm/` | Implemented | Dedicated `collector_instagram_dm` container |
| TikTok collector | `src/collectors/tiktok/` | Implemented | gallery-dl + yt-dlp + Playwright fallback |
| YouTube collector | `src/collectors/youtube/` | Implemented | yt-dlp, OAuth-aware |
| Lemon8 collector | `src/collectors/lemon8/` | Implemented | Public scrape only |
| Telegram collector | `src/collectors/telegram/` (mixin composition) | Implemented | 4 accounts, MTProto via Telethon |
| WhatsApp bridge + consumer | `src/bridges/whatsapp/` (TS Baileys) + `src/collectors/whatsapp/` | Implemented | 2 bridges, batched contact upsert (drain 15+/s) |
| Beeper/Matrix collector | `src/collectors/beeper/` | Implemented | Matrix protocol via matrix-nio |
| Browser extension bridge | `src/bridges/ig_ingest/` (package, 15 modules) | Implemented | 36 aiohttp routes |
| Chrome MV3 extension | `extension/{src,dist}/` | Implemented | esbuild IIFE bundle, 5 typed shared modules |
| Dashboard API | `src/dashboard/api/` (14 modules) | Implemented | 110 FastAPI routes, JWT auth |
| Dashboard SPA | `src/dashboard/frontend/` (React 19 + Vite) | Implemented | 40+ routes; Vitest smoke test in place |
| Scheduler | `src/scheduler/` + `handlers/` (12 handlers) | Implemented | Per-tick isolation, __init__.py 183 LOC |
| Watchdog | `src/watchdog/freshness.py` | Implemented | Restarts realtime containers on staleness |
| Realtime post feed | `src/notifications/realtime_feed.py` | Implemented | Redis → Telegram, rate-limited, dedupe TTL |
| DB backup | `src/backup/db_backup.py` | Implemented | Daily pg_dump, verified rename, 7/4/3 retention |
| Enrichment (maigret / SpiderFoot / GHunt) | `src/core/recon_spiderfoot.py`, `src/recon_spiderfoot_service.py` | Implemented | Compose profile `recon` |
| Phone-OSINT (offline) | `src/core/wa_phone_intel.py` | Implemented | Enrichment-only, never `identity_signals` |
| CLI subcommands | `src/main.py` + `src/cli/commands/` (10 modules) | Implemented | 22 subcommands |
| CI workflows | `.github/workflows/` (16 files) | Implemented | python-ci, codeql, bandit, semgrep, trufflehog, scorecard, lfs-guard, env-split-verify |
| Vitest frontend smoke test | `src/dashboard/frontend/src/components/layout/__tests__/AppShell.test.tsx` | Implemented | 2/2 tests pass |
| Outbound writes to source platforms | — | **Absent (by design)** | Read-only architecture |
| Contact-event batching | `_upsert_contacts_batch` in `src/collectors/whatsapp/__init__.py` | Implemented | 15+/s drain via unnest arrays |

## 4. Data Model

**Primary table:** `media_items` — one row per downloaded artifact from
any source. Live counts (2026-09-09): telegram 251,281 · instagram
166,353 · search 136,169 · website 112,626 · threads 55,476 · youtube
51,122 · tiktok 29,734 · github 18,135 · strava 17,343 · facebook
15,774 · beeper 15,379 · lemon8 11,556 · whatsapp 3,226 · x 2,853.
Total ≈ 887,027 rows.

**Dedup:** `(source, content_id)` UNIQUE (`src/db/schemas/collector.sql`
composite index); `sha256` unique-per-source added later by
`add_media_sha256_unique.sql`. `sha256` is also used at the vault-blob
level to store one physical file for cross-collector duplicates.

**Cross-platform identity:** `social_users` (120k+ rows) plus per-source
tables (`telegram_users`, `whatsapp_users` with 20k rows,
`whatsapp_lid_map` with 17k rows, `youtube_channels`, etc.). Contract
documented in `docs/contracts/IDENTITY_KEYS.md`.

**Recon:** `recon_targets` (work queue), `recon_observations` (results),
`recon_maigret_fp_sites` (FP blocklist), `wa_phone_intel` (offline
phone metadata, never joined into `identity_signals`).

**Migrations:** 124 migration files under `src/db/migrations/` (3
archived under `_archive/`). 122 applied in the live DB. Applied by
`src/db/migrate.py::apply_all` on every container start; ledger table
`schema_migrations` tracks by filename + sha256 checksum. Editing an
applied migration bricks every container on next boot; new work is
always a new file.

## 5. External Interfaces

**HTTP surfaces:**

| Service | Port | Framework | Routes |
|---|---|---|---|
| `dashboard` | 8700 | FastAPI | 110 `@app.<verb>` decorators + 1 websocket, JWT auth via `Depends(require_role(...))` |
| `ig_ingest` | 8765 | aiohttp | 36 routes under `/social/*` and `/ig/*`, no auth (localhost + extension-origin only) |
| `browser_cookie_vault` | 8790 | aiohttp | `/health`, CDP snapshot lifecycle |
| `wa-bridge-1` / `wa-bridge-2` | 3011 / 3012 → 3001 internal | Node/express | `/health`, `/qr`, `/session`, `/reconnect`, `/media/decrypt`, `/devices/:number` |

**Message queues (RabbitMQ, `whatsapp.events` exchange):**

| Queue | Producer | Consumer | Prefetch |
|---|---|---|---|
| `unifiedcollector.messages` | `wa-bridge-*` | `collector_whatsapp` (4 parallel) | 64 channel-wide |
| `unifiedcollector.contacts` | `wa-bridge-*` | `collector_whatsapp` (4 parallel, batched) | 64 |
| `unifiedcollector.groups` | `wa-bridge-*` | `collector_whatsapp` (1) | 64 |
| `unifiedcollector.sessions` | `wa-bridge-*` | `collector_whatsapp` (1) | 64 |

**Redis (7-alpine):** list `uc:realtime_post_feed` + companion dedupe
keys. 73k+ keys typical steady state.

**Docker socket:** `watchdog` posts to
`http://docker/containers/{name}/restart?t=15` for stale-source
recovery.

**Outbound:** Telegram Bot API (alerts + realtime feed), Postgres
socket (all services), RabbitMQ AMQP, Redis, third-party HTTP GET to
platform CDNs for media download. No outbound writes to source
platforms.

**CLI:** `python -m src.main <subcommand>`. 22 subcommands including
`worker`, `scheduler`, `run`, `status`, `coverage-snapshot`,
`recon-queue`, `recon-spiderfoot`, `recon-seed`, `optional-rollout`,
`realtime-media-backfill`, `rebuild-report`, `rebuild-rehearsal`,
`vault-inspect`, `repair-media-sidecars`, `repair-media-file-paths`,
`recover-missing-media-files`, `media-artifact-audit`,
`backfill-discovered-links`, `restore-drill`, `schedule`, `target`.

## 6. Security Posture

**Present:**
- Per-service credential scoping: `docker/env/*.env` split with `../.env`
  removed from every `env_file:` list (SEC-003 subtractive complete).
  `collector_spiderfoot` verified to have 0 Instagram/Telegram
  credentials in its environment.
- JWT auth on dashboard (`Depends(require_role("viewer"|"operator"))`);
  bcrypt password hashing; `DASHBOARD_JWT_SECRET` required at startup.
- Postgres server-side `idle_in_transaction_session_timeout=5min` on
  every pooled connection reaps leak sessions.
- `application_name` = container hostname on every pooled connection
  → pg_stat_activity attribution.
- All credentials-holding paths (`credentials/`, GHunt `creds.m`)
  mount read-only into containers.
- No hardcoded secrets in tracked source (verified by grep + trufflehog
  in CI).
- Read-only-by-design against source platforms: static tripwire test
  (`tests/test_readonly_guard.py`) scans for outbound method patterns;
  `ReadOnlyTelegramClient` wrapper enforces at runtime.
- Migration `schema_migrations` ledger with sha256 checksums prevents
  drift on edit.
- Daily automated backup with `pg_restore --list` verification and
  atomic rename; 7 daily / 4 weekly / 3 monthly retention.
- CI: python-ci (ruff + pytest + clean-boot schema), codeql, bandit,
  semgrep, trufflehog, scorecard, lfs-guard, env-split-verify.
- Chrome MV3 extension uses IIFE bundle (esbuild) — no eval, no
  dynamic import, MV3 CSP-compliant.
- Chrome CDP debug port `9336` is loopback-only.

**Absent by intent:**
- Multi-user auth (system is single-tenant).
- TLS between compose services (internal Docker network only).
- Rate limiting on `ig_ingest` internal routes (localhost + extension
  origin).

**Absent + noted:**
- `SYS_PTRACE` cap_add on collector containers (documented in compose
  header) — retained for py-spy diagnostics; safe on single-tenant
  rig, not for multi-tenant.

## 7. Performance & Scalability

**Observed characteristics** (2026-09-08 measurements):

- Per-UPSERT (asyncpg pool, warm): 26 ms p50 / 85 ms p95 on
  `whatsapp_users`.
- Contact-event drain peak: ~15.7 events/s per consumer × 4 = 63/s
  after batching landed (was 0.23-0.39/s baseline). 96k contacts
  backlog cleared in ~1.7 hours.
- Steady-state drain: bottlenecked by producer rate from Baileys
  bridges (~40-50/s during history-sync), not consumer capacity.
- Dashboard `/collectors/source-matrix` payload cached with
  `_SOURCE_MATRIX_SECTION_CACHE_TTL_SECONDS=30`.
- Postgres pool per service: default `DB_POOL_MAX_SIZE=10`,
  `collector_whatsapp` overridden to 20. Global `max_connections=200`.

**Known bottlenecks:**
- The `_archive_raw_event` fsync path to the Z-drive vault was 1,110 ms
  p50 per write. Disabled for contact events (`WA_CONTACT_ARCHIVE_RAW=0`);
  other events still fsync but at lower volume.
- Postgres cross-collector contention when analyzer cursor loops hold
  connections idle-in-transaction (mitigated by the pool-side timeout,
  fully fixed only when analyzer's `build_timeline` refactor lands).

**Concurrency model:**
- Every source in its own container (SIGCHLD isolation for yt-dlp /
  gallery-dl subprocess wedges).
- WhatsApp consumer: 4 parallel tasks per hot queue (contacts,
  messages), 1 for groups + sessions.
- Batched contact upserts: 100-event buffer or 250 ms timeout.
- `_FatalSpinLogWatcher` triggers `os._exit(42)` on Telethon MTProto
  desync / SQLite lock floods so Docker's `restart: unless-stopped`
  recovers with a clean process.

**Resource limits:** Per-container `mem_limit` tuned to a 7.7 GiB WSL2
VM. Collector images share `unifiedcollector-collector:latest` (~2 GiB).

## 8. Non-Functional Behavior

**Logging:** structured `%(asctime)s [%(levelname)s] %(name)s:
%(message)s` on stdout for Python containers; JSON via pino for
Node bridges. Third-party loggers `httpx`, `httpcore`, `telethon`,
`asyncio` capped at WARNING.

**Error handling:** every collector wraps its cycle in `resilience.py`
circuit breaker + adaptive rate limiter. Dead-letter queue table for
retryable failures. Realtime bridge failures land in
`rate_limit_events` for dashboard visibility.

**Retry/timeout:** asyncpg pool `command_timeout=60`. Per-request
timeouts documented per collector via env (`YOUTUBE_DOWNLOAD_TIMEOUT`,
`WA_CONSUMER_HANDLER_TIMEOUT_SECONDS`, etc.). Exponential 429 backoff
persisted in DB so container restart preserves cooldown.

**Graceful shutdown:** each worker installs signal handlers; scheduler
uses `stop_event` in `SchedulerContext`; consumers check
`self._stop.is_set()` between messages.

**Health checks:** Docker healthchecks on 22 of 26 services (`backup`
and older `ig_ingest` are cron-style, no defined check). `dashboard`
`/health` returns database + drive + vault + backup status.
`ig_ingest` `/health` returns 503 when `db_pool` is absent and
startup task has given up (honest health).

**Observability:** Prometheus metrics on `dashboard:/metrics`
(collector quota / crash count / source-freshness). Structured logs.
No OpenTelemetry / tracing.

## 9. Known Limitations

- Two Dependabot moderate CVEs on `qs` were runtime-mitigated (bridge
  images pinned to qs 6.16.0). Verify Dependabot has auto-closed after
  next scan.
- TikTok Stories (Tier 1 ephemeral) is not implemented — TikTok has
  no supported story extractor in yt-dlp or gallery-dl and no public
  story API. Documented in `docs/contracts/COLLECTION_SPEC.md`.
- Instagram DMs and TikTok DMs depend on the host Chrome extension's
  DM-sample hook. If Chrome CDP is not on port 9336, DM hooks go
  stale (watchdog alerts via `PostgresIdleTxnAlertHandler` +
  `WatchdogStaleAlertHandler`).
- `browser-autorecover` scheduled task is registered under the current
  user's Startup folder (docker/env registration path was denied for
  elevated `Register-ScheduledTask`). Runs `AtLogOn`; won't reap Chrome
  on a locked screen without a logon event.
- Analyzer-repo `build_timeline` (in `C:\unifiedanalyzer\`) leaks
  idle-in-transaction sessions against this DB; mitigated by our
  pool-side `idle_in_transaction_session_timeout=5min`. Full fix
  requires an analyzer PR (out of this repo's scope).
- Cross-repo sync policy in `AGENTS.md` says `docs/` is force-cleaned
  in target repos. This project's `docs/` now contains real content;
  sync source must be updated to exempt.
- The dashboard SPA has one pre-existing test failure
  (`test_whatsapp_pairing_code_proxies_phone_to_unregistered_bridge`)
  that predates the audit work; still fails 2026-09-09.
- Some `scripts/*.py` helper scripts still contain non-canonical
  hostname/path references (documented as low-value drift).
