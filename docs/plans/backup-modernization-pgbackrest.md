# backup-modernization-pgbackrest.md

Author: agent handoff, 2026-09-18. Status: proposed. Blast radius on adoption: full
collector Postgres downtime for ~30 min (base backup ingest), then ongoing.

## Why this doc exists

The current backup path (see `docs/plans/pool-leak-fix.md` for the wider audit +
`docs/plans/whatsapp-contact-architecture-alternatives.md` for a sibling
architecture doc) is a hand-rolled `pg_basebackup` wrapper driven by a Windows
PowerShell orchestration script:

- `Z:\unifiedcollector\backups\cluster\maintenance-20260916-r2\job-r2.sh` — the
  actual backup job (client-gzip:1, `--max-rate=2M`, single stream, 24h cap).
- `X:\01 REPOSITORIES\audit_results\maintenance-2026-09-09\l390-full-backup-r2-control-20260916.ps1`
  — the launcher, with 20+ gates that pin specific container UUIDs, memory
  cgroup values, and 15-minute-fresh pre-flight JSONs.

The Sep-15 failure at `wal_sender_timeout=1min` was the trigger, but the deeper
issues surfaced during recovery are:

| # | Weakness | Consequence |
|---|---|---|
| 1 | Container UUIDs pinned in the control script | Any Docker restart breaks the retry path (every Docker Desktop shutdown = manual UUID surgery). |
| 2 | 15-min-fresh JSON gates | Retries after a > 15min gap require re-running upstream preflight tooling — not a one-liner. |
| 3 | `archive/` dir must not exist | Failed backups leave partial output that blocks the next run without manual rename. |
| 4 | No incremental / differential backup — always a full `pg_basebackup` | 3+ GB copied every run at 2 MiB/s = ~30-40 min minimum, growing linearly with DB size. |
| 5 | Client-side single-thread gzip level 1 | CPU-bound during compression, but only one core; `zstd` would compress better in less time. |
| 6 | No WAL archiving after the base backup completes | Point-in-time recovery is not possible — restore lands at the base timestamp, losing any messages that arrived after. |
| 7 | Restore drill (`src/backup/restore_drill.py`) has no CLI entry point + never runs in CI | The scary path (recover a dead cluster) is untested. First real restore is where you find out it doesn't work. |
| 8 | Backup and restore are separate scripts in separate languages | Ownership is diffuse; no single artifact you can point at and say "run this to prove backup + restore work end-to-end." |
| 9 | Off-site copy is manual (rclone/aws CLI outside the pipeline) | Backups on Z: are one drive failure away from lost. |
| 10 | No integrated verification | The current `db_backup run` says `latest verified dump is fresh (38960s < 82800s)` but the verification is a checksum, not a real restore. |

## The wrong fix: rewrite in Go/Rust

`pg_basebackup` and `pg_restore` are C tools shipped with Postgres. They already
saturate the bottleneck (postgres server output rate + disk write + gzip on this
particular workload). Rewriting them in Go or Rust would:

- Reproduce well-tested C code for no measurable speed gain (I/O bound, not CPU).
- Introduce a fresh unofficial client with its own bugs and its own edge cases
  against the Postgres wire protocol.
- Not fix any of the ten problems above — those are protocol / orchestration
  layer issues, not compression / network-copy speed issues.

A Go or Rust *orchestration layer* is defensible (better error paths than a
Windows PowerShell 5-page control script) — but there is already a mature C tool
that solves all ten problems above without a from-scratch project.

## The right fix: pgBackRest

[pgBackRest](https://pgbackrest.org/) is the current industry-standard Postgres
backup tool. Written in C, actively maintained, used by GitLab / Zalando /
CrunchyData / many others. Solves every problem in the table above:

| # | Weakness | pgBackRest resolution |
|---|---|---|
| 1 | UUID-pinned control script | Config is a `pgbackrest.conf` file; no container UUIDs. Runs as its own container or as a systemd unit. |
| 2 | Stale preflight JSONs | No preflight — every run is idempotent from config. |
| 3 | archive/ dir blocks retry | Repository layout is `stanza/backup/<label>/*`; a failed run leaves a partial stanza that the next run cleanly resumes or replaces. |
| 4 | Always full | Native support for `full` / `diff` / `incr` backups. Weekly full + nightly diff + hourly incr is a normal config. |
| 5 | Single-thread gzip:1 | `process-max=N` for parallel compression; `compress-type=zst` for zstd. On a 4-core machine, 3-5x throughput improvement is typical. |
| 6 | No WAL archiving | `archive-push` command wired into `postgresql.conf.archive_command = 'pgbackrest --stanza=main archive-push %p'` — every WAL segment ships automatically. |
| 7 | Restore never tested | `pgbackrest restore --delta` restores in place; CI can `pgbackrest verify` a repository weekly to catch corruption. |
| 8 | Diffuse ownership | One binary, one config file, one repository. |
| 9 | Manual off-site | Native S3-compatible target (Cloudflare R2, MinIO, AWS S3, Backblaze B2). Set `repo1-type=s3`, done. |
| 10 | Checksum ≠ restore | `pgbackrest verify --stanza=main` checks every archive against its manifest checksum; a scheduled tick can also do a full test-restore into a scratch dir. |

## Migration plan

Not doing a big-bang cutover. Both systems run in parallel until pgBackRest has
one successful full + one successful restore drill, then the old path is
retired.

### Phase 0 — read-only preparation (no downtime)
- Land this doc + a stub `docker/pgbackrest/` with `pgbackrest.conf` template.
- Add a compose service `unifiedcollector_pgbackrest` (image
  `pgbackrest/pgbackrest:latest`) that runs `--dry-run` verification only.
- Add a scheduler handler `PgBackRestVerifyHandler` that runs `pgbackrest info`
  every 6 h and posts to the alert channel if the last backup is > 26 h old.

### Phase 1 — WAL archiving (10 min downtime)
- Add to `docker/postgres/postgres.conf`:
    ```
    archive_mode = on
    archive_command = 'pgbackrest --stanza=main archive-push %p'
    max_wal_senders = 5      # already there
    wal_level = replica      # already default
    ```
- Add sidecar container that mounts the postgres data dir read-only + the R2
  credentials + the repository target dir.
- Recreate collector_postgres to pick up the new config. Verify WAL segments
  start flowing to R2 within 5 min via `pgbackrest info`.

### Phase 2 — first full backup (30-40 min, no downtime for collectors)
- `pgbackrest --stanza=main --type=full backup` — runs from the sidecar; the
  collector Postgres is under read/write load the whole time.
- With `process-max=4 --compress-type=zst --compress-level=3`, the same ~3 GB
  cluster completes in 6-10 min vs the current 3+ h.
- Restore drill immediately after: `pgbackrest --stanza=main --repo1-path=/tmp
  restore` into a throwaway scratch dir + verify row counts of the top-5
  tables match production.

### Phase 3 — retire the old path (no downtime)
- Move the old `job-r2.sh` + `l390-full-backup-r2-control-20260916.ps1` into an
  `archive/` subfolder with a README explaining they are superseded.
- Keep `src/backup/db_backup.py` for the local logical dump (it doesn't use
  pg_basebackup and is not affected by the WAL sender timeout class of bug).
  Local logical dumps are cheap disaster-recovery insurance separate from the
  cluster-level pgBackRest path.

## Expected wins after phase 2

| Metric | Current | With pgBackRest |
|---|---|---|
| Full backup wall time (3 GB cluster) | ~3 hours | 6-10 min |
| Incremental backup wall time | N/A (always full) | 20-60 s |
| WAL loss on catastrophic pg_data loss | Everything since last full | ≤ archive_timeout (default 60 s) |
| Restore drill cadence | Manual | Automated verify every 6 h + scheduled full-restore drill weekly |
| R2 upload | Manual outside pipeline | Native, part of backup command |
| Container UUID coupling | Every run brittle | Zero |

## Restore drill design (the real win)

Even after the migration, restore is only trustworthy if it runs unattended.
Proposed drill:

```
unifiedcollector_pgbackrest_drill:
  image: pgbackrest/pgbackrest:latest
  volumes:
    - /var/lib/postgresql/drill:/pgdata:rw
    - pgbackrest_repo:/repo:ro
  command:
    - sh
    - -c
    - |
      pgbackrest --stanza=main --pg1-path=/pgdata --delta restore &&
      /usr/lib/postgresql/16/bin/pg_ctl -D /pgdata start -o "-p 5433 -c hot_standby=on" &&
      psql -h 127.0.0.1 -p 5433 -U collector -tAc "SELECT count(*) FROM whatsapp_users" &&
      /usr/lib/postgresql/16/bin/pg_ctl -D /pgdata stop -m fast
```

Runs weekly (or every N days per `PGBACKREST_DRILL_INTERVAL_HOURS`). Failed
drill → Telegram alert. Successful drill → source of truth that "we know the
backup works", updated in the dashboard as a green tick.

## What I'm NOT proposing

- Do not port `pg_basebackup` / `pg_restore` / `pg_dump` to Go or Rust. Those
  are Postgres's own client tools; the wire protocol implementations there are
  the reference implementations. A rewrite is negative-value.
- Do not migrate the whole collector stack away from Python. The bottleneck of
  the current stack isn't language — it's the backup orchestration + the
  browser-scraper cadence + the whatsapp bridge sync rate. Rust would help
  none of them.
- Do not remove `src/backup/db_backup.py` after the migration. Logical dumps
  (SQL) are a distinct safety net from cluster-level physical backups; keep
  both.

## Next tickets

1. `feat(backup): add pgbackrest sidecar in dry-run mode` (Phase 0).
2. `feat(pg): enable WAL archiving to pgbackrest repository` (Phase 1, needs
   10-min pg restart window).
3. `feat(backup): first full pgbackrest run + immediate restore drill` (Phase 2).
4. `chore(backup): archive the old job-r2 + l390 control scripts under a
   README explaining their replacement` (Phase 3).

Each is independently landable + rollbackable — the old path stays in place
until phase 3.
