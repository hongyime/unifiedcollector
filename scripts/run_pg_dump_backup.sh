#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
volume_root="/media/sg-ultra"
backup_dir="$volume_root/unifiedcollector/backups/db"
image="unifiedcollector-pg-dump:latest"
pause_file="${CUTOVER_PAUSE_FILE:-/var/lib/unifiedcollector/cutover.pause}"

if [[ "${BACKUP_ENABLED:-1}" != "1" ]]; then
    printf 'pg_dump backup disabled by BACKUP_ENABLED\n'
    exit 0
fi

if [[ -f "$pause_file" ]]; then
    printf 'pg_dump backup paused for cutover: %s\n' "$pause_file"
    exit 0
fi

if ! mountpoint -q "$volume_root"; then
    printf 'pg_dump backup aborted: %s is not mounted\n' "$volume_root" >&2
    exit 1
fi

mkdir -p "$backup_dir"

docker build --quiet \
    -f "$repo_dir/docker/pg_dump_runner.Dockerfile" \
    -t "$image" \
    "$repo_dir/docker"

exec docker run --rm \
    --network unifiedcollector_default \
    --user "$(id -u):$(id -g)" \
    --env-file "$repo_dir/.env" \
    # Preserve an explicit cutover/maintenance kill switch from the host;
    # default to enabled only when the operator has not set it.
    -e "BACKUP_ENABLED=${BACKUP_ENABLED:-1}" \
    -e POSTGRES_HOST=unifiedcollector_postgres \
    -e POSTGRES_HOST_PORT=5432 \
    -e POSTGRES_DB=unifiedcollector \
    -e "BACKUP_DRY_RUN=${BACKUP_DRY_RUN:-0}" \
    -v "$repo_dir/scripts/pg_dump_backup.py:/app/pg_dump_backup.py:ro" \
    -v "$backup_dir:$backup_dir" \
    "$image" "$@"
