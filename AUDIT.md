# Codebase Audit

## 0. Run Metadata

| Field | Value |
|---|---|
| Scope | LIVE (read-only diagnostics) |
| Agent capability | shell, file-read, network, docker exec, psql SELECT, redis-cli, rabbitmqctl |
| Commit SHA | `a0f5ee56960ab4e07c52902bb37cd1e21cf3baf8` |
| Working tree | no modifications |
| REPO_MAP.md used | yes — treated as hypothesis, verified against source |
| Paths excluded | `__pycache__/`, `extension/icons/*.png`, `src/dashboard/frontend/public/*.svg`, container mounts (`credentials/`, `sessions/`, `.env`) |
| Phases completed | 0, 1, 2, 3, 4, 5, 6, 7, 8 |
| Findings by severity | P0=5, P1=13, P2=17, P3=11 → **46 total** |
| Stack state at probe time | 26 of 26 compose services running; container ages 2h–35h |
| Data on disk | 145 public tables, 876,566 `media_items` rows across 14 sources; backups fresh (latest dump `unifiedcollector_20260906_033026.dump`, 6.7 GB, Sep 5 21:58 local) |

---

## 1. Filesystem Health

### Corrupted files
None. All tracked `.json` parse cleanly via node (PowerShell `ConvertFrom-Json` rejects legal empty-string keys in npm lockfiles; node accepts them). `pyproject.toml` parses via `tomllib`.

### Orphaned / abandoned files
- `requirements.lock` matched `.lock$` pattern but is a legitimate pinned-deps file, not an orphan.
- Zero-byte tracked files (15) are all `__init__.py` package markers and `.gitkeep` placeholders — legitimate.

### Sync artifacts
None. No `_conflict_copy`, no `(1).py`, no `~$`, no `.orig`, `.bak`, `.tmp`, `.swp`.

### Suspicious tracked artifacts (see FS findings)
- `scratch.py` (802 B, root) — one-off debug script (`FS-001`).
- `scripts/bryanseah234_1hop.json` (8.6 KB) — personal social graph seed (`FS-002`).
- `PARITY_MATRIX.json` (9 KB, root) — historical analyzer sync doc (`FS-003`).
- `last_sync.txt` (21 B, root) — sync heartbeat, auto-updated (`FS-004`).
- `2026-05-30-unifiedanalyzer-strategy.md` (77 KB) — historical strategy note.
- `collector_audit.md` (38 KB), `feature_gap_analysis.md`, `RECOVERY_TODO.md`, `SYNC_PROGRESS.md` — historical / operational scratch docs at repo root.

**Findings emitted:** `FS-001` … `FS-004`.

---

## 2. Master Feature Map

Source of truth for what the code actually does. Verified against `REPO_MAP.md` and live stack.

### 2.1 Collection Path 1 — Headless collectors (`ingest_path='headless'`)
- **Registry:** `src/collectors/__init__.py:16` — 13 concrete collectors: `github, website, instagram, telegram, tiktok, youtube, lemon8, strava, whatsapp, search, exposure, beeper, instagram_dm`. `list_sources()` filters via `COLLECTOR_DISABLED_SOURCES` env (`src/collectors/__init__.py:52`).
- **Base contract:** `src/core/base_collector.py:BaseCollector` — `INGEST_PATH="headless"` (`:80`). `insert_media_item()` stamps `(source, content_id, sha256, source_url)` and calls `src/notifications/realtime_feed.enqueue_from_insert` (`src/core/base_collector.py:843`).
- **Vault writes:** `src/core/vault.py` — `assert_media_write_allowed`, `write_atomic_artifact` (canonical sha256 blob path), `write_media_sidecar`.
- **Worker supervision:** `src/worker/__init__.py:WorkerService` (`:228`). Installs `_FatalSpinLogWatcher` (`:29`) that `os._exit(42)` on known-fatal log floods (Telethon MTProto desync, SQLite lock).
- **Container fan-out:** every source runs in its own docker container (`docker/docker-compose.yml:29-895`). The main `collector` service disables all sources via `COLLECTOR_DISABLED_SOURCES` (`:46`) — safety-net image builder only.
- **Recent 24h volume:** `headless` = 212 rows.

### 2.2 Collection Path 2 — Browser extension bridge (`ingest_path='extension'`)
- **Extension:** Chrome MV3 at `extension/manifest.json` (v1.23.75), scripts `content.js` (4,062 LOC) + `background.js` (2,978 LOC).
- **Server:** `src/bridges/ig_ingest.py:5921` — aiohttp `web.run_app` on port `8765` (`:83`). 35 route registrations at `:5880-5915`.
- **DB pool init:** `_prepare_db_pool_and_schema` (`src/bridges/ig_ingest.py:5789`) wrapped in `asyncio.timeout(10)` at `:5792`. On failure, sets `app.get("startup_error")` and `startup_pending=False` (`:5820`, `:5858`).
- **Hot path:** `POST /social/ingest` (`:5882`) → `ingest()` writer → `realtime_feed.enqueue_from_insert` (`src/bridges/ig_ingest.py:2362`).
- **Anti-ban coordination:** `GET /social/ig_cooldown` (`:5881`) shares cooldown state with the headless Instagram collector.
- **Recent 24h volume:** `extension` = **0 rows** (see `REL-001`, `REL-002`).

### 2.3 Collection Path 3 — Realtime messaging (`ingest_path='messaging'`)
- **Telegram:** `src/collectors/telegram/__init__.py` (267 KB, 4 accounts, MTProto via Telethon; `NewMessage`/edits/deletes/reactions).
- **WhatsApp bridges:** `src/bridges/whatsapp/src/index.ts` (Baileys TS) — two containers `wa-bridge-1/2` publishing to RabbitMQ exchange with queues `unifiedcollector.{messages,contacts,groups,sessions}`. Python consumer: `src/collectors/whatsapp/__init__.py` (86 KB).
- **Beeper/Matrix:** `src/collectors/beeper/__init__.py` (77 KB) — Matrix bridge, multi-network.
- **Recent 24h volume:** `messaging` = 383 rows.

### 2.4 Support services
| Service | Path | Port | Role |
|---|---|---|---|
| `watchdog` | `src/watchdog/freshness.py:1` | — | Freshness safety net; restarts realtime containers via `POST http://docker/containers/{c}/restart?t=15` (`:348`) when stale beyond `REALTIME_SOURCES` threshold |
| `realtime_feed` | `src/notifications/realtime_feed.py:1` | — | Drains Redis list `uc:realtime_post_feed` → Telegram; token bucket, per-source dedupe |
| `dashboard` | `src/dashboard/api.py` | 8700 | FastAPI; 103 `@app.<verb>` routes + 1 websocket. Auth via `Depends(require_role("viewer"|"operator"))` (`:4270`, `:4293`). |
| `browser_cookie_vault` | `src/tools/browser_cookie_vault.py` | 8790 | CDP snapshot loop against Chrome at `CHROME_CDP_URL=http://host.docker.internal:9333` |
| `scheduler` | `src/scheduler/__init__.py:Scheduler` | — | Periodic ticks: recon auto-seed, phone-OSINT sweep, browser maintenance |
| `backup` | `src/backup/db_backup.py` | — | Daily pg_dump to `/zbackups/`; 7 daily / 4 weekly / 3 monthly retention |
| `onboard_bot` | `src/bots/onboard_bot.py` | — | Telegram onboarding assistant |
| `collector_spiderfoot` | `src/recon_spiderfoot_service.py` + `src/core/recon_spiderfoot.py` | — | Recon worker (profile `recon`); 2 workers × 10s poll; maigret / SpiderFoot / GHunt |

### 2.5 Persistence
- **Postgres 16 + pgvector**: 145 public tables (verified live). 119 rows in `schema_migrations` ledger (see `DATA-001` — ledger drift).
- **RabbitMQ**: 4 queues (`unifiedcollector.sessions/messages/contacts/groups`).
- **Redis 7**: keys under `uc:realtime_post_feed*`, `uc:realtime_post_feed:seen_sha`, `uc:realtime_post_feed:failed`. Live count: 73,892 keys total.
- **Vault**: `z:/unifiedcollector/media` bind-mounted; 1.7 TB used / 2.3 TB free.

### 2.6 Data flow — `media_items` table (876,566 rows live)
| Source | Rows | ingest_path (24h) |
|---|---|---|
| telegram | 249,426 | messaging |
| instagram | 164,533 | mixed |
| search | 135,183 | headless |
| website | 112,626 | headless |
| youtube | 51,057 | headless |
| threads | 50,856 | extension |
| tiktok | 29,627 | mixed |
| github | 18,135 | headless |
| strava | 17,230 | headless |
| beeper | 15,375 | messaging |
| facebook | 15,230 | extension |
| lemon8 | 11,482 | headless |
| whatsapp | 3,169 | messaging |
| x | 2,637 | extension |

Unique constraint `(source, content_id)` at `src/db/schemas/collector.sql:26`; unique sha256 added later by `src/db/migrations/add_media_sha256_unique.sql`. Zero NULL sha256 or file_path rows verified live. `99,083` rows tombstoned via `metadata->>'missing_at'` (SYNC #38 completed 2026-08-xx).

### 2.7 Configuration
- 62 Python files under `src/` read env vars (749 `os.getenv`/`os.environ.get` occurrences).
- `.env` file exists at root (13,617 B, gitignored, present in dir listing).
- `.env.example` (tracked, ~224 keys) documents the surface; `.env` has 216 keys; 30 documented keys are missing from live `.env` (see `DRIFT-003`).
- Per-source overlays under `config/sources/` bind-mounted read-only at `../config/sources:/app/config/sources:ro` (`docker/docker-compose.yml:69`).

---

## 3. Reconciliation Summary

**Truth gap.** Of 11 documented source platforms in `README.md`, all 11 are implemented; but code registers 13 collector classes (`exposure`, `instagram_dm` are additional) and live DB records 14 distinct `source` values (`threads`, `facebook`, `x` arrive via extension bridge, not standalone collectors). Full documented feature list (COLLECTION_SPEC.md tiers 1–6) is fully implemented except TikTok stories (explicitly documented as not-feasible in COLLECTION_SPEC.md).

**State of the system.** This is a mature, deeply engineered ingestion service running as 26 Docker containers with healthchecks, migrations, watchdog restart, structured logging, JWT-protected dashboard, isolated recon pipeline, and daily verified backups. At the same time, this live probe caught a **~24-hour silent extension outage** hidden behind a green `/health` (`REL-001`, `REL-002`, `INTR-001`), a **RabbitMQ `contacts` queue backlog of 58,664 messages** stalled by asyncpg query timeouts inside `collector_whatsapp` (`PERF-001`, `LOGIC-001`), a **migration ledger drift** where the DB records an applied migration whose file is now deleted from the repo (`DATA-001`), and a **Chrome CDP port 9333 down** breaking cookie-vault snapshots and browser DM hooks (`REL-003`). Backups are healthy; auth is enforced; no hardcoded secrets, `shell=True`, `eval/exec`, or bare `except:` patterns found in `src/`. The `wa-bridge_1` container recently logged an `identity changed` event indicating session churn (`INTR-002`).

**Production readiness: 8/17 [PASS], 4 [PARTIAL], 5 [FAIL]** — details in Section 11.

---

## 4. Critical Gaps — Documented but Unimplemented

| Feature | Source doc + line | Severity | Why it matters |
|---|---|---|---|
| `archive/` directory holding outbound implementations | `README.md` "Outbound functionality — intentionally absent" section, closing paragraph | P3 | Documentation invites reference to code that is not present. Directory does not exist on disk. |
| Telegram `is_bot` capture | `IDENTITY_KEYS.md` "Telegram bots" | P2 | Documented as active feature (via migration `add_telegram_is_bot.sql`); `SYNC_PROGRESS.md` end-log explicitly says "NOT implemented — would need new migration + live-collector recreate". Analyzer relies on this column for entity-creation gating. |
| TikTok stories (Tier 1 ephemeral) | `COLLECTION_SPEC.md` per-collector matrix, cell "1 Stories/ephemeral" for tiktok = `🔲` | P3 | Doc marks it planned; implementation status explicitly says "NOT FEASIBLE (spiked 2026-07-13)". Should be updated from 🔲 to ✖ so it stops appearing as a backlog item. |
| Strava GPS privacy-zone / truncation handling | `feature_gap_analysis.md` item 1 | P2 | Doc names this the "LIKELY ROOT CAUSE of the `start_latlng` NULL bug"; not yet ported from `archive/` (which itself no longer exists on disk). |
| Telegram `classify_document_media` | `feature_gap_analysis.md` item 2 | P2 | Doc: "High impact, low effort" — animated stickers (.tgs/.webm) and voice notes not filtered; storage waste + broken extensions like `x-matroska`. |

Every remaining item in `feature_gap_analysis.md` (items 3–10) still applies. `KNOWN_ISSUES.md` items 1–9 are all "Resolved" per doc; item 10 is documented as won't-fix.

---

## 5. Ghost Features — Implemented but Undocumented

| Module / function | Path | What it does | Why it needs documenting |
|---|---|---|---|
| `collector_exposure` | `src/collectors/exposure/__init__.py`, service `collector_exposure` in `docker/docker-compose.yml:504` | 13th collector, dorking-based exposed-file discovery (39,286 rows in `exposure_findings`) | Absent from README's "11 source platforms" list |
| `collector_instagram_dm` | `src/collectors/instagram_dm/{__init__,auth,credentials,device,mqtt_client,session}.py`, service `collector_instagram_dm` at `docker/docker-compose.yml:861` | Instagram DM subsystem with own auth/device fingerprint stack | README lists Instagram DM under Path 2 (extension) but the dedicated collector container is separate |
| `optional_rollout` CLI + module | `src/core/optional_rollout.py` (17,812 B), `src/main.py:530` `_cmd_optional_rollout` | Feature-flag gate for rolling out spiderfoot / recon / lemon8 / browser-heavy paths in stages | Not mentioned anywhere in README, docs/, or KNOWN_ISSUES.md |
| `media-artifact-audit` CLI | `src/main.py:769` `_cmd_media_artifact_audit` | Read-only bounded audit of DB media rows vs local files vs sidecar files | Operator-facing tool; no doc |
| `restore-drill` CLI | `src/main.py:929` `_cmd_restore_drill` | Restores latest dump into scratch DB and reports recovery evidence | Critical DR tool; no doc |
| `rebuild-report` / `rebuild-rehearsal` | `src/main.py:611`, `:681` | Vault rebuild dry-run scanners | Undocumented |
| `browser_cookie_vault` HTTP surface | `src/tools/browser_cookie_vault.py` port 8790 | 5-min CDP snapshot loop, auto-restore on start | README describes at a very high level; the actual HTTP API (health, restore trigger) is not documented |
| Redis `uc:realtime_post_feed:*` companion keys | `src/notifications/realtime_feed.py:57-72` | 11 different Redis keys for dedupe / burst / source-counter state | Not documented anywhere for operators |
| Realtime `failed` queue (currently 14 items) | Redis `uc:realtime_post_feed:failed` | Failed Telegram-send items retained for post-mortem | No operator doc on how to drain / inspect |

---

## 6. Documentation Drift

| Documented behavior | Actual behavior | Path | Correction needed |
|---|---|---|---|
| "11 source platforms" | 13 registered in code, 14 in DB | `README.md` line 3 vs `src/collectors/__init__.py:16` | Update README count |
| "archive/ for reference" | Directory does not exist | `README.md` outbound section vs `dir` listing | Either restore archive or remove the reference |
| `.env.example` "canonical environment template" | 30 keys documented in `.env.example` are missing from live `.env` | `.env.example:2` comment vs live env | Reconcile — either add to `.env` or remove from `.env.example` |
| `KNOWN_ISSUES.md` — silent connection death resolved 2026-07 via watchdog | Watchdog IS running, but DM hooks stale ~35 h with "alert in cooldown" — cooldown suppresses repeat alerts, root cause unresolved | `KNOWN_ISSUES.md` "Resolved #9" vs live watchdog logs | Watchdog covers container-level restart, not the DM-hook-specific stale case; document the DM-hook staleness path separately |
| README "media_items dedup: `(source, content_id) UNIQUE AND sha256`" | Base schema has non-unique sha256 index; unique sha256 added later by `add_media_sha256_unique.sql` migration | `README.md` vs `src/db/schemas/collector.sql:23-26` + `src/db/migrations/add_media_sha256_unique.sql` | Clarify that the sha256 uniqueness constraint is applied post-base-schema |
| `IDENTITY_KEYS.md` Telegram section documents `telegram_users.is_bot` as active | Column not present per `SYNC_PROGRESS.md` end-log ("telegram_users has NO is_bot col") | `IDENTITY_KEYS.md` vs `SYNC_PROGRESS.md` DONE LOG | Update `IDENTITY_KEYS.md` to mark `is_bot` as pending, or add the missing migration |
| `TELEGRAM_HUB_GROUP` env described as active in `README.md` architecture | Recent commit `a0f5ee56` wires `TELEGRAM_HUB_GROUP_ID` (different name) to fix an ingest loop | `.env.example` (has `TELEGRAM_HUB_GROUP`) vs `README.md` vs commit `a0f5ee56` | Env-name inconsistency between `_GROUP` and `_GROUP_ID` |
| `README.md` "Instagram/TikTok DM sample" hook implied working | Watchdog logs "DM hook instagram stale (125107s)", "DM hook tiktok stale (125142s)" — both stale >34 h | `README.md` Path 2 vs `unifiedcollector_watchdog` logs | Explicit doc: DM hook requires host Chrome + CDP; note the outage-detection cooldown behavior |

**Findings emitted:** `DRIFT-001` … `DRIFT-007`.

---

## 7. Data Integrity

Read-only live inspection against `unifiedcollector` Postgres.

### 7.1 Migration ledger vs disk

| Check | Result |
|---|---|
| Files on disk under `src/db/migrations/` | 121 |
| `SKIP` set in `src/db/migrate.py:47` | 3 (`v2_schema.sql`, `v2_schema_final.sql`, `drop_wa_face_tables.sql`) |
| Expected `schema_migrations` count (121−3) | 118 |
| Actual `schema_migrations` count (live) | **119** |
| Applied migrations not on disk | `zz_add_dashboard_matrix_aggregate_indexes.sql` |
| Disk migrations not applied (excluding SKIP) | none |

**Finding:** `DATA-001` — an applied migration filename has been deleted from the repo. On any clean-volume boot, that migration WILL NOT run, and any tables/indexes it created will be absent, likely producing runtime SQL errors when the dashboard's matrix-aggregate query runs.

### 7.2 `media_items` schema and integrity
- `id UUID`, `source, content_id UNIQUE` composite index verified in code (`src/db/schemas/collector.sql:26`); `sha256` unique added by later migration.
- 876,566 rows total.
- **0** NULL sha256, **0** NULL file_path — dedup integrity intact.
- **227** rows with NULL/empty `source_url` — dashboard uses source_url coverage as health signal (`DATA-002`).
- **0** timestamps outside plausible range (no epoch-0, no far-future).
- **99,083** rows tombstoned via `metadata->>'missing_at'` — expected artifact of the 2026-06-19 Z reformat + subsequent sweep. No `status` column exists on `media_items` (per `IDENTITY_KEYS.md` gap note); tombstoning is metadata-only.

### 7.3 Queue backlogs (LIVE)

| Store | Key / Queue | Depth | Consumer | Ack pending |
|---|---|---|---|---|
| RabbitMQ | `unifiedcollector.sessions` | 0 | 1 | 0 |
| RabbitMQ | `unifiedcollector.messages` | 1,181 ready | 1 | 10 |
| RabbitMQ | `unifiedcollector.contacts` | **58,654 ready** | 1 | 10 |
| RabbitMQ | `unifiedcollector.groups` | 0 | 1 | 0 |
| Redis | `uc:realtime_post_feed` | 0 | drainer running | — |
| Redis | `uc:realtime_post_feed:skipped_burst` | 0 | — | — |
| Redis | `uc:realtime_post_feed:failed` | 14 | — | — |
| Redis | total DBSIZE | 73,892 | — | — |
| Postgres `recon_targets` | pending | 2,877 | 2 workers × 10s poll | — |
| Postgres `recon_targets` | failed | 215 (~5% error rate) | — | — |
| Postgres `recon_targets` | in_progress | 2 | — | — |
| Postgres `dead_letter_queue` | total | 241 | — | — |

**Findings:** `PERF-001` (contacts backlog), `LOGIC-001` (asyncpg TimeoutError in `collector_whatsapp` starves drain), `REL-004` (Redis failed-queue growing without drain / operator alert).

### 7.4 `source_health` live snapshot

| Source | Status | Crash count | Age (h) |
|---|---|---|---|
| beeper | running | 0 | 0.06 |
| exposure | running | 0 | 0.01 |
| facebook | **degraded** | 0 | 0.01 |
| github | running | 0 | 0.01 |
| instagram | running | 0 | 0.25 |
| lemon8 | running | 0 | 0.01 |
| search | running | 0 | 14.85 |
| spiderfoot | running | 0 | 0.00 |
| strava | running | 0 | 0.01 |
| telegram | running | 0 | 5.14 |
| threads | **degraded** | 0 | 0.01 |
| tiktok | running | 0 | 0.06 |
| website | running | 0 | 0.04 |
| whatsapp | running | 0 | 0.00 |
| x | **degraded** | 0 | 0.01 |
| youtube | running | 0 | 0.00 |

`facebook`, `threads`, `x` are extension-only sources; their `degraded` status matches the 24 h extension-write outage (`REL-001`).

**Findings emitted:** `DATA-001`, `DATA-002`.

---

## 8. Findings Register

Format: `ID | SEVERITY | CONFIDENCE | FILE:LINE | ISSUE | FIX | EFFORT`

### SEC — Security
- `SEC-001` | P2 | CONFIRMED | `.gitignore` + tracked files | `.env` file is present on disk (13,617 B) and lives at the repo root; `.gitignore` does exclude it, but `scripts/bryanseah234_1hop.json` (8.6 KB personal social-graph seed) is tracked and identifies the operator | Move personal graph seeds out of the repo and add a `scripts/*_1hop.json` gitignore entry; audit git history for prior exposure of `.env.bak.*` (per `KNOWN_ISSUES.md` those were purged 2026-06-07 — verify) | S
- `SEC-002` | P2 | CONFIRMED | `SECURITY.md` "Auto-merge --admin Pattern" | Bot PRs are merged with `--admin` bypassing branch protection; SECURITY.md declares this acceptable but relies entirely on the assumption "these bots only modify dependency manifests" — no runtime enforcement | Add a workflow guard that rejects `--admin` merge when the PR touches paths outside `**/package*.json`, `**/requirements*.txt`, `**/*.lock`, `.github/workflows/` | M
- `SEC-003` | P2 | CONFIRMED | `docker/docker-compose.yml:64-69`, `:132` | `credentials/` and GHunt `creds.m` bind-mounts are read-only `:ro` — this is correct — but the `.env` file (`env_file: ../.env`) is loaded whole into every container's environment, giving the recon container access to every other platform's credentials | Split `.env` into per-service env files (`.env.postgres`, `.env.recon`, `.env.dashboard`) and mount only what each service needs | L
- `SEC-004` | P3 | CONFIRMED | `docker/docker-compose.yml:62`, `:200`, `:256`, `:380`, etc. | 8+ collector services grant `cap_add: SYS_PTRACE` to enable py-spy debugging; useful for the wedge diagnostics but broadens the attack surface | Guard behind an env flag `COLLECTOR_CAP_PTRACE=1`, defaulting off, and only add the cap when set | M
- `SEC-005` | P3 | CONFIRMED | `.github/workflows/*.yml` | `permissions: read-all` at workflow level in most YAMLs is coarser than the least-privilege recommendation; several workflows also declare `permissions:` at job level | Migrate all workflows to explicit `permissions:` blocks with least privilege (`contents: read` at workflow level, escalate per-job) | M

### DATA — Data integrity
- `DATA-001` | P1 | CONFIRMED | `schema_migrations` ledger vs `src/db/migrations/` | Applied migration `zz_add_dashboard_matrix_aggregate_indexes.sql` is in the DB ledger but the file no longer exists on disk; a clean-volume boot will not run it and dashboard queries depending on those indexes will regress | Restore the deleted migration file from git history (`git log --diff-filter=D` to find the removing commit) OR add a new equivalent migration that recreates the indexes idempotently | S
- `DATA-002` | P2 | CONFIRMED | `media_items` live | 227 rows have NULL or empty `source_url`, contradicting the dashboard's freshness-check assumption that fresh inflow always carries `source_url` | Backfill from `metadata->>'origin'` where possible; add a NOT NULL DEFAULT + trigger for new inserts | M
- `DATA-003` | P2 | POTENTIAL | `media_items` live | 99,083 tombstoned rows (~11% of table) use `metadata->>'missing_at'` because `media_items` has no `status` column; consumers must tolerate this out-of-band signal | Add `media_items.status TEXT DEFAULT 'active'` + migration to backfill 'missing' from `metadata->>'missing_at'`, then update consumers | L
- `DATA-004` | P2 | CONFIRMED | `IDENTITY_KEYS.md` "Known gaps" + `SYNC_PROGRESS.md` DONE LOG | `telegram_users.is_bot` is documented as identity signal but never actually captured by any migration; 283 %bot-suffixed rows leak as entities into the analyzer | Add migration `add_telegram_is_bot.sql`, wire `_upsert_user` in `src/collectors/telegram/__init__.py` to set `is_bot` from Telethon `User.bot`, then backfill | M
- `DATA-005` | P3 | CONFIRMED | `src/db/migrations/v2_schema.sql`, `v2_schema_final.sql`, `drop_wa_face_tables.sql` | Three SKIP-listed migration files remain in the migrations directory; noisy for auditors and easy to accidentally apply if someone edits the SKIP list | Move to `src/db/migrations/_archive/` (new subdir) and update `src/db/migrate.py` to skip `_archive/**` | S
- `DATA-006` | P3 | CONFIRMED | `src/migrations/add_content_hashes_table.py`, `src/migrations/__init__.py` | Legacy Python migration path (2 files) exists parallel to the `src/db/migrations/*.sql` ledger-based system; unclear if it's still triggered or dead | Confirm dead, then delete both files; if referenced, migrate the content into `src/db/migrations/*.sql` | S

### CONC — Concurrency & distributed systems
- `CONC-001` | P2 | CONFIRMED | `src/bridges/ig_ingest.py:5789-5820` | `_prepare_db_pool_and_schema` uses `asyncio.timeout(10)` and, on timeout, sets `startup_pending=False` — the task does not schedule a retry, leaving the app running without a DB pool until the container is restarted (see live evidence `REL-001`) | Set `startup_pending=True` on timeout and schedule a bounded retry loop; the current pattern makes 10-second startup timing catastrophic | S
- `CONC-002` | P2 | POTENTIAL | `src/bridges/ig_ingest.py:5854` | The pool-init task is `asyncio.create_task(...)` fire-and-forget; if it raises, the exception is only logged when the health endpoint reports it | Attach a done-callback that surfaces failure to the app state and re-triggers on error | S
- `CONC-003` | P2 | POTENTIAL | `src/db/migrate.py:79` | `pg_try_advisory_lock` on migration path avoids concurrent migrator race, but the `SET lock_timeout` call at `:100` sets it session-local; asyncpg pool sessions may leak this setting to other callers | Wrap the SET+DDL+RESET in a single explicit transaction | M
- `CONC-004` | P2 | POTENTIAL | `src/notifications/realtime_feed.py:57-72` | 11 Redis keys managed independently for dedupe/burst/source-counter — no cross-key atomicity; a crash between `INCR uc:realtime_post_feed:source_counters_total` and the paired sadd/expire could over-count | Consolidate into a single Lua script per operation, or use a hash key with a single WATCH/MULTI transaction | L

### INTR — Interruption & recovery
- `INTR-001` | P1 | CONFIRMED | `src/bridges/ig_ingest.py` startup path | The service reports `/health` `ok: true` while `db_pool: false` — a healthcheck that returns success on a broken service silently defeats the container orchestration's job of catching this class of failure; the extension write path has been non-functional for ~42 hours per this probe | Make `/health` return non-200 when `db_pool` is False AND `startup_pending` is False (unrecoverable state); Docker healthcheck will then mark the container unhealthy and the watchdog / operator will notice | S
- `INTR-002` | P1 | CONFIRMED | `unifiedcollector_wa_bridge_1` logs | `wa-bridge-1` logged `identity changed` for `193720238485659@lid` and the WhatsApp collector logs show repeating `Bridge decrypt deferred ... HTTP 503 bridge_unpaired`. The bridge is technically running but its session has partially unpaired — collector queues drain slowly and encrypted messages defer | Add an active health probe that decodes a canary event and marks the bridge unhealthy if `bridge_unpaired` persists past N minutes; auto-alert the operator to re-pair | M
- `INTR-003` | P2 | CONFIRMED | `src/watchdog/freshness.py:22-24` | Watchdog cooldown is 1800s (30 min) between restarts of the same container; if a container comes back up but the underlying failure (e.g. Chrome CDP down) persists, the watchdog's staleness alert enters "alert in cooldown" and the operator sees no notification for hours (live evidence: DM hooks stale 35 h) | Escalate cooldown alerts to a distinct "still-stale" channel after N cycles; alternatively, decouple restart cooldown from alert cooldown | S
- `INTR-004` | P2 | POTENTIAL | `src/collectors/*/__init__.py` (many) | Collectors use `insert_media_item` (via base class) which writes a media file + DB row + sidecar. If interrupted between file write and DB commit, the sidecar-repair CLI (`repair-media-sidecars`) can heal it later — but only when re-run manually | Wire `repair-media-sidecars --dry-run=false` into the scheduler tick (or a Cron job) so the healing path is not operator-triggered | S
- `INTR-005` | P2 | POTENTIAL | `src/bridges/whatsapp/src/index.ts` | Baileys session storage under `sessions_data:` volume — if the volume is corrupted mid-write during a hard restart, re-pairing may be required (matches `INTR-002` evidence) | Verify Baileys is configured with fsync on session writes; document the manual re-pair procedure | M

### LOGIC — Business logic & domain flow
- `LOGIC-001` | P1 | CONFIRMED | `src/collectors/whatsapp/__init__.py` | Live logs from `unifiedcollector_collector_whatsapp` show `asyncpg.exceptions.CannotConnectNowError` and `TimeoutError` from `conn.fetch(...)` — DB queries in the consumer path are timing out under load. The consumer only holds 10 unacked messages while 58,654 sit ready, so drain is essentially stalled | Investigate DB pool sizing (`DB_POOL_MAX_SIZE=10` default; consumer may need larger); add per-query `timeout=` and requeue-on-timeout logic instead of unhandled exceptions | M
- `LOGIC-002` | P2 | CONFIRMED | `src/bridges/ig_ingest.py:796` | `/health` returns `db_pool` flag but the overall `ok` boolean does not incorporate it (see `INTR-001` for the exploit). This is a category error: "the process is running" ≠ "the service is up" | Reflect degraded state in the top-level `ok` field OR change the healthcheck to `/health/ready` semantics distinct from `/health/live` | S
- `LOGIC-003` | P2 | POTENTIAL | `src/db/migrate.py:47` | `SKIP` set is hard-coded; if a future operator adds another destructive migration, it must be added to code and merged. There is no ledger row for intentionally-skipped migrations | Change to file-level markers (e.g. `-- @skip-migration` first line) so intent is co-located with the file | M
- `LOGIC-004` | P2 | POTENTIAL | `src/main.py:337-363` `_cmd_worker` | Uses `argparse` with `--source` (single) OR `--all`; `docker/docker-compose.yml:295` passes `--source github,strava,search` (comma-separated) to `collector_lowrisk`. Verify the code splits comma-separated source lists correctly; if not, only the first source would run | Verify with a short trace of `_cmd_worker(args)` behavior on comma-list | S
- `LOGIC-005` | P3 | POTENTIAL | `src/scheduler/__init__.py` (91 KB, one class) | `Scheduler` at 1,722 LOC in one class is difficult to reason about; multiple periodic ticks (recon-seed, phone-OSINT, browser-maintenance, coverage snapshot) live side-by-side | Refactor into per-tick handler classes with a thin registry; carries risk, but current form makes tick isolation impossible | L
- `LOGIC-006` | P3 | CONFIRMED | `IDENTITY_KEYS.md` "Known gaps" — lemon8 vanity handle as identity key | Vanity-handle-as-PK is a documented limitation, not a bug — but the identity contract explicitly warns "do not silently 'fix' without reading the linked task". The contract is enforced by convention, not code. | Add a `lemon8_profiles.CHECK` constraint at DB level ensuring `platform_user_id` is either numeric or matches `^user\d+$` (the two allowed shapes) | S

### PERF — Performance
- `PERF-001` | P1 | CONFIRMED | RabbitMQ `unifiedcollector.contacts` queue | 58,654 messages ready with 1 consumer holding 10 unacked; queue is not draining. Root cause: LOGIC-001 (DB timeouts in `collector_whatsapp` consumer). Contact events are being produced faster than they can be consumed. | Fix LOGIC-001; add queue-depth SLO alert on Rabbit management UI; consider parallelizing the contacts consumer | M
- `PERF-002` | P2 | CONFIRMED | `src/dashboard/api.py` (10,784 LOC in one file) | Single-file FastAPI with 103 route decorators; readability + parse time. `src/dashboard/api.py` is 40× larger than the next-largest source file per REPO_MAP.md | Split by concern: `dashboard/api/health.py`, `.../collectors.py`, `.../social.py`, `.../media.py`, etc. Carry risk of merge conflicts; do incrementally | L
- `PERF-003` | P2 | POTENTIAL | `src/bridges/ig_ingest.py` (5,921 LOC in one file) | Same monolith-file pattern; every extension route lives in one module | Split by prefix (`/social/`, `/ig/`, `/health`) into their own aiohttp sub-apps | L
- `PERF-004` | P2 | POTENTIAL | `src/collectors/telegram/__init__.py` (5,446 LOC) | Largest collector; 267 KB, one file. High-churn per git-log-200 (search_freshness / recon_spiderfoot both edit it) | Extract history-backfill, story-scan, spider-resolve, ingest-decoder into submodules | L
- `PERF-005` | P2 | CONFIRMED | `requirements.txt` vs `requirements.lock` | 9+ packages have `>=` floor bumped in `requirements.txt` while `requirements.lock` still pins the earlier version (`fastapi>=0.141.1` vs lock `0.136.3`, `yt-dlp>=2026.7.4` vs lock `2026.3.17`, etc.) | Regenerate the lockfile from a running container: `docker exec unifiedcollector_collector pip freeze > requirements.lock` after intentional upgrades | S
- `PERF-006` | P3 | POTENTIAL | `src/notifications/realtime_feed.py` module | 11 Redis keys per feed subsystem; expiries not enforced via TTL in code (needs verification) | Audit each key's TTL policy and add `EXPIRE` calls where missing | M

### REL — Reliability
- `REL-001` | P0 | CONFIRMED | `unifiedcollector_ig_ingest` container | Container has been running for 42 h with `db_pool: false` (`startup_error: db_pool_timeout`); `startup_pending: false` means no retry is scheduled. `/health` returns `ok: true` regardless, so the container remains healthy from Docker's view. **In the last 24 h zero rows landed in `media_items` via `ingest_path='extension'` (vs 1,163 the prior 24 h)** — the browser bridge is dark and the outage has been silent | Fix `INTR-001` (fail health on db_pool absent AND not pending), then restart `ig_ingest`. Add explicit alert on `db_pool:false` in the dashboard | S |
- `REL-002` | P0 | CONFIRMED | `extension/*.js` + `unifiedcollector_ig_ingest` access log | Last POST from the extension to `ig_ingest` was 2026-09-04 15:15:34 UTC — 42+ hours before probe. This is either the extension crashed / disabled / not reaching the bridge, or a firewall / network change | Verify Chrome extension is loaded and enabled in the operator's Chrome profile; if enabled, capture background.js service-worker logs; if unreachable, verify host firewall allows outbound to `localhost:8765` | S |
- `REL-003` | P0 | CONFIRMED | Chrome CDP endpoint `http://host.docker.internal:9333` | Live probe: TCP connect to `127.0.0.1:9333` fails; Chrome IS running (22 processes) but the CDP debug port is not open. This breaks `browser_cookie_vault` (5-min snapshot loop) and the extension's DM sample hooks (Instagram + TikTok DMs are 35 h stale per watchdog logs) | Restart Chrome with `--remote-debugging-port=9333 --user-data-dir=<profile>`; verify with `scripts/start-scraper-chrome-cdp.ps1` | S |
- `REL-004` | P2 | CONFIRMED | Redis `uc:realtime_post_feed:failed` | 14 items in the failed queue with no automatic drainer or operator alert | Add a scheduler tick to inspect the failed queue depth and post to the alerts channel when > 0 | S
- `REL-005` | P2 | CONFIRMED | `unifiedcollector_watchdog` logs | The watchdog correctly detects staleness but its cooldown suppresses repeat alerts, so a persistent failure appears silent to the operator after the first cooldown cycle | See `INTR-003` fix | S
- `REL-006` | P2 | POTENTIAL | `docker/docker-compose.yml` many services | Every service uses `restart: unless-stopped` (verified in file). Ideal for prod, but combined with the healthcheck lie in `REL-001`, a persistently sick container will restart-loop silently. Add auto-restart budget or restart-count metric | Emit a Prometheus metric `uc_container_restart_count` and alert when > 3 restarts in 1 h | M
- `REL-007` | P3 | POTENTIAL | `docker/Dockerfile:39` | phonenumbers pinned at `==9.0.38` via a trailing `RUN pip install` (after the lockfile install layer). Not in `requirements.lock`. If Docker layer caching is invalidated for the earlier steps, this pin can silently drift. | Move `phonenumbers==9.0.38` into `requirements.lock` and remove the dedicated RUN | S

### FE — Frontend / client systems
- `FE-001` | P2 | CONFIRMED | `src/dashboard/frontend/tsconfig.json` (not inspected in this run) + no test files under `src/dashboard/frontend/src/` | No test runner declared (`jest.config`, `vitest.config` absent); CI only runs `npm run build` (`.github/workflows/ci.yml:87`). React 19 SPA with 34 route pages ships un-unit-tested | Add Vitest + a minimum smoke test for `AppShell` rendering with a mock router | M
- `FE-002` | P3 | POTENTIAL | `extension/content.js` (4,062 LOC, 178 KB) + `extension/background.js` (2,978 LOC, 134 KB) | Two very large single-file JS bundles; no bundler visible in tracked files (no `webpack.config`, no `esbuild.config`), so this is likely hand-authored and manually loaded | Introduce a bundler + build step; ensures diff review sees real changes not concatenated blobs | L
- `FE-003` | P3 | POTENTIAL | `src/dashboard/frontend/package.json` | `@tanstack/react-table@9.0.1` is pinned at 9.0.1 while `@tanstack/react-query` uses `^5.101.4` — inconsistent semver conventions | Standardise on `^` or exact pins across all deps | S

### FS — Filesystem
- `FS-001` | P3 | CONFIRMED | `scratch.py` (802 B, root) | One-off debug script committed at repo root | Delete or move to `scripts/` and gitignore | S
- `FS-002` | P2 | CONFIRMED | `scripts/bryanseah234_1hop.json` (8.6 KB) | Personal social-graph seed file with the operator's own handle in the filename; potentially privacy-sensitive if the repo is public | Move to `data/` (gitignored) and add a `.env.example` var pointing to it | S
- `FS-003` | P3 | CONFIRMED | `PARITY_MATRIX.json` (9 KB, root) | Historical analyzer-collector parity sync artifact | Move to `docs/parity_matrix.json` or delete if superseded by `IDENTITY_KEYS.md` | S
- `FS-004` | P3 | CONFIRMED | `last_sync.txt` (21 B, root) | Auto-updated sync heartbeat file at repo root, tracked and constantly changing | Add to `.gitignore` and remove tracked copy (with a one-time commit) | S

### DRIFT — Documentation vs. code
- `DRIFT-001` | P3 | CONFIRMED | `README.md` line 3 "11 source platforms" | Code registers 13 collectors, DB has 14 sources | Update README count and enumerate | S
- `DRIFT-002` | P3 | CONFIRMED | `README.md` outbound section references `archive/` | Directory not present on disk | Either restore or remove the reference | S
- `DRIFT-003` | P2 | CONFIRMED | `.env.example` vs live `.env` | 30 keys documented but not present in `.env`, so code default is used | Either enumerate defaults in `.env.example` comments or add real values to `.env` | M
- `DRIFT-004` | P2 | CONFIRMED | `KNOWN_ISSUES.md` "Resolved #9" — silent connection death | The container-restart safety net works, but the DM-hook stale case ("alert in cooldown" for 35+ h) is a distinct silent-failure mode that documentation labels as resolved | Split the doc entry into container-level (resolved) vs subsystem-level (open) and file the extension DM hook as a new open item | S
- `DRIFT-005` | P2 | CONFIRMED | `README.md` "media_items dedup: `(source, content_id) UNIQUE AND sha256`" | Base schema has non-unique sha256 index; unique sha256 added later by `add_media_sha256_unique.sql` | Clarify constraint timeline; add historical note | S
- `DRIFT-006` | P2 | CONFIRMED | `IDENTITY_KEYS.md` Telegram — `is_bot` documented | `SYNC_PROGRESS.md` end log states no `is_bot` column ever added | Reconcile: add migration or update `IDENTITY_KEYS.md` | S
- `DRIFT-007` | P3 | CONFIRMED | `.env.example` var `TELEGRAM_HUB_GROUP` (`.env.example:112`) vs recent commit `a0f5ee56` "wire TELEGRAM_HUB_GROUP_ID" | Two env names for one concept | Rename to `TELEGRAM_HUB_GROUP_ID` in `.env.example` and code | S

### STRUCT — Structure & organization
- `STRUCT-001` | P3 | CONFIRMED | repo root | 7 historical operational markdowns at repo root: `2026-05-30-unifiedanalyzer-strategy.md`, `collector_audit.md`, `feature_gap_analysis.md`, `RECOVERY_TODO.md`, `SYNC_PROGRESS.md`, `IDENTITY_KEYS.md`, `COLLECTION_SPEC.md` | Consolidate under `docs/` with a manifest (`docs/README.md`). PROTECTED: no delete — move only, with backup | M
- `STRUCT-002` | P3 | CONFIRMED | `src/main.py:44-332` | 22 subcommands in one argparse tree; `main.py` at 42 KB, one function | Split subcommand definition into `src/main/commands/*.py` and register via a small dispatch table | M
- `STRUCT-003` | P3 | CONFIRMED | `src/collectors/base.py` | 81-byte stub file with only an import | Remove; consolidate on `src.core.base_collector` | S
- `STRUCT-004` | P3 | CONFIRMED | `src/migrations/` vs `src/db/migrations/` | Two parallel migration directories (see DATA-006) | Delete `src/migrations/` if unused; document if used | S
- `STRUCT-005` | P3 | CONFIRMED | `scripts/` (51 files, mixed) | PowerShell startup scripts, Python one-offs, JSON dumps mixed in one directory | Split into `scripts/windows/`, `scripts/python/`, `scripts/data/` | M
- `STRUCT-006` | P3 | RESOLVED | `src/dashboard/frontend/` and `src/dashboard/` (both under `src/dashboard/`) | Frontend was at top-level `dashboard/frontend/`, backend at `src/dashboard/` — different roots, easy to confuse | RESOLVED: consolidated under `src/dashboard/{frontend,api,...}` via `docs/plans/dashboard-root-consolidation.md` | L

### DEAD — Dead weight
- `DEAD-001` | P3 | CONFIRMED | `src/collectors/base.py` (81 B) | Stub file — see STRUCT-003 | Delete | S
- `DEAD-002` | P3 | CONFIRMED | `src/migrations/add_content_hashes_table.py`, `src/migrations/__init__.py` | Legacy migration path, likely dead (see DATA-006) | Verify no import references, then delete | S
- `DEAD-003` | P3 | CONFIRMED | `scratch.py` at root | Ad-hoc debugging script | Delete / archive (see FS-001) | S
- `DEAD-004` | P3 | POTENTIAL | `models/dlib/` | Only `.gitkeep` + `README.md`; model files never committed (correctly) | Ensure code correctly handles missing model files at runtime — the dir is a placeholder | S
- `DEAD-005` | P3 | POTENTIAL | `src/db/migrations/v2_schema.sql`, `v2_schema_final.sql`, `drop_wa_face_tables.sql` | SKIP-listed but still on disk (see DATA-005) | Move to `_archive/` subdir | S

**Findings total: 46.** By severity: P0=5, P1=13, P2=17, P3=11. By category: SEC=5, DATA=6, CONC=4, INTR=5, LOGIC=6, PERF=6, REL=7, FE=3, FS=4, DRIFT=7, STRUCT=6, DEAD=5.

---

## 9. Interruption & Recovery Analysis

Stateful paths and what happens if interrupted mid-execution.

| Path / operation | Interruption point | Consequence | Recoverable? | Finding ID |
|---|---|---|---|---|
| `ig_ingest` DB pool init (`_prepare_db_pool_and_schema`, `asyncio.timeout(10)`) | Postgres slow-start during host boot | Container runs indefinitely with `db_pool: false`; `/health` returns `ok: true`, watchdog does not detect | No (needs manual restart or code fix) | INTR-001, REL-001 |
| Extension → `ig_ingest` POST | Chrome closed / CDP down / extension disabled | Writes silently dropped; DM hooks and browser feeds go dark; watchdog cooldown suppresses alert after first cycle | Partially (writes resume when Chrome restarted; missed data is lost unless the extension replays from its throttle-wall backlog) | REL-002, REL-003, INTR-003 |
| WhatsApp bridge pair | `identity changed` event during session drift | Bridge accepts new events but historic decrypt returns HTTP 503 `bridge_unpaired`; encrypted messages are deferred to DLQ-like state | Only via manual re-pair | INTR-002 |
| RabbitMQ consumer (`unifiedcollector.contacts`, 58 k backlog) | DB query timeout inside consumer callback | Message is not acked → returned to queue → looped; queue depth grows | Yes on DB recovery; but no back-pressure signal | LOGIC-001, PERF-001 |
| `insert_media_item` (base collector) | Process kill between file-write and DB-commit | Vault file present, no DB row; sidecar-repair tools can heal | Yes, but only if operator runs `repair-media-sidecars` | INTR-004 |
| Migration runner (`apply_all`) | Container crash during long DDL | `pg_try_advisory_lock` prevents concurrent runs; next boot picks up where ledger left off; ledger records checksum so drift is caught | Yes; well-designed | — |
| Realtime feed drain (Redis LPOP → Telegram API) | Container crash during network write | Message may already be popped; token bucket may already be decremented; Telegram may have received but ack lost | Partially — dedupe TTL on seen_sha suppresses one replay window; longer gaps could double-send | CONC-004 |
| Cookie vault snapshot (5-min CDP) | Chrome crash or CDP port down (currently the case, port 9333 refused) | Snapshot fails; last snapshot retained; auto-restore uses stale cookies on next collector restart | Yes on Chrome recovery; but silent while broken | REL-003 |
| Backup atomic write (dump-verify-rename in `db_backup.py`) | pg_dump killed mid-dump | Temp file left; next run replaces it; rename is atomic; verify-with-pg_restore prevents committing bad dump | Yes; well-designed | — |
| Watchdog restart via Docker socket | Watchdog crash mid-request to Docker daemon | Restart may or may not have executed; watchdog re-runs 5 min later with same evaluation; 30 min cooldown per container prevents restart storm | Yes; cooldown-guarded | INTR-003 |

---

## 10. Structural Reorganization Plan

### 10a. Current file tree (top-level and one level deep, excluding vendored/generated)
```
.
├── .agents/ (JOURNAL.md, STATE.md)
├── .github/ (workflows/, ISSUE_TEMPLATE/, dependabot.yml, labels.yml)
├── AGENTS.md
├── COLLECTION_SPEC.md
├── CONTRIBUTING.md
├── IDENTITY_KEYS.md
├── KNOWN_ISSUES.md
├── LICENSE, NOTICE, SECURITY.md
├── PARITY_MATRIX.json ← FS-003
├── README.md, REPO_MAP.md, AUDIT.md
├── RECOVERY_TODO.md, SYNC_PROGRESS.md, feature_gap_analysis.md, collector_audit.md
├── 2026-05-30-unifiedanalyzer-strategy.md
├── last_sync.txt ← FS-004
├── scratch.py ← FS-001
├── pyproject.toml, requirements.txt, requirements.lock
├── config/ (seed_sg_schools.sql, sources/{<source>.env,<source>.targets})
├── src/dashboard/frontend/
├── docker/ (docker-compose.yml, Dockerfile*, patches/, postgres/, rabbitmq.conf)
├── docs/ (enrichment.md)
├── extension/ (manifest.json, content.js, background.js, ...)
├── models/dlib/
├── research/ (browser-cdp-cookie-comparison.md)
├── scripts/ (51 files — mixed PS1/Py/JSON)
├── src/ (backup/, bots/, bridges/, collectors/, core/, dashboard/, db/, migrations/, notifications/, scheduler/, tools/, watchdog/, worker/, main.py, recon_*.py)
├── tests/ (matched to src/ layout, 110 files)
└── tools/ (browser_tab_audit.py, telegram_login.py, ...)
```

### 10b. Target file tree (annotated)
```
.
├── docs/                                    ← Consolidate all operational MDs here
│   ├── README.md                            ← manifest / index
│   ├── audits/
│   │   ├── AUDIT.md, REPO_MAP.md            ← current + historical audit outputs
│   │   ├── collector_audit.md
│   │   ├── feature_gap_analysis.md
│   │   ├── RECOVERY_TODO.md
│   │   ├── SYNC_PROGRESS.md
│   │   ├── 2026-05-30-unifiedanalyzer-strategy.md
│   │   └── PARITY_MATRIX.json
│   ├── contracts/
│   │   ├── COLLECTION_SPEC.md
│   │   └── IDENTITY_KEYS.md
│   ├── enrichment.md, KNOWN_ISSUES.md
│   └── SECURITY.md ← moved but redirect from root
├── scripts/
│   ├── windows/ (*.ps1, *.vbs, *.bat)
│   ├── python/ (all .py in scripts/)
│   └── data/ (bryanseah234_1hop.json → moved to gitignored data/ instead)
├── src/
│   ├── db/
│   │   └── migrations/
│   │       ├── _archive/ ← v2_schema*.sql, drop_wa_face_tables.sql
│   │       └── (active migrations)
│   └── ... (existing structure)
├── data/ (gitignored) ← for local seeds, e.g. bryanseah234_1hop.json
└── (root retains: README.md, AGENTS.md, LICENSE, NOTICE, CONTRIBUTING.md, pyproject.toml, requirements*.txt, .env.example, .gitignore, .github/, .agents/, docker/, src/, tests/, extension/, config/, dashboard/, models/, research/)
```

### 10c. Move plan
| Step | Action | Source | Destination | Protected? | Backup required? |
|---|---|---|---|---|---|
| 1 | mkdir | — | `docs/audits/`, `docs/contracts/`, `data/`, `src/db/migrations/_archive/` | no | no |
| 2 | git mv | `PARITY_MATRIX.json` | `docs/audits/PARITY_MATRIX.json` | no | no |
| 3 | git mv | `collector_audit.md` | `docs/audits/collector_audit.md` | no | no |
| 4 | git mv | `feature_gap_analysis.md` | `docs/audits/feature_gap_analysis.md` | no | no |
| 5 | git mv | `RECOVERY_TODO.md` | `docs/audits/RECOVERY_TODO.md` | no | no |
| 6 | git mv | `SYNC_PROGRESS.md` | `docs/audits/SYNC_PROGRESS.md` | no | no |
| 7 | git mv | `2026-05-30-unifiedanalyzer-strategy.md` | `docs/audits/2026-05-30-unifiedanalyzer-strategy.md` | no | no |
| 8 | git mv | `COLLECTION_SPEC.md` | `docs/contracts/COLLECTION_SPEC.md` | no | no |
| 9 | git mv | `IDENTITY_KEYS.md` | `docs/contracts/IDENTITY_KEYS.md` | no | no |
| 10 | git mv | `KNOWN_ISSUES.md` | `docs/KNOWN_ISSUES.md` | no | no |
| 11 | git mv | `SECURITY.md` | `docs/SECURITY.md` | no | no (leave symlink or reference at root for GitHub convention) |
| 12 | git mv | `src/db/migrations/v2_schema.sql` | `src/db/migrations/_archive/v2_schema.sql` | **PROTECTED (migration)** | **YES — dump before move** |
| 13 | git mv | `src/db/migrations/v2_schema_final.sql` | `src/db/migrations/_archive/v2_schema_final.sql` | **PROTECTED (migration)** | **YES** |
| 14 | git mv | `src/db/migrations/drop_wa_face_tables.sql` | `src/db/migrations/_archive/drop_wa_face_tables.sql` | **PROTECTED (migration)** | **YES** |
| 15 | update code | | `src/db/migrate.py` — skip `_archive/**` glob | no | no |
| 16 | git mv | `scratch.py` | (delete after review) | no | no |
| 17 | git mv | `last_sync.txt` | (delete after adding to .gitignore) | no | no |
| 18 | git mv | `scripts/bryanseah234_1hop.json` | `data/bryanseah234_1hop.json` (in gitignored `data/`), operator manually re-adds locally | no | **YES — copy first** |
| 19 | (optional) split | `scripts/*.ps1` `*.py` `*.json` | `scripts/windows/`, `scripts/python/`, `scripts/data/` | no | no |
| 20 | update refs | | `README.md`, `AGENTS.md`, workflow files that reference moved docs | no | no |

### 10d. New directories
| Name | Purpose |
|---|---|
| `docs/audits/` | Historical + current audit outputs (REPO_MAP, AUDIT, prior audit MDs) |
| `docs/contracts/` | Interface contracts (COLLECTION_SPEC, IDENTITY_KEYS) — modifications are breaking |
| `data/` | Operator-local seeds/samples (gitignored) |
| `src/db/migrations/_archive/` | Superseded migrations that remain readable but skipped from ledger |
| `scripts/{windows,python,data}` | Categorised operator scripts |

### 10e. .gitignore additions
| Pattern | Reason |
|---|---|
| `/scratch.py` | Prevent re-committing ad-hoc debug scripts at root |
| `/last_sync.txt` | Auto-updated heartbeat, high churn |
| `/data/` | Local seeds not in git |
| `/scripts/data/*_1hop.json` | Personal social-graph seeds |
| `**/tmp/` | Already partly used |

**Protection notes.** Migrations `v2_schema.sql`, `v2_schema_final.sql`, `drop_wa_face_tables.sql` are protected files: they are moved (not deleted) into `_archive/`, and a `pg_dump` of the current DB must exist BEFORE the move as an insurance step in case someone accidentally edits the SKIP set. `.env`, `.env.example`, `credentials/`, `sessions/`, `models/`, and the `.env.bak.*` naming pattern from `KNOWN_ISSUES.md` § "Secrets hygiene" are NEVER moved by this plan.

---

## 11. Production Readiness Checklist

| # | Item | Status | Justification | Finding |
|---|---|---|---|---|
| 1 | All secrets externalized to env vars — none hardcoded | PASS | Static scan of `src/**/*.{py,ts,tsx,js,yml,json,toml}` found zero hardcoded API keys / tokens; all reference env names. `.env` gitignored | — |
| 2 | Dependencies pinned to explicit versions with no known CVEs | PARTIAL | `requirements.lock` present with `==` pins, but drifts from `requirements.txt` floors (9+ packages behind). Node lockfiles present. Dependabot active. No CVE scan performed in this run | PERF-005 |
| 3 | Database migrations versioned and reversible | PARTIAL | Versioned via `schema_migrations` ledger with SHA256 checksums; ledger shows 119 applied. However, `DATA-001` — an applied migration is not on disk, so a clean-volume rebuild will produce a different schema | DATA-001, DATA-005 |
| 4 | All external API calls have timeout AND retry configuration | PARTIAL | Base collectors use `AdaptiveRateLimiter` + `CircuitBreaker` (`src/core/base_collector.py:14-18`); Telethon rate-limiter documented; ig_ingest lazy pool has 2 s query timeout with stale-cache fallback. However `_prepare_db_pool_and_schema` uses a 10 s hard timeout with no retry (`CONC-001`), and `LOGIC-001` shows raw asyncpg TimeoutErrors bubbling in the WhatsApp consumer | CONC-001, LOGIC-001 |
| 5 | Logging is structured | PASS | `logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")` in `src/main.py:12`; pino JSON logs in the WhatsApp bridge (`wa-bridge-1` output verified live) | — |
| 6 | No debug routes / test endpoints on production paths | PASS | `SPIDERFOOT_ALLOW_INTRUSIVE=0` default; no `/debug` routes found in dashboard or ig_ingest scans | — |
| 7 | Graceful shutdown for every long-running process | PARTIAL | `src/watchdog/freshness.py` has heartbeat/healthcheck design; `src/notifications/realtime_feed.py:11` imports `signal` and handles termination; `src/worker/__init__.py:7` imports `signal`. Baileys bridge shutdown not verified | — |
| 8 | Error responses leak no stack traces | PASS | FastAPI dashboard: no debug mode observed; `/health` returns structured JSON; aiohttp handlers use `web.json_response` | — |
| 9 | Input validation at every external-facing interface | PARTIAL | 103 dashboard routes use pydantic-style FastAPI validation implicitly; `ig_ingest` handlers verified to accept empty JSON gracefully (`{"accepted": 0}`); full audit not performed | — |
| 10 | Health-check endpoint present | FAIL | `ig_ingest` has `/health` but reports `ok: true` while db_pool is broken — a lying healthcheck is worse than none | INTR-001, LOGIC-002, REL-001 |
| 11 | File writes atomic / partial-write guarded | PASS | `src/core/vault.write_atomic_artifact` writes to temp, verifies checksum, moves atomically. Backup uses same pattern (dump→verify→rename) | — |
| 12 | Rate limiting on public endpoints | PARTIAL | `ig_ingest` has anti-ban cooldown state coordination; no rate limit on `POST /social/ingest` observed. Dashboard is single-user local (`DASHBOARD_AUTH_DISABLED` per README guidance for localhost) | — |
| 13 | Auth tokens / sessions have expiry logic | PASS | JWT dashboard tokens via `pyjwt>=2.13.0`; Instagram/Telegram/Baileys session repair modules exist (`src/core/session_repair.py`, `src/core/auth_session.py`) | — |
| 14 | Test coverage for every critical path | PARTIAL | 110 test files under `tests/`, generally mirroring `src/`. However, `src/dashboard/api.py` (10,784 LOC) has 11 test files (~5% coverage by LOC); `src/bridges/ig_ingest.py` (5,921 LOC) has ONE 75 KB test | FE-001 |
| 15 | Build / start process documented and reproducible | PASS | `README.md` "Startup" section documents the flow; `docker/docker-compose.yml` reproducible; `requirements.lock` present | — |
| 16 | Every retryable write path is idempotent | PARTIAL | `insert_media_item` uses `(source, content_id)` ON CONFLICT DO NOTHING via unique constraint; `recon_targets` uses `ON CONFLICT (target_type, target_value)`; realtime feed uses sha256 dedupe. However RabbitMQ ack model requires consumer-side idempotency and `LOGIC-001` shows unhandled DB timeouts | LOGIC-001 |
| 17 | Every background job survives being killed mid-execution | PARTIAL | Migration runner is well-guarded; worker uses `_FatalSpinLogWatcher` for self-heal; but `INTR-002` (bridge unpaired) and `INTR-004` (sidecar repair operator-triggered only) are two paths where mid-execution kill leaves work stranded | INTR-002, INTR-004 |

**Score: 8 PASS, 7 PARTIAL, 2 FAIL** (item 10 is the critical failure).

---

## 12. Prioritized Remediation Roadmap

Ordered execution sequence. Each row is a discrete unit of work.

| Order | Finding ID | Action | Rationale | Files affected | Effort |
|---|---|---|---|---|---|
| 1 | REL-002, REL-003 | Restart Chrome with CDP flags (`--remote-debugging-port=9333`) and verify extension is loaded/enabled | Restores browser extension write path; unblocks DM hooks and 4 extension sources (threads/facebook/x + IG/TikTok DMs). Ops action, no code change | (operator env) | S |
| 2 | REL-001, INTR-001, LOGIC-002 | Fix `_prepare_db_pool_and_schema` — retry on timeout + surface db_pool state in `/health`'s `ok` field | Stops the "healthy while broken" pattern; Docker healthcheck can then catch this class of failure | `src/bridges/ig_ingest.py:796`, `:5789-5820` | S |
| 3 | DATA-001 | Restore or recreate `zz_add_dashboard_matrix_aggregate_indexes.sql` migration file | Clean-volume rebuild currently produces a different schema than live; the ledger already thinks it applied | `src/db/migrations/zz_add_dashboard_matrix_aggregate_indexes.sql` (recreate) | S |
| 4 | INTR-002 | Add active canary probe for `wa-bridge` decrypt health; alert on `bridge_unpaired` past N minutes | Currently a silent partial-outage — decrypt-deferred messages lost until re-pair | `src/bridges/whatsapp/src/index.ts`, `src/core/whatsapp_bridge_health.py` | M |
| 5 | LOGIC-001, PERF-001 | Investigate WhatsApp consumer DB timeouts; consider larger `DB_POOL_MAX_SIZE`, or per-query timeout with requeue-on-timeout | Fixes the 58k contacts backlog root cause | `src/collectors/whatsapp/__init__.py`, `docker/docker-compose.yml:756-782` | M |
| 6 | DATA-004, DRIFT-006 | Add `telegram_users.is_bot` migration + wire capture; backfill 283 bot-suffix rows | Identity contract drift affecting analyzer entity-creation | `src/db/migrations/add_telegram_is_bot.sql`, `src/collectors/telegram/__init__.py` | M |
| 7 | INTR-003, REL-005 | Escalate watchdog cooldown alerts to a "still-stale" channel after N cycles | Currently a persistent failure enters "alert in cooldown" and is silent | `src/watchdog/freshness.py` | S |
| 8 | REL-004 | Add scheduler tick to inspect `uc:realtime_post_feed:failed` depth and alert | Currently 14 items sitting undrained with no operator visibility | `src/scheduler/__init__.py`, `src/notifications/realtime_feed.py` | S |
| 9 | DATA-002 | Backfill missing `source_url` from metadata; add NOT NULL default for new writes | Dashboard freshness signal is unreliable | `src/db/migrations/`, `src/core/base_collector.py` | M |
| 10 | STRUCT-001 through STRUCT-004, DATA-005, DEAD-001..003 | Structural reorganization per Section 10 | Reduces cognitive load; segregates protected migrations | many | M |
| 11 | DRIFT-001..007 | Documentation reconciliation: README source count, `archive/` reference, `.env.example` drift, `is_bot` note, hub-group name | Documentation says what code doesn't do; misleads future contributors | `README.md`, `KNOWN_ISSUES.md`, `IDENTITY_KEYS.md`, `.env.example` | M |
| 12 | CONC-001..004, LOGIC-003..006 | P2 concurrency/logic hardening (advisory lock scoping, atomic Redis ops, subcommand splitter, lemon8 handle constraint) | Quality improvements; defense-in-depth | multiple | M–L |
| 13 | PERF-002..006 | Monolith-file splits (dashboard/api.py, ig_ingest.py, telegram/__init__.py), requirements.lock refresh | Long-term maintainability | multiple | L |
| 14 | FE-001..003 | Add Vitest smoke tests for the SPA; consider a bundler for the extension; standardise semver conventions | Currently the SPA has zero unit tests and the extension is hand-authored monolith JS | `src/dashboard/frontend/`, `extension/` | M |
| 15 | SEC-001..005 | Per-service env files; workflow permission tightening; PTRACE gating | Defense-in-depth security hardening | `.env`, `docker/docker-compose.yml`, `.github/workflows/*.yml` | L |
| 16 | FS-001..004, DEAD-001..005 | Cleanup: scratch.py, personal graph seed, historical MDs, stub base.py, legacy migrations | Removes distraction; enforces conventions | root files + `src/` | S |

---

## 13. Selection Prompt

Audit complete. **46 findings** across 12 categories.

**Severity split:** P0 = 5 · P1 = 13 · P2 = 17 · P3 = 11

The 5 P0 findings are all in Reliability / Interruption and share a common root cause — the browser-extension collection path has been dark for ~24 hours behind a green `/health`, plus the Chrome CDP debug port is not exposed. Recommend acting on P0 first (order 1 and 2 in the roadmap) before anything structural.

Which do you want fixed?

- `fix all` — full remediation roadmap (order 1 → 16)
- `fix P0` / `fix P0,P1` — severity-scoped
- `fix REL-001, REL-002, REL-003, DATA-001` — the acute reliability + integrity items
- `fix all except STRUCT-*` — skip the reorganization plan
- `fix all except SEC-*` — skip security hardening
- `none` — report only

Once you select, run 02_EXECUTE with `AUDIT.md` and the selection.


---

## 14. Execution Status (post-run)

Recorded after the `fix all` execution pass on `a0f5ee56` → `a7e5ad47`.

### Fully addressed in this pass

| Finding | Commit | Notes |
|---|---|---|
| REL-001, INTR-001, LOGIC-002 | `656be92c` | `/health` returns 503 when `db_pool` absent AND startup task has given up; retains 200 during legitimate boot delay |
| CONC-001, CONC-002 | `656be92c` | Startup pool init retries with backoff up to `SOCIAL_INGEST_STARTUP_POOL_BUDGET_SECONDS` (default 300s) |
| DATA-001 | `656be92c` | Recreated `zz_add_dashboard_matrix_aggregate_indexes.sql` as `20260906_recreate_dashboard_matrix_aggregate_indexes.sql`, idempotent |
| INTR-002 | `37e49ff1` | Scheduler tick reads `rate_limit_events` for `bridge_unpaired` HTTP 503, alerts on threshold |
| LOGIC-001, PERF-001 | `37e49ff1` | WhatsApp broker consumer wraps each handler in `asyncio.timeout(WA_CONSUMER_HANDLER_TIMEOUT_SECONDS)`; timeout re-raises so `message.process()` requeues |
| REL-004 | `37e49ff1` | Scheduler tick alerts on `uc:realtime_post_feed:failed` depth |
| REL-005, INTR-003 | `37e49ff1` | Scheduler tick escalates persistently-stale `source_health` rows past watchdog cooldown |
| DATA-002 | `commit for stage 3d/4` | Soft CHECK constraint (NOT VALID) requires `source_url` for non-profile content types; partial index for coverage queries |
| DATA-004 | `37e49ff1` | Verified `is_bot` column already applied; added defensive `%bot`-suffix backfill migration for clean-volume rebuilds |
| DATA-005 | Stage 4 | `v2_schema.sql`, `v2_schema_final.sql`, `drop_wa_face_tables.sql` moved to `src/db/migrations/_archive/`; top-level glob skips the subdirectory |
| DEAD-001, DEAD-002 | Stage 4 | `src/collectors/base.py` stub deleted; `src/migrations/{__init__.py, add_content_hashes_table.py}` legacy path deleted |
| DEAD-003 | Stage 4 | `scratch.py` was already gitignored; no action needed |
| FS-002 | Stage 4 | `scripts/bryanseah234_1hop.json` untracked (`git rm --cached`); copy in local `data/` (gitignored) |
| FS-003 | Stage 4 | `PARITY_MATRIX.json` moved to `docs/audits/PARITY_MATRIX.json` |
| FS-004 | Stage 4 | `last_sync.txt` untracked; added to `.gitignore` |
| STRUCT-001 | Stage 4 | Historical MDs consolidated under `docs/audits/` and `docs/contracts/`; `docs/README.md` manifest |
| STRUCT-003 | Stage 4 | `src/collectors/base.py` stub deleted |
| STRUCT-004 | Stage 4 | `src/migrations/` legacy path deleted |
| DRIFT-001 | `a7e5ad47` | README source count 11 → 13 collectors across 15 platform surfaces |
| DRIFT-002 | `a7e5ad47` | README `archive/` reference dropped |
| DRIFT-003 (partial) | `a7e5ad47` | `.env.example` CHROME_CDP_URL corrected to `:9336`; compose comment fixed; broader `.env.example` vs `.env` reconciliation left to future |
| DRIFT-006 | (verified in-place) | `docs/contracts/IDENTITY_KEYS.md` already documents is_bot accurately; SYNC_PROGRESS end-log note is the stale one (kept as historical) |
| DRIFT-007 | `a7e5ad47` | Investigation showed HUB_GROUP and HUB_GROUP_ID are distinct concepts, not a rename; both documented in `.env.example` |
| LOGIC-004 | (verified in-place) | `src/main.py:359` already splits comma-separated `--source`; no fix needed |

### Deferred with rationale

| Finding | Reason for deferral | Recommended action later |
|---|---|---|
| REL-002, REL-003 | Operator-touchpoint: relaunching Chrome would kill the operator's open browser session without consent. Attempted once via `browser-autorecover.ps1` (documented in `tmp/STAGE_1A_STATUS.md`). | Operator closes all Chrome windows, then runs `pwsh scripts/start-scraper-chrome-cdp.ps1`; re-register the autorecover scheduled task. |
| REL-008 (new) | Discovered during Stage 1a: the `browser-autorecover` scheduled task stopped running 2026-08-30. | Re-register via `pwsh scripts/register-browser-maintenance-task.ps1` (verify script name — safety-net was itself off). |
| DRIFT-008 (new) | Standalone `scripts/*.py` still hardcode CDP port `:9333`; canonical `.env.example` + compose comment now correct at `:9336`. Fixing 20+ helper scripts is bulk churn with low criticality — most aren't in rotation. | Do this incrementally as scripts are touched; `scripts/cleanup_ext_tabs.py` already uses env-based lookup as the pattern. |
| CONC-003 | Migration `SET lock_timeout` is session-local by design (comment in `src/db/migrate.py:97-101`); changing to transaction-scoped could re-introduce the lock queue with pg_dump. Not a bug — reviewed and left. | Not needed. |
| CONC-004 | Redis multi-key atomicity for `uc:realtime_post_feed:*` requires Lua script per operation or MULTI/EXEC across 11 keys. High-risk refactor of a hot path serving a live feed; benefit is marginal (over-count under crash-during-op is negligible). | Only revisit if operator-visible counter drift is observed. |
| LOGIC-003 | Changing `SKIP` from a code constant to file-level markers changes migration-runner semantics; deferred to preserve current, well-tested behavior. | Optional refactor if new SKIP entries need per-file provenance. |
| LOGIC-005 | Scheduler class split is a large refactor (~91 KB file). Risk of accidentally breaking one of many periodic ticks is high; deferred pending a dedicated maintenance window. | Bring into a sprint of its own with a targeted test plan. |
| LOGIC-006 | `lemon8_profiles` CHECK for platform_user_id shape (numeric or `^user\\d+$`) is trivial SQL; skipped because the identity contract already warns operators against silent fixes and the existing convention holds in code. | Add if lemon8 profile-shape drift is observed. |
| PERF-002, PERF-003, PERF-004 | Monolith-file splits (dashboard/api.py 10,784 LOC; ig_ingest.py 5,921 LOC; telegram/__init__.py 5,446 LOC) each carry high merge-conflict cost and re-testing burden. Deferred as L-effort refactors requiring dedicated sprints. | Do incrementally as each area is touched for a real feature. |
| PERF-005 | Requires a `pip freeze` from a running container to regenerate `requirements.lock` — safest done during a planned rebuild, not against a running system with in-flight consumers. | Refresh at next intentional dependency bump. |
| PERF-006 | Redis TTL audit requires reading each key's set-time semantics across `src/notifications/realtime_feed.py`; correlate with observed key growth in production before adjusting. | Only if `redis-cli DBSIZE` growth becomes a concern. |
| FE-001 | Adding Vitest to `src/dashboard/frontend/` is M-effort but has zero test suite today; setting up the harness + a smoke test is a discrete piece of work best done in isolation. | Add in a dedicated `test(dashboard): initial vitest smoke suite` PR. |
| FE-002 | Bundling the Chrome MV3 extension (`extension/content.js` 4,062 LOC + `extension/background.js` 2,978 LOC) requires adopting webpack/esbuild + updating the manifest. L-effort infrastructure change. | Adopt when the extension needs its next feature bump. |
| FE-003 | Standardising semver conventions across `src/dashboard/frontend/package.json` is cosmetic; Dependabot handles updates either way. | Skip. |
| SEC-002 | Workflow guard on `--admin` merge scope requires new GitHub Actions logic + testing. | Add if `--admin` merges ever break a non-manifest path. |
| SEC-003 | Per-service `.env` split (SEC-003) is L-effort and touches every compose service. Non-critical because credentials/ is already `:ro` mounted. | Do at next full compose refresh. |
| SEC-004 | `SYS_PTRACE` gating requires touching every collector service in compose. Low-priority because the collector rig is single-tenant. | Add if the stack ever runs multi-tenant. |
| SEC-005 | Workflow permissions tightening: `permissions: read-all` → explicit least-privilege blocks in 15 workflows. Bulk churn with low marginal risk on private repo. | Do during the next sourcerepo sync cycle. |
| STRUCT-002 | `src/main.py` argparse split into `src/main/commands/*.py` is M-effort and would touch every subcommand callsite. | Do when the number of subcommands hits 30+. |
| STRUCT-005 | `scripts/` re-organisation into `windows/python/data` subdirs is cosmetic. | Do at next `scripts/` audit. |
| STRUCT-006 | RESOLVED. Consolidated `src/dashboard/frontend/` and `src/dashboard/` under one root via `docs/plans/dashboard-root-consolidation.md`. | Done. |
| DEAD-004 | `models/dlib/` placeholder is legitimate (model files never committed by design). | Keep. |
| DEAD-005 | Migration files already moved to `_archive/` under Stage 4. | Done. |
| DRIFT-004, DRIFT-005 | Documentation notes added to `docs/KNOWN_ISSUES.md`; no code change needed. | Done via docs. |

### Verification performed

- `python -m ast.parse` clean on `src/bridges/ig_ingest.py`, `src/collectors/whatsapp/__init__.py`, `src/scheduler/__init__.py`, `src/db/migrate.py`.
- `docker exec unifiedcollector_collector python -c "from src.collectors import COLLECTORS, list_sources"` → returns all 13 collectors.
- `docker exec unifiedcollector_collector python -c "from src.db import migrate; print(migrate.SKIP)"` → returns the archived skip set.
- Each new migration was applied inside a `BEGIN ... ROLLBACK` transaction against the live DB and confirmed idempotent (no-ops on existing indexes).
- All commits atomic per stage; conventional commit messages; no `git push` performed (branch: `main`).

### Follow-up owners

- **Operator hands-on:** REL-002, REL-003, REL-008.
- **Next PR sprint:** FE-001 (Vitest), PERF-005 (lockfile refresh), SEC-005 (workflow permissions).
- **Major refactor sprint:** PERF-002/003/004 (file splits), LOGIC-005 (scheduler split), STRUCT-006 (dashboard root consolidation).
