# Runbook — common ops recipes

Short, copy-pasteable one-liners for the operator on-call. Everything assumes
you are on the host with docker + PowerShell 7 and the repo at
`C:\unifiedcollector`.

## Freshness (per-source liveness)

Corrected liveness (uses `activity_last_seen_at` for text-heavy sources —
threads/x/facebook/etc. read their own posts table, not `media_items`):

```powershell
docker exec -w /app -e PYTHONPATH=/app unifiedcollector_scheduler `
  python -c "import asyncio,asyncpg,os
from src.core.source_freshness import compute_liveness
async def m():
    c=await asyncpg.connect(os.environ['DATABASE_URL'])
    for r in await compute_liveness(c):
        print(f\"{r['source']:12} {round((r.get('age_seconds') or 0)/60):>7} min stale={r['stale']}\")
    await c.close()
asyncio.run(m())"
```

Raw `media_items.created_at` view (older; useful for spotting when file
downloads have stopped even though posts still land in their per-platform
table):

```powershell
docker exec unifiedcollector_postgres psql -U collector -d unifiedcollector -c `
  "SELECT source, ROUND(EXTRACT(EPOCH FROM (now()-MAX(created_at)))/60)::int AS min_ago FROM media_items GROUP BY 1 ORDER BY 2 LIMIT 15;"
```

## Cookie vault

Health + last backup timestamp:

```powershell
docker exec unifiedcollector_browser_cookie_vault `
  python -c "import urllib.request,json;print(json.dumps(json.loads(urllib.request.urlopen('http://127.0.0.1:8790/health').read()),indent=2))"
```

Manual restore (force autorestore now, without a container restart):

```powershell
# Push cookies from latest.json into Chrome via CDP.
docker exec unifiedcollector_browser_cookie_vault `
  python -c "import asyncio; from src.tools.browser_cookie_vault import restore_from_latest; asyncio.run(restore_from_latest())"
```

Reload container (the on-start autorestore fires when
`BROWSER_COOKIE_VAULT_AUTORESTORE=1`):

```powershell
docker restart unifiedcollector_browser_cookie_vault
docker logs unifiedcollector_browser_cookie_vault --tail 10
# expect: "cookie restore: pushed N cookies from <ts> domains={...}"
```

## Extension reload (soft) via CDP

Trigger a background-service-worker reload without restarting Chrome:

```powershell
# List targets, look for the extension's SW target (title starts with "UnifiedCollector Bridge").
curl -s http://localhost:9222/json | ConvertFrom-Json | Where-Object { $_.title -like 'UnifiedCollector*' }
# Then hit its `webSocketDebuggerUrl` and evaluate:
#   Runtime.evaluate({expression: "chrome.runtime.reload()"})
# For a real reload of the SW state, use scripts/hard_reload_ext.py (already in-repo).
```

## Realtime feed troubleshooting

Queue depth + dedupe set size:

```powershell
docker exec -e REDISCLI_AUTH=localdevredis123 unifiedcollector_redis `
  redis-cli LLEN uc:realtime_post_feed
docker exec -e REDISCLI_AUTH=localdevredis123 unifiedcollector_redis `
  redis-cli SCARD uc:realtime_post_feed:seen_sha
```

Live tail of outcomes (post-05adb56 the drain logs `sent to telegram:
ok=<bool> ...` per item):

```powershell
docker logs -f unifiedcollector_realtime_feed --tail 30
```

Force one test message (uses a real local vault file so multipart upload
succeeds; adjust content_id per run so dedupe doesn't skip):

```powershell
# Grab any existing local file to reuse:
$fp = docker exec unifiedcollector_postgres psql -U collector -d unifiedcollector -tAc `
  "SELECT file_path FROM media_items WHERE file_path LIKE '/vault/media/blobs/%' ORDER BY created_at DESC LIMIT 1"
$payload = @{
  source='strava'; author='ops_test'; content_id="ops_test_$(Get-Date -UFormat %s)"
  file_path=$fp; sha256=[System.Guid]::NewGuid().Guid
  caption='ops test'; kind='image'; content_type='image'; enqueued_at=(Get-Date -UFormat %s)
} | ConvertTo-Json -Compress
$payload | Out-File -NoNewline -Encoding utf8 .tmp_payload.json
docker cp .tmp_payload.json unifiedcollector_redis:/tmp/p.json
docker exec unifiedcollector_redis sh -c "cat /tmp/p.json | redis-cli -a localdevredis123 -x RPUSH uc:realtime_post_feed"
Remove-Item .tmp_payload.json
Start-Sleep 8
docker logs unifiedcollector_realtime_feed --tail 5
```

## Reset a stuck source (headless collectors)

```powershell
# Full container recreate — picks up env changes and clears in-memory state.
docker compose -f docker/docker-compose.yml up -d --force-recreate collector_<name>
# Softer: just restart to reload code from bind-mount (no env change).
docker restart unifiedcollector_collector_<name>
```

## Backup — force a run

```powershell
docker exec unifiedcollector_backup python3 -m src.backup.db_backup run
# Or restart the loop wrapper (it runs a backup on start).
docker restart unifiedcollector_backup
```

## Migration ledger check

```powershell
docker exec unifiedcollector_postgres psql -U collector -d unifiedcollector -c `
  "SELECT filename, applied_at FROM schema_migrations ORDER BY applied_at DESC LIMIT 8;"
```

## Container health sweep

```powershell
docker ps --format 'table {{.Names}}\t{{.Status}}' | Select-String unifiedcollector_
```
