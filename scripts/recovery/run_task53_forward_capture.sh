#!/usr/bin/env bash
# Capture a fresh Task 53 production point, always restoring approved writers.
set -Eeuo pipefail
umask 077

usage() {
  printf '%s\n' 'usage: run_task53_forward_capture.sh --check | --run' >&2
}

mode=${1:-}
[[ $mode == --check || $mode == --run ]] && [[ $# == 1 ]] || { usage; exit 2; }

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
evidence=$root/.omo/evidence/collector-production-recovery
compose=(docker compose --env-file "$root/.env" -f "$root/docker/docker-compose.yml")
dump=$evidence/task-53-production-forward-db.dump
checksum=$dump.sha256
temporary=$dump.in-progress
writers=(collector_paypal collector_whatsapp wa-bridge-1 wa-bridge-2 recon_egress_guard collector_spiderfoot)
writer_containers=(unifiedcollector_collector_paypal unifiedcollector_collector_whatsapp unifiedcollector_wa_bridge_1 unifiedcollector_wa_bridge_2 unifiedcollector_recon_egress_guard unifiedcollector_spiderfoot)
resume_order=(wa-bridge-1 wa-bridge-2 collector_whatsapp collector_paypal recon_egress_guard collector_spiderfoot)
writers_may_be_stopped=false

resume_writers() {
  local service rc=0
  for service in "${resume_order[@]}"; do
    "${compose[@]}" start "$service" || rc=1
  done
  writers_may_be_stopped=false
  return "$rc"
}

cleanup() {
  local rc=$?
  rm -f -- "$temporary"
  if [[ $writers_may_be_stopped == true ]]; then
    resume_writers || rc=1
  fi
  exit "$rc"
}
trap cleanup EXIT

for path in "$dump" "$checksum" \
  "$evidence/task-53-production-forward-cursor-queue.json" \
  "$evidence/task-53-production-forward-media-rows.csv" \
  "$evidence/task-53-production-forward-snapshot.json" \
  "$evidence/task-53-production-forward-capture-complete"; do
  [[ ! -e $path ]] || { printf 'ERROR: refusing to overwrite %s\n' "$path" >&2; exit 2; }
done
[[ ! -e $temporary ]] || { printf '%s\n' 'ERROR: stale Task 53 in-progress dump exists' >&2; exit 2; }

python3 "$root/scripts/recovery/task53_forward_capture.py"
for index in "${!writers[@]}"; do
  [[ $(docker inspect --format '{{.State.Running}}' "${writer_containers[$index]}") == true ]] \
    || { printf 'ERROR: writer is not running: %s\n' "${writers[$index]}" >&2; exit 2; }
done

if [[ $mode == --check ]]; then
  printf '%s\n' 'PASS: Task 53 capture guard preflight only; writers remain running'
  exit 0
fi

writers_may_be_stopped=true
"${compose[@]}" stop "${writers[@]}"
docker exec -u postgres unifiedcollector_postgres \
  pg_dump --format=custom --compress=0 --username=postgres --dbname=unifiedcollector >"$temporary"
[[ -s $temporary ]] || { printf '%s\n' 'ERROR: Task 53 dump is empty' >&2; exit 2; }
mv -- "$temporary" "$dump"
sha256sum "$dump" >"$checksum"
docker run --rm --pull=never --network none -v "$dump:/task-53-forward.dump:ro" \
  pgvector/pgvector:pg16 pg_restore --list /task-53-forward.dump >/dev/null
bash "$root/scripts/recovery/complete_task53_forward_capture.sh"
resume_writers
printf '%s\n' 'PASS: Task 53 fresh forward capture completed and writers resumed'
