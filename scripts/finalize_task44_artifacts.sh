#!/usr/bin/env bash
# Build and freeze Task 44 evidence without starting any Compose service.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
evidence="$root/.omo/evidence/collector-production-recovery"
revision="${TASK44_REVISION:-}"
if [[ -n "$revision" && ! "$revision" =~ ^r[0-9]+$ ]]; then
  echo "TASK44_REVISION must be empty or r followed by digits" >&2
  exit 2
fi
suffix="${revision:+-$revision}"
runtime="$evidence/task-44-runtime-artifact-manifest$suffix.json"
rendered="$evidence/task-44-rendered-compose$suffix.yml"
artifact="$evidence/task-44-artifact-manifest$suffix.json"
activation="task-49-activation-manifest$suffix.json"
cd "$root"
export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"

test ! -e "$runtime"
test ! -e "$rendered"
test ! -e "$artifact"

docker compose --env-file .env -f docker/docker-compose.yml build
python3 scripts/create_task44_runtime_manifest.py \
  --root "$root" \
  --output "$runtime"
scripts/recovery/compose_branch.sh \
  --namespace production --branch recovered --expect-volume unifiedcollector_pgdata_recovered \
  -- config --no-interpolate >"$rendered"

env_args=(--env-file "$root/.env")
while IFS= read -r -d '' path; do
  env_args+=(--env-file "$path")
done < <(find "$root/docker/env" -maxdepth 1 -type f -name '*.env' -print0 | sort -z)

python3 scripts/freeze_task44_artifact_manifest.py \
  --root "$root" \
  --runtime-manifest "$runtime" \
  --source-contract "$evidence/task-44-source-contract.json" \
  --source-ledger "$evidence/task-44-source-acceptance.json" \
  --review-receipt "$evidence/task-44-review.json" \
  --rendered-compose "$rendered" \
  "${env_args[@]}" \
  --evidence-root "$evidence" \
  --recon-evidence-root /var/lib/unifiedcollector/recon-evidence \
  --namespace production --branch recovered --volume unifiedcollector_pgdata_recovered \
  --activation-manifest "$activation" \
  --output "$artifact"
