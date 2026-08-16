# UnifiedCollector Agent State

Updated: 2026-08-16 07:18 UTC / 2026-08-16 15:18 SGT

Current task status: Browser self-heal, X containment, SpiderFoot daily100 gate application, and post-reboot service verification are implemented and live-verified. Collector changes are ready to commit/push.

Implemented in this slice:
- UnifiedCollector Bridge extension bumped to `1.23.72`; X remains in the registry for manual/login access but is no longer a scraper-managed platform.
- Browser auto-open/reload intent paths now use the auto-managed scraper list only: Instagram, Threads, TikTok, Facebook, and Strava.
- Browser tab audit enforces X as an excluded platform with allowed count `0` by default.
- Browser reload planner sweeps live excluded CDP targets even when the preceding audit misses them.
- Chrome launcher prefers the primary extension id before historical fallback ids, so stale `nke...` control tabs do not count as success.
- Maintenance skips whole-profile restarts for tab-health failures unless `UC_BROWSER_PROFILE_RESTART_ON_TAB_HEALTH` is explicitly enabled.
- Maintenance loop still kills timed-out child processes but now defaults to `UC_BROWSER_MAINTENANCE_PASS_TIMEOUT_SECONDS=600`; the prior 300s default was too low under backup/social-tab load.
- `dashboard`, `ig_ingest`, and `scheduler` were recreated with `UC_EXTENSION_EXPECTED_VERSION=1.23.72`.
- SpiderFoot `daily100` rollout gate was applied after the live gate returned `can_proceed=true` and `stop_reasons=[]`; apply queued 100 targets, skipped 0.

Verification completed:
- Focused tests passed: `tests\tools\test_browser_maintenance_scripts.py` and `tests\extension\test_extension_bundle_static.py`.
- Compile/syntax checks passed: `python -m compileall tools\browser_tab_audit.py tools\browser_tab_reload.py`, `node --check extension\background.js`, `node --check extension\tabs.js`.
- `docker compose -f docker\docker-compose.yml config --quiet` passed.
- Live CDP audit on `127.0.0.1:9336` reports exactly one extension control tab, no blank tabs, one each for Instagram/Threads/TikTok/Facebook/Strava, X missing, and content script `1.23.72` on all managed tabs.
- Browser maintenance status is `ok`; loop PID `21912` is alive with 10-minute interval and 300-second initial delay.
- Docker is broadly healthy: Collector/Analyzer/Musicstream/Pi-hole containers are up; `dashboard`, `scheduler`, `realtime_feed`, `collector_spiderfoot`, and source collectors are healthy where healthchecks exist.
- Dashboard `/health` reports `database_status=ok`, `drive_status=ok`, all 14 sources live, and no `source_issues`.
- `ig_ingest /health` reports DB pool ready and heartbeat/write/revisit/DM lane concurrency `16/4/2/1`.
- Realtime feed is available with queue depth 0; 82 local fallbacks are historical by source, with no reason buckets yet for old events.
- GitHub quota pusher is enabled with target ratio `0.8`, concurrency `12`, batch `250`, `transport_blocked=false`, and `pending=0`.
- Analyzer host `/api/health` is ok, and Supabase export status is `postgres_direct`, compact normalized rows only, with `ready_to_export=112`.

Known caveats:
- Dashboard overall status remains `degraded` only because vault sidecar counts are partial/slow and DB backup is actively running; this is not a DB/drive/source failure.
- Realtime delivery ledger can still show `TimeoutError` during the active DB backup, but the realtime queue itself is available and empty.
- Dashboard browser source rows may briefly show older `1.23.71` heartbeats until the extension posts fresh browser heartbeats; live CDP audit is the current source of truth for `1.23.72`.
- WhatsApp is collecting through bridge 2; bridge 1 is waiting for QR/session pairing and only needs action if a second WhatsApp device is expected.
- SpiderFoot remains weak-lead/passive only. A follow-up gate read after apply briefly timed out while backup load was active, then a retry returned `advance_stage` with no stop reasons.
