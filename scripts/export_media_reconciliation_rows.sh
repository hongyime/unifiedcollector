#!/usr/bin/env bash
# Export the complete restored media_items reference set without mutating its DB.
set -Eeuo pipefail

usage() {
  printf '%s\n' 'usage: export_media_reconciliation_rows.sh --container NAME --output PATH' >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

container=
output=
while (($#)); do
  case "$1" in
    --container|--output)
      (($# >= 2)) || die "$1 requires a value"
      if [[ $1 == --container ]]; then container=$2; else output=$2; fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
done

[[ -n $container && -n $output ]] || { usage; exit 2; }
[[ $container == uc-task9-restore-* ]] || die 'only the isolated task-9 restore container is accepted'
[[ $(docker inspect -f '{{.State.Running}}|{{.HostConfig.NetworkMode}}' "$container") == true\|none ]] || die 'restore container must be running with network none'

output=$(realpath -m -- "$output")
evidence_root=$(realpath -e -- "$(dirname -- "$output")") || die 'output parent must already exist'
case "$evidence_root" in
  */.omo/evidence/collector-production-recovery) ;;
  *) die 'output must be placed directly in the private recovery evidence root' ;;
esac
[[ $(stat -c '%a' "$evidence_root") == 700 ]] || die 'recovery evidence root must remain mode 700'

temporary=$output.tmp
umask 077
docker exec -u postgres "$container" psql -q -v ON_ERROR_STOP=1 -d unifiedcollector -c \
  "COPY (SELECT id, file_path, file_size, sha256 FROM media_items ORDER BY id) TO STDOUT WITH CSV HEADER" > "$temporary"
mv -- "$temporary" "$output"
printf 'PASS: rows_csv=%s sha256=%s\n' "$output" "$(sha256sum "$output" | awk '{print $1}')"
