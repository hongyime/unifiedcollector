#!/usr/bin/env bash
# Read-only state verifier used by the Task 13 recovery runbook.
set -Eeuo pipefail

usage() {
  printf '%s\n' 'usage: verify_state.sh [--namespace production|rehearsal] [--db-user NAME] --expect baseline|forward --branch baseline|recovered --expect-volume NAME --media-root PATH' >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

expect=
branch=
expected_volume=
media_root=/media/sg-ultra/unifiedcollector
namespace=production
db_user=
while (($#)); do
  case "$1" in
    --expect|--branch|--expect-volume|--media-root|--namespace|--db-user)
      (($# >= 2)) || die "$1 requires a value"
      case "$1" in
        --expect) expect=$2 ;;
        --branch) branch=$2 ;;
        --expect-volume) expected_volume=$2 ;;
        --media-root) media_root=$2 ;;
        --namespace) namespace=$2 ;;
        --db-user) db_user=$2 ;;
      esac
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
done

[[ $expect == baseline || $expect == forward ]] || die '--expect must be baseline or forward'
[[ $branch == baseline || $branch == recovered ]] || die '--branch must be baseline or recovered'
[[ $namespace == production || $namespace == rehearsal ]] || die '--namespace must be production or rehearsal'
[[ -n $expected_volume ]] || die '--expect-volume is required'
# Task 14 keeps disposable evidence/media under a root-owned 0700 directory.
# The verifier never reads it, but must be able to prove it exists without
# requiring the invoking operator to traverse private recovery evidence.
sudo -n test -d "$media_root" || die "media root is unavailable: $media_root"

if [[ $namespace == rehearsal ]]; then
  container_name=uc-rehearsal-postgres
  db_user=${db_user:-postgres}
else
  container_name=unifiedcollector_postgres
  db_user=${db_user:-collector}
fi

mounts=$(docker inspect "$container_name" --format '{{range .Mounts}}{{println .Name .Destination}}{{end}}') || die "cannot inspect $container_name"
grep -Fx -- "$expected_volume /var/lib/postgresql/data" <<<"$mounts" >/dev/null || die 'postgres does not mount the expected pgdata volume'
if [[ $expect == baseline ]]; then
  [[ $branch == baseline ]] || die 'baseline state requires the baseline branch'
else
  [[ $branch == recovered ]] || die 'forward state requires the recovered branch'
fi

ready_output=
ledger_ready=false
for _ in $(seq 1 30); do
  if ready_output=$(docker exec "$container_name" psql -U "$db_user" -v ON_ERROR_STOP=1 -At -d unifiedcollector -c 'SELECT count(*) FROM schema_migrations;' 2>&1); then
    ledger_ready=true
    break
  fi
  sleep 1
done
[[ $ledger_ready == true ]] || die "migration ledger is unreadable after PostgreSQL readiness wait: $ready_output"
printf 'PASS: namespace=%s expect=%s branch=%s pgdata=%s media_root=%s\n' "$namespace" "$expect" "$branch" "$expected_volume" "$media_root"
