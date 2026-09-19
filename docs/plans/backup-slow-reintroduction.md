# backup-slow-reintroduction.md

Author: agent handoff, 2026-09-19. Status: proposed. Blast radius: none in Phase A
(dry-run only). Phase B adds one nightly job that must not hinder collectors.

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
| Single long-lived connection | Yes | No — one COPY per table, each finishes |
| Throughput bottleneck | Whole-cluster physical pages | Row-serialized data, much smaller |
| Locks on running writers | Access-share on every table (safe) | Same access-share + `--serializable-deferrable` for consistency |
| Output pattern | Thousands of small files (pgBackRest) | One `.dump` archive file |
| SMB write penalty | Devastating (small-file random write) | Modest (one large sequential write) |
| Restore | `pg_restore` from tar | `pg_restore` from custom-format dump |
| Point-in-time recovery | Yes (WAL replay after base) | No — restores at dump timestamp |
| Cluster-wide dump | Everything including postgres system tables | Just the one database (unifiedcollector) |
| Wall time on this cluster | 3-8+ hours | ~15-30 min at compression 1 (historic evidence: the src/backup/db_backup.py that ran daily until 2026-09-19 completed in that window) |

**We give up point-in-time recovery.** The tradeoff is a backup mechanism that
doesn't fight the collector. The daily dump loses at most 24 h if pg is
destroyed, but the cluster snapshot at the last dump is fully restorable.

## Proposed design

### Constraints (must-haves)
1. **Never blocks a collector**: use `--serializable-deferrable` isolation,
   read-only connection, no schema DDL locks.
2. **Never spikes CPU on the pg container**: cap the dumper container at
   `--cpus=0.5 --memory=256m`. pg_dump inside stays inside limits.
3. **Never blocks a concurrent backup**: `pg_try_advisory_lock` on a fixed
   key. Second invocation exits fast with a log line.
4. **Never blocks under SMB write pressure**: single `.dump` file (sequential
   write), compression level 1 (fast, less CPU on the pg side), `--jobs=1` (no
   parallel connections, one COPY at a time).
5. **Runs at quiet hour**: 04:00 SGT default (post-peak-collection window).
   Env-configurable.
6. **Skip if fresh**: default 20 h freshness window — a container restart
   after a successful dump does not immediately re-dump.
7. **Kill switch**: `BACKUP_ENABLED=0` in `.env` fully disables the cron
   without touching code.

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
  table-of-contents. Runs in seconds. If it succeeds, the dump is a valid
  archive.
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
  temporary docker container that has pg_dump. Measure:
  - Wall time
  - Peak Postgres CPU %
  - Peak SMB write speed to Z:
  - Peak dump file size
- If wall time > 60 min OR Postgres CPU > 80% sustained → back off, don't
  proceed to Phase B.

### Phase B — one live dump this week
- Run once with `--dry-run=false` at a chosen quiet moment.
- Confirm the output at `Z:/unifiedcollector/backups/db/*.dump`.
- Confirm `pg_restore --list` verification.
- Confirm collectors kept ingesting during the dump (telegram_messages
  count / 15 min doesn't drop noticeably).

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
