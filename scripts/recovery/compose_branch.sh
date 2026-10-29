#!/usr/bin/env bash
# Fail-closed Compose selector for Task 13's immutable recovery branches.
set -Eeuo pipefail

usage() {
  printf '%s\n' 'usage: compose_branch.sh [--namespace production|rehearsal] [--env-file PATH] --branch baseline|recovered --expect-volume NAME -- <docker compose arguments>' >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

namespace=production
branch=
expected_volume=
env_file=
while (($#)); do
  case "$1" in
    --namespace)
      (($# >= 2)) || die '--namespace requires a value'
      namespace=$2
      shift 2
      ;;
    --branch)
      (($# >= 2)) || die '--branch requires a value'
      branch=$2
      shift 2
      ;;
    --expect-volume)
      (($# >= 2)) || die '--expect-volume requires a value'
      expected_volume=$2
      shift 2
      ;;
    --env-file)
      (($# >= 2)) || die '--env-file requires a value'
      env_file=$2
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      die "unknown recovery-selector option: $1"
      ;;
  esac
done

[[ $namespace == production || $namespace == rehearsal ]] || die 'namespace must be production or rehearsal'
[[ $branch == baseline || $branch == recovered ]] || die 'branch must be baseline or recovered'
[[ -n $expected_volume ]] || die '--expect-volume is required'
(($#)) || die 'docker compose arguments are required after --'

for argument in "$@"; do
  [[ $argument != -f && $argument != --file && $argument != --project-directory ]] || die 'extra Compose file/project options are forbidden'
done

case "$namespace:$branch" in
  production:baseline)
    override_name=task-13-compose-baseline.yml
    selected_volume=unifiedcollector_pgdata
    ;;
  production:recovered)
    override_name=task-13-compose-recovered.yml
    selected_volume=unifiedcollector_pgdata_recovered
    ;;
  rehearsal:baseline)
    override_name=task-13-compose-rehearsal-baseline.yml
    selected_volume=uc-rehearsal-pgdata
    ;;
  rehearsal:recovered)
    override_name=task-13-compose-rehearsal-recovered.yml
    selected_volume=uc-rehearsal-pgdata-recovered
    ;;
esac

[[ $expected_volume == "$selected_volume" ]] || die "branch/volume mismatch: $namespace/$branch requires $selected_volume"
if [[ $namespace == production ]]; then
  [[ $selected_volume != uc-rehearsal-* ]] || die 'rehearsal volume is forbidden in production'
else
  [[ $selected_volume == uc-rehearsal-* ]] || die 'production volume is forbidden in rehearsal'
fi

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
root_env_file=$(realpath -e -- "$repo_root/.env") || die 'repository-root .env is missing'
if [[ -z $env_file ]]; then
  [[ $namespace == production ]] || die 'rehearsal requires an explicit non-production --env-file'
  env_file=$root_env_file
else
  env_file=$(realpath -e -- "$env_file") || die 'selected --env-file is missing'
fi
if [[ $namespace == production && $env_file != "$root_env_file" ]]; then
  die 'production accepts only the repository-root .env'
fi
if [[ $namespace == rehearsal && $env_file == "$root_env_file" ]]; then
  die 'rehearsal must not use the production repository-root .env'
fi
compose_file=$repo_root/docker/docker-compose.yml
evidence_dir=$repo_root/.omo/evidence/collector-production-recovery
override=$evidence_dir/$override_name
checksum=$override.sha256
[[ -r $compose_file && -r $override && -r $checksum && -r $env_file ]] || die 'required base/override/checksum/env file is missing'
(cd "$evidence_dir" && sha256sum --check --status "$(basename "$checksum")") || die "override checksum failed: $override_name"

rendered_volume=$(docker compose --env-file "$env_file" -f "$compose_file" -f "$override" config --format json | python3 -c '
import json
import sys
document = json.load(sys.stdin)
name = document.get("volumes", {}).get("pgdata", {}).get("name")
if not isinstance(name, str) or not name:
    raise SystemExit("rendered pgdata volume name is missing")
print(name)
') || die 'could not render and inspect the selected Compose branch'
[[ $rendered_volume == "$selected_volume" ]] || die "rendered pgdata mismatch: expected $selected_volume, got $rendered_volume"

exec docker compose --env-file "$env_file" -f "$compose_file" -f "$override" "$@"
