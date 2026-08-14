# UnifiedCollector Agent State

Updated: 2026-08-14 02:28 UTC / 2026-08-14 10:28 SGT

Current task status: Production-completion slice implemented, focused tests passed, touched Collector services recreated, and live dashboard/CDP checks completed. Changes are ready to commit.

Implemented in this slice:
- Added deterministic web/search request personas for User-Agent, Accept-Language, viewport width, Origin, and Referer so content-variance probes are deliberate and auditable.
- Added `WEBSITE_ROBOTS_POLICY=allowlist_override` plus `WEBSITE_ROBOTS_OVERRIDE_DOMAINS`; robots remain respected by default except explicit owned/authorized domains, with `robots_override` domain-pacing events.
- Broadened SpiderFoot rollout intake from `collector_seen_targets` to domain, URL, email, phone, IPv4/IP, user, username, and channel labels; queue aliases normalize to SpiderFoot target types and the new `daily100` stage is available.
- Added operation-scope helper for rate-limit events and made YouTube API cooldowns endpoint-scoped by API key, so `playlistItems.list` cooldowns do not stop `videos.list` or other known-video work.
- Updated compose defaults for controlled ramp: GitHub concurrency/quota target raised first, web/search active domain families raised, Telegram spidering uses all four configured accounts, YouTube cheap backfill/enrichment batches increased with search quota kept small, and dashboard DB acquire timeout raised.
- Aligned `UC_EXTENSION_EXPECTED_VERSION` in compose to live version `1.23.70`.

Verification completed:
- Passed focused Collector tests for request personas, website robots override, optional rollout, recon aliases, YouTube scoped cooldowns, and existing website/search/GitHub/YouTube coverage.
- Passed Python compile checks for touched Collector modules.
- Passed `docker compose -f docker\docker-compose.yml config --quiet`.
- Recreated `collector_spiderfoot`, `collector_lowrisk`, `collector_website`, `collector_telegram`, `collector_youtube`, `ig_ingest`, `scheduler`, and `dashboard`; all health-managed touched services reported healthy after recreate.
- Chrome CDP audit showed 7 page tabs, exactly 1 extension control tab, 0 blank tabs, and 0 duplicate page URLs.
- Instagram health showed active ingestion and realtime delivery, with current stuck stage `cooldown` caused by `daily_profile_view_quota`, not an HTTP 429 cooldown.
- Quota dashboard showed current GitHub core snapshots at target ratio `0.90` with no paused GitHub/YouTube quota workers.
- Realtime media status showed per-source delivery counters and local fallback totals.

Operational notes:
- SpiderFoot remains a Collector sidecar/adapter and weak lead source only; truth promotion belongs to Analyzer.
- Website robots override must remain empty unless the operator explicitly lists owned or authorized domains.
- Optional SpiderFoot rollout remains deliberately blocked at dry-run because recent source-health/rate-limit stop criteria exist.
