# UnifiedCollector — Principal Systems Architect Design Review
Date: 2026-05-30 | Reviewer: skeptical design-review pass | Commit: a612991

This is an adversarial review. It assumes nothing works until the code proves it.
Claims in docs/memory were checked against source and the live database.

---

## 1. CURRENT STATE REPORT

### Objective
A **read-only unified ingestion plane** for 11 source platforms (telegram, instagram,
tiktok, lemon8, youtube, github, strava, search, website, whatsapp, beeper/matrix).
It observes and archives; it never writes back to source platforms (enforced by
convention, not by code — see risks). Media to disk, structured rows to Postgres.

### Architecture status: WORKING BUT FRAGILE
- 10-container docker-compose stack, all healthy at review time.
- Single monolithic `collector` process runs ALL 11 collectors as asyncio tasks in
  one event loop, supervised by an in-process watchdog.
- Telegram is the mature path (4 accounts, 1500+ chats, ~9.5k msgs, spider queue).
- Instagram/TikTok event-loop blocking just fixed (p6, run_in_executor).

### Tech stack
- Python 3.12, asyncio, asyncpg, FastAPI+uvicorn (dashboard), Telethon, instaloader,
  yt-dlp, gallery-dl, playwright, matrix-nio[e2e], python-telegram-bot.
- Postgres 16 (pgvector image), RabbitMQ 3.13, Redis 7, Tor sidecar.
- Node/TS WhatsApp bridges (2x, Baileys-style) via RabbitMQ+Redis.
- Storage: docker NAMED volumes (media_data, sessions_data) on WSL2 ext4.

### Orchestration pipeline (as-built)
```
scheduler ──writes──> collection_schedules / collection_runs(queued) / targets(pending)
   │                                  │
   │ (NO HANDOFF — decoupled)         X  collection_runs never consumed (292 stuck queued)
   ▼                                  ▼
worker ──polls every 300s──> collection_targets WHERE status IN (pending,error)
   └─ launches 11 collector tasks, staggered 3s, watchdog relaunches on death
```

### Storage / memory
- `media_items` + `service_cursors` = generic base layer.
- Per-source tables (github_*, instagram_*, telegram_* x11, etc.).
- Checkpoint/cursor per source. Disk-first dedupe (scan filenames into a set).

### Observability: WEAK
- Logging only (stdout). Health = liveness row in `service_cursors('_worker')`.
- NO metrics (prometheus/otel/statsd), NO tracing, NO error aggregation (sentry).
- Health module is 23 lines. Dashboard reads DB counts.

### Reliability/recovery
- Per-source crash counter, exponential backoff, max 5 restarts then give-up.
- Atomic file writes (tmp+fsync+rename). Circuit breaker, DLQ table.
- Boot autostart via Startup .bat (re-syncs sessions, restarts collector).

### Config
- Single 173-line `.env` (4 stale backups beside it). No schema/validation.
- Behaviour gated by ~dozens of `os.getenv(...).lower() in {true,1,yes}` checks
  scattered across collectors. No central config object.

### Testing: SHALLOW
- 23 test files (one per collector + core units). pytest_cache present.
- NO CI (.github/workflows absent). Tests run only when someone remembers.
- No integration test that boots the stack. `tests/verify_production.py` exists.

---

## 2. SYSTEM DIAGRAM (text)

```
                         ┌──────────────────────────────────────────┐
   Windows host          │           docker compose network          │
   Startup .bat ───────► │                                            │
   (boot, session sync)  │  ┌────────────┐   ┌──────────────────────┐ │
                         │  │ postgres16 │◄──┤ collector (MONOLITH) │ │
   Beeper Desktop        │  │ +pgvector  │   │  asyncio event loop  │ │
   127.0.0.1:23373 ◄─────┼──┤            │   │  ├ telegram (4 wrk)   │ │
   (host-gateway)        │  └─────┬──────┘   │  ├ instagram          │ │
                         │        │          │  ├ tiktok/lemon8/yt   │ │
   Tor sidecar ◄─────────┼─ tor   │          │  ├ github/strava/...  │ │
   (search only)         │        │          │  ├ beeper (matrix)    │ │
                         │        │          │  └ watchdog+health    │ │
                         │  ┌─────▼──────┐   └──────────┬───────────┘ │
                         │  │ scheduler  │ (decoupled)  │ writes        │
                         │  └────────────┘              ▼              │
                         │  ┌────────────┐   media_data (named vol)    │
                         │  │ dashboard  │   sessions_data (named vol) │
                         │  │ :8700 FastAPI                            │
                         │  └────────────┘                            │
                         │  ┌──────────┐ ┌───────┐ ┌────────────────┐ │
                         │  │ rabbitmq │ │ redis │ │ wa-bridge 1 & 2 │ │
                         │  └──────────┘ └───────┘ │  (Node/TS)      │ │
                         │       ▲──────────▲──────┘                  │
                         │  ┌──────────┐  onboard_bot (telethon)      │
                         │  │ backup   │  (manual profile only)       │
                         │  └──────────┘                              │
                         └──────────────────────────────────────────┘
```

---

## 3. CAPABILITY MATRIX

| Capability                         | Status   | Notes |
|------------------------------------|----------|-------|
| Telegram ingestion + media         | DONE     | 4 acct, spider, hot-reload, reactions/members |
| Instagram ingestion                | PARTIAL  | IP-blocked (no proxy), cookie/instaloader hybrid |
| TikTok ingestion                   | PARTIAL  | async subprocess gallery-dl; just unblocked |
| YouTube / GitHub / Strava / Search | PARTIAL  | code present, runtime maturity unverified |
| Website / Lemon8                   | PARTIAL  | present |
| WhatsApp (TS bridges)              | PARTIAL  | 2 bridges, RabbitMQ path; auth state external |
| Beeper / Matrix shadow             | PARTIAL  | polymorphic Local API path |
| Worker supervision / watchdog      | DONE     | crash counts, backoff, relaunch |
| Scheduler → worker handoff         | MISSING  | decoupled; collection_runs never consumed |
| Schema reproducibility             | MISSING  | 7/11 telegram tables only in unapplied migrations/ |
| Migration runner                   | MISSING  | migrations/*.sql applied by hand only |
| Metrics / tracing                  | MISSING  | logs only |
| CI/CD                              | MISSING  | no workflows, no gate |
| Dependency pinning / lockfile      | MISSING  | all >=, no lock |
| Config validation                  | MISSING  | raw os.getenv everywhere |
| Outbound-write prevention          | PARTIAL  | convention/README only, not enforced |
| Backups                            | PARTIAL  | manual profile, pg_dump loop, not scheduled |
| Secrets management                 | WEAK     | 173-line .env + 4 plaintext backups in repo dir |

---

## 4. TOP 20 IMPROVEMENT OPPORTUNITIES (ranked by impact)

1. **Schema reproducibility** — fold migrations/*.sql into schemas/ OR build a real
   migration runner with a schema_migrations ledger. Today a clean volume = broken DB.
2. **Migration runner** — ordered, idempotent, recorded. Stop hand-applying psql.
3. **Scheduler↔worker contract** — either make worker consume collection_runs and
   mark started/running/completed, or delete the dead queued-runs bookkeeping (292 stuck).
4. **CI pipeline** — GitHub Actions: lint + pytest + schema-boot-on-clean-volume test.
5. **Dependency lockfile** — pin via uv/pip-tools; reproducible builds. Currently any
   rebuild can silently pull a breaking telethon/yt-dlp/instaloader.
6. **Metrics** — prometheus exporter: items/sec per source, queue depth, error rate,
   rate-limit hits, account cooldowns. You are flying blind on throughput.
7. **Split the monolith** — one blocking/crashing collector still shares a loop+process
   with 10 others. Move heavy/risky collectors (instagram, tiktok, whatsapp) to
   separate processes/containers; keep the watchdog per-process.
8. **Config object** — typed settings (pydantic-settings) parsed once; fail fast on
   missing/invalid. Kills the scattered getenv-truthy duplication.
9. **Secrets hygiene** — remove .env.bak.* from the working dir; move to a secrets
   store or at least gitignore+chmod. 173 secrets in plaintext beside the repo.
10. **Enforce read-only** — a write-guard wrapper around every platform client so an
    accidental send/react/edit call raises. README convention is not a control.
11. **Structured logging** — JSON logs with source/account/target fields; current
    free-text logs are hard to query and the httpx-buffer-freeze hack proves log I/O
    is on the critical path.
12. **Backpressure on spider queue** — unbounded auto_backfill enqueue (1500+); add
    rate/size caps + priority aging so discovery can't starve collection.
13. **DLQ consumer** — dead_letter_queue is written; verify it is drained/retried,
    else failures rot silently.
14. **Idempotent media dedupe via DB** — disk-scan-into-set is O(files) per boot and
    racy across workers; rely on media_items unique(source,content_id) + content hash.
15. **Health depth** — per-source last-success timestamp + staleness alerting, not a
    single _worker liveness row.
16. **Scheduled backups** — backup is profile=manual; nothing runs it. Wire a cron/
    schtasks or flip to a real schedule; verify restore.
17. **Graceful interruption** — collectors mid-write on SIGTERM: confirm checkpoint is
    committed before exit so a reboot doesn't double-collect or lose cursor.
18. **Account-pool fairness** — verify per-account quota/cooldown is enforced centrally,
    not per-collector, to avoid one source burning a shared account's budget.
19. **Dashboard authz** — JWT present; verify endpoints are actually gated and the
    dashboard isn't exposing raw collected PII on :8700 without auth.
20. **Prune dead code/docs** — 200KB analysis txt, dup PORT_PLAN v1/v2, archive/,
    telegramcollector/, tmp/. Signal-to-noise hurts onboarding and audits.

---

## 5. HIDDEN RISKS / BOTTLENECKS / FAILURE MODES

### State corruption / reproducibility
- **CLEAN-VOLUME BOOT IS BROKEN.** 7 of 11 telegram tables (reactions, members,
  polls, user_accounts, user_changes, discussion_visits, reaction_counts) exist only
  in the live DB and in unapplied migrations/. `init_db()` globs schemas/ only.
  Disaster-recovery from scratch will not reproduce the working DB. **This is the #1
  latent outage.**
- Session files live on a named volume but the authoritative copies are host files
  re-synced by the boot .bat. If the .bat is skipped (manual `docker compose up`),
  the volume can drift to unauthorized sessions → silent telegram auth failure.

### Concurrency / race conditions
- Single event loop for 11 collectors: any un-wrapped sync call (the class of bug
  just fixed for instagram/tiktok) freezes ALL collectors, not just one. The fix was
  applied; the architecture still permits the next instance of it.
- `BaseCollector._scan_existing_media()` builds a per-instance `_known_ids` set; with
  the telegram multi-worker model and shared media_dir, dedupe state can race. DB
  unique constraint is the only real guard.
- Scheduler uses FOR UPDATE SKIP LOCKED (good) but flips ALL completed/error targets
  back to pending on every tick — a long-running collector can be re-triggered while
  still working.

### Interruption failure modes
- Watchdog "give up after 5 restarts" means a persistently-failing source goes dark
  with only a log line — no alert. Combined with no metrics, you won't notice.
- httpx log volume can block stdout and freeze the loop (already hit; mitigated by
  silencing httpx to WARNING). Root cause — synchronous log I/O on the hot path —
  remains.

### Scalability
- Disk-scan-on-every-run is O(files); media volume will make boot/run slower over time.
- collection_runs accumulates forever (292 and counting) with no consumer/GC.
- Unbounded spider enqueue vs. fixed worker cadence = ever-growing backlog.

### Coupling / maintainability
- Dashboard, scheduler, worker, bots all re-`init_db()` independently — 4 copies of
  the schema-apply loop, all reading schemas/ only.
- Collectors are 1–2.6k LOC each (instagram 2661, telegram 2567) — large, hard to test
  in isolation, mixed concerns (auth, rate-limit, fetch, parse, persist).
- whatsappcollector/ is a separate TS codebase pulled into this compose; build context
  coupling and an untracked dir in git status.

### Security
- 173-secret .env + 4 plaintext backups in the project dir. Tor only scopes 3 sources.
- Read-only is a convention; archive/ retains outbound code as "reference" — one bad
  import away from contamination.

---

## 6. CHALLENGED ASSUMPTIONS

- "Telegram is done." — Ingestion works; **disaster recovery does not** (schema drift).
  Done-for-now ≠ reproducible.
- "restart: unless-stopped covers boot." — Only if Docker Desktop + the .bat both run;
  manual compose up bypasses session re-sync.
- "We have a scheduler." — It schedules nothing the worker obeys; it's a clock writing
  to a table nobody reads.
- "Tests exist." — They do, but nothing runs them and nothing tests the integrated
  stack or a clean-volume boot.
- "Read-only by design." — By documentation. No code-level guard enforces it.
```
```
```
