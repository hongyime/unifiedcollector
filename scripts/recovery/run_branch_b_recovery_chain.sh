#!/usr/bin/env bash
# Run the approved Branch-B cutover chain; each durable receipt gates the next step.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
evidence="$root/.omo/evidence/collector-production-recovery"
cd "$root"
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"

test ! -e "$evidence/task-44-artifact-manifest.json"
test ! -e "$evidence/task-48-forward-authority.json"
test ! -e "$evidence/task-49-activation-manifest.json"
test ! -e "$evidence/task-50-preflight.json"

bash scripts/finalize_task44_artifacts.sh
test -s "$evidence/task-44-artifact-manifest.json"
python3 scripts/recovery/promote_branch_b.py --check
python3 scripts/recovery/promote_branch_b.py --run
test -s "$evidence/task-48-forward-authority.json"

env_args=(--env-file .env)
while IFS= read -r -d '' path; do
  env_args+=(--env-file "$path")
done < <(find docker/env -maxdepth 1 -type f -name '*.env' -print0 | sort -z)
python3 scripts/recovery/create_task49_activation_manifest.py \
  --root . "${env_args[@]}" \
  --rendered-compose "$evidence/task-49-rendered-compose.yml" \
  --output "$evidence/task-49-activation-manifest.json"
python3 scripts/recovery/verify_activation_manifest.py \
  --manifest "$evidence/task-49-activation-manifest.json" \
  --root . --task44 "$evidence/task-44-artifact-manifest.json" \
  --rendered-compose "$evidence/task-49-rendered-compose.yml"

python3 scripts/recovery/record_task50_fail_closed.py \
  --manifest "$evidence/task-12-reconciliation-manifest.json" \
  --output "$evidence/task-50-preflight.json"
