#!/usr/bin/env bash
# Disposable Task 14 Branch-B rehearsal. Never names or mounts a production volume.
set -Eeuo pipefail

usage() {
  printf '%s\n' 'usage: rehearse_branch_b.sh --check | --run' >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

mode=${1:-}
[[ $mode == --check || $mode == --run ]] || { usage; exit 2; }
[[ $# == 1 ]] || { usage; exit 2; }

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
compose=$repo_root/scripts/recovery/compose_branch.sh
verify=$repo_root/scripts/recovery/verify_state.sh
fixture_env=$repo_root/tests/fixtures/recovery-rehearsal.env
evidence_dir=$repo_root/.omo/evidence/collector-production-recovery
receipt=$evidence_dir/task-14-collector-production-recovery.json
candidate=/var/lib/unifiedcollector/recovery-candidates/20261023T042116Z_preserved-pg16.dump
baseline_source=/var/lib/unifiedcollector/preservation-20261023T021822Z/pgdata
baseline_volume=uc-rehearsal-pgdata
recovered_volume=uc-rehearsal-pgdata-recovered
restore_container=uc-rehearsal-restore

for path in "$compose" "$verify" "$fixture_env"; do
  [[ -r $path ]] || die "required rehearsal input is missing: $path"
done
sudo -n test -r "$candidate" || die 'candidate dump is unavailable'
sudo -n test -d "$baseline_source" || die 'preserved baseline copy is unavailable'

if [[ $mode == --check ]]; then
  "$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
    --expect-volume "$baseline_volume" -- config --quiet
  "$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
    --expect-volume "$recovered_volume" -- config --quiet
  printf 'PASS: disposable rehearsal preflight only; no container or volume was created\n'
  exit 0
fi

for volume in "$baseline_volume" "$recovered_volume"; do
  docker volume inspect "$volume" >/dev/null 2>&1 && die "refusing to reuse existing rehearsal volume: $volume"
done
docker inspect "$restore_container" >/dev/null 2>&1 && die "refusing to reuse existing rehearsal restore container"

root=$(sudo -n mktemp -d /var/lib/unifiedcollector/task-14-rehearsal-XXXXXXXX)
sudo -n chmod 700 "$root"
logs=$root/logs
media_root=$root/media
sudo -n install -d -m 700 "$logs" "$media_root"
cleanup_needed=true

cleanup() {
  local rc=$?
  if [[ $cleanup_needed == true ]]; then
    docker rm -f "$restore_container" >/dev/null 2>&1 || true
    docker rm -f uc-rehearsal-postgres >/dev/null 2>&1 || true
    docker volume rm "$recovered_volume" "$baseline_volume" >/dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

volume_mountpoint() {
  docker volume inspect "$1" --format '{{.Mountpoint}}'
}

remove_rehearsal_postgres() {
  # Compose has already rendered and started this exact isolated name.  Dispose
  # it directly so a non-mutating cleanup never depends on a second Compose
  # render after the disposable database has been stopped.
  docker rm -f uc-rehearsal-postgres >/dev/null || die 'cannot remove disposable rehearsal PostgreSQL container'
}

# The preserved baseline predates the current Compose bootstrap and deliberately
# has no `postgres` login role.  Its normal `collector` role is NOLOGIN, so a
# disposable single-user bootstrap is the only way to authenticate the
# read-only state verifier.  This changes only the disposable rehearsal copy;
# it runs with no network and emits a private receipt.
enable_disposable_collector_login() {
  local volume=$1 label=$2 output rc
  output=$logs/"$label"-single-user.log
  set +e
  docker run --rm --pull=never --network none --user postgres -i \
    -v "$volume:/var/lib/postgresql/data" \
    --entrypoint postgres pgvector/pgvector:pg16 \
    --single -D /var/lib/postgresql/data unifiedcollector <<'SQL' 2>&1 \
    | sudo -n tee "$output" >/dev/null
ALTER ROLE collector LOGIN;
SQL
  rc=${PIPESTATUS[0]}
  set -e
  [[ $rc == 0 ]] || die "cannot enable the disposable collector login for $label"
}

wait_for_stable_restore_database() {
  local successes=0 output=''
  for _ in $(seq 1 120); do
    if output=$(docker exec "$restore_container" psql -U postgres -v ON_ERROR_STOP=1 -At \
      -d unifiedcollector -c 'SELECT 1;' 2>&1); then
      ((successes += 1))
      [[ $successes -ge 2 ]] && return 0
    else
      successes=0
    fi
    sleep 1
  done
  die "disposable restore server did not become stably queryable: $output"
}

docker volume create "$baseline_volume" >/dev/null
baseline_mount=$(volume_mountpoint "$baseline_volume")
sudo -n rsync -aHAXS --numeric-ids "$baseline_source/" "$baseline_mount/"
# Only the disposable copy is relaxed for the fake rehearsal role.
sudo -n sed -i -E 's/^(local|host[[:space:]]+all[[:space:]]+all[[:space:]].*)$/# task14-original: \1/' "$baseline_mount/pg_hba.conf"
printf 'local all all trust\nhost all all 0.0.0.0/0 trust\nhost all all ::0/0 trust\n' | sudo -n tee -a "$baseline_mount/pg_hba.conf" >/dev/null
enable_disposable_collector_login "$baseline_volume" baseline

"$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
  --expect-volume "$baseline_volume" -- up --no-start --no-deps postgres
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
  --expect-volume "$baseline_volume" -- start postgres
"$verify" --namespace rehearsal --db-user collector --expect baseline --branch baseline \
  --expect-volume "$baseline_volume" --media-root "$media_root"
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
  --expect-volume "$baseline_volume" -- stop postgres
remove_rehearsal_postgres

docker volume create "$recovered_volume" >/dev/null
docker run -d --pull=never --network none --restart no --name "$restore_container" \
  -e POSTGRES_DB=unifiedcollector -e POSTGRES_USER=postgres -e POSTGRES_HOST_AUTH_METHOD=trust \
  -v "$recovered_volume:/var/lib/postgresql/data" -v "$candidate:/candidate.dump:ro" \
  -v "$logs:/logs" pgvector/pgvector:pg16 >/dev/null
wait_for_stable_restore_database
docker exec -u 0 "$restore_container" psql -v ON_ERROR_STOP=1 -U postgres -d unifiedcollector -c \
  "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'collector') THEN CREATE ROLE collector NOLOGIN; END IF; END \$\$;" >/dev/null
set +e
docker exec -u 0 "$restore_container" sh -c '
  umask 077
  /usr/lib/postgresql/16/bin/pg_restore --exit-on-error --no-owner --no-privileges --verbose \
    --host=/var/run/postgresql --username=postgres --dbname=unifiedcollector /candidate.dump \
    > /logs/pg_restore.stdout 2> /logs/pg_restore.stderr
'
restore_rc=$?
set -e
printf '%s\n' "$restore_rc" | sudo -n tee "$logs/pg_restore.rc" >/dev/null
[[ $restore_rc == 0 ]] || die 'disposable pg_restore failed'
docker stop "$restore_container" >/dev/null
docker rm "$restore_container" >/dev/null

"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$recovered_volume" -- up --no-start --no-deps postgres
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$recovered_volume" -- start postgres
"$verify" --namespace rehearsal --db-user postgres --expect forward --branch recovered \
  --expect-volume "$recovered_volume" --media-root "$media_root"
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$recovered_volume" -- stop postgres
remove_rehearsal_postgres

"$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
  --expect-volume "$baseline_volume" -- up --no-start --no-deps postgres
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
  --expect-volume "$baseline_volume" -- start postgres
"$verify" --namespace rehearsal --db-user collector --expect baseline --branch baseline \
  --expect-volume "$baseline_volume" --media-root "$media_root"
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch baseline \
  --expect-volume "$baseline_volume" -- stop postgres
remove_rehearsal_postgres

sudo -n sha256sum "$candidate" | sudo -n tee "$logs/candidate.sha256" >/dev/null
sudo -n sha256sum "$logs/pg_restore.stderr" "$logs/pg_restore.rc" | sudo -n tee "$logs/SHA256SUMS" >/dev/null
cleanup_needed=false
docker volume rm "$recovered_volume" "$baseline_volume" >/dev/null
candidate_sha=$(sudo -n awk 'NR == 1 { print $1 }' "$logs/candidate.sha256")
[[ $candidate_sha =~ ^[0-9a-f]{64}$ ]] || die 'candidate checksum receipt is malformed'
receipt_tmp=$(mktemp "$evidence_dir/.task-14-collector-production-recovery.XXXXXX")
python3 - "$receipt_tmp" "$candidate_sha" "$logs" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "schema_version": 1,
    "status": "passed",
    "branch": "recovered",
    "candidate_sha256": sys.argv[2],
    "restore_logs": sys.argv[3],
    "rehearsal_resources_removed": True,
    "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
mv "$receipt_tmp" "$receipt"
printf 'PASS: rehearsal_root=%s\n' "$root"
