#!/usr/bin/env bash
# Wait locally for the bounded capture unit, then run the fail-closed follow-up.
set -Eeuo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
unit=uc-task53-recapture.service

while systemctl is-active --quiet "$unit"; do
  sleep 60
done

exec bash "$root/scripts/recovery/run_task53_post_capture.sh" --run
