# docker/pgbackrest/

Sidecar service for the pgBackRest migration (see
`docs/plans/backup-modernization-pgbackrest.md`).

**Status: Phase 0 — dry-run only. Not writing backups yet.**

## What's here

| File | Purpose |
|---|---|
| `pgbackrest.conf.template` | Config template. Rendered by the sidecar entrypoint into `/etc/pgbackrest.conf` at start-time. Credentials stay in env_file / secrets. |
| `README.md` | This file. |

## Phase 0 sanity checks (safe to run)

Once the sidecar container is registered in `docker/docker-compose.yml`, these
commands validate the setup without writing any backup data:

```bash
# Verify the sidecar reads the postgres wire protocol at all.
docker exec unifiedcollector_pgbackrest pgbackrest --stanza=main check

# List repository state (should be empty in Phase 0).
docker exec unifiedcollector_pgbackrest pgbackrest --stanza=main info

# Verify config parses.
docker exec unifiedcollector_pgbackrest pgbackrest --stanza=main help
```

None of the above touch the running cluster's data. `check` opens a read-only
connection, `info` reads the repository dir, `help` just parses the config.

## What Phase 1 will add (needs 10-min pg restart)

- Enable `archive_mode=on` + `archive_command=pgbackrest archive-push` in
  `docker/postgres/postgres.conf`.
- Add the `wal_level=replica` line if not already present (it usually is).
- Recreate `unifiedcollector_postgres` to pick up the new config.
- WAL segments start streaming into the pgBackRest repository within 60 s.

## What Phase 2 will add (no downtime for collectors, 6-10 min backup wall)

- Run `pgbackrest --stanza=main --type=full backup` from the sidecar.
- Immediately run `pgbackrest --stanza=main verify` against the repository.
- Restore drill: `pgbackrest --stanza=main restore --repo1-path=/tmp` into a
  scratch dir, run a smoke query, tear down.

## What Phase 3 will do

Archive the current `Z:\unifiedcollector\backups\cluster\maintenance-20260916-r2\`
tree and the `X:\...\l390-full-backup-r2-control-20260916.ps1` orchestration
under a `superseded/` folder with a README pointing here.
