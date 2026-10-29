#!/usr/bin/env bash
# Validate a completed Task 53 capture only after its writers have been restored.
set -Eeuo pipefail
umask 077

usage() {
  printf '%s\n' 'usage: run_task53_post_capture.sh --check | --run' >&2
}

mode=${1:-}
[[ $mode == --check || $mode == --run ]] && [[ $# == 1 ]] || { usage; exit 2; }

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
evidence=$root/.omo/evidence/collector-production-recovery
rows=$evidence/task-53-production-forward-media-rows.csv
coverage=$evidence/task-53-production-forward-media-coverage.json
delta=$evidence/task-53-production-forward-media
writers=(unifiedcollector_wa_bridge_1 unifiedcollector_wa_bridge_2 unifiedcollector_collector_whatsapp unifiedcollector_collector_paypal unifiedcollector_recon_egress_guard unifiedcollector_spiderfoot)

for path in \
  "$evidence/task-53-production-forward-db.dump" \
  "$evidence/task-53-production-forward-db.dump.sha256" \
  "$evidence/task-53-production-forward-cursor-queue.json" \
  "$rows" \
  "$evidence/task-53-production-forward-snapshot.json" \
  "$evidence/task-53-production-forward-capture-complete"; do
  [[ -f $path ]] || { printf 'ERROR: capture is incomplete: %s\n' "$path" >&2; exit 2; }
done
for container in "${writers[@]}"; do
  [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]] \
    || { printf 'ERROR: writer is not restored: %s\n' "$container" >&2; exit 2; }
done
[[ ! -e $coverage && ! -e $delta ]] || { printf '%s\n' 'ERROR: fresh Task 53 coverage artifacts already exist' >&2; exit 2; }

if [[ $mode == --check ]]; then
  printf '%s\n' 'PASS: Task 53 post-capture guard only; no coverage or rehearsal started'
  exit 0
fi

python3 "$root/scripts/recovery/build_task53_media_coverage.py" \
  --rows-csv "$rows" \
  --baseline-manifest "$evidence/task-46-destination-manifest.sha256" \
  --root /media/sg-ultra/unifiedcollector \
  --delta-dir "$delta" \
  --output "$coverage"
bash "$root/scripts/recovery/rehearse_task53_forward.sh" --run
printf '%s\n' 'PASS: Task 53 fresh coverage and disposable forward readback completed'
