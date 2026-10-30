#!/usr/bin/env bash
# Restore the Task 53 forward dump only into disposable rehearsal state.
set -Eeuo pipefail
umask 077

usage() {
  printf '%s\n' 'usage: rehearse_task53_forward.sh --check | --run' >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

mode=${1:-}
[[ $mode == --check || $mode == --run ]] || { usage; exit 2; }
[[ $# == 1 ]] || { usage; exit 2; }

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
evidence=$root/.omo/evidence/collector-production-recovery
dump=$evidence/task-53-production-forward-db.dump
cursor_export=$evidence/task-53-production-forward-cursor-queue.json
rows=$evidence/task-53-production-forward-media-rows.csv
baseline_manifest=$evidence/task-46-destination-manifest.sha256
delta_dir=$evidence/task-53-production-forward-media
coverage=$evidence/task-53-production-forward-media-coverage.json
fixture_env=$root/tests/fixtures/recovery-rehearsal.env
compose=$root/scripts/recovery/compose_branch.sh
verify=$root/scripts/recovery/verify_state.sh
volume=uc-rehearsal-pgdata-recovered
container=uc-task53-rehearsal-restore
receipt=$evidence/task-53-rehearsal-forward.json
media_stage=

[[ -f $dump && -f $dump.sha256 ]] || die 'Task 53 forward dump and checksum are required'
[[ -f $cursor_export ]] || die 'Task 53 cursor/queue export is required'
[[ -f $rows && -f $baseline_manifest && -d $delta_dir && -f $coverage ]] || die 'Task 53 media coverage inputs are required'
[[ ! -e $receipt ]] || die 'Task 53 rehearsal receipt already exists'
[[ $(stat -c '%a' "$dump") == 600 ]] || die 'Task 53 forward dump must remain mode 600'
[[ $(stat -c '%a' "$cursor_export") == 600 ]] || die 'Task 53 cursor/queue export must remain mode 600'
jq -e '(.coverage_complete == true) and (.unaccounted_count == 0) and (.candidate_count == (.baseline_count + .delta_count + .unaccounted_count))' "$coverage" >/dev/null \
  || die 'Task 53 media coverage is incomplete'
sha256sum --check --status "$dump.sha256" || die 'Task 53 forward dump checksum failed'
docker run --rm --pull=never --network none -v "$dump:/task-53-forward.dump:ro" \
  pgvector/pgvector:pg16 pg_restore --list /task-53-forward.dump >/dev/null \
  || die 'Task 53 forward dump is unreadable'
[[ -r $fixture_env ]] || die 'rehearsal fixture env is unavailable'

"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$volume" -- config --quiet

if [[ $mode == --check ]]; then
  printf 'PASS: Task 53 forward rehearsal preflight only; no container or volume was created\n'
  exit 0
fi

logs=$(mktemp -d "$evidence/task-53-rehearsal-XXXXXXXX")
media_stage=$(mktemp -d "$evidence/task-53-rehearsal-media-XXXXXXXX")
# The union verifier creates its own new stage and rejects reuse. Reserve a
# private collision-resistant pathname, then release the empty directory so
# the verifier can prove it did not inherit staged media from another run.
rmdir -- "$media_stage"
docker volume inspect "$volume" >/dev/null 2>&1 && die "refusing to reuse existing rehearsal volume: $volume"
docker inspect "$container" >/dev/null 2>&1 && die "refusing to reuse existing rehearsal restore container"

cleanup_needed=true
cleanup() {
  rc=$?
  if [[ $cleanup_needed == true ]]; then
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker rm -f uc-rehearsal-postgres >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
    [[ -z $media_stage ]] || rm -rf -- "$media_stage"
  fi
  exit "$rc"
}
trap cleanup EXIT

media_union=$(python3 "$root/scripts/recovery/verify_task53_media_union.py" \
  --rows-csv "$rows" --baseline-manifest "$baseline_manifest" \
  --root /media/sg-ultra/unifiedcollector --delta-dir "$delta_dir" --stage "$media_stage") \
  || die 'disposable baseline-plus-delta media union verification failed'

docker volume create "$volume" >/dev/null
docker run -d --pull=never --network none --restart no --name "$container" \
  -e POSTGRES_DB=unifiedcollector -e POSTGRES_USER=postgres -e POSTGRES_HOST_AUTH_METHOD=trust \
  -v "$volume:/var/lib/postgresql/data" -v "$dump:/task-53-forward.dump:ro" \
  -v "$logs:/logs" pgvector/pgvector:pg16 >/dev/null

ready=false
for _ in $(seq 1 30); do
  if docker exec -u postgres "$container" psql -U postgres -d unifiedcollector -Atqc 'SELECT 1' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
[[ $ready == true ]] || die 'disposable restore database did not become ready'
docker exec -u postgres "$container" pg_restore --exit-on-error --no-owner --no-privileges \
  --host=/var/run/postgresql --username=postgres --dbname=unifiedcollector /task-53-forward.dump \
  >"$logs/pg_restore.stdout" 2>"$logs/pg_restore.stderr" || die 'disposable forward pg_restore failed'
docker cp "$cursor_export" "$container:/tmp/task-53-cursor-queue.json"
docker exec -u 0 "$container" chown postgres:postgres /tmp/task-53-cursor-queue.json
import_check=$(docker exec -u postgres "$container" psql -U postgres -d unifiedcollector -v ON_ERROR_STOP=1 -Atq -c "
  CREATE TEMP TABLE task53_cursor_queue_import (document jsonb NOT NULL);
  INSERT INTO task53_cursor_queue_import SELECT pg_read_file('/tmp/task-53-cursor-queue.json')::jsonb;
  SELECT CASE WHEN document->>'owner_identity' = 'legal-data-use-owner' THEN 1 ELSE 0 END,
         jsonb_array_length(document->'service_cursors'),
         jsonb_array_length(document->'browser_native_scrape_queue')
    FROM task53_cursor_queue_import;
")
IFS='|' read -r ownership_ok cursor_count queue_count <<<"$import_check"
[[ $ownership_ok == 1 && $cursor_count =~ ^[0-9]+$ && $queue_count =~ ^[0-9]+$ ]] || die 'disposable cursor/queue ownership import failed'
docker stop "$container" >/dev/null
docker rm "$container" >/dev/null

"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$volume" -- up --no-start --no-deps postgres
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$volume" -- start postgres
"$verify" --namespace rehearsal --db-user postgres --expect forward --branch recovered \
  --expect-volume "$volume" --media-root /media/sg-ultra/unifiedcollector
media_count=$(docker exec uc-rehearsal-postgres psql -U postgres -d unifiedcollector -Atqc 'SELECT count(*) FROM media_items;')
"$compose" --namespace rehearsal --env-file "$fixture_env" --branch recovered \
  --expect-volume "$volume" -- stop postgres
docker rm uc-rehearsal-postgres >/dev/null
docker volume rm "$volume" >/dev/null
rm -rf -- "$media_stage"
media_stage=
cleanup_needed=false

dump_sha=$(awk 'NR == 1 { print $1 }' "$dump.sha256")
cursor_sha=$(sha256sum "$cursor_export" | awk '{ print $1 }')
python3 - "$receipt" "$dump_sha" "$cursor_sha" "$media_count" "$cursor_count" "$queue_count" "$media_union" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path, digest, cursor_digest, media_count, cursor_count, queue_count, media_union = map(str, sys.argv[1:])
document = {
    "task": 53,
    "schema_version": 1,
    "status": "passed",
    "branch": "recovered",
    "namespace": "rehearsal",
    "dump_sha256": digest,
    "cursor_queue_sha256": cursor_digest,
    "media_items": int(media_count),
    "imported_service_cursors": int(cursor_count),
    "imported_browser_native_scrape_queue": int(queue_count),
    "media_union": json.loads(media_union),
    "rehearsal_resources_removed": True,
    "completed_at_utc": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
}
target = Path(path)
temporary = target.with_suffix('.tmp')
temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
temporary.replace(target)
PY
printf 'PASS: Task 53 forward dump restored and rehearsal resources removed\n'
