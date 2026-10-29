#!/usr/bin/env bash
# Capture the remaining Task 53 forward-point artifacts while writers are stopped.
set -Eeuo pipefail
umask 077

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
evidence=$root/.omo/evidence/collector-production-recovery
dump=$evidence/task-53-production-forward-db.dump
cursor=$evidence/task-53-production-forward-cursor-queue.json
rows=$evidence/task-53-production-forward-media-rows.csv
snapshot=$evidence/task-53-production-forward-snapshot.json
complete=$evidence/task-53-production-forward-capture-complete

for path in "$dump" "$dump.sha256"; do
  [[ -f $path && $(stat -c '%a' "$path") == 600 ]] || { echo "ERROR: verified private dump is required" >&2; exit 2; }
done
for path in "$cursor" "$rows" "$snapshot" "$complete"; do
  [[ ! -e $path ]] || { echo "ERROR: refusing to overwrite Task 53 capture artifact" >&2; exit 2; }
done
docker run --rm --pull=never --network none -v "$evidence:/evidence:ro" pgvector/pgvector:pg16 \
  pg_restore --list /evidence/task-53-production-forward-db.dump >/dev/null

for container in unifiedcollector_collector_paypal unifiedcollector_collector_whatsapp unifiedcollector_wa_bridge_1 unifiedcollector_wa_bridge_2 unifiedcollector_recon_egress_guard unifiedcollector_spiderfoot; do
  [[ $(docker inspect --format '{{.State.Running}}' "$container") == false ]] || { echo "ERROR: writer remains running" >&2; exit 2; }
done

python3 "$root/scripts/recovery/capture_task53_cursor_queue.py" --output "$cursor"
docker exec -u postgres unifiedcollector_postgres psql -U postgres -d unifiedcollector -q -v ON_ERROR_STOP=1 -c \
  "COPY (SELECT id, file_path, file_size, sha256 FROM media_items ORDER BY id) TO STDOUT WITH CSV HEADER" >"$rows"
[[ $(stat -c '%a' "$rows") == 600 ]] || { echo "ERROR: media rows must remain private" >&2; exit 2; }

dump_sha=$(awk 'NR == 1 { print $1 }' "$dump.sha256")
cursor_sha=$(sha256sum "$cursor" | awk '{ print $1 }')
rows_sha=$(sha256sum "$rows" | awk '{ print $1 }')
media_count=$(($(wc -l <"$rows") - 1))
migrations=$(docker exec -u postgres unifiedcollector_postgres psql -U postgres -d unifiedcollector -Atqc 'SELECT count(*) FROM schema_migrations;')
image=$(docker inspect --format '{{.Image}}' unifiedcollector_postgres)
python3 - "$snapshot" "$dump_sha" "$cursor_sha" "$rows_sha" "$media_count" "$migrations" "$image" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path, dump_sha, cursor_sha, rows_sha, media_count, migrations, image = sys.argv[1:]
document = {
    "task": 53,
    "schema_version": 1,
    "selected_namespace": "production",
    "selected_branch": "recovered",
    "selected_volume": "unifiedcollector_pgdata_recovered",
    "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "postgres_image": image,
    "schema_migrations": int(migrations),
    "media_items": int(media_count),
    "dump_sha256": dump_sha,
    "cursor_queue_sha256": cursor_sha,
    "media_rows_sha256": rows_sha,
    "writer_resume_order": [
        "wa-bridge-1", "wa-bridge-2", "collector_whatsapp", "collector_paypal",
        "recon_egress_guard", "collector_spiderfoot",
    ],
}
target = Path(path)
temp = target.with_suffix(".tmp")
temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temp.replace(target)
PY
touch "$complete"
printf 'PASS: media_items=%s schema_migrations=%s\n' "$media_count" "$migrations"
