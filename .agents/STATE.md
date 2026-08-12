# UnifiedCollector Agent State

Updated: 2026-08-12 11:42 SGT

Current task: browser resource cleanup and Telegram media fallback hardening are implemented, tested, and live-verified; X and Facebook browser content progress remain the only degraded live sources.

Recent completed work:
- SpiderFoot recon sidecar is live and verified under the `recon` Compose profile.
- Dashboard browser-health optional timeout noise was fixed, tested, rebuilt, deployed, committed, and pushed as `0ec39ea0 fix: suppress optional browser health noise`.
- Additional browser optional diagnostic suppression was committed and pushed as `531298f7 fix: suppress optional browser diagnostics`.
- Collector-derived recon seeding was committed and pushed as `70212a23 feat: seed collector recon targets`.

In-progress work:
- X browser capture is loaded on `https://x.com/home` with cookies preserved, but accepted X content progress has not advanced since `x_posts` max `2026-08-11 23:11:08+00`; recovery is now less aggressive and stays on `x.com` only.
- Facebook browser content progress is stale in `/health?include_sources=true`; the page/tab is open, but accepted content progress has not advanced inside the freshness window.

Current evidence:
- `python -m pytest tests/test_recon.py tests/test_recon_spiderfoot_service.py -q` passed with 17 tests.
- Live Docker core services checked recently: dashboard, spiderfoot, postgres, realtime_feed, watchdog are up/healthy.
- `docker exec unifiedcollector_collector python -m src.main recon-seed --dry-run --json --limit 20 --per-source-limit 5` worked against live DB and returned 20 candidates.
- A broken dirty Docker change was found and corrected: SpiderFoot must use `src.recon_spiderfoot_service`, not `src.main`, because the slim SpiderFoot image does not install full collector dependencies.
- `.gitignore` now allows `.agents/` so this state can be committed as required by AGENTS.md.
- Lazy `src.core` exports were verified with `from src.core import check_drive, ProfilePhotoTracker`.
- `python -m pytest tests/test_recon.py tests/test_recon_spiderfoot_service.py tests/dashboard/test_recon_api.py tests/verify_clean_boot.py -q` passed with 19 tests.
- `python -m pytest tests/dashboard/test_extension_health.py tests/dashboard/test_source_matrix.py -q` passed after browser optional diagnostic suppression.
- Dashboard was rebuilt/restarted and live `/health?include_sources=true` returned `status=ok`, `source_issues=0`, no browser diagnostic errors, and active browser content.
- `src.recon_seed_service` works as an optional one-shot entrypoint and redacts dry-run samples.
- Final live check after push: dashboard/spiderfoot/postgres/realtime_feed/watchdog and collector services are healthy; `/health?include_sources=true` returned `status=ok`, `source_issues=0`, and no browser diagnostic errors.
- WhatsApp partial bridge health was patched so one ready bridge plus one optional empty QR slot keeps WhatsApp source status `live` instead of making `/health` degraded.
- Recon observation storage now uses `value_hash` for idempotent SpiderFoot upserts; live DB migration `20260811_fix_recon_observation_value_hash.sql` is applied with checksum `f37e6c38e7ae`.
- Collector-derived username recon targets are scoped to `RECON_USERNAME_MODULES` (`sfp_accounts` by default) so username targets do not run DNS/WHOIS modules.
- Live SpiderFoot sidecar processed a target after the value-hash migration and wrote 44 observations.
- Committed and pushed `54c69a7e fix: harden spiderfoot recon observations`.
- Final live check: worktree clean, `unifiedcollector_spiderfoot` healthy, `/health?include_sources=true` status `ok` with 0 source issues, and all 106 recon observations had `value_hash` populated.
- Browser media revisit endpoints now expire stale exhausted `claimed`/`pending` rows to `failed` audit state before claiming new work.
- Live Threads media revisit queue recovered old stuck claims: 42 claimed rows fell to 2 claimed rows, with the remaining attempt-5 row still inside the configured 30-minute claim timeout at verification.
- `python -m pytest tests/bridges/test_ig_ingest_vault.py tests/dashboard/test_extension_health.py -q` passed with 82 tests.
- Media revisit stale-claim fix was committed and pushed as `423fbbf3 fix: expire exhausted media revisit claims`.
- Fresh read-only audit on 2026-08-12 found tracked worktree clean, `unifiedcollector_spiderfoot` healthy, recon targets `completed=24 failed=0`, `recon_observations=128`, all observations have `value_hash`, `/health?include_sources=true` status `ok` with `source_issues=0`, live CDP `page_targets=9`, and `duplicate_url_groups=0`.
- Browser media revisit queue visibility was committed and pushed as `05b76522 feat: show browser media revisit queue health`.
- Live Collector dashboard health returned `status=ok`, `source_issues=0`, browser ingest active, all configured sources live, and WhatsApp bridge partial state correctly shown as one ready slot plus one optional QR slot.
- Live SpiderFoot sidecar is healthy and idle, with no repeated malformed-JSON target failures in the latest log window.
- Live CDP duplicate tab cleanup closed duplicate extension control pages and duplicate Lemon8 topic tabs; follow-up check returned `PAGE_TARGETS=13` and `DUPLICATE_URL_GROUPS=0`.
- Focused tests passed: `python -m pytest tests/tools/test_browser_maintenance_scripts.py -q` -> 32 passed; `python -m pytest tests/dashboard/test_extension_health.py -q` -> 18 passed.
- Stronger duplicate control-page cleanup now runs inside `Ensure-ExtensionControlTab` and after degraded/successful maintenance exits.
- WhatsApp recovered after `collector_whatsapp` restart: bridge 2/session_2 is ready and bridge 1/session_1 remains an optional QR slot.
- Browser CDP was recovered after scraper Chrome profile restart attempt; browser ingest is active and maintenance status returned `ok`.
- Live X audit showed the content script attached and responsive on `https://x.com/home`, but page health is `recoverable_error_shell` with reason `try_again_empty_state`; `/health?include_sources=true` still reports X as the only degraded source.
- Follow-up audit after manual X login still found Collector health degraded only for X: `browser content progress is ~2.4h old (>3600s)`, while Instagram, TikTok, Lemon8, Threads, Facebook, Strava, Telegram, WhatsApp, Beeper, YouTube, Website, GitHub, and Search were live.
- Live CDP page targets showed one exact duplicate group: two `https://x.com/home` tabs. There were also two extension control tabs from different extension IDs, so browser resource cleanup needs extension-side dedupe plus old-control-tab cleanup.
- Realtime Telegram feed is healthy and sending messages. Large local files are archived in Collector first; if Telegram upload is too large or rejected, `src.notifications.realtime_feed` sends a concise text fallback without the full local path by default.
- Browser extension refresh/loop code now closes canonical duplicate scraper tabs during normal operation.
- Auto-opened scraper tabs are unpinned by default; optional local storage key `ucPinScraperTabs=true` can re-enable pinning.
- Lemon8 is no longer an active browser scraper tab by default; Collector health still shows Lemon8 live through backend/headless collection freshness.
- Browser maintenance now parses CDP target lists from raw JSON, closes duplicate extension control pages including query-string variants, and closes the blank startup tab once the extension control page exists.
- X hard recovery no longer bounces to `twitter.com`; X shell recovery stays on `x.com` and waits 5 minutes before another Try Again navigation.
- Telegram local-media fallback text no longer prints the full Collector vault path by default. It reports that media was stored in the Collector vault and includes only the basename unless `REALTIME_POST_FEED_INCLUDE_LOCAL_PATHS=1`.
- Realtime feed container was restarted after the fallback wording change and continued sending Telegram messages.
- Live CDP check after cleanup returned `PAGE_TARGETS=8`, `DUPLICATE_URL_GROUPS=0`, one extension control tab, no `about:blank`, and no Lemon8 browser tab.
- Live `/health?include_sources=true` after cleanup returned Instagram, TikTok, Threads, Lemon8, and WhatsApp live; Facebook and X remained degraded for browser content progress.
- Focused tests passed: `python -m pytest tests\extension\test_extension_bundle_static.py tests\tools\test_browser_maintenance_scripts.py tests\notifications\test_realtime_feed.py -q` -> 104 passed.
- Final post-cleanup tests passed: `python -m pytest tests\extension\test_extension_bundle_static.py tests\tools\test_browser_maintenance_scripts.py -q` -> 64 passed.

Next steps:
1. Let X sit on `x.com/home` long enough for the less aggressive recovery path, then verify whether `x_posts` or X media rows advance.
2. Investigate Facebook browser content progress if it remains stale after the next maintenance cycle.
3. Watch DB lock pressure from long GitHub COPY/backfill work; broad media/source rollup queries may time out while backup/backfill holds locks, but live source health and realtime feed continue operating.
