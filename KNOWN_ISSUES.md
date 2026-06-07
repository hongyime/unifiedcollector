# Known Issues / Architectural Debt

Living tracker of unresolved architectural concerns. Distilled from the
2026-05-30 design review (that dated snapshot was removed; this is the
actionable residue). Update or strike items as they're fixed.

## Open

1. ~~**Clean-volume boot is broken.**~~ → Resolved, see below.

2. ~~**Scheduler <-> worker contract is dead.**~~ → Resolved, see below.

3. ~~**No CI.**~~ → Resolved, see below.

4. ~~**No dependency lockfile.**~~ → Resolved, see below.

5. ~~**Read-only is convention-only.**~~ → Resolved, see below.

6. ~~**Secrets hygiene.**~~ → Resolved, see below.

7. ~~**4x duplicated schema-apply.**~~ → Resolved, see below.

8. **Observability gap.** Prometheus `/metrics` endpoint exists with 12 metrics
   (totals, throughput, staleness, spider queue, DLQ, runs, worker liveness,
   dead sources, error rate, cycle duration). Still missing: per-account
   rate-limit hit counters, account cooldown/quota metrics. Consider exposing
   `account_quota_usage` table via metrics.

## Resolved

- **Clean-volume boot (was #1)** -- DONE. The `src.db.migrate.apply_all()`
  runner applies base `schemas/*.sql` (idempotent) then incremental
  `migrations/*.sql` (ledger-tracked, apply-once). Missing tables
  `graph_edges` and `wa_discovered_links` added as migrations. CI now runs
  `verify_clean_boot.py` against a throwaway pgvector database on every PR.
  (2026-06-07)

- **Scheduler <-> worker contract (was #2)** -- DONE. `mark_target_collected()`
  now writes `status='completed'` (was `'active'`, which the worker's
  `_load_targets` filter skipped — targets became invisible until the next
  scheduler tick). Scheduler creates runs as `'running'` and closes as
  `'completed'` (was stuck as `'queued'`). GC prunes runs older than 7 days.
  `collection_runs` is retained as a scheduler audit log (dashboard reads it);
  the worker intentionally does not consume it. (2026-06-07)

- **CI (was #3)** -- DONE. `.github/workflows/python-ci.yml` runs on every PR
  touching `*.py`: ruff lint (`F` + `E9` rules), pytest (unit tests), and a
  clean-volume schema boot test against pgvector:pg16. The existing JS build
  CI (`.github/workflows/ci.yml`) remains for dashboard frontend. (2026-06-07)

- **Dependency lockfile (was #4)** -- DONE. `requirements.lock` has exact pins
  from the proven running container (2026-05-30). `docker/Dockerfile` installs
  from the lock, not `requirements.txt`. Hand-maintained but reproducible.

- **Read-only guards (was #5)** -- DONE. Static tripwire
  (`tests/test_readonly_guard.py`) scans for outbound method patterns at PR
  time. Runtime guard added for Telethon: `ReadOnlyTelegramClient` wrapper
  (`src/core/readonly_client.py`) blocks `send_message`, `edit_message`,
  `delete_messages`, etc. at runtime. Other clients (httpx, instaloader,
  yt-dlp) are inherently read-only (HTTP GET / subprocess download only).
  (2026-06-07)

- **Secrets hygiene (was #6)** -- DONE. `.env.bak.*` files moved out of repo
  to `~/.unifiedcollector_env_backups/`. Historical commits containing secrets
  (`.env.bak.*`, archive toolkit `.env.template`/`.env.example` files) purged
  via `git filter-repo` (2026-06-07). `.gitignore` covers `.env`, `.env.bak.*`,
  `credentials/`, `sessions/`, `*.pickle`, `client_secret.json`. See SECURITY.md
  for the full secrets layout.

- **4x duplicated schema-apply (was #7)** -- DONE. All four `init_db()` call
  sites (main, worker, scheduler, dashboard) now delegate to
  `src.db.migrate.apply_all()`, the single DDL authority. Base `schemas/*.sql`
  applied idempotently every boot + incremental `migrations/*.sql` tracked via
  a `schema_migrations` ledger. (Consolidated as part of the P0-1/P0-2 work.)

- **Monolith split (was #7)** -- DONE. Every source now runs in its own
  dedicated container (`collector_<source>`) via `worker --source X`, each with
  a tuned `mem_limit`. The main `collector` disables all sources and idles as a
  safety-net/image-builder. instagram has no running container yet (host IP
  429-blocked); add one when the block clears. A blocking/OOMing collector can
  no longer take down its siblings. (Resolved 2026-06-02; previously only
  youtube + tiktok were split.)
