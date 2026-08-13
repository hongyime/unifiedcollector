# UnifiedCollector Agent State

Updated: 2026-08-13 16:24 UTC / 2026-08-14 00:24 SGT

Current task status: Collector production-completion implementation is complete and live-verified. The remaining issues below are runtime/source conditions, not missing code.

Implemented in this slice:
- Browser/extension hygiene now has a hard tab budget: exactly one extension control tab, no duplicate scraper tabs, one canonical scraper tab per active browser-social platform, no persistent `about:blank`, and no pinned scraper/control tabs unless explicitly opted in.
- Extension reload verification now checks the control tab, service-worker diagnostics, content-script version, per-platform responsiveness, and tab-budget assertions.
- Instagram has bounded safe tuning knobs for story sweep, deep profile pass, rest window, revisit cap, 429 cooldown, and famous-account skip cap.
- `/instagram/health` reports sanitized stuck-stage diagnostics across login/browser, targets, API fetch, posts, media download, vault, realtime feed, Telegram upload, cooldown, and source health.
- Website/search crawls use domain-aware pacing with registrable-domain grouping, per-domain concurrency limits, jitter, round-robin scheduling, robots/backoff/status counters, and `/domain-pacing/status`.
- GitHub and YouTube workers write API quota snapshots and use adaptive budget controllers: GitHub targets 85% of core hourly quota from rate-limit headers; YouTube targets 90% of the daily data/search budget with Pacific reset handling.
- Realtime Telegram reporting is shortened and source-scoped; private text stays out of operator messages, local media fallback wording is concise, and per-source media counters are exposed.
- Optional rollout monitor is guarded for SpiderFoot/recon, Lemon8, and browser-heavy paths with dry-run/five/daily25 stages, seen-registry candidates, passive weak-lead SpiderFoot policy, and stop-or-rollback criteria.

Verification completed:
- Focused Collector test suites passed for extension bundle/static checks, browser maintenance scripts, domain pacing, quota controllers, optional rollout, dashboard coverage, realtime feed, GitHub, YouTube, website, and search.
- `docker compose -f docker\docker-compose.yml config --quiet` passed.
- Touched Collector services were rebuilt/recreated and are healthy: collector, lowrisk, website, youtube, instagram, scheduler, realtime_feed, and dashboard.
- Live CDP audit passed with 7 page targets: 1 extension control page, 6 scraper tabs, 0 blank tabs, and 1 tab each for instagram, threads, tiktok, x, facebook, and strava.
- Extension control-page diagnostics passed after the commit hook version bump: version `1.23.70`, ingest `http://127.0.0.1:8765`, loop running, tab budget ok, pinned scraper/control tabs 0, expanded platform tabs false.
- Live `/instagram/health`: stuck_stage `cooldown`; source_health running; latest browser heartbeat fresh; 24h browser ingest includes profile/posts/comments/media; realtime delivery counts present; vault available and writable; latest media query timed out and is recorded as `section_errors.latest_media=TimeoutError`.
- Live `/media/realtime-feed/status`: queue_depth 0, failed_depth 3, local_fallback_total 49, and source counters present for x, threads, telegram, youtube, facebook, lemon8, instagram, and tiktok.
- Live `/domain-pacing/status`: search source has one recent domain family, 40 HTTP 403 events, 0 robots-blocked events, and 0 HTTP 429 events in the current window.
- Live `/api-quotas/status`: three GitHub core-hour snapshots and one YouTube data_api snapshot are present; none are paused.
- Live optional rollout check for SpiderFoot dry-run reports `recommended_action=dry_run`, `can_proceed=false`, `target_cap=0`, and 50 current stop reasons, so optional expansion stays disabled.

Operational notes for the next agent:
- Instagram is working end-to-end enough to report profile/browser/media/realtime/vault state, but production throughput is intentionally held by the active daily profile-view cooldown until the quota window resets.
- The latest-media section is isolated behind a timeout so the health report stays useful even when the production `media_items` lookup is slow.
- Optional SpiderFoot/recon should not be advanced until the stop reasons clear in the rollout monitor.
