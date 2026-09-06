# Repository Map

## 1. Provenance
- Repository name / remote: `hongyime/unifiedcollector` — `https://github.com/hongyime/unifiedcollector.git`
- Commit analyzed: `a0f5ee56960ab4e07c52902bb37cd1e21cf3baf8`
- Branch: `main`
- Working tree: no modifications (0 modified, 0 untracked)
- PR data source: gh (authenticated as `bryanseah234`)
- Agent capability: shell
- Not analyzed: bytecode caches under `__pycache__/`, image binaries under `extension/icons/` and `dashboard/frontend/public/`, the `archive/` directory referenced in `README.md` but not tracked here, the two large sync/state markdowns (`2026-05-30-unifiedanalyzer-strategy.md`, `collector_audit.md`) beyond first pass. Deep-dive sections capped at MAX_MODULES_DEEP=10.

## 2. What this repository is
A Python-plus-TypeScript ingestion service that collects public and semi-public content from 11 named source platforms (github, youtube, strava, search, website, tiktok, lemon8, whatsapp, telegram, instagram, beeper/matrix) and writes them into one shared Postgres database (`pgvector/pgvector:pg16`, `docker/docker-compose.yml:5`). Collection runs entirely inside Docker Compose services defined in `docker/docker-compose.yml` (24 services declared). The primary artifacts written are rows in `media_items`, `social_users`, and per-source tables under `src/db/schemas/` and `src/db/migrations/`, along with binary media files placed under the mounted `z:/unifiedcollector` vault. A React 19 + Vite operations dashboard (`dashboard/frontend/`, backend at `src/dashboard/api.py:8700`) surfaces status. An enrichment pipeline (`docker/Dockerfile.spiderfoot`, `src/recon_spiderfoot_service.py`, `src/core/recon_spiderfoot.py`) processes queued OSINT targets via SpiderFoot, maigret, and GHunt when enabled under compose profile `recon`. There is no outbound-messaging capability in this repo.

## 3. Quick facts
| Field | Value |
|---|---|
| Primary language(s) | Python (301 files, `.py`), TypeScript/TSX (75 files: 54 `.tsx` + 21 `.ts`), SQL (136 files), JavaScript (6 files including `extension/`), PowerShell (15 files under `scripts/`) |
| Runtime / version constraint | Python 3.12 (`docker/Dockerfile:3` `FROM python:3.12-slim`; `pyproject.toml:16` `target-version = "py312"`). CI uses Python 3.12 (`.github/workflows/python-ci.yml:22`). Node 20 (`docker/Dockerfile:11` `nodesource setup_20.x`) for the WhatsApp bridge; `src/bridges/whatsapp/package.json` declares dev dep `typescript ^7.0.2`. Postgres 16 with pgvector (`docker/docker-compose.yml:5`). |
| Package manager | pip (`requirements.txt`, `requirements.lock`); npm (dashboard `dashboard/frontend/package.json`, whatsapp bridge `src/bridges/whatsapp/package.json`, both with `package-lock.json`) |
| Tracked files | 644 |
| Total lines (tracked) | 167,416 across text files inventoried |
| Deployment artifact | Docker Compose stack (`docker/docker-compose.yml`, 24 services); container images built from `docker/Dockerfile`, `docker/Dockerfile.dashboard`, `docker/Dockerfile.spiderfoot`, `docker/Dockerfile.backup`, `src/bridges/whatsapp/Dockerfile` |
| Persistence | Postgres 16 + `pgvector` (`docker/docker-compose.yml:5`), Redis 7 (`docker/docker-compose.yml:1240`), RabbitMQ 3.13-alpine (`docker/docker-compose.yml:1214`), filesystem vault at `z:/unifiedcollector` mounted read-write into most collectors |
| Test framework(s) | pytest with `pytest-asyncio` auto mode (`pyproject.toml:1-6`); `testpaths = ["tests"]` |
| CI | GitHub Actions — 15 workflows under `.github/workflows/` (see Section 12) |
| License | Apache-2.0 (`LICENSE`, `NOTICE` states `Copyright 2026 The Prawn Organisation`) |

## 4. How it runs
Entry points table:

| Entry point | Path | Trigger | What it starts |
|---|---|---|---|
| `python -m src.main worker --source X` / `--all` | `src/main.py:44` | Container `command:` in most compose collector services | Boot DB pool + migrations then run `WorkerService` (`src/worker/__init__.py:228`) for the named source(s) |
| `python -m src.main scheduler` | `src/main.py:56` | `docker/docker-compose.yml:948` (service `scheduler`) | `Scheduler` (`src/scheduler/__init__.py:182`) periodic reconciliation, recon auto-seed, phone-OSINT sweep |
| `python -m src.main run` | `src/main.py:59` | `_cmd_run` combines worker + scheduler for ad-hoc runs | Both `WorkerService` and `Scheduler` inside the same process |
| `python -m src.bridges.ig_ingest` | `src/bridges/ig_ingest.py:5921` | `docker/docker-compose.yml:911` service `ig_ingest`, port `8765` (`src/bridges/ig_ingest.py:83`) | `aiohttp` HTTP server with 35 route registrations (see Section 9) |
| `uvicorn src.dashboard.api:app --host 0.0.0.0 --port 8700` | `docker/Dockerfile.dashboard:8` | `docker/docker-compose.yml:1087` service `dashboard`, port 8700 (also mapped :8001) | FastAPI app with 103 HTTP route decorators + 1 websocket route (`src/dashboard/api.py`) |
| `python -m src.watchdog.freshness` | `src/watchdog/freshness.py:1` | `docker/docker-compose.yml:407` service `watchdog` | Freshness loop restarts stale realtime-source containers via Docker socket (`src/watchdog/freshness.py:348`) |
| `python -m src.notifications.realtime_feed` | `src/notifications/realtime_feed.py:1` | `docker/docker-compose.yml:1040` service `realtime_feed` | Drains Redis list `uc:realtime_post_feed` (`src/notifications/realtime_feed.py:57`) and posts to Telegram |
| `python -m src.recon_spiderfoot_service --workers N --poll-interval N` | `src/recon_spiderfoot_service.py:1` | `docker/docker-compose.yml:84` service `collector_spiderfoot` under profile `recon` | Recon worker consuming `recon_targets` |
| `python -m src.bots.onboard_bot` | `src/bots/onboard_bot.py` | `docker/docker-compose.yml:1009` service `onboard_bot` | Telegram onboarding bot |
| `python -m src.tools.browser_cookie_vault` | `src/tools/browser_cookie_vault.py` | `docker/docker-compose.yml:1336` service `browser_cookie_vault`, port 8790 (`src/tools/browser_cookie_vault.py`, README ref) | Chrome cookie snapshot loop |
| `python -m src.backup.db_backup run` | `src/backup/db_backup.py` | `docker/docker-compose.yml:1138` service `backup` | Daily pg_dump with atomic rename + retention (`src/backup/db_backup.py`) |
| `node build/index.js` (WhatsApp bridge) | `src/bridges/whatsapp/package.json:5` | `docker/docker-compose.yml:1254` and `:1293` services `wa-bridge-1` / `wa-bridge-2` | Baileys TypeScript bridge publishing to RabbitMQ (`src/bridges/whatsapp/src/index.ts:1200 lines`) |
| React dev/build | `dashboard/frontend/package.json` scripts `dev`, `build`, `preview` | Bundle served static from `dashboard/frontend/dist/` by dashboard image (`docker/Dockerfile.dashboard:6`) | React 19 SPA (`dashboard/frontend/src/App.tsx:1`) |
| Chrome MV3 extension | `extension/manifest.json`, `extension/background.js`, `extension/content.js` | Installed manually into the operator's Chrome | POSTs to `ig_ingest:8765` (routes in Section 9) |

Install and run (from `README.md` procedure, verified against `docker/docker-compose.yml`):
```
cp .env.example .env
docker compose -f docker/docker-compose.yml up -d              # core stack
docker compose -f docker/docker-compose.yml --profile recon up -d  # add recon
```
Required environment: `POSTGRES_USER`, `POSTGRES_PASSWORD`, and platform credentials keyed as documented in `.env.example` (~200 variable names — see Section 10). Ports bound on the host: `POSTGRES_HOST_PORT` default `5433` → container 5432 (`docker/docker-compose.yml:16`), `8765` for `ig_ingest`, `8700` for `dashboard`, `8790` for `browser_cookie_vault`, RabbitMQ port `5672` and management `15672` internal-only (`docker/docker-compose.yml:1214-1240` region).

## 5. Execution paths

### Flow A — Headless collector cycle (e.g. `--source youtube`)
1. `src/main.py:337` `_cmd_worker` — parses `--source`, calls `check_drive()` (`src/core/drive_check.py`), constructs the DB pool via `src/db/connection.py:_dsn`.
2. `src/main.py:38` `init_db` → `src/db/migrate.py:apply_all` applies base schemas under `src/db/schemas/*.sql` and any pending migrations under `src/db/migrations/*.sql`, gated by advisory lock `pg_try_advisory_lock(hashtext('unifiedcollector_migrate'))` (`src/db/migrate.py:79`) and tracked in the `schema_migrations` ledger (`src/db/migrate.py:57`).
3. `src/worker/__init__.py:1297` `run_worker` → constructs `WorkerService` (`src/worker/__init__.py:228`); the class attaches `_FatalSpinLogWatcher` (`src/worker/__init__.py:29`) to the root logger for known-fatal log-flood patterns that trigger `os._exit(42)`.
4. `WorkerService` resolves the collector via `src/collectors/__init__.py:36` `get_collector(source)` returning an instance of the concrete class (e.g. `YoutubeCollector` in `src/collectors/youtube/__init__.py`); base contract in `src/core/base_collector.py:BaseCollector`, `INGEST_PATH` class attribute default `"headless"` (`src/core/base_collector.py:80`).
5. Collector produces media rows via `src/core/base_collector.py:insert_media_item` which stamps `source`, `content_id`, `sha256`, `source_url` from a `_build_<source>_source_url` static method, and `ingest_path` from the class attribute.
6. On successful insert, `src/core/base_collector.py:843` calls `src/notifications/realtime_feed.enqueue_from_insert` best-effort — pushing onto Redis list `uc:realtime_post_feed` (`src/notifications/realtime_feed.py:57`).
7. `realtime_feed` service (Flow B) consumes the queue and posts to Telegram.

### Flow B — Realtime post feed drain
1. `src/notifications/realtime_feed.py` module docstring at `:1` describes the token-bucket rate limit and dedupe design.
2. Service entrypoint declared at `docker/docker-compose.yml:1040`; command runs `python -m src.notifications.realtime_feed`.
3. Reads Redis config from env (`REDIS_URL` / `REDIS_HOST` / `REDIS_PASSWORD`), pops JSON payloads from `uc:realtime_post_feed`, applies dedupe keys `uc:realtime_post_feed:seen_sha` (`src/notifications/realtime_feed.py:58`), rate-limits per-minute via `REALTIME_POST_FEED_MAX_PER_MINUTE` (`.env.example:230-244`).
4. Emits `sendPhoto`/`sendVideo` HTTP calls to Telegram API using `NOTIFY_TELEGRAM_BOT_TOKEN` and `NOTIFY_TELEGRAM_CHAT_ID` (`.env.example:194-222`).
5. On rate cap, defers into `uc:realtime_post_feed:skipped_burst` (`src/notifications/realtime_feed.py:60`) and emits a 15-minute burst summary when `REALTIME_POST_FEED_BURST_SUMMARY=1`.

### Flow C — Browser extension → `ig_ingest` bridge
1. `extension/content.js` (4,062 lines) and `extension/background.js` (2,978 lines) — Chrome MV3 content-script/service-worker pair; installed manually per `extension/README.md`.
2. Extension POSTs batches to `http://<host>:8765/social/ingest` and related routes registered in `src/bridges/ig_ingest.py:5880-5915`.
3. `src/bridges/ig_ingest.py:5921` `if __name__ == "__main__"` block calls `aiohttp.web.run_app(app, host="0.0.0.0", port=PORT)` where `PORT = int(os.getenv("IG_INGEST_PORT", "8765"))` (`src/bridges/ig_ingest.py:83`).
4. Handlers write into `media_items`, `social_users`, `browser_ingest_events`, and other browser tables (migrations `src/db/migrations/add_browser_ingest_events.sql`, `add_browser_media_candidates.sql`, `add_social_users.sql`, etc.).
5. Successful writes call `src/bridges/ig_ingest.py:2362` `realtime_feed.enqueue_from_insert` reusing Flow B.
6. Anti-ban cooldown state served on `GET /social/ig_cooldown` (`src/bridges/ig_ingest.py:5881`) coordinates the headless Instagram collector with the extension.

## 6. Architecture
Directional dependencies read from `src/` imports:

```
src/main.py
  ├── src.core.drive_check
  ├── src.db.connection ──► asyncpg
  ├── src.db.migrate    ──► src/db/schemas + src/db/migrations
  ├── src.core.maintenance
  ├── src.worker         ──► src.collectors (COLLECTORS registry)
  └── src.scheduler

src/worker/__init__.py
  ├── src.collectors.get_collector
  ├── src.core.collection_priority
  ├── src.core.drive_check
  ├── src.core.priority_hints
  ├── src.core.proximity
  └── src.db.connection

src/collectors/*/__init__.py   (13 sources incl. instagram_dm, exposure)
  ├── src.core.base_collector          ── central template method
  ├── src.core.vault                    ── file writes, sidecars
  ├── src.core.media_download           ── HTTP + subprocess download
  ├── src.core.dedupe_hash              ── content-hash dedup
  ├── src.core.file_naming              ── canonical filename
  ├── src.core.subprocess_downloader   ── bounded yt-dlp / gallery-dl
  ├── src.core.human_rate_limiter / rate_limit / adaptive_rate
  ├── src.core.account_pool / account_quota / auth_session
  ├── src.core.spider_discover / seen_targets
  └── src.notifications.realtime_feed  (enqueue best-effort)

src/dashboard/api.py           ── FastAPI, imports src/db/connection and src/core/*
src/bridges/ig_ingest.py       ── aiohttp, imports src/db/connection, src/core/vault, src/notifications/realtime_feed
src/bridges/whatsapp/          ── standalone Node/TypeScript service; RabbitMQ producer
src/notifications/{realtime_feed,alerts,status,telegram}.py
src/watchdog/freshness.py      ── DB-only reader; posts to Docker socket
src/recon_spiderfoot_service.py ── src/core/recon_spiderfoot, src/core/ghunt_enrich
src/backup/db_backup.py        ── pg_dump wrapper
```

Observable pattern: dockerised **plugin-style** collectors (`src/collectors/*/__init__.py`) all sharing a template base (`src/core/base_collector.py`) and a common vault/persistence layer (`src/core/vault.py`, `src/db/`). Cross-cutting concerns (rate-limiting, session repair, deduplication) live under `src/core/`. Message passing is out-of-band via Postgres tables (`recon_targets`, `dead_letter_queue`, `collection_action_queue`), Redis lists (`uc:realtime_post_feed`), and RabbitMQ exchange `whatsapp.events` (`src/bridges/whatsapp/package.json:3` description). The dashboard, ig_ingest bridge, and realtime feed are peers reading and writing the same Postgres.

Boundary: transport (`src/bridges/ig_ingest.py`, `src/dashboard/api.py`), business logic (`src/collectors/`, `src/core/`), persistence (`src/db/`, `src/core/vault.py`).

## 7. File inventory
Depth-capped directory summary (Python-first, then bridges, dashboard, config, tests):

| Directory | Files | One-line purpose |
|---|---|---|
| `.` | 25 | Top-level manifests, README, license, several audit markdowns, `.env.example`, `pyproject.toml`, `requirements.txt`, `requirements.lock` |
| `.agents/` | 2 | `STATE.md`, `JOURNAL.md` handover for multi-agent workflows (see `AGENTS.md`) |
| `.github/workflows/` | 15 | CI/CD workflows (list in Section 12) |
| `.github/` | 5 non-workflow | Dependabot, labels, funding, ISSUE_TEMPLATE (bug/feature), PR template |
| `config/` | 20 | `seed_sg_schools.sql`, `sg_schools.csv`, and per-source `.targets` / `.env` / `.dorks` files under `config/sources/` |
| `dashboard/frontend/` | 40 | React 19 + Vite SPA; `App.tsx` router with 34 routes (`dashboard/frontend/src/App.tsx:58`) |
| `docker/` | 6 | Compose file (`docker-compose.yml` 1,345 lines), five Dockerfiles, `postgres/postgres.conf`, `rabbitmq.conf`, 3 patch scripts under `docker/patches/` |
| `docs/` | 1 | `enrichment.md` (referenced by README `# 91ff90c6 docs: full README rewrite + enrichment.md`) |
| `extension/` | 10 | Chrome MV3 extension source: `manifest.json`, `background.js` (2,978 LOC), `content.js` (4,062 LOC), `inject.js`, `popup.html/js`, `tabs.html/js`, `platforms.js`, `README.md`, `icons/{16,48,128}.png` |
| `models/dlib/` | 2 | `.gitkeep` and `README.md` — model files not committed |
| `research/` | 1 | `browser-cdp-cookie-comparison.md` |
| `scripts/` | 51 | Windows PowerShell startup/maintenance (`.ps1`), Python maintenance tools, `hooks/pre-commit`, `run_hidden.vbs`, `beeper_openapi.json` (775 KB), `_beeper_msgs.json` |
| `src/` | 5 top-level Python entrypoints incl. `main.py`, `recon_seed_service.py`, `recon_spiderfoot_service.py`, `recon_maigret_fp_refresh.py` | Core Python package |
| `src/backup/` | 3 | `db_backup.py`, `restore_drill.py`, `__init__.py` |
| `src/bots/` | 2 | `onboard_bot.py`, `__init__.py` |
| `src/bridges/` | 2 (Python) + WhatsApp bridge subtree | `ig_ingest.py` (5,921 LOC aiohttp), plus `whatsapp/` Node bridge (7 TS files under `src/`, `package.json`, `Dockerfile`) |
| `src/bridges/whatsapp/src/` | 5 top + 4 under `event_handlers/` | `index.ts`, `auth_manager.ts`, `device_intel.ts`, `producer.ts`, `store.ts`, `utils/normalize.ts`, `event_handlers/{contacts,groups,history,messages}.ts` |
| `src/collectors/` | 3 top + 13 per-source subdirs | `__init__.py` (COLLECTORS registry, 13 entries), `base.py` (81-byte stub), `MATRIX_README.md`, subdirs: `beeper`, `exposure`, `github`, `instagram`, `instagram_dm`, `lemon8`, `search`, `strava`, `telegram`, `tiktok`, `website`, `whatsapp`, `youtube` (each `__init__.py` implements the source, some also carry a `parse.py`) |
| `src/core/` | 84 modules | Cross-cutting: base collector, vault, rate limiters, account pools, dedup, recon, phone-OSINT, watchdog helpers — see Section 8 |
| `src/dashboard/` | 3 | `api.py` (10,784 LOC), `websocket.py`, `__init__.py` |
| `src/db/` | 3 top + `schemas/` + `migrations/` | `connection.py`, `migrate.py`, `__init__.py` |
| `src/db/schemas/` | 15 files (14 `.sql` + 1 empty `__init__.py`) | Base schemas: `collector.sql`, `github.sql`, `instagram.sql`, `lemon8.sql`, `strava.sql`, `telegram.sql`, `tiktok.sql`, `whatsapp.sql`, `website.sql`, `youtube.sql`, `exposure.sql`, `face_recognition.sql`, `account_state.sql`, `profile_access.sql` |
| `src/db/migrations/` | 122 files (121 `.sql` + `.gitkeep`) | Incremental migrations tracked in `schema_migrations` ledger; skiplist in `src/db/migrate.py:47` (`v2_schema.sql`, `v2_schema_final.sql`, `drop_wa_face_tables.sql`) |
| `src/migrations/` | 2 | Legacy `add_content_hashes_table.py`, `__init__.py` (2 entries; not the main migration path) |
| `src/notifications/` | 7 | `alerts.py`, `collector_bot.py`, `realtime_backfill.py`, `realtime_delivery.py`, `realtime_feed.py`, `telegram.py`, `__init__.py` |
| `src/scheduler/` | 1 + `__init__.py` | `__init__.py` (91,375 bytes) contains `Scheduler` class |
| `src/tools/` | 2 | `browser_cookie_vault.py`, `__init__.py` |
| `src/watchdog/` | 2 | `freshness.py` (45,998 bytes), `__init__.py` |
| `src/worker/` | 1 + `__init__.py` | `__init__.py` contains `WorkerService`, `_FatalSpinLogWatcher` |
| `sessions/`, `credentials/`, `backups/`, `media/`, `sidecars/`, `data/`, `models/`, `tmp/` | placeholders / mount points | Not tracked (excluded via `.gitignore`) |
| `tests/` | 15 top + 8 subdirs (110 files total) | pytest suite mirrored to source layout — see Section 12 |
| `tools/` | 6 | `browser_tab_audit.py`, `browser_tab_reload.py`, `optional_rollout_monitor.py`, `telegram_login.py`, `telegram_relogin.py`, `TELEGRAM_LOGIN_README.md` |

Total tracked files: 644. Full sorted inventory retrieved via `git ls-files | sort` — the top-40 directories by file count are documented above and account for the observed distribution. Truncated below MAX_FILES_LISTED for the migration list; the complete migration set is enumerable in-tree at `src/db/migrations/`. Generated files: `dashboard/frontend/package-lock.json`, `src/bridges/whatsapp/package-lock.json` (both large lockfiles, tracked). Vendored files: none in-tree — Python deps installed at image build, JS deps installed at image build. Untracked-but-not-ignored files: none observed.

## 8. Key modules in depth

### `src/collectors/base.py` and `src/core/base_collector.py`
- Responsibility: template method for every source-specific collector; declares `INGEST_PATH` provenance tag (default `"headless"`, `src/core/base_collector.py:80`), owns `insert_media_item`, DB consistency verification (`verify_media_item_db_consistency` imported from `src/core/vault.py`), and drops best-effort into the realtime feed (`src/core/base_collector.py:843`).
- Key files and symbols: `src/core/base_collector.py` (40,382 bytes) — imports `.checkpoint.CheckpointManager`, `.drive_check.check_drive/DRIVE_PATH`, `.file_naming.build_filename`, `.rate_limiter.AdaptiveRateLimiter`, `.resilience.CircuitBreaker`, `.scrape_pacing.sleep_rate_limit`, `.user_agent.UserAgentPool`, `.vault.{VAULT_ROOT, assert_media_write_allowed, write_media_sidecar, write_atomic_artifact}`.
- Depends on: `src/core/vault.py`, `src/core/checkpoint.py`, `src/core/rate_limiter.py`, `src/core/resilience.py`, `src/notifications/realtime_feed.py`.
- Depended on by: every `src/collectors/*/__init__.py` (13 subclasses via `src/collectors/__init__.py:16`).
- Notable behavior: canonical-vault-blob detection at `_is_canonical_vault_blob_path` (`src/core/base_collector.py:44`), duplicate-file cleanup (`_unlink_duplicate_media_file`), DB consistency timeout keyed on `COLLECTOR_MEDIA_DB_CONSISTENCY_TIMEOUT_SECONDS`.
- Tests covering it: `tests/core/test_base_collector.py` (17,158 bytes), `tests/core/test_vault.py`, `tests/core/test_file_naming.py` (indirect via `tests/collectors/*`).

### `src/collectors/` (per-source implementations)
- Responsibility: source-specific scraping, session mgmt, backfill loops. Each `<source>/__init__.py` defines a subclass registered in `src/collectors/__init__.py:16`.
- Key files (by size, all under `src/collectors/*/__init__.py`): `telegram` 267,148 bytes, `instagram` 213,183 bytes, `strava` 215,077 bytes, `youtube` 178,142 bytes, `lemon8` 125,221 bytes, `tiktok` 115,581 bytes, `github` 112,661 bytes, `whatsapp` 86,508 bytes, `search` 84,078 bytes, `website` 83,371 bytes, `beeper` 77,688 bytes, `exposure` 19,754 bytes, `instagram_dm` 9,887 bytes (with additional module split: `auth.py`, `credentials.py`, `device.py`, `mqtt_client.py`, `session.py`).
- Depends on: `src/core/base_collector.py`, `src/core/media_download.py`, `src/core/vault.py`, third-party libs (yt-dlp, gallery-dl, instaloader, telethon, matrix-nio) per `requirements.txt`.
- Depended on by: `src/worker/__init__.py:WorkerService` via `get_collector()`; also imported by tests under `tests/collectors/`.
- Notable behavior: `COLLECTOR_DISABLED_SOURCES` env in `src/collectors/__init__.py:40` list-filters active sources; the main `collector` container disables all sources via `docker/docker-compose.yml:46` and each source runs in its own isolated container.
- Tests: `tests/collectors/test_<source>.py` and `test_<source>_parse.py` for every source with parse logic.

### `src/core/vault.py`
- Responsibility: file writes to the shared vault, per-artifact sidecars, atomic write, canonical blob path checks, media DB consistency verification.
- Key symbols: `VAULT_ROOT`, `assert_media_write_allowed`, `verify_media_item_db_consistency`, `write_media_sidecar`, `write_artifact_sidecar`, `write_atomic_artifact` (48,732 bytes at `src/core/vault.py`).
- Depends on: stdlib, `src/core/env.py`, `src/core/file_naming.py`.
- Depended on by: `src/core/base_collector.py`, `src/core/media_sidecar_repair.py`, `src/bridges/ig_ingest.py`, `src/dashboard/api.py`.
- Tests: `tests/core/test_vault.py`, `tests/core/test_vault_inspect.py`, `tests/core/test_media_sidecar_repair.py`.

### `src/worker/__init__.py`
- Responsibility: process-level supervisor for one or more collector instances; installs `_FatalSpinLogWatcher`, `_RecoverableTelethonWarningFilter`.
- Key symbols: `WorkerService` (`:228`), `_FatalSpinLogWatcher` (`:29`), `run_worker` (`:1297`), `get_worker_health` (`:1303`).
- Depends on: `src.collectors.get_collector`, `src.core.collection_priority`, `src.core.priority_hints`, `src.core.proximity`, `src.db.connection`.
- Depended on by: `src/main.py:_cmd_worker`.
- Notable behavior: hard-exit `os._exit(42)` on fatal-log flood (see class docstring at `:32`). Cycle-sleep envs `COLLECTOR_CYCLE_SLEEP_<SOURCE>` (`docker/docker-compose.yml:353-355`). Watchdog spin persistence into `source_health` table.
- Tests: `tests/test_worker_fatal_spin.py`, `tests/test_worker_cycle_sleep.py`, `tests/test_worker_health_report.py`, `tests/test_worker_target_priority_refresh.py`.

### `src/scheduler/__init__.py`
- Responsibility: periodic reconciliation ticks; owns `Scheduler` class.
- Key symbols: `Scheduler` (`:182`), `run_scheduler` (`:1823`).
- Depends on: `src.db.connection`, `src.core.source_freshness`.
- Depended on by: `src/main.py:_cmd_scheduler`, `_cmd_run`.
- Notable behavior: internal ticks documented in `README.md` include recon auto-seed (~6h) and phone-OSINT sweep (~12h); browser maintenance status at `_browser_maintenance_status` (`:32`).
- Tests: `tests/test_scheduler_graph_edges.py`.

### `src/bridges/ig_ingest.py`
- Responsibility: aiohttp bridge receiving browser-extension batches; 35 route registrations at `:5880-5915`.
- Key routes: `/social/ingest`, `/social/ingest-upload`, `/social/ingest-upload-binary`, `/social/discover`, `/social/target-status`, `/social/posts`, `/social/comments`, `/social/users`, `/social/profile`, `/social/seed`, `/social/dms`, `/social/cookies`, `/social/browser-media-candidates`, `/social/ig_cooldown` (GET), `/social/browser-heartbeat`, `/social/tiktok-revisit-target`, `/social/tiktok-revisit-result`, `/social/browser-revisit-target`, `/social/browser-revisit-result`, `/social/strava-route-queue`, `/social/strava-route-visit`, `/social/strava-streams`, `/social/x-profile-target`, `/social/sw-crash`, `/ig/targets`, `/ig/ingest`, `/ig/discover`, `/health`.
- Depends on: aiohttp, `src.db.connection`, `src.core.vault`, `src.notifications.realtime_feed`.
- Depended on by: Chrome extension (`extension/background.js`, `extension/content.js`).
- Notable behavior: port default `8765` (`:83`); calls `enqueue_from_insert` at `:2362`.
- Tests: `tests/bridges/test_ig_ingest_vault.py` (75,746 bytes).

### `src/dashboard/api.py`
- Responsibility: FastAPI operations dashboard; 103 HTTP route decorators + 1 websocket route (`src/dashboard/websocket.py`, 5,153 bytes).
- Endpoint samples (each from `^@app\.(get|post)`): `/health` (`:4300`), `/metrics` (`:4472`), `/collectors` (`:4888`), `/collectors/live` (`:4899`), `/collectors/source-matrix` (`:5083`), `/collectors/action-queue/sync` (`:5212`), `/media` (`:6124`), `/media/stats` (`:6145`), `/media/realtime-feed/status` (`:6349`), `/media/realtime-feed/deliveries` (`:6359`), `/instagram/health` (`:6428`), `/media/artifact-audit` (`:6792`), `/ingestion/hourly` (`:6814`), `/rate-limits/recent` (`:6961`), `/domain-pacing/status` (`:7067`), `/api-quotas/status` (`:7232`), `/social/stats` (`:7477`), `/social/network` (`:7498`), `/social/users` (`:7523`), `/social/scrape-config` (`:7544`), `/dlq` (`:7608`), `/auth/login` (`:7646`), `/auth/me` (`:7691`), `/targets` (`:7737`), and 79 additional routes.
- Depends on: FastAPI, `src.db.connection`, many `src.core.*` reads.
- Depended on by: `dashboard/frontend/src/services/api.ts` (React SPA client, 16,747 bytes).
- Notable behavior: `DASHBOARD_JWT_SECRET`, `DASHBOARD_ADMIN_USERNAME`, `DASHBOARD_ADMIN_PASSWORD` for auth (`.env.example:378-380`); `_SOURCE_MATRIX_*` env-tuned timeouts (`src/dashboard/api.py:54-92`).
- Tests: `tests/dashboard/*.py` — 11 files including `test_source_matrix.py` (88,927 bytes), `test_extension_health.py` (58,077 bytes), `test_coverage_api.py`.

### `src/db/connection.py` and `src/db/migrate.py`
- Responsibility: asyncpg pool factory with retry (`src/db/connection.py:78`), SSL context builder, migration runner with ledger + advisory lock.
- Key symbols: `get_pool()` (`src/db/connection.py:107`), `close_pool()` (`:133`), `apply_all(pool)` (`src/db/migrate.py:63`).
- Notable behavior: per-container pool sizing via `DB_POOL_MIN_SIZE`/`DB_POOL_MAX_SIZE` (`src/db/connection.py:120-126`) tuned to stay under `max_connections=200`; migration lock timeout via `MIGRATE_LOCK_TIMEOUT_MS` (`src/db/migrate.py:100`); ledger table `schema_migrations` with `filename PRIMARY KEY, checksum, applied_at`.
- Depends on: asyncpg, stdlib.
- Depended on by: every process entry point (worker, scheduler, dashboard, ig_ingest, notifications, watchdog).
- Tests: `tests/core/test_db_connection.py`, `tests/core/test_db_migrate.py`, `tests/verify_clean_boot.py` (used by CI `.github/workflows/python-ci.yml:47` `schema-boot`).

### `src/watchdog/freshness.py`
- Responsibility: freshness safety net; monitors newest `media_items` row per realtime source (telegram/whatsapp/beeper) and restarts the responsible container via Docker socket when stale.
- Key behavior: `INTERVAL` default 300s, `COOLDOWN` default 1800s (`:22-24`); `REALTIME_SOURCES = {"telegram","whatsapp","beeper"}` (`:73`); restart via `POST http://docker/containers/<name>/restart?t=15` over UDS (`:348`); heartbeat file at `WATCHDOG_HEARTBEAT_FILE` for its own container healthcheck (`:19-30`).
- Depends on: aiohttp UDS, asyncpg, `src.core.source_freshness.GITHUB_PROGRESS_QUERY`, `STRAVA_PROGRESS_QUERY`.
- Depended on by: nothing else — invoked directly by `docker/docker-compose.yml:407`.
- Tests: `tests/test_watchdog_freshness.py` (30,663 bytes), `tests/test_watchdog_autoheal.py`, `tests/watchdog/test_browser_source_pause.py`.

### `src/notifications/realtime_feed.py`
- Responsibility: Redis-backed Telegram post feed.
- Key symbols: constants `QUEUE_KEY_DEFAULT="uc:realtime_post_feed"`, `SEEN_SHA_KEY_DEFAULT`, `DEFERRED_KEY_DEFAULT`, `FAILED_KEY_DEFAULT` (`:57-63`); `ALLOWED_KINDS = {"image","video","post","photo"}` (`:73`); `GLOBAL_MEDIA_DEDUPE_SOURCES_DEFAULT` covering instagram/threads/tiktok/lemon8/facebook/x/twitter (`:74`).
- Depends on: `redis`, `httpx`/`aiohttp`, `src.db.connection`.
- Depended on by: `src/core/base_collector.py`, `src/bridges/ig_ingest.py`, both call `enqueue_from_insert` best-effort.
- Notable behavior: token-bucket via `REALTIME_POST_FEED_MAX_PER_MINUTE`, sha256/URL dedupe TTL `REALTIME_POST_FEED_DEDUPE_TTL_DAYS`, 15-min burst summary.
- Tests: `tests/notifications/test_realtime_feed.py` (56,703 bytes).

### `src/core/recon_spiderfoot.py` + `src/recon_spiderfoot_service.py`
- Responsibility: recon worker. Consumes `recon_targets` rows and shells out to `maigret`, SpiderFoot `sf.py`, or GHunt.
- Key files: `src/core/recon_spiderfoot.py` (50,000 bytes), `src/recon_spiderfoot_service.py` (7,269 bytes), `src/recon_seed_service.py` (2,384 bytes), `src/recon_maigret_fp_refresh.py` (3,280 bytes), `src/core/recon_seed.py`, `src/core/ghunt_enrich.py`, `src/core/recon.py`.
- Depends on: `RECON_ALLOWLIST`, `RECON_ALLOW_UNSCOPED`, `RECON_USERNAME_ENGINE`, `MAIGRET_*`, `GHUNT_*`, `SPIDERFOOT_*` env vars (see `docker/docker-compose.yml:96-134`).
- Depended on by: `docker/docker-compose.yml:84` service `collector_spiderfoot` under compose profile `recon`.
- Notable behavior: two workers, poll interval default 10s (`docker/docker-compose.yml:88`); stale-target reclaim (`SPIDERFOOT_STALE_TARGET_MINUTES` default 30, `.env.example:427`). Advisory-locked FP-refresh loop (`src/recon_maigret_fp_refresh.py`).
- Tests: `tests/test_recon.py` (19,086 bytes), `tests/test_recon_spiderfoot_service.py`, `tests/dashboard/test_recon_api.py`.

## 9. External interfaces

### HTTP endpoints — Dashboard (`src/dashboard/api.py`, FastAPI, port 8700)
103 routes registered via `@app.get|post|put|delete|patch`. Selected surface (sorted by path):

| Method | Path | Handler line | Notes |
|---|---|---|---|
| GET | `/accounts` | `:6032` | |
| POST | `/auth/login` | `:7646` | JWT via `DASHBOARD_JWT_SECRET` |
| GET | `/auth/me` | `:7691` | |
| GET | `/api/backfill-equilibrium` | `:4719` | |
| GET | `/collectors` | `:4888` | |
| GET | `/collectors/action-queue` | `:5293` | |
| POST | `/collectors/action-queue/sync` | `:5212` | |
| GET | `/collectors/live` | `:4899` | |
| GET | `/collectors/source-matrix` | `:5083` | |
| GET | `/dlq` | `:7608` | |
| GET | `/domain-pacing/status` | `:7067` | |
| GET | `/health` | `:4300` | |
| GET | `/ingestion/hourly` | `:6814` | |
| GET | `/instagram/health` | `:6428` | |
| GET | `/media` | `:6124` | |
| GET | `/media/artifact-audit` | `:6792` | |
| GET | `/media/realtime-feed/deliveries` | `:6359` | |
| GET | `/media/realtime-feed/status` | `:6349` | |
| GET | `/media/stats` | `:6145` | |
| GET | `/metrics` | `:4472` | |
| GET | `/platform/{name}/summary` | `:5751` | Path parameter |
| GET | `/rate-limits/recent` | `:6961` | |
| GET | `/social/follow-edges/stats` | `:5957` | |
| GET | `/social/network` | `:7498` | |
| GET | `/social/scrape-config` | `:7544` | |
| GET | `/social/stats` | `:7477` | |
| GET | `/social/users` | `:7523` | |
| POST | `/targets` | `:7737` | |
| GET | `/api-quotas/status` | `:7232` | |
| ... | (74 more) | | Full set enumerable via `grep -n '^@app\.' src/dashboard/api.py` |

Websocket: 1 route in `src/dashboard/api.py` (`@app.websocket`), plus supporting module `src/dashboard/websocket.py:5153 bytes`.

### HTTP endpoints — ig_ingest (`src/bridges/ig_ingest.py`, aiohttp, port 8765)
35 routes registered at `:5880-5915`. All under `/social/*` or `/ig/*`, plus `/health`.

| Method | Path | Handler | Line |
|---|---|---|---|
| GET | `/social/targets` | `get_targets` | 5880 |
| GET | `/social/ig_cooldown` | `ig_cooldown` | 5881 |
| POST | `/social/ingest` | `ingest` | 5882 |
| POST | `/social/ingest-upload` | `ingest_upload` | 5883 |
| POST | `/social/ingest-upload-binary` | `ingest_upload_binary` | 5884 |
| POST | `/social/browser-media-candidates` | `browser_media_candidates` | 5885 |
| POST | `/social/discover` | `discover` | 5886 |
| POST | `/social/target-status` | `target_status_handler` | 5887 |
| POST | `/social/posts` | `posts_handler` | 5888 |
| POST | `/social/comments` | `comments_handler` | 5889 |
| POST | `/social/users` | `users_handler` | 5890 |
| POST | `/social/profile` | `profile_handler` | 5891 |
| POST | `/social/seed` | `seed_handler` | 5892 |
| POST | `/social/dms` | `dms_handler` | 5893 |
| POST | `/social/cookies` | `cookies_handler` | 5894 |
| POST | `/social/dm-frame` | `dm_frame_handler` | 5895 |
| POST | `/social/dm-sample` | `dm_sample_handler` | 5896 |
| POST | `/social/dm-probe` | `dm_probe_handler` | 5897 |
| POST | `/social/dm-heartbeat` | `dm_hook_heartbeat_handler` | 5898 |
| POST | `/social/dm-decoded` | `dm_decoded_handler` | 5899 |
| GET | `/social/x-profile-target` | `x_profile_target_next` | 5900 |
| POST | `/social/x-profile-target-result` | `x_profile_target_result` | 5901 |
| GET | `/social/browser-revisit-target` | `browser_revisit_target` | 5902 |
| POST | `/social/browser-revisit-result` | `browser_revisit_result` | 5903 |
| GET | `/social/tiktok-revisit-target` | `tiktok_revisit_target` | 5904 |
| POST | `/social/tiktok-revisit-result` | `tiktok_revisit_result` | 5905 |
| GET | `/social/strava-route-queue` | `strava_route_queue_handler` | 5906 |
| POST | `/social/strava-route-visit` | `strava_route_visit_handler` | 5907 |
| POST | `/social/strava-streams` | `strava_streams_handler` | 5908 |
| POST | `/social/browser-heartbeat` | `browser_heartbeat_handler` | 5909 |
| POST | `/social/sw-crash` | `sw_crash_handler` | 5910 |
| GET | `/ig/targets` | `get_targets_ig` | 5912 |
| POST | `/ig/ingest` | `ingest_ig` | 5913 |
| POST | `/ig/discover` | `discover_ig` | 5914 |
| GET | `/health` | `health` | 5915 |

### CLI subcommands — `python -m src.main`
Argparse subparsers defined in `src/main.py:44`: `worker`, `scheduler`, `run`, `list`, `status`, `coverage-snapshot`, `recon-queue`, `recon-spiderfoot`, `recon-seed`, `optional-rollout`, `realtime-media-backfill`, `rebuild-report`, `rebuild-rehearsal`, `vault-inspect`, `repair-media-sidecars`, `repair-media-file-paths`, `recover-missing-media-files`, `media-artifact-audit`, `backfill-discovered-links`, `restore-drill`, `schedule`, `target`.

### Message queues and external services
- RabbitMQ topic exchange `whatsapp.events` published by `src/bridges/whatsapp/src/producer.ts` (description in `src/bridges/whatsapp/package.json:3`), consumed by `src/collectors/whatsapp/__init__.py` (via `aio-pika`, `requirements.txt:29`).
- Redis list `uc:realtime_post_feed` and companion keys (`uc:realtime_post_feed:seen_sha`, `uc:realtime_post_feed:skipped_burst`, `uc:realtime_post_feed:failed`, `uc:realtime_post_feed:local_fallback_*`, `uc:realtime_post_feed:source_counters_*`) — full list at `src/notifications/realtime_feed.py:57-72`.
- Docker Engine API over UDS at `/var/run/docker.sock` (`src/watchdog/freshness.py:25`) — used only by `watchdog` to restart containers.
- Telegram Bot API — outbound HTTP by `src/notifications/realtime_feed.py`, `src/notifications/alerts.py`, `src/notifications/telegram.py`; tokens named `NOTIFY_TELEGRAM_BOT_TOKEN`, `PRAWNPRODUCTIONS_BOT_TOKEN`, `SHOTSBYSEAH_BOT_TOKEN`, `BRYANSEAH_BOT_TOKEN` (`.env.example:194-421`).
- Third-party APIs invoked by collectors (per `requirements.txt` and per-collector imports): YouTube Data API (`google-auth`, `google-auth-oauthlib`), Telegram MTProto (`telethon`), Instagram (`instaloader`), Matrix (`matrix-nio[e2e]`), DuckDuckGo (`ddgs`), yt-dlp/gallery-dl subprocess.

### Public API surface
This repository is not a library — no `pyproject.toml` `[project]` metadata is defined (`pyproject.toml` only carries `[tool.pytest]` and `[tool.ruff]`), no `setup.py` / `setup.cfg` / `PEP 517 project` declaration is present, no `__all__` exports at the top level, no published package name. External surface = HTTP endpoints + CLI.

## 10. Data and configuration

### Schemas — 14 base tables under `src/db/schemas/*.sql`
- `collector.sql` — `media_items` (columns `id`, `source`, `entity_id`, `entity_name`, `content_type`, `content_id`, `filename`, `file_path`, `file_size`, `width`, `height`, `sha256`, `collected_at`, `source_url`, `metadata`, `created_at`; unique index `(source, content_id)` at `src/db/schemas/collector.sql:26`), `service_cursors`, `dead_letter_queue` with `status` / `next_retry_at`, `collection_targets`, `account_proximity_cache`, `collection_runs`, `discovered_links`, `collection_schedules`, `dashboard_users`.
- Per-source schemas: `github.sql`, `instagram.sql`, `lemon8.sql`, `strava.sql`, `telegram.sql`, `tiktok.sql`, `whatsapp.sql`, `website.sql`, `youtube.sql`, `exposure.sql`.
- Cross-cutting: `account_state.sql`, `face_recognition.sql`, `profile_access.sql`.
- The unique `sha256` index is added incrementally by `src/db/migrations/add_media_sha256_unique.sql`.

### Migrations — 121 SQL files under `src/db/migrations/`
Named either with a date prefix (e.g. `20260813_add_domain_pacing_events.sql`) or descriptively (e.g. `add_x_targets_and_edges.sql`). Applied by `src/db/migrate.py:63 apply_all` in sorted order, tracked in the `schema_migrations` ledger, with `SKIP = {"v2_schema.sql","v2_schema_final.sql","drop_wa_face_tables.sql"}` (`src/db/migrate.py:47`).

Themes visible in file names:
- Realtime messaging schema (`add_telegram_phase1.sql`, `add_beeper_shadow_tables.sql`, `add_matrix_*.sql`, `add_whatsapp_lid_map.sql`)
- Recon (`20260810_add_coverage_and_recon.sql`, `20260811_fix_recon_observation_value_hash.sql`, `20260821_add_collection_action_queue.sql`)
- Phone/device OSINT (`20260901_add_wa_device_intel.sql`, `20260902_add_wa_phone_intel.sql`)
- Browser bridge (`add_browser_ingest_events.sql`, `add_browser_media_candidates.sql`, `20260731_add_browser_media_revisit_queue.sql`)
- Cross-platform (`add_social_users.sql`, `add_follow_edges.sql`, `add_graph_edges_table.sql`)
- Indexes and hot-path performance fixes (numerous `add_*_index.sql`)

`20260810_add_coverage_and_recon.sql` adds `collection_coverage_snapshots`, `recon_targets`, `recon_observations`, `recon_edges` (`src/db/migrations/20260810_add_coverage_and_recon.sql:1-60`).

### Configuration files
- `.env.example` — 500 lines, references ~200 env variable NAMES organised by section (Database, Storage, DB Backups, GitHub, Instagram, Telegram, YouTube, Strava, TikTok, Lemon8, Search, Website, WhatsApp, Postgres SSL, Face Models, Dashboard, Recon/SpiderFoot, and more).
- `config/sources/` — 19 per-source overlays: `<source>.env` and `<source>.targets` files for github, instagram, lemon8, strava, telegram, tiktok, website, youtube; plus `beeper.targets`, `exposure.dorks`, `exposure.targets`, `search.targets`, `website.url-policy.txt`. Bind-mounted read-only at `../config/sources:/app/config/sources:ro` (`docker/docker-compose.yml:69`).
- `docker/postgres/postgres.conf`, `docker/rabbitmq.conf` — service config.
- `.env` — present at the repo root (13,617 bytes per Section 3 listing), listed in `.gitignore`; a real value is on disk. Reference in code: `DATABASE_URL` read via `src/db/connection.py:14`.

### Environment variable table (names read from code, sample)
Non-exhaustive (749 total occurrences across 62 Python files under `src/`, top consumers in `src/dashboard/api.py`, `src/collectors/telegram/__init__.py`, `src/collectors/youtube/__init__.py`, `src/collectors/instagram/__init__.py`).

| Variable | Read at | Default |
|---|---|---|
| `DATABASE_URL` | `src/db/connection.py:14`, `src/core/health.py`, `src/watchdog/freshness.py:21` | fallback DSN sans password |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | `src/db/connection.py:120-121` | `1` / `10` |
| `MIGRATE_LOCK_TIMEOUT_MS` | `src/db/migrate.py:100` | `10000` |
| `COLLECTOR_DISABLED_SOURCES` | `src/collectors/__init__.py:52` | empty |
| `COLLECTOR_DRIVE_PATH`, `COLLECTOR_VAULT_ROOT` | `src/core/drive_check.py`, `src/core/vault.py` | required per compose env |
| `COLLECTOR_HANG_TIMEOUT_SECONDS` | various collectors | `7200` (compose) |
| `WATCHDOG_INTERVAL`, `WATCHDOG_RESTART_COOLDOWN`, `WATCHDOG_HEARTBEAT_FILE`, `DOCKER_SOCK` | `src/watchdog/freshness.py:22-30` | `300`, `1800`, `/tmp/watchdog_heartbeat`, `/var/run/docker.sock` |
| `IG_INGEST_PORT` | `src/bridges/ig_ingest.py:83` | `8765` |
| `REDIS_URL` / `REDIS_HOST` / `REDIS_PASSWORD` | `src/notifications/realtime_feed.py`, `src/core/io_pacer.py` | none |
| `REALTIME_POST_FEED_ENABLED` and 8 companion vars | `src/notifications/realtime_feed.py` (module docstring lists all) | `1`, various |
| `RECON_ALLOWLIST`, `RECON_ALLOW_UNSCOPED`, `RECON_USERNAME_ENGINE`, `SPIDERFOOT_*`, `MAIGRET_*`, `GHUNT_*` | `docker/docker-compose.yml:96-135`, `src/core/recon_spiderfoot.py`, `src/core/ghunt_enrich.py` | see compose |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_ACCOUNT_[1-4]_*` | `src/collectors/telegram/__init__.py:341,586-588` | required |
| `YOUTUBE_API_KEY(S)`, `YOUTUBE_COOKIE_FILE`, `YOUTUBE_YTDLP_FORMAT`, `YOUTUBE_MAX_FILESIZE`, `YOUTUBE_DAILY_QUOTA_UNITS` | `src/collectors/youtube/__init__.py:178-236` | see compose defaults |
| `INSTA_ACCOUNT_[1-6]_*`, `INSTA_WINDOW_*`, `INSTA_DAILY_QUOTA_*`, `SLIDING_WINDOW_ENABLED`, `CONTENT_AWARE_ENABLED` | `src/collectors/instagram/__init__.py:138-277` | many |
| `TIKTOK_COOKIES_FILE`, `TIKTOK_YTDLP_FALLBACK_ENABLED`, `TIKTOK_GALLERY_DL_ENABLED`, `TIKTOK_TIMEOUT_SECONDS`, `TIKTOK_YTDLP_MAX_DOWNLOADS` | `src/collectors/tiktok/__init__.py`, override in `docker/docker-compose.yml:239-254` | many |
| `STRAVA_COOKIES_FILE`, `STRAVA_COLLECTOR_ENABLED`, `STRAVA_GPS_*`, `STRAVA_SPIDER_MAX_PER_CYCLE` | `src/collectors/strava/__init__.py` + `docker/docker-compose.yml:329-347` | many |
| `GITHUB_TOKEN`, `GITHUB_API_DELAY`, `GITHUB_MAX_*`, `GITHUB_SPIDER_*` | `src/collectors/github/__init__.py` + `docker/docker-compose.yml:353-377` | many |
| `SEARCH_API_KEY`, `SEARCH_SPIDER_PAGES`, `SEARCH_MAX_RESULTS`, `SEARCH_TOR_PROXY` | `src/collectors/search/__init__.py`, compose | many |
| `WHATSAPP_SESSION_NAMES`, `WHATSAPP_RABBITMQ_URL`, `WHATSAPP_REDIS_URL`, `WHATSAPP_MEDIA_BRIDGE_SECRET`, `WHATSAPP_MAX_BACKFILL_AGE_DAYS` | `src/collectors/whatsapp/__init__.py` and `src/bridges/whatsapp/src/*` | many |
| `BEEPER_DESKTOP_API_URL`, `BEEPER_DESKTOP_API_TOKEN`, `BEEPER_COLLECTOR_ENABLED` | `src/collectors/beeper/__init__.py`, `.env.example:415-417` | required if enabled |
| `DASHBOARD_ADMIN_USERNAME`, `DASHBOARD_ADMIN_PASSWORD`, `DASHBOARD_JWT_SECRET`, `DASHBOARD_AUTH_DISABLED` | `src/dashboard/api.py` | required in prod; `AUTH_DISABLED` docs in README |
| `BROWSER_COOKIE_VAULT_*` (`AUTORESTORE`, `INTERVAL_SECONDS`, `KEEP_SNAPSHOTS`, `HEALTH_PORT`), `CHROME_CDP_URL` | `src/tools/browser_cookie_vault.py`, `.env.example:246-252` | `1`, `300`, `10`, `8790`, `http://host.docker.internal:9333` |
| `COLLECTOR_DB_BACKUP_*` (7 vars: dir, DAILY, WEEKLY, MONTHLY, INTERVAL_SECONDS, DATABASE, DOCKER_CONTAINER) | `src/backup/db_backup.py`, `.env.example:15-22` | see .env.example |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST_PORT`, `POSTGRES_SSL_MODE`, `POSTGRES_SSL_CERT` | `docker/docker-compose.yml:7-16`, `src/db/connection.py:20` | `5433`, empty defaults |
| `RABBITMQ_USER`, `RABBITMQ_PASSWORD`, `RABBITMQ_VHOST`, `REDIS_PASSWORD` | `docker/docker-compose.yml`, `.env.example:298-302` | required |
| `UC_EXTENSION_EXPECTED_VERSION`, `UC_NOTIFY_BOT_USER_ID`, `TELEGRAM_LOGS_CHAT_ID` | `src/dashboard/api.py`, `.env.example:210-222` | see comments |
| `MEDIA_IO_PACER_ENABLED`, `MEDIA_IO_BYTES_PER_SEC`, `MEDIA_IO_BURST_BYTES`, `MEDIA_IO_MAX_SLEEP` | `src/core/io_pacer.py`, `.env.example:436-441` | `0`, `8000000`, empty, `30` |

Configuration precedence: process env → per-service `env_file: ../.env` (`docker/docker-compose.yml:74`) → compose service-level `environment:` overrides. Nothing in code reads from a config file directly except `config/sources/*` (per-collector target lists, bind-mounted).

## 11. Dependencies

### Python direct runtime (`requirements.txt` — 30 packages)
Grouped by observed use (imports checked in `src/`):

| Package (spec in `requirements.txt`) | Locked (`requirements.lock`) | Used in |
|---|---|---|
| `asyncpg>=0.31.0` | `0.31.0` | `src/db/connection.py`, `src/watchdog/freshness.py`, everywhere DB is touched |
| `fastapi>=0.141.1` | `0.136.3` | `src/dashboard/api.py` (drift: lock < spec) |
| `uvicorn[standard]>=0.52.1` | `0.48.0` | dashboard entry (`docker/Dockerfile.dashboard:8`) (drift) |
| `httpx[socks]>=0.28.1` | `0.28.1` | outbound HTTP in collectors, `src/notifications/*` |
| `aiofiles>=25.1.0` | `24.1.0` | file I/O in vault/sidecars (drift) |
| `python-dotenv>=1.2.2` | `1.2.2` | environment loading |
| `beautifulsoup4>=4.15.0` | `4.14.3` | HTML parsing (search/lemon8/website) (drift) |
| `Pillow>=12.3.0` | `12.2.0` | image processing, EXIF; `qrcode[pil]` (drift) |
| `defusedxml>=0.7.1` | `0.7.1` | safe XML parsing |
| `telethon>=1.44.0` | `1.43.2` | `src/collectors/telegram/__init__.py` (drift) |
| `imagehash>=4.3.2` | `4.3.2` | profile photo change detection (pHash) |
| `instaloader>=4.15.3` | `4.15.1` | `src/collectors/instagram/__init__.py` (drift) |
| `ddgs>=9.14.4` | `9.14.4` | `src/collectors/search/__init__.py` |
| `yt-dlp>=2026.7.4` | `2026.3.17` | `src/collectors/youtube/__init__.py`, `src/collectors/tiktok/__init__.py` (drift) |
| `curl_cffi>=0.16.0,<0.17` | `0.14.0` | TLS fingerprint HTTP (drift) |
| `gallery-dl>=1.32.9` | `1.32.1` | tiktok fallback (drift) |
| `PyMuPDF>=1.28.2` | `1.27.2.3` | website PDF-to-image (drift) |
| `playwright>=1.62.0` | `1.60.0` | Instagram/TikTok headless fallback (drift) |
| `aio-pika>=10.0.1` | `9.6.2` | RabbitMQ consumer (drift) |
| `redis>=8.1.0` | `8.0.0` | realtime feed queue, io_pacer (drift) |
| `aiohttp>=3.14.3` | `3.13.5` | `src/bridges/ig_ingest.py`, watchdog Docker calls (drift) |
| `pyjwt>=2.13.0` / `PyJWT>=2.8.0` | `2.13.0` | dashboard auth (`src/dashboard/api.py`) |
| `bcrypt>=5.0.0` | `5.0.0` | dashboard auth |
| `qrcode[pil]>=8.2` | `8.2` | `src/dashboard/api.py` WhatsApp QR endpoint |
| `google-auth>=2.56.3` | `2.53.0` | YouTube OAuth (drift) |
| `google-auth-oauthlib>=1.4.0` | `1.4.0` | YouTube OAuth |
| `pgvector>=0.5.0` | `0.4.2` | face recognition schema (drift) |
| `matrix-nio[e2e]>=0.26.0` | `0.25.2` | `src/collectors/beeper/__init__.py` (drift) |
| `python-telegram-bot>=22.8` | `22.7` | `src/bots/onboard_bot.py` (drift) |
| `phonenumbers>=8.13.0` | absent from lock — baked at Dockerfile end (`docker/Dockerfile:36`) at `==9.0.38` | `src/core/wa_phone_intel.py` |

Lockfile status: `requirements.lock` present, header at `requirements.lock:1-14` notes it is generated from the live container and used by `docker/Dockerfile:26` (`pip install --no-cache-dir -r requirements.lock`). `requirements.txt` uses `>=` floors; `requirements.lock` uses `==`. Drift between the two indicated in the table (nine or more packages).

### Node dependencies
- `dashboard/frontend/package.json` runtime: `react@^19.2.6`, `react-dom@^19.2.6`, `react-router@^8.3.0`, `@tanstack/react-query@^5.101.4`, `@tanstack/react-table@9.0.1`, `clsx@^2.1.1`, `date-fns@^4.4.0`, `lucide-react@^1.30.0`. Dev: `vite@^8.2.1`, `typescript@~7.0.2`, `@vitejs/plugin-react@^6.0.5`, `tailwindcss@^4.3.0` + `@tailwindcss/vite@^4.3.3`, `@types/react@^19.2.18`, `@types/react-dom@^19.2.4`. Lockfile: `dashboard/frontend/package-lock.json`.
- `src/bridges/whatsapp/package.json` runtime: `@whiskeysockets/baileys@^7.0.0-rc14`, `amqplib@^2.0.1`, `express@^5.2.1`, `pino@^10.3.1`, `@hapi/boom@^10.0.1`, `qrcode-terminal@^0.12.0`. Dev: `typescript@^7.0.2`, `ts-node@^10.9.2`, `@types/*`. Explicit `overrides` for `axios`, `body-parser`, `follow-redirects`, `protobufjs`, `sharp` (`src/bridges/whatsapp/package.json:35-42`). Lockfile: `src/bridges/whatsapp/package-lock.json`.

### System packages installed in images
- `docker/Dockerfile:8-14`: `libnss3`, `libatk-bridge2.0-0`, Chromium libs, `libolm-dev`, `nodejs` (setup_20.x), `sqlite3`, `ffmpeg`.
- `docker/Dockerfile.spiderfoot:7-9`: `swig`, `libffi-dev`, `libxml2-dev`, `libxslt1-dev`, `libjpeg-dev`, `libopenjp2-7-dev`.

Base images: `python:3.12-slim` (`docker/Dockerfile:3`), `python:3.11-slim` (`docker/Dockerfile.spiderfoot:1`), `postgres:16-alpine` (`docker/Dockerfile.backup:1`), `pgvector/pgvector:pg16` (`docker/docker-compose.yml:5`), `rabbitmq:3.13-alpine` (`docker/docker-compose.yml:1214` region), `redis:7-alpine` (`docker/docker-compose.yml:1240` region). Two dependabot open PRs propose bumps to `python:3.14-slim` and `python:3.13-slim` (merged PRs #22 and #1; see Section 14).

## 12. Testing and CI

### Tests — `tests/` (110 files)
- Framework: pytest with `asyncio_mode = "auto"` (`pyproject.toml:3`); coverage: `pathpython = ["."]` on the repo root.
- Ignores: `tests/verify_clean_boot.py` and `tests/verify_production.py` skipped by `.github/workflows/python-ci.yml:32` (`pytest tests/ -q --ignore=tests/verify_clean_boot.py --ignore=tests/verify_production.py`).
- Top-level directories:
  - `tests/collectors/` (24 files) — one `test_<source>.py` per active source plus lightweight `test_<source>_parse.py` for parse helpers.
  - `tests/core/` (44 files) — cover `src/core/*` modules by name.
  - `tests/dashboard/` (11 files).
  - `tests/notifications/` (5 files).
  - `tests/bridges/` (1 file) — `test_ig_ingest_vault.py` (75,746 bytes).
  - `tests/tools/`, `tests/watchdog/`, `tests/bots/`, `tests/extension/`.
  - `tests/` root: watchdog, worker, recon, reconciler, coverage, scheduler, readonly-guard tests.
- Largest test files: `tests/dashboard/test_source_matrix.py` 88,927; `tests/bridges/test_ig_ingest_vault.py` 75,746; `tests/dashboard/test_extension_health.py` 58,077; `tests/notifications/test_realtime_feed.py` 56,703; `tests/collectors/test_telegram.py` 50,484.

### CI — `.github/workflows/` (15 workflows)
| Workflow file | Trigger | What it runs |
|---|---|---|
| `ci.yml` | PR to main/master on JS/TS/CSS/config paths | Detects Node project and runs `npm ci && npm run build` (or pnpm/bun equivalent) |
| `python-ci.yml` | PR to main on Python/schema paths | 3 jobs: `lint` (ruff on `src/` `tests/`), `test` (pytest against `requirements.lock`), `schema-boot` (spins up `pgvector/pgvector:pg16` service and runs `tests/verify_clean_boot.py`) |
| `codeql.yml` | push/PR to main/master on code paths, weekly Wed 14:00 UTC cron | CodeQL for auto-detected JS/TS and Python (`detect` job outputs matrix) |
| `bandit.yml` | scheduled + on push | Python security lint |
| `semgrep.yml` | scheduled + on push | Semgrep security scan |
| `trufflehog.yml` | on push | Secret scanning |
| `scorecard.yml` | scheduled | OSSF Scorecard |
| `dependency-review.yml` | on PR | Dependabot dependency review |
| `dependabot-auto-merge.yml` | Dependabot PR events | Auto-merges patch/minor per policy |
| `auto-merge-bots.yml` | Bot PR events | Auto-merge for bot-authored PRs |
| `labeler.yml` | on PR | Applies labels per `.github/labels.yml` |
| `greetings.yml` | on issue/PR | New-contributor greetings |
| `heartbeat.yml` | scheduled | Sync heartbeat (commits `sync: heartbeat [skip ci]`) |
| `summary.yml` | scheduled/manual | Repository summary |
| `lfs-guard.yml` | push/PR to main/master | Blocks Git LFS pointer files unless repo has `keep-lfs` topic |

Local hook: `scripts/hooks/pre-commit` present under scripts (not automatically installed).

Linting/formatting: ruff configured in `pyproject.toml:14-27` with `select = ["F","E9"]` and `extend-exclude = ["archive","telegramcollector","whatsappcollector","tmp","src/db/migrations"]`. No formatter (black/isort/prettier) declared. `.deepsource.toml` and `.sourcery.yml` present at repo root.

Areas with no direct test references: `src/bots/onboard_bot.py` has `tests/bots/test_onboard_bot_start.py` (single 4.6 KB test), `src/tools/browser_cookie_vault.py` has one test (`tests/tools/test_browser_cookie_vault.py`). The full `src/dashboard/api.py` (10,784 LOC) is exercised primarily by the 11 dashboard tests; the ig_ingest bridge (5,921 LOC) is exercised by one 75 KB test. `src/scheduler/__init__.py` (1,722 LOC) is exercised by `tests/test_scheduler_graph_edges.py` (1,188 bytes) plus indirect coverage.

## 13. Branches
Sorted by committer date descending (excluding local branches with `refs/heads/` prefix duplicates already shown by `refs/remotes/origin/`):

| Branch | Last commit | Author | Ahead/behind main | Apparent purpose |
|---|---|---|---|---|
| `main` | HEAD `a0f5ee56` | b | 0 / 0 | Default branch |
| `origin/main` | `a0f5ee56` | b | 0 / 0 | Same |
| `origin/dependabot/github_actions/trufflesecurity/trufflehog-3.97.0` | committer date `2026-08-17` | `dependabot[bot]` | 56 ahead / 1 behind | `[inferred]` trufflehog action bump — superseded by merged PR #27 to 3.97.1 |
| `origin/dependabot/github_actions/actions/labeler-7` | `2026-08-10` | `dependabot[bot]` | 135 / 1 | `[inferred]` labeler action bump 6 → 7 (matches open PR #25) |
| `origin/dependabot/github_actions/actions/setup-python-7` | `2026-08-10` | `dependabot[bot]` | 135 / 1 | `[inferred]` setup-python bump (matches open PR #24) |
| `origin/shell/standardise` | `2026-08-10` | b | 197 / 0 | `[inferred]` merged into main via PR #16 (`chore(shell): standardise repository metadata`) |
| `origin/dependabot/github_actions/actions/checkout-7` | `2026-08-08` | `dependabot[bot]` | 320 / 1 | `[inferred]` checkout 4 → 7 (matches merged PR #10) |
| `origin/dependabot/github_actions/actions/upload-artifact-7` | `2026-08-08` | `dependabot[bot]` | 320 / 1 | `[inferred]` upload-artifact 5 → 7 (matches merged PR #7) |
| `origin/dependabot/docker/src/bridges/whatsapp/node-25-alpine` | `2026-08-08` | `dependabot[bot]` | 320 / 1 | `[inferred]` bump base image for WhatsApp bridge |
| `tiktok-yield-fix` | `2026-08-05` | b | 1324 / 969 | `[inferred]` long-lived divergent local branch (subject inspection would confirm) |
| `recovery/z-refill-and-notifications` | `2026-06-20` | b | 1324 / 207 | `[inferred]` recovery/backfill work stream |

Tags: none present (`git tag --sort=v:refname` returned empty).

## 14. Pull requests
PR data source: `gh` authenticated.

Open PRs (2 total):

| # | Title | Author | Head → Base | Draft | Size |
|---|---|---|---|---|---|
| 25 | `chore(deps): bump actions/labeler from 6 to 7` | `app/dependabot` | `dependabot/github_actions/actions/labeler-7` → `main` | no | +1/−1 |
| 24 | `chore(deps): bump actions/setup-python from 6 to 7` | `app/dependabot` | `dependabot/github_actions/actions/setup-python-7` → `main` | no | +1/−1 |

Recently merged PRs (last 22 by merge order):

| # | Title | Author | Head |
|---|---|---|---|
| 27 | chore(deps): bump trufflesecurity/trufflehog from 3.97.0 to 3.97.1 | `app/dependabot` | trufflehog-3.97.1 |
| 23 | chore(deps): bump actions/dependency-review-action from 4 to 5 | `app/dependabot` | dependency-review-action-5 |
| 22 | chore(deps): bump python from 3.12-slim to 3.14-slim in /docker | `app/dependabot` | python-3.14-slim |
| 21 | Sync General Configurations | `bryanseah234` | sync-31378691633-1 |
| 20 | chore(deps-dev): bump typescript from 5.9.3 to 7.0.2 in whatsapp-bridge group | `app/dependabot` | whatsapp-bridge-b7ceb5d816 |
| 19 | chore(deps): bump the dashboard-frontend group with 3 updates | `app/dependabot` | dashboard-frontend-bccfeb4619 |
| 18 | chore(deps): bump the python-runtime group with 2 updates | `app/dependabot` | python-runtime-b6bd9c5d7d |
| 17 | chore(shell): remove identity scanner hits | `bryanseah234` | shell/phase5-r5-cleanup |
| 16 | chore(shell): standardise repository metadata | `bryanseah234` | shell/standardise |
| 14 | chore(deps): bump the python-runtime group with 28 updates | `app/dependabot` | python-runtime-0ef6b0b3b2 |
| 13 | chore(deps): bump actions/labeler from 6 to 7 | `app/dependabot` | labeler-7 |
| 12 | chore(deps): bump actions/setup-python from 5 to 7 | `app/dependabot` | setup-python-7 |
| 11 | chore(deps): bump actions/ai-inference from 2 to 3 | `app/dependabot` | ai-inference-3 |
| 10 | chore(deps): bump actions/checkout from 4 to 7 | `app/dependabot` | checkout-7 |
| 9 | chore(deps): bump the dashboard-frontend group with 11 updates | `app/dependabot` | dashboard-frontend-d01a624d22 |
| 8 | chore(deps): bump the whatsapp-bridge group with 7 updates | `app/dependabot` | whatsapp-bridge-17ab8c4985 |
| 7 | chore(deps): bump actions/upload-artifact from 5 to 7 | `app/dependabot` | upload-artifact-7 |
| 6 | chore(deps): bump actions/dependency-review-action from 4 to 5 | `app/dependabot` | dependency-review-action-5 |
| 5 | chore(deps): bump trufflesecurity/trufflehog from 3.95.2 to 3.96.0 | `app/dependabot` | trufflehog-3.96.0 |
| 3 | chore(deps): bump ossf/scorecard-action from 2.4.0 to 2.4.4 | `app/dependabot` | scorecard-action-2.4.4 |
| 2 | chore(deps): bump postgres from 16-alpine to 18-alpine in /docker | `app/dependabot` | postgres-18-alpine |
| 1 | chore(deps): bump python from 3.12-slim to 3.13-slim in /docker | `app/dependabot` | python-3.13-slim |

Themes: dependabot dominates PR history (20 of 22 recent PRs). Two operator-authored PRs (#16, #17) tagged `chore(shell)` synced repo metadata / removed identity-scanner hits.

## 15. Change activity
Most-modified files across the last 200 commits (`git log -n 200 --name-only`):

| Count | Path |
|---|---|
| 43 | `docker/docker-compose.yml` |
| 43 | `tests/tools/test_browser_maintenance_scripts.py` |
| 40 | `.agents/STATE.md` |
| 37 | `.agents/JOURNAL.md` |
| 27 | `scripts/browser-tab-maintenance.ps1` |
| 25 | `tests/tools/test_startup_scripts.py` |
| 25 | `scripts/start-scraper-chrome-cdp.ps1` |
| 23 | `tools/browser_tab_reload.py` |
| 22 | `scripts/verify-collector-boot.ps1` |
| 22 | `src/dashboard/api.py` |
| 20 | `extension/manifest.json` |
| 15 | `extension/content.js` |
| 14 | `tests/extension/test_extension_bundle_static.py` |
| 13 | `src/core/recon_spiderfoot.py` |
| 12 | `tools/browser_tab_audit.py` |
| 12 | `src/core/source_freshness.py` |
| 11 | `tests/bridges/test_ig_ingest_vault.py` |
| 11 | `extension/background.js` |
| 11 | `src/bridges/ig_ingest.py` |
| 10 | `tests/test_recon.py` |

Top contributors by commit count on all refs (`git shortlog -sn --all`):
- `b` — 2,248 commits
- `dependabot[bot]` — 25 commits
- `GitHub Actions Sync` — 18 commits
- `github-actions[bot]` — 8 commits

Tags/releases present: none.

## 16. Documentation vs. code

| Doc claim | Source | Code evidence | Status |
|---|---|---|---|
| "24 Docker Compose services" — README does not state this explicitly; README lists services descriptively. | `README.md` "Support services" section | `docker/docker-compose.yml` declares `postgres, collector, collector_spiderfoot, collector_youtube, collector_tiktok, collector_lowrisk, watchdog, collector_website, collector_exposure, collector_lemon8, collector_telegram, collector_beeper, collector_whatsapp, collector_instagram, collector_instagram_dm, ig_ingest, scheduler, onboard_bot, realtime_feed, dashboard, backup, rabbitmq, redis, wa-bridge-1, wa-bridge-2, browser_cookie_vault` = 26 services (24 + 2 wa-bridges) | confirmed |
| "11 source platforms" | `README.md` line 3 | `src/collectors/__init__.py:1-13` registers 13 collector classes: `github, website, instagram, telegram, tiktok, youtube, lemon8, strava, whatsapp, search, exposure, beeper, instagram_dm`. `README.md` names 11 (excludes `exposure` and `instagram_dm`). | contradicted — code has 13 collectors; README names 11 |
| "ig_ingest :8765 (aiohttp)" | `README.md` architecture diagram | `src/bridges/ig_ingest.py:83` `PORT = int(os.getenv("IG_INGEST_PORT", "8765"))`; `src/bridges/ig_ingest.py:5921` `if __name__ == "__main__"` | confirmed |
| "dashboard :8700 (React + FastAPI ops)" | `README.md` | `docker/Dockerfile.dashboard:8` `uvicorn ... --port 8700`; `docker/docker-compose.yml:1092` ports | confirmed |
| "Watchdog restarts stale containers via Docker socket" | `README.md` Support services | `src/watchdog/freshness.py:348` `http://docker/containers/{container}/restart` | confirmed |
| "Two Baileys TS bridges push raw messages over AMQP into RabbitMQ" | `README.md` Path 3 | `docker/docker-compose.yml:1254` `wa-bridge-1`, `:1293` `wa-bridge-2`; `src/bridges/whatsapp/package.json:3` mentions AMQP topic exchange | confirmed |
| "media_items dedup: `(source, content_id) UNIQUE AND sha256`" | `README.md` Key mechanisms | `src/db/schemas/collector.sql:26` `CREATE UNIQUE INDEX ... idx_media_source_content ON media_items(source, content_id)`; `src/db/schemas/collector.sql:25` non-unique sha index; `src/db/migrations/add_media_sha256_unique.sql` adds unique sha256 later | confirmed (with historical caveat: base schema is non-unique; migration adds uniqueness) |
| "`schema_migrations` ledger with checksums" | `README.md` Migrations | `src/db/migrate.py:57` DDL declaring `filename PRIMARY KEY, checksum, applied_at` | confirmed |
| "Watchdog exists specifically to catch dead MTProto / Baileys" | `README.md` | `src/watchdog/freshness.py:73` `REALTIME_SOURCES = {"telegram","whatsapp","beeper"}` | confirmed |
| "Backup: pg_dump ... with `pg_restore --list` verification" | `README.md` Infrastructure table | `src/backup/db_backup.py` present at `src/backup/db_backup.py` (33,839 bytes); companion `src/backup/restore_drill.py` (23,179 bytes) | unverified (not fully inspected) |
| ".env.example is complete current set" | `.env.example` header comment | `.env.example` contains ~200 variables; code reads 749 `os.getenv/os.environ.get` occurrences across `src/` including many names not in `.env.example` (e.g. `SOURCE_MEDIA_TOTALS_TTL_SECONDS`) | outdated — many operational env knobs used in code (particularly under `src/dashboard/api.py`) are not enumerated in `.env.example` |
| "Never edit an applied migration file" | `README.md`, `AGENTS.md` | `src/db/migrate.py:70-72` docstring: "A migration whose on-disk checksum no longer matches the ledger raises loudly" | confirmed |
| "maigret replaced dead sfp_accounts" | `README.md` and `docker/Dockerfile.spiderfoot:15` comment | `docker/Dockerfile.spiderfoot:15` installs `maigret==0.5.0`; env `RECON_USERNAME_ENGINE` default `maigret` at `docker/docker-compose.yml:117` | confirmed |
| "GHunt off by default; skipped without `GHUNT_CREDS`" | `README.md` GHunt section | `src/core/ghunt_enrich.py` present; compose sets `GHUNT_CREDS: ${GHUNT_CREDS:-/app/.ghunt/creds.m}` (`docker/docker-compose.yml:132`); README rationale | confirmed (behavior of `run_lookup()` returning `{"status": "skipped"}` not directly inspected here) |
| "Outbound functionality intentionally absent" | `README.md` last section | No import of a Telegram Bot outbound send in `src/collectors/`; `src/notifications/telegram.py` and `realtime_feed.py` send outbound to Telegram, but these are alert channels, not source-platform writes | confirmed for source platforms; note the notifications module does perform outbound HTTP to Telegram Bot API for its own status/feed channel |
| "Migrations count references" | Diagram in README | `git ls-files "src/db/migrations/*.sql"` returns 121 | confirmed |

Capabilities present in code but not covered by top-level `README.md`:
- `src/collectors/exposure/__init__.py` (19,754 bytes) — 13th collector, not named in README source list.
- `src/collectors/instagram_dm/` — dedicated DM subsystem with its own `auth.py`, `credentials.py`, `device.py`, `mqtt_client.py`, `session.py`, `ACTIVATION.md`.
- `src/core/wa_device_sweep.py` — separate WhatsApp device intel sweep from `wa_phone_intel.py`.
- `src/core/tor_proxy.py` (13,245 bytes), `SEARCH_TOR_PROXY` env — README references `search` collector's Tor mode only tangentially.
- `src/core/optional_rollout.py` (17,812 bytes) + `optional-rollout` CLI subcommand — README does not describe.
- `src/core/rebuild_report.py` + `rebuild-rehearsal` CLI subcommand.
- `src/core/media_sidecar_repair.py` (81,995 bytes) + related `repair-media-sidecars`, `repair-media-file-paths`, `media-artifact-audit` subcommands.
- `src/core/browser_rotator.py` — headless collector browser rotation.
- `src/core/tm_probe.py` — TM (traffic manager?) probe harness; not covered.
- `src/tools/browser_cookie_vault.py` port 8790 — README briefly mentions; internal APIs not documented.

## 17. Gaps and unknowns
- The exact list of "11 source platforms" cited in `README.md` differs from the 13 classes registered in `src/collectors/__init__.py:1-13`. Whether `exposure` and `instagram_dm` are considered production sources or scaffolds is not clarified in code (compose service `collector_instagram_dm` exists at `docker/docker-compose.yml:861`; `collector_exposure` at `:504`).
- Neither the base schema (`src/db/schemas/collector.sql:23-26`) nor `README.md` states which of `(source, content_id)` and `sha256` unique constraint takes precedence when both fire; `README.md` phrases both as ANDed but the migration adding `sha256` uniqueness lives at `src/db/migrations/add_media_sha256_unique.sql` and is applied later than the base schema.
- The version drift between `requirements.txt` floors and `requirements.lock` pins (documented row-by-row in Section 11) is not reconciled in-tree; whether the floors were bumped without regenerating the lock, or the lock intentionally lags production, is not stated.
- `.env` is present on disk with 13,617 bytes but is not tracked; whether the tracked `.env.example` is authoritative for the full set of consumed env names is contradicted by Section 10's variable count (749 `os.getenv/environ.get` in source vs ~200 names in `.env.example`).
- The `archive/` directory referenced in `README.md` ("Outbound functionality — intentionally absent" section and rationale for archived implementations) is not present in the working tree or tracked file list.
- `NOTICE` file states `Copyright 2026 The Prawn Organisation`; no other author attribution or contributor list is present.
- No test framework declaration for the React SPA (`dashboard/frontend/`); no `jest.config`, `vitest.config`, or equivalent found in the inventory. The React build is validated by `.github/workflows/ci.yml` doing `npm run build` only.
- No tests found for `src/bridges/whatsapp/src/*.ts` in the inventory; the WhatsApp bridge has no per-package test target.
- `src/scheduler/__init__.py` is 91,375 bytes with one main `Scheduler` class; the granular tick logic (which sub-scheduler owns recon-seed vs phone-OSINT vs browser-maintenance) is not enumerated in code comments visible from the top-level symbol listing.
- Migration files `v2_schema.sql` and `v2_schema_final.sql` remain in the tree but are on the `SKIP` list (`src/db/migrate.py:47`); no in-code comment indicates when they can be safely removed.
- `dashboard/frontend/tsconfig.json` and `dashboard/frontend/vite.config.ts` inspection was not performed in this run — the exact TS build target is not verified.
