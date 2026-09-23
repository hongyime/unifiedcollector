# backup-slow-reintroduction.md

Author: agent handoff, 2026-09-19, updated 2026-09-22. Status: Phase A shipped;
Phase B's first live attempt is CONFIRMED CORRUPT (not merely unverified);
Phase C gated on a fresh trial. Scheduling is Phase C.

## Current status — 2026-09-22 update (supersedes the 2026-09-19 notes below)

- **Phase A:** script committed in `a8da528f`; dry-run demonstrates command
  construction only, not throughput or collector impact.
- **Phase B — DEFINITIVE verdict: the orphan is corrupt, not just unverified.**
  `uc-pgdump-raw2` is absent. Its only remaining artifact,
  `20260919T0741.dump.tmp` (589,962,062 bytes), was restored into an isolated,
  network-disconnected scratch PostgreSQL 16.13 container (`--network none`,
  no bind mounts, destroyed immediately after). `pg_restore -U postgres
  --no-owner --no-privileges --jobs=1 -d scratch` failed with:
  ```
  pg_restore: error: could not read from input file: end of file
  ```
  This is a genuine truncated-archive error, not an auth/tooling artifact —
  the file was cut off mid-write when the original dump process died. The
  earlier `pg_restore --list` success (992 TOC entries, header-only) was
  consistent with this all along: a truncated custom-format archive can still
  have a readable header. **This file cannot be used as a backup. Do not
  rename it to `.dump`; delete it or leave it named `.tmp` so nothing mistakes
  it for valid.** Phase B needs a genuinely fresh, completed dump attempt —
  not a re-verification of this one.
- **Phase C:** do not add a second scheduled task. Existing
  `UnifiedCollectorBackup` is enabled at **03:30 SGT**, invokes `backup.bat`
  and the deleted `src.backup.db_backup` module, and has a one-hour runtime limit.
  Last task result was `0x41306` (terminated), not success. An attempt to disable
  this obsolete task was denied by Windows permissions; it remains enabled.
  An elevated operator can stop future obsolete launches with
  `Disable-ScheduledTask -TaskName UnifiedCollectorBackup`.
- **Phases D/E:** future work. This recovery did not start another dump, enable
  a new schedule, or prune backups.

## Original 2026-09-19 recovery notes (superseded above, retained for history)

- Another session was running analyzer backup/restore drills during this check;
  both exited cleanly (status 0) by 2026-09-22 and are no longer active.
  Coordinate with any future concurrent session before a fresh trial.
  Only after successful dump completion and accepted collection-impact evidence
  should the existing task be replaced with the new wrapper at **04:00 SGT**.

The proposal below is retained as design history; corrected guarantees and
rollout instructions take precedence over its earlier estimates.

## What went wrong last time

### Attempt 1 — pg_basebackup with default wal_sender_timeout (Sep-15)
`pg_basebackup --wal-method=stream` on a ~10 GB compressed cluster took several
hours because the R2 job throttled to `--max-rate=2M`. The WAL sender connection
sat idle for ~1 min at a time between segment ships during that window and
Postgres closed it at the default `wal_sender_timeout=1min`. Backup died at
2.05 GB with `terminating connection due to wal_sender_timeout`. This is a
Postgres server-side timer, not a client bug — pg_basebackup did not misbehave.

**What I fixed**: `ALTER SYSTEM SET wal_sender_timeout = '10min'` then
`pg_reload_conf()`. Backup passed the 2 GB failure point cleanly on retry.

**Why that fix wasn't enough**: it removed the timeout failure mode but did not
fix the underlying speed problem. See Attempt 2.

### Attempt 2 — pg_basebackup at 2 MB/s on SMB share (Sep-18)
Same command, wal_sender_timeout=10min now. Ran 8+ hours to 11.6 GB and still
had not finished. Root cause was the combination of `--max-rate=2M` (deliberate
throttle) + writes going over SMB to `Z:` from a Windows share. SMB latency +
tar-format's write-heavy pattern gave effective throughput of 0.6-1.5 MB/s even
though the throttle allowed 2 MB/s. Backup was correct but glacially slow, and
Postgres sat at 200%+ CPU the entire time (single-thread client-side gzip:1
compression eats one core, plus WAL sender activity).

**What I tried**: killed it. There is no clean "resume" for pg_basebackup —
the partial output blocks the next run.

### Attempt 3 — pgBackRest with process-max=4 (Sep-19)
Switched to pgBackRest which supports parallel workers + zstd + native resume.
Configured `process-max=4 compress-type=zst compress-level=3`. Immediately hit
**676% CPU on the postgres container** (6.7 cores worth) which caused a
Postgres restart under WSL2 memory pressure. The partial `20260919-020739F`
backup was orphaned.

**Why the parallelism hurt**: pgBackRest spawns N workers that each open their
own read connection to Postgres. On our 4-core WSL2 VM those workers competed
for CPU with the running collectors + scheduler + dashboard. Postgres itself
was fine — the *container* was fine — but Docker Desktop's WSL2 memory limit
noticed the whole-system pressure and reaped Postgres.

### Attempt 4 — pgBackRest with process-max=2 + resume (Sep-19)
Retried at lower parallelism. pgBackRest's resume path detected the orphaned
`20260919-020739F` and started restoring valid files + streaming the rest.
At 4.7 GB / 90+ min elapsed the operator killed it because sustained
postgres CPU was blocking other work.

**What I learned**: parallel-compressed physical backups against a hot
Postgres on a constrained VM with a slow output disk (SMB) are not a good
fit for a "silent nightly" pattern. The tool was correct, the environment
was wrong.

## Why pg_dump is different (why this proposal is different)

`pg_dump` is a *logical* backup — it reads data through the SQL protocol as
`COPY` streams, not through the WAL sender + tar-file interface. That changes
every failure mode above:

| Concern | pg_basebackup / pgBackRest | pg_dump |
|---|---|---|
| WAL sender timeout | Yes — the whole class of failure | Not applicable, no replication protocol |
| Single long-lived connection | Yes | Yes — one database snapshot spans successive table COPY operations |
| Throughput bottleneck | Whole-cluster physical pages | Row-serialized data, much smaller |
| Locks and workload impact | Mechanism-dependent; measure against the running workload | ACCESS SHARE normally permits row writes but conflicts with some DDL; snapshot and I/O impact still need measurement |
| Output pattern | Thousands of small files (pgBackRest) | One `.dump` archive file |
| SMB write penalty | Devastating (small-file random write) | Modest (one large sequential write) |
| Restore | Physical restore plus required WAL, using the matching backup tool | `pg_restore` from custom-format dump |
| Point-in-time recovery | Yes (WAL replay after base) | No — restores at dump timestamp |
| Cluster-wide dump | Everything including postgres system tables | Just the one database (unifiedcollector) |
| Wall time on this cluster | Historical attempts took multiple hours | No completed new-wrapper measurement yet; SMB output may also take multiple hours |

**We give up point-in-time recovery.** The intended tradeoff is lower operational
impact, to be measured. Recovery is limited to the last successful database
snapshot; failures and multi-hour dump duration can make loss exceed 24 hours.
A logical dump of one database is not a complete cluster/media backup.

## Proposed design

### Constraints (must-haves)
1. **Minimize collector interference**: use a read-only consistent snapshot;
   `--serializable-deferrable` may wait for a safe snapshot and does not remove
   ACCESS SHARE locks or the need to avoid concurrent DDL.
2. **Constrain the client**: launch the dumper with
   `--cpus=0.5 --memory=256m`. These limits do not cap PostgreSQL's server work;
   measure server CPU and ingestion continuity separately.
3. **Exclude cooperating backups**: `pg_try_advisory_lock` on a fixed key makes
   a second wrapper invocation exit. Raw pg_dump and other tools do not honor
   this lock automatically.
4. **Prefer sequential SMB writes**: one `.dump` file, compression level 1,
   `--jobs=1`. A slow or unavailable share can still block the client.
5. **Runs at quiet hour**: 04:00 SGT default (post-peak-collection window).
   Env-configurable.
6. **Skip if fresh**: default 20 h freshness window — a container restart
   after a successful dump does not immediately re-dump.
7. **Kill switch**: pass `BACKUP_ENABLED=0` in the invoked script's environment.
   The script does not load `.env` itself; disabling the Windows task is the
   separate control for future scheduled launches.

### Architecture (much smaller than last time)
- One Python script: `scripts/pg_dump_backup.py`.
- Runs from the collector image (pg client tools already there) via
  `docker run --rm` so no long-lived sidecar container.
- Triggered by:
  - **Windows Task Scheduler** (host-side cron equivalent) at 04:00 SGT daily.
    Owner: operator, not baked into compose.
  - **OR** by a new scheduler handler `PgDumpBackupHandler` if we want it
    inside the container graph.
- Output: `Z:/unifiedcollector/backups/db/<YYYYMMDDTHHMM>.dump` (custom
  format, level 1) + `<YYYYMMDDTHHMM>.manifest.json` next to it.

### Retention (bounded — never grows)
- Daily: last 7
- Weekly (any Sunday dump): last 4
- Monthly (any first-of-month dump): last 6
- Prune ONLY runs after a verified-good dump exists (never delete the last
  good dump).

### Verification without restore (fast)
- After every successful dump: `pg_restore --list <dump>` — parses the archive
  table-of-contents. This is a quick structural check, not a full data-integrity
  check. Require pg_dump exit 0 as well; use a restore drill for recovery proof.
- Weekly (Sunday): also load the dump into a scratch database, count rows on
  the top-5 tables, drop the scratch database. This is the slow drill but only
  weekly.

### Failure handling
- Dump failure → keep the previous dumps, do not prune, log the failure with
  its exit code, send one Telegram alert (best-effort), exit.
- If `pg_dump` exits nonzero → remove the partial `.dump.tmp` file, don't
  rename to final. Old backups untouched.
- Advisory lock timeout → exit 0 with a log "another dump in progress".

## Roll-out plan

### Phase A — dry-run today (no state change)
- Land `scripts/pg_dump_backup.py` with `--dry-run` support.
- Run it manually: `python scripts/pg_dump_backup.py --dry-run` from a
  configured environment. It prints the intended arguments and may create the
  output directory; it does not contact PostgreSQL or perform a dump. Resource
  and collection-impact measurements belong to the live Phase B trial.

### Phase B — one live dump this week
- Run once with `python scripts/pg_dump_backup.py` at a chosen quiet moment,
  with the required environment and matching PostgreSQL client installed.
  Omit `--dry-run`; `--dry-run=false` is not a supported argument.
- Confirm the output at `Z:/unifiedcollector/backups/db/*.dump`.
- Confirm pg_dump exit 0, TOC verification, and final manifest. Record actual
  wall time, output growth, PostgreSQL CPU and ingestion before/during/after.
- Confirm collectors kept ingesting during the dump (telegram_messages
  count / 15 min doesn't drop noticeably).
- Multi-hour duration is acceptable only with continued progress and accepted
  collector impact. Do not infer low server impact from a client resource cap.
- Manifest live row counts are monitoring samples, not a comparison against the
  archive's consistent snapshot and not a substitute for a restore drill.

### Phase C — nightly cron (opt-in via Task Scheduler entry)
- Register a Windows Task Scheduler entry that runs the script at 04:00 SGT.
- Do NOT bake this into compose or into the scheduler handler list yet.
  Operator can un-register the task with one click.
- Run for a week. Watch for any collection dip, disk fill, or noise.

### Phase D — automate + alert (only after Phase C is quiet)
- Add `PgDumpBackupHandler` to `src/scheduler/handlers/` that runs the same
  script on schedule from inside the scheduler container. Then remove the
  Task Scheduler entry.
- Wire the failure Telegram alert path.
- Bring back `backup_status()` (not just a stub) so the dashboard's health
  panel surfaces "last successful dump, age" again.

### Phase E — optional off-site copy (Q4 or later)
- Add an rclone/aws copy of just the newest verified dump to Cloudflare R2
  or Backblaze B2 after each successful dump. Bandwidth-bounded, retention
  independent.
- Only if operator wants the extra layer.

## What Phase A ships

`scripts/pg_dump_backup.py`:
- Reads config from env (`POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `POSTGRES_DB`, `BACKUP_DIR`, `BACKUP_ENABLED`, `BACKUP_SKIP_IF_FRESH_HOURS`).
- Acquires advisory lock `pg_try_advisory_lock(hashtext('unifiedcollector_pg_dump'))`.
- Skips if last dump < 20h old.
- Writes to `<BACKUP_DIR>/<YYYYMMDDTHHMM>.dump.tmp` via `pg_dump --format=custom
  --compress=1 --jobs=1 --no-owner --no-privileges`.
- Verifies with `pg_restore --list`.
- Atomic rename to `<YYYYMMDDTHHMM>.dump`.
- Prunes older-than-retention (never touches the most recent successful dump).
- Writes `<YYYYMMDDTHHMM>.manifest.json` next to it with size + duration + row
  counts on 5 sampled tables.
- Exits 0 on success, nonzero on failure with a compact reason.

Roughly 150 lines of Python, standard library + asyncpg. No new container, no
long-lived sidecar, no build image change.

## What we don't build

- **No WAL archiving.** That was the pgBackRest path that hurt us. Nightly
  logical dumps + accepting the 24 h RPO is the correct answer for this
  workload right now.
- **No pgBackRest.** Even at process-max=1 it was still fighting the SMB write
  pattern. Revisit only if we ever move the repository off SMB.
- **No sidecar container.** Every long-lived container is more to manage. One
  cron entry running a script is enough.
- **No R2 upload in Phase C.** Off-site is Phase E. Local dumps to Z: first.
