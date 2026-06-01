# Known Issues / Architectural Debt

Living tracker of unresolved architectural concerns. Distilled from the
2026-05-30 design review (that dated snapshot was removed; this is the
actionable residue). Update or strike items as they're fixed.

## Open

1. **Clean-volume boot is broken (schema reproducibility).**
   Several telegram tables (reactions, members, polls, discussion_visits, etc.)
   exist only via hand-applied `migrations/*.sql`, not in `schemas/`. A fresh
   Docker volume comes up with a partial schema. Fix: fold `migrations/*.sql`
   into `schemas/` OR build a real ordered/idempotent migration runner with a
   `schema_migrations` ledger. (DR != ingestion; ingestion works.)

2. **Scheduler <-> worker contract is dead.**
   The scheduler writes `collection_schedules` / `collection_runs(queued)` /
   `targets(pending)` but the worker consumes none of it -- it's a clock writing
   to tables nobody reads (~292 stuck queued runs). Fix: either make the worker
   consume `collection_runs` (mark started/running/completed) or delete the dead
   bookkeeping.

3. **No CI.** Tests exist but nothing runs them; nothing tests the integrated
   stack or a clean-volume boot. Add GitHub Actions: lint + pytest +
   schema-boot-on-clean-volume.

4. **No dependency lockfile.** Any rebuild can silently pull a breaking
   telethon / yt-dlp / instaloader. Pin via uv / pip-tools.

5. **Read-only is convention-only.** No code-level guard. Add a write-guard
   wrapper around every platform client so an accidental send/react/edit raises.

6. **Secrets hygiene.** Plaintext `.env` / `.env.bak.*` beside the repo. Move to
   a secrets store, or at minimum gitignore + chmod.

7. **4x duplicated schema-apply.** dashboard, scheduler, worker, bots each
   `init_db()` independently against `schemas/` only. Consolidate.

8. **Observability gap.** No metrics on items/sec per source, queue depth, error
   rate, rate-limit hits, account cooldowns. Consider a prometheus exporter.

## Partially addressed

- **Monolith split** -- whatsapp now runs as its own bridge (`src/bridges/whatsapp`)
  and youtube/tiktok run in separate collector containers. instagram still shares
  the main worker loop; a blocking collector there can still stall siblings
  (mitigated by the per-worker zero-progress watchdog).
