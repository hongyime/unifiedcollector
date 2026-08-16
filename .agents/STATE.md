# UnifiedCollector Agent State

Updated: 2026-08-16 05:04 UTC / 2026-08-16 13:04 SGT

Current task status: Collector runtime hardening, dashboard status cleanup, GitHub quota pusher, realtime fallback bucketing, WhatsApp bridge health, ig_ingest lane isolation, and scraper-browser Threads recovery are implemented and live-verified. Dashboard and affected Collector services were recreated; dashboard is healthy with a larger DB pool and longer healthcheck timeout.

Implemented in this slice:
- Dashboard `/health` now reports explicit `database_status`, `drive_status`, `vault.status`, and normalized backup states instead of vague Database/Vault/DB backup labels.
- Dashboard health uses bounded DB acquire plus optional source/browser timeouts, so heavy backup or source diagnostics should degrade the payload instead of wedging the whole dashboard.
- GitHub lowrisk worker now has quota pusher support and live env: enabled, target ratio `0.80`, max concurrency `12`, batch size `250`, HTTP timeout `20s`.
- Realtime Telegram fallback tracking now records future fallback reason buckets and source+reason buckets; existing Redis counters remain by source until new fallbacks occur.
- `ig_ingest` now has lane isolation for heartbeat/write/revisit/DM sample paths: heartbeat 16, write 4, revisit 2, DM sample 1.
- WhatsApp bridge health now probes fallback host URLs and treats one ready bridge plus one waiting QR slot as partial/live, not a hard source failure.
- X is classified as `external_auth_or_page_shell` and removed from browser-required recovery/default maintenance open sets; maintenance no longer reopens X by default.
- Scraper Chrome/CDP defaults are on port `9336`, with launcher lock/state files and current critical scripts/tests updated to match.
- Analyzer ignored `.env` was updated with the supplied Supabase API key aliases and password; Analyzer `analyzer` and `scheduler` containers both read the Supabase env.

Verification completed:
- Python compile passed for `src\dashboard\api.py`; focused pytest passed for dashboard and browser-maintenance script coverage.
- Earlier focused compile/pytest, frontend `tsc --noEmit`, and Vite build passed for the broader Collector changes in this slice.
- Compose config validation passed after the dashboard DB pool/healthcheck edits.
- Recreated Collector `dashboard`, `collector_lowrisk`, `ig_ingest`, `realtime_feed`, and `watchdog`; dashboard is healthy.
- Live `/health?include_storage=1&include_sources=1` returned `database_status=ok`, `drive_status=ok`, `vault_status=degraded`, `backup_status=backup_running`, 14 sources, and only WhatsApp partial pairing plus X external shell as source issues.
- Live `/api-quotas/status` shows GitHub quota pusher enabled with target `0.8`, concurrency `12`, batch `250`, no transport block, and reason `idle_no_work` because pending GitHub spider work is currently 0.
- Live `/media/realtime-feed/status` shows queue depth 0 and 82 historical local fallbacks by source: telegram 39, search 26, youtube 10, instagram 4, website 1, x 1, threads 1. Reason buckets will populate for new fallback events.
- Live `ig_ingest /health` returned `ok=true`, DB pool ready, heartbeat/write/revisit/DM lane concurrency `16/4/2/1`.
- Browser audit on `127.0.0.1:9336` now has one current extension control tab, no blank tabs, and one tab each for Instagram, Threads, TikTok, Facebook, and Strava; Threads, Instagram, Facebook, and Strava had content script `1.23.70` running.
- Analyzer service-to-service readback from the Docker network (`http://analyzer:8002`) returned healthy API and Supabase export status configured with `write_method=postgres_direct`, `mode=postgres_direct`.

Known caveats:
- The Windows host port proxy for Analyzer `127.0.0.1:8002` currently returns empty replies even though the Analyzer app is healthy inside the container and reachable from other Docker containers as `http://analyzer:8002`; this appears to be Docker Desktop/Windows port-proxy state, not Supabase or Analyzer app failure.
- Collector backup is actively running, and Postgres is under heavy load; dashboard now stays bounded, but ledger-style optional queries may still show `TimeoutError` while the dump/COPY workload is active.
- Vault is writable/available but dashboard marks it `degraded` because artifact sidecar counts are partial or slow, not because vault writes are blocked.
- WhatsApp is live through bridge 2; bridge 1 is still waiting for QR/session pairing. Scan bridge 1 only if that second WhatsApp account/device is expected.
- X is intentionally not reopened by default because it was producing external shell/worker churn; treat it as operator-auth/page-shell work, not a blocker for Threads/Facebook browser capture.
