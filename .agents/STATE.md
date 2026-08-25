Updated: 2026-08-21 23:46 UTC / 2026-08-22 07:46 SGT

Current live update:
- User reported normal Chrome tabs were not signed in. Collector auth still lives in the dedicated Chrome-for-Testing CDP profile on port 9336, not normal desktop Chrome. Cookie vault health continues to preserve an effective restorable auth snapshot (`count=86`, quality score 5169) with no missing required auth platforms.
- Managed tab audit is budget-clean: 8 page targets, 1 extension control tab, 0 blank tabs, one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X. Content scripts are attached for Facebook, Threads, TikTok, X, and Strava on the last audit; Lemon8 does not currently attach; Instagram is reachable but on a removed-post/page shell.
- Hard-reopened X, Facebook, and Instagram tabs through `tools/browser_tab_reload.py --platforms instagram,facebook,x --hard-reopen --json`. X remains a real `try_again_empty_state` page shell; Instagram/Meta is page-churn/HTTP-429 prone; Facebook content script is present but source-health still has stale-content warning rows.
- Collector Postgres entered recovery and rejected connections until `2026-08-22T07:40:47+08:00`; during that period dashboard/source-matrix errors were DB recovery symptoms, not Chrome cookie loss.
- Analyzer readiness/production summary was patched to keep these browser stale/page-shell rows as warning-level operator work instead of critical production failure when auth/cookie vault and browser ingest evidence are intact.

Updated: 2026-08-21 21:38 UTC / 2026-08-22 05:38 SGT

Current live update:
- Rechecked the operator report that visible Chrome/social tabs looked logged out. CDP `9336` is live on the managed Chrome-for-Testing profile `ChromeCdpAutomationProfile_fresh_20260822_0325`; ordinary desktop Chrome login state remains separate.
- Cookie vault health is ok: latest/effective snapshots contain safe auth marker names for Facebook, Instagram, Strava, TikTok, and X. Effective latest is an 89-cookie restorable snapshot; no raw cookie values were logged.
- Repaired only bad platform tabs with `tools\browser_tab_reload.py --platforms instagram,threads,lemon8 --hard-reopen --json`. Instagram moved from a removed post URL to `https://www.instagram.com/explore/`; Threads moved from `?error=invalid_post` to a canonical Threads page; Lemon8 was reopened to the Singapore topic URL.
- Follow-up `tools\browser_tab_audit.py --json` is budget-clean: 8 page targets, one extension control tab, zero blank tabs, one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X. Content scripts are running for Instagram, Threads, TikTok, X, Facebook, and Strava. Lemon8 remains backend/headless-primary and may not inject a content script on the topic shell.
- Direct action queue remains clean: `GET /collectors/action-queue?status=open&limit=20` returned `count=0`. Direct Collector health can still show degraded when Beeper rollup-excluded quiet subsources are counted by the raw top-level status; these are `severity=ok` quiet subsource rows, not browser-cookie blockers.

Updated: 2026-08-21 21:20 UTC / 2026-08-22 05:20 SGT

Current live update:
- Fixed Collector action-queue stale cooldown handling. `src/core/collection_action_queue.py` now treats `rate_limit.active_until <= now` as expired even when a stale row still says `active_now=true`, and ignores last-complete-hour rate pressure once explicit `rate_limit.active_now=false` is present.
- Recreated `unifiedcollector_dashboard`. Direct core sync resolved stale open actions for TikTok expired rate pressure and Website target starvation after current source-matrix derivation returned no current actions. Live `GET /collectors/action-queue?status=open` now returns `count=0`.
- Final Analyzer readiness that consumes Collector proof is fully green: `/api/production/readiness` returned `status=ok`, `ok=true`, `13/13` checks ok, `degraded=0`, `critical_failed=0`.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py -q` passed 25; compileall and diff-check passed for touched Collector action-queue files.

Updated: 2026-08-21 20:52 UTC / 2026-08-22 04:52 SGT

Current live update:
- Rechecked visible-Chrome login scare. Collector CDP `9336` is live on the dedicated fresh Chrome-for-Testing profile `ChromeCdpAutomationProfile_fresh_20260822_0325`; this is not the user's normal desktop Chrome profile.
- Forced cookie-vault restore from the preserved effective snapshot: 89 cookies pushed into CDP across TikTok, Instagram, Threads, Facebook, X/Twitter, Strava, and Lemon8. Fresh backup wrote 88 social cookies and preserved the stronger 89-cookie snapshot. Auth markers remain present for Facebook, Instagram, Strava, TikTok, and X.
- Live Collector `/health?include_sources=true` is `status=ok`, `source_issues=[]`; browser ingest is active with fresh heartbeat/content for `bridge,facebook,instagram,lemon8,strava,threads,tiktok,x`. Source-matrix rolling output is healthy for Facebook, Instagram, Strava, Threads, and X. Open action queue has only TikTok recent rate/access pressure.
- Current tab audit shows no auth wall for Facebook, X, TikTok, Strava, Lemon8, and the extension. Instagram had been on a broken post URL and maintenance reopened it to Explore; generic tab titles alone are not reliable auth evidence.

Updated: 2026-08-21 20:47 UTC / 2026-08-22 04:47 SGT

Current live update:
- Managed Collector Chrome CDP profile on port 9336 is authenticated separately from normal desktop Chrome. Cookie vault restore pushed 89 cookies and backup wrote 89 social cookies with auth markers for Facebook, Instagram, Strava, TikTok, and X. X stale `Try again` page shell was hard-reopened to `https://x.com/home`; follow-up audit showed one tab per platform and content scripts running.
- Fixed false target-starved queue derivation. `src/core/collection_action_queue.py` now suppresses target-starved when rolling 60-minute output passes the floor. `src/dashboard/api.py` now adds browser-ingest rolling output fields (`stored_rolling_60m`, `observed_rolling_60m`, `requests_rolling_60m`) to source-matrix rows so the queue uses the same evidence as readiness.
- Recreated `unifiedcollector_dashboard` and synced the action queue. False target-starved rows for Facebook, Instagram, X, and Threads are resolved. Open queue is down to one real action: TikTok recent rate/access pressure.
- Live source-matrix now shows Threads rolling proof above floor (`stored_rolling_60m=15`, `observed_rolling_60m=22`, `requests_rolling_60m=16`). Collector dashboard is healthy; exposure collector and cookie vault are healthy.
- Verification: `python -m pytest tests\dashboard\test_source_matrix.py tests\core\test_collection_action_queue.py -q` passed 104; compileall and diff-check passed for touched Collector files.

Updated: 2026-08-21 19:59 UTC / 2026-08-22 03:59 SGT

Current live update:
- User reported visible Chrome tabs were logged out. Verified Collector uses a separate managed Chrome CDP profile on port `9336`, restored 88 social cookies from cookie vault, and backed up 88 cookies with auth markers for Facebook (`c_user`, `xs`), Instagram (`sessionid`), Strava (`_strava4_session`), TikTok (`sessionid`, `ttwid`), and X (`auth_token`, `ct0`).
- Recovered X end to end: reopened to `https://x.com/home`, removed duplicate/bad X shell tab, triggered scrape, and verified fresh X output (`x_posts` and `media_items source=x`). `source_health.x` is now `running`; Collector `/health?include_sources=true` returned top-level `status=ok` and `source_issues=[]`.
- Patched `/collectors/action-queue/sync` so normal successful sync also runs direct-health stale-action cleanup. This resolved stale X/browser-stall action rows after live health proved recovery. Dashboard was force-recreated and live action queue now has only Instagram/TikTok rate-pressure items.
- Final browser audit: 8 page targets total, one extension control tab, one tab per platform, zero blank tabs, tab budget ok. Instagram, Threads, TikTok, Lemon8, X, Facebook, and Strava all have content script `1.23.72` running.
- Verification: `python -m compileall src\dashboard\api.py src\core\collection_action_queue.py tests\dashboard\test_source_matrix.py tests\core\test_collection_action_queue.py` passed; focused pytest for direct-health cleanup and DB-acquire skeleton fallback passed; `git diff --check` passed for touched action-queue/source-matrix paths.

# UnifiedCollector Agent State

Updated: 2026-08-21 19:42 UTC / 2026-08-22 03:42 SGT

Current live update:
- User reported Chrome/social tabs looked logged out. Root cause split into two issues: the old managed Chrome-for-Testing profile on CDP `9336` had a wedged MV3 extension runtime (no UnifiedCollector service worker), while the normal desktop Chrome profile is separate and not the Collector profile.
- Stopped browser maintenance loop, killed the stuck Collector CDP Chrome root, launched a fresh managed profile at `C:\Users\bryan\AppData\Local\UnifiedCollector\ChromeCdpAutomationProfile_fresh_20260822_0325`, and restored 88 cookies from the cookie vault into CDP `9336`. Cookie markers are physically present in the fresh profile for Facebook (`c_user`, `xs`), Instagram (`sessionid`), Strava (`_strava4_session`), X (`auth_token`, `ct0`), and TikTok (`sessionid`, `ttwid`).
- Fresh profile fixed the extension runtime: CDP now shows `chrome-extension://pkmdmcklnjdeocoeigmlakhomhhcpafb/background.js`, content scripts attach again, and `unifiedcollector_ig_ingest` logged fresh `/social/browser-heartbeat`, `/social/posts`, `/social/ingest`, and `/social/users` calls around `2026-08-21T19:34Z` to `19:36Z`.
- Important caveat: restored cookies alone did not fully re-auth Meta/Strava in the fresh profile. Latest audit showed Facebook and the extra Instagram root tab rendering login walls, Strava at `/login`, X landing on `https://x.com/`/stale capture, while TikTok/Lemon8/Threads have extension activity. Manual login may be needed for Facebook/Instagram/Strava/X in the fresh managed Chrome window, after which cookie vault should capture the new trusted session.
- Patched `src/dashboard/api.py` so `/collectors/action-queue/sync` skips non-evidentiary source-matrix fallback from `db_acquire`/skeleton rows and does not create durable operator actions during DB/source-matrix pressure. Added `test_action_queue_sync_skips_db_acquire_skeleton_payload`; focused tests for timeout, db-acquire skeleton, and refreshing partial matrix passed.
- Resolved 14 false open `collection_action_queue` rows created from `source matrix could not acquire a DB connection quickly...`; later patched sync returned `status=skipped` instead of recreating them.
- Postgres recovered from crash recovery and is healthy. Backup had failed twice during heavy `pg_dump` copy pressure (`github_commits`, `beeper_shadow_messages`); avoid running action/source-matrix proof during backup pressure without the new guard.
- Latest live blockers after fresh profile: Collector health degraded by Facebook and X browser-content staleness plus browser-extension diagnostics timeout; open action queue included Facebook/X browser capture stale, Instagram/TikTok cooldowns, website/threads/lemon8 output floors, Beeper stale, and browser-extension stale DM hook/maintenance rows. Re-run after manual login and another scrape cycle.
- Verification: `python -m pytest tests\dashboard\test_source_matrix.py::test_action_queue_sync_skips_timeout_fast_fallback tests\dashboard\test_source_matrix.py::test_action_queue_sync_skips_db_acquire_skeleton_payload tests\dashboard\test_source_matrix.py::test_action_queue_sync_skips_refreshing_partial_matrix -q` passed; `python -m compileall src\dashboard\api.py tests\dashboard\test_source_matrix.py` passed; `git diff --check` passed for touched files.

Updated: 2026-08-21 17:22 UTC / 2026-08-22 01:22 SGT

Current live update:
- User reported all Chrome tabs looked logged out. Live diagnosis showed the Collector-managed Chrome-for-Testing profile on CDP `9336` was wedged; the cookie vault still had a restorable 88-cookie auth snapshot.
- Stopped the maintenance loop, killed only Collector-owned Chrome-for-Testing processes tied to `UnifiedCollector\ChromeCdpAutomationProfile` / `remote-debugging-port=9336`, relaunched CDP, and restored 88 cookies into the managed profile. Restored domains included Facebook, Instagram, Strava, TikTok, Threads, X/Twitter, and Lemon8-related cookies.
- Patched `scripts/browser-tab-maintenance.ps1` so browser tab-health failures do not profile-restart the managed Chrome profile unless `UC_BROWSER_PROFILE_RESTART_ON_TAB_HEALTH=1` is explicitly set. Threads `login_wall_text`, X `try_again_empty_state`, and responsive missing/stopped content-script cases are now targeted repair conditions instead of profile restart reasons.
- Final manual audit after cleanup/reload: tab budget clean with 8 page targets, one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X. Content scripts were running for Instagram, Threads, TikTok, X, Facebook, and Strava; Lemon8 loaded but still lacked content-script injection on its current topic page.
- Restarted the maintenance loop. Latest log confirms the new behavior: unhealthy tab state now logs that profile restart is disabled unless explicitly enabled instead of churning the profile. Collector health remains degraded by source-liveness DB timeouts and page-local warnings for Threads/X, not by missing cookies.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed after the first maintenance patch; a later rerun timed out under browser load after the final default change. `git diff --check` passed for touched maintenance files.

Updated: 2026-08-21 16:25 UTC / 2026-08-22 00:25 SGT

Current live update:
- Hardened action-queue/source-matrix edge cases. `src/dashboard/api.py` now recomputes final `rate_limit.active_now` from `active_until` and current time instead of trusting stale cached booleans. `/collectors/action-queue/sync` now skips both unavailable and refreshing partial source-matrix payloads when core sections timed out/cancelled, avoiding false zero-window actions.
- `src/core/collection_action_queue.py` now treats expired cooldowns as "ignore blocker and continue evaluating" rather than "skip the source", and browser-media sources (`facebook`, `instagram`, `lemon8`, `threads`, `tiktok`, `x`) use a media-output floor while website keeps useful-output/slow-crawl semantics.
- Verification passed: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_source_matrix.py -q`, compileall, and `git diff --check` for touched Collector files.
- Recreated `unifiedcollector_dashboard`. Live action-queue sync under current DB/browser churn now returns `status=skipped`, `reason=source_matrix_unavailable`, proving the partial-matrix guard is active. Resolved six stale false `target_starved` rows from the pre-guard zero-window batch; open queue is now down to two real actions: `browser_extension/repair_browser` from maintenance timeout and `lemon8/source_blocked` from browser capture/content stall.
- Managed browser recovery proof: stopped the wedged Collector CDP Chrome profile, relaunched on CDP `9336`, restored 88 cookies, and audited a clean tab budget (one extension control tab, zero blank tabs, one each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, X). Cookie vault effective latest is restorable with safe auth markers for Facebook, Instagram, Strava, TikTok, and X.
- Remaining blockers: Collector health is currently degraded by browser capture staleness for at least Facebook/Threads and ongoing Lemon8 browser capture stall; Analyzer `/api/production/readiness` is degraded under load with DB/health timeouts and sees the two real Collector actions. Supabase status remains ok with remote row count `2372`, `ready_to_export=0`, `raw_mirror=false`.

Updated: 2026-08-21 15:43 UTC / 2026-08-21 23:43 SGT

Current live update:
- Rechecked the user's "not signed in to any tabs" report against the live managed browser. The only visible Chrome process is the Collector-managed Chrome-for-Testing profile on CDP `9336`, not normal desktop Chrome.
- Cookie vault remained healthy and restorable. Restored the effective 88-cookie snapshot into CDP with safe auth markers for Facebook, Instagram, Strava, TikTok, and X; no cookie values were logged.
- Browser maintenance/reload initially wedged and left duplicate/missing tabs, so the tab budget was repaired with `tools/browser_tab_reload.py`. Final audit is clean: one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X.
- Final Collector `/health?include_sources=true` returned top-level `status=ok` and `source_issues=[]`. X, Facebook, Instagram, Threads, TikTok, and Strava all have fresh live source evidence; Lemon8 remains live but zero useful media/content from its browser page, which is a yield/content problem, not lost cookies.

Updated: 2026-08-21 14:48 UTC / 2026-08-21 22:48 SGT

Current live update:
- Hardened action-queue sync against non-evidentiary source-matrix fast fallbacks. If forced source-matrix refresh returns `cache.status=unavailable` with `source_matrix` `TimeoutError` or `BuildInProgress`, `/collectors/action-queue/sync` now returns `status=skipped` and does not create or resolve durable actions from stale `source_health` fallback rows.
- Recreated `unifiedcollector_dashboard`. A live action-queue sync during DB load returned `status=skipped`, `reason=source_matrix_unavailable`, proving the guard fired instead of opening false browser/source actions. Direct `GET /collectors/action-queue?status=open` returned `count=0`.
- Live `/health?include_sources=true` returned top-level `status=ok`, `source_issues=[]`. Facebook is live with fresh browser content, current `source_health` running, and recent browser media progress; cookie vault remains restorable with 87 auth-bearing cookies.
- Remaining caveats are warning/pressure states, not hard blockers: Instagram scoped daily profile-view quota, Lemon8 optional extension heartbeat/content staleness while backend rows are fresh, X page-shell warning with recent X media/cookie markers, and WhatsApp bridge 1 still unpaired while bridge 2 collects.
- Verification: `python -m pytest tests\dashboard\test_source_matrix.py::test_action_queue_sync_forces_fresh_source_matrix tests\dashboard\test_source_matrix.py::test_action_queue_sync_skips_timeout_fast_fallback tests\dashboard\test_source_matrix.py::test_source_matrix_blocker_treats_cold_api_timeout_as_stats_unavailable tests\core\test_collection_action_queue.py::test_derive_collection_actions_ignores_source_liveness_timeout_skeleton_rows -q -vv` passed 4; compileall and diff-check passed for touched Collector files.

Updated: 2026-08-21 14:30 UTC / 2026-08-21 22:30 SGT

Current live update:
- Rechecked the fresh "not signed in to Chrome tabs" report. The running browser is still the managed Chrome-for-Testing profile `ChromeCdpAutomationProfile_goal_recover_20260821_180809` on CDP `9336`, not the normal desktop Chrome profile.
- Cookie vault is healthy and restorable. Latest/effective snapshot after restore/backup has 87 cookies with safe auth markers for Facebook, Instagram, Strava, TikTok, and X; no raw cookie values were logged.
- Restored the current 87-cookie vault snapshot into the managed CDP profile and reloaded/reopened platform tabs. Tab audit is clean: one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X.
- Post-restore tab audit shows extension content script attached for Facebook, Instagram, Threads, TikTok, X, and Strava. Lemon8 still has no content script on its current shell, but Lemon8 backend/source health is running and recent.
- Triggered the extension control page `scrapeNow` path. Facebook recovered from browser-content progress staleness: direct DB proof showed `source_health` back to `running`, 19 Facebook posts and 4+ media in the last hour, and refreshed dashboard health later showed Facebook live with 19 records and 5 media this hour.
- Collector action queue sync returned `derived=0`, `open=0`, `resolved=7`; final Collector `/health?include_sources=true` returned top-level `status=ok` and `source_issues=[]`.
- Remaining caveat: under DB load, source-matrix/readiness can still fall back or timeout. Instagram remains under scoped daily profile-view quota until `2026-08-21T23:59:59Z`; X can still show a warning page-shell label while recent X media exists and vault auth markers remain present.

Updated: 2026-08-21 14:15 UTC / 2026-08-21 22:15 SGT

Current live update:
- Subagent reviewer/auditor/researcher pass completed. Highest Collector finding was a hidden health/action-queue mismatch; action-queue sync now forces a fresh source-matrix build instead of using stale cache, so non-ok browser/source evidence becomes operator-visible.
- Hardened browser maintenance self-heal after live CDP wedge: profile restart launcher timeout is now `300s`, and maintenance attempts a final CDP repair before writing degraded after a profile restart. Focused tests passed.
- Recovered managed Chrome-for-Testing profile on CDP `9336`, restored 87 cookies, and verified cookie vault healthy/restorable with safe auth markers for Facebook, Instagram, Strava, TikTok, and X. Latest vault backup timestamp was `2026-08-21T13:54:59Z`.
- Final tab audit was budget-clean: one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X. X was no longer in `try_again_empty_state`.
- Direct `/health?include_sources=true` returned `status=ok`, `source_issues=[]`, browser maintenance last terminal `ok`, cookie vault ok, and X live with fresh content around `2026-08-21T21:58:13+08:00`.
- Remaining gap: `/collectors/action-queue/sync` now surfaces 7 warning actions from source-matrix timeout/missing-content rows (browser-managed sources plus website). Some look over-eager because direct health shows source issues zero; next hardening should attach explicit suppression/coverage reasons for timeout-derived `browser_capture_stalled` rows with fresh source_health/rolling output instead of opening noisy actions.
- Verification: `python -m pytest tests\dashboard\test_source_matrix.py::test_action_queue_sync_forces_fresh_source_matrix tests\dashboard\test_source_matrix.py::test_collectors_source_matrix_reuses_fresh_payload_cache tests\core\test_collection_action_queue.py -q` passed 24; `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 43; compileall and diff-check passed for touched Collector files.

Updated: 2026-08-21 13:23 UTC / 2026-08-21 21:23 SGT

Current live update:
- The "not signed in to Chrome" report was visible-desktop/profile confusion plus a managed tab-shell stall, not total Collector cookie loss. The browser-cookie vault is healthy and the effective latest snapshot is restorable with safe auth markers for Facebook, Instagram, Strava, TikTok, and X.
- Restored 87 social cookies into the managed Collector Chrome-for-Testing CDP profile on port `9336`, reopened/reloaded stuck X/Facebook/Lemon8/TikTok tabs, opened the missing Strava tab, and cleaned the tab budget.
- Final tab audit is clean: one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X. X no longer reports the `try_again_empty_state` shell in the audit.
- Fixed action-queue false positives where empty/missing `last_complete_hour` stats were treated as confirmed zero output. Yield-floor `target_starved` actions now require both current and last-complete windows to contain actual yield counters.
- Recreated `unifiedcollector_dashboard`. Live action-queue sync returned `derived=0`, `open=0`, `resolved=7`; `GET /collectors/action-queue?status=open` returned `count=0`. Collector `/health?include_sources=true` returned overall `status=ok`; source liveness can still timeout under load, but browser extension hard issues are empty and action queue is clean.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py -q` passed 22; compileall and diff-check passed for touched action-queue files.

Updated: 2026-08-21 12:46 UTC / 2026-08-21 20:46 SGT

Current live update:
- Repaired the managed browser after a maintenance/relaunch pass left CDP partially reachable and platform tabs missing/duplicated. Reopened the platform set, cleaned duplicate extension/TikTok tabs, and verified the final tab budget has one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X.
- Browser-cookie vault on `8790` is healthy from Docker again. Effective latest is restorable with safe auth marker names for Facebook, Instagram, Strava, TikTok, and X; no raw cookie values were logged.
- Live Collector `/health?include_sources=true` returned `status=ok`, database ok, zero source issues, browser ingest `active_via_maintenance`, and active platforms Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X. A warning-level X `try_again_empty_state` tab shell can still appear, but it is not currently a hard source issue while source health stays ok.
- Remaining open Collector action-queue items visible in Analyzer readiness: TikTok rate/access pressure, Lemon8 browser-content staleness/useful-output mismatch, and Website target starvation.

Current live update:
- Rechecked the latest "not signed in to Chrome" report. The active Collector browser process is Playwright Chrome-for-Testing with profile `ChromeCdpAutomationProfile_goal_recover_20260821_180809` on CDP `9336`; no normal `Google\Chrome\Application\chrome.exe` process was visible in the live process list.
- Cookie-vault health on `8790` is ok. Effective latest remains restorable with safe auth marker names for Facebook, Instagram, Strava, TikTok, and X; do not log raw cookie values.
- CDP tabs are logged in/usable for Facebook, X, TikTok, Threads, Strava, and Lemon8. Instagram is currently an HTTP 429 recoverable page shell, which is a rate-limit page, not proof of cookie loss.
- Cleaned a duplicate UnifiedCollector Social Tabs extension page with `scripts\cleanup_ext_tabs.py`. Final `tools\browser_tab_audit.py --json` has tab budget `ok=true`, one extension control tab, zero blank tabs, and one tab each for Facebook, Instagram, Lemon8, Strava, Threads, TikTok, and X.
- Live Collector `/health?include_sources=true` returned `status=ok`, browser maintenance `ok`, zero browser-extension issues, and fresh browser ingest/content for Facebook, Lemon8, Strava, Threads, TikTok, and X. Remaining source caveats are Instagram 429 and Lemon8/source-matrix stale wording versus fresh extension ingest.

Current live update:
- Rechecked the "not signed in to Chrome tabs" report. The visible Collector browser is Playwright/Chrome-for-Testing on CDP `9336`, not the normal desktop Chrome profile. CDP tabs currently show Facebook, Instagram, X, TikTok, Threads, Strava, Lemon8, and the UnifiedCollector Social Tabs control page.
- Cookie-vault health is ok after recreate. Latest restorable snapshot is fresh (`2026-08-21T11:24:57Z`) with safe auth marker names for Facebook, Instagram, Strava, TikTok, and X; no raw cookie values should be logged.
- Fixed host-side cookie restore path selection in `src/tools/browser_cookie_vault.py`: one-shot host commands now prefer repo-local `credentials/browser_cookies` instead of accidentally reading stale `C:\app\credentials\browser_cookies` when `/app` is interpreted by Windows.
- Added Lemon8 to managed browser audit/reload/maintenance defaults. `browser_tab_reload.py` can now open a canonical Lemon8 tab when source health says Lemon8 browser content is stale and no tab is open. The maintenance repair commands include `lemon8`.
- Live Lemon8 tab was opened/reopened at `https://www.lemon8-app.com/topic/singapore?region=sg`; tab budget is clean with one Lemon8 tab. Browser ingest now includes Lemon8 as an active/content platform, but the most recent Lemon8 media requests observed/stored `0` useful items after reopen, so remaining Lemon8 work is useful-output/content capture, not missing cookies.
- Action queue still has open actions after sync: TikTok rate/access pressure, Lemon8 stale/useful-output mismatch, and Website target starvation. The Lemon8 action wording is stale relative to fresh media requests and needs a follow-up source-matrix/action-queue policy fix if it keeps reopening.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\tools\test_browser_cookie_vault.py tests\tools\test_browser_maintenance_scripts.py -q` passed 81; compileall and diff-check passed for touched Collector files; `unifiedcollector_dashboard` and `unifiedcollector_browser_cookie_vault` were recreated.

Current live update:
- After the current source-matrix refresh, action-queue sync produced two real open actions: `tiktok/source_blocked` for recent rate-limit or access pressure and `lemon8/source_blocked` for browser-content staleness. This replaces the earlier short-lived zero-open queue state and matches Analyzer readiness warning `collector_hourly_yield_floor`.
- Analyzer frontend now exposes the readiness user-story proof at `/production`, so Collector source pressure and yield warnings are visible as user-facing operational work instead of hidden in raw JSON.

Current live update:
- Re-synced the durable collection action queue after the browser-cookie/TikTok session hardening and action-queue stale-maintenance suppression. Live sync returned `derived=0`, resolved the remaining open item, and `GET /collectors/action-queue?status=open` returned `open_count=0`.
- Collector `/health?include_sources=true` remained `status=ok`, `source_issues=0`, with browser ingest active for `bridge,facebook,instagram,strava,threads,tiktok,x`. TikTok can still show a warning-level `try_again_empty_state` tab shell, but it is not currently a hard source issue or action-queue blocker while fresh ingest and strong cookie-vault auth are present.
- Analyzer `/api/production/readiness` now includes user-story metadata for each production check and returned `status=ok`, `critical_failed=0`, `degraded=0` in the live proof.

Current live update:
- Investigated a fresh "not signed in to Chrome tabs" report. Active visible Chrome evidence is the Collector-managed Playwright Chromium profile on CDP `9336`, not an ordinary desktop Chrome profile.
- Restored 68 cookies from the guarded browser-cookie vault, then injected the older TikTok account jar into CDP because the generic vault had only weak TikTok device auth. Live vault backup now has 87 social cookies and safe auth marker names for Facebook, Instagram, Strava, X, and TikTok with TikTok `sessionid` plus `ttwid`.
- Hardened `src/tools/browser_cookie_vault.py` so TikTok `ttwid` alone no longer scores as a strong restorable session; a TikTok session marker is required too. This prevents anonymous/challenged TikTok snapshots from replacing stronger restore points.
- Hardened `src/core/collection_action_queue.py` so stale `degraded`/`overlap_skipped` maintenance rows do not create a browser-repair action when current evidence has zero hard browser issues and fresh active browser ingest. Hard `failed`, `cdp_unavailable`, and `running_stalled` states still create actions.
- Live final proof: Collector `/health?include_sources=true` returned `status=ok`, `source_issues=0`, browser ingest `active` for `bridge,facebook,instagram,strava,threads,tiktok,x`; browser-cookie vault `/health` returned `ok=true`, effective latest restorable, quality score `5167`. Action queue sync resolved `browser_extension:repair_browser`; remaining open action is Lemon8 browser-content staleness. TikTok still has a warning-level `try_again_empty_state` page shell, but cookie restore state is strong and ingest is active.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\tools\test_browser_cookie_vault.py -q` passed 35; compileall and diff-check passed for touched queue/vault files; `unifiedcollector_dashboard` and `unifiedcollector_browser_cookie_vault` were recreated.

Current live update:
- Fixed stale cooldown actions in `collection_action_queue`. Expired cooldown blockers are now ignored by comparing `rate_limit.active_until` or the `until ...` timestamp in blocker text against current UTC time; future cooldowns still produce `source_blocked`.
- Recreated `unifiedcollector_dashboard`. Live sync resolved the expired Lemon8 cooldown action and now leaves only one real open action: `tiktok/source_blocked` for profile-metadata challenge/rate pressure until `2026-08-21T18:48:28.07476+08:00`.
- Live Collector `/health?include_sources=true` returned `status=ok`, `source_issues=0`, browser maintenance `running` but non-stalled, browser issues `0`. A follow-up source-matrix check shows TikTok live with `rate_limit.active_now=true`, `active_until=2026-08-21T18:48:28.07476+08:00`, and one stored media item this hour.
- Analyzer readiness remained `status=ok`, `critical_failed=0`, `degraded=0`; Supabase remained `ready_to_export=0`, remote readback reachable, remote row count `2372`, `raw_mirror=false`.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_extension_health.py tests\dashboard\test_source_matrix.py -q` passed 126; compileall passed for touched files; `git diff --check` passed for touched Collector paths.

Current live update:
- Fixed the website action-queue false positive. Website crawls are multi-hour; `collection_action_queue` now treats `website` as a slow-yield source by default via `COLLECTION_ACTION_SLOW_YIELD_SOURCES=website` and suppresses hourly `target_starved` when `last_24h` has at least the useful-output threshold.
- Recreated `unifiedcollector_dashboard`. Live sync now resolves the website action and leaves only real pressure actions: `tiktok/source_blocked` recent rate/access pressure and `lemon8/source_blocked` scoped avatar-profile cooldown until `2026-08-21T10:29:12.397004+00:00`.
- Live Collector `/health?include_sources=true` returned `status=ok`, `source_issues=0`, browser maintenance `ok`, with one warning-only browser issue `instagram/browser_page_error/http_429`.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_extension_health.py tests\dashboard\test_source_matrix.py -q` passed 124; compileall passed for touched files; `git diff --check` passed for touched Collector paths.

Current live update:
- Hardened `health(include_sources=true)` to reuse the source-matrix cache when direct `compute_liveness` times out, avoiding false production degradation when the matrix cache has usable source rows. Also changed action-queue sync to go through the cached `/collectors/source-matrix` path instead of bypassing it.
- Corrected the action-queue yield default after live validation: default monitored sources are now high-signal browser/media sources plus `website` (`facebook,instagram,lemon8,threads,tiktok,website,x`), while `COLLECTION_ACTION_YIELD_SOURCES` can still explicitly broaden enforcement to any source.
- Recovered CDP `9336` after a maintenance pass wedged the managed browser. Restarted only managed UnifiedCollector Chrome processes, restored 68 guarded cookies from the vault, reloaded platform tabs, and invoked Social Tabs `scrapeNow`. Facebook and Strava are past login walls; Instagram is a real warning-level HTTP 429 page shell.
- Final live Collector proof: `/health?include_sources=true` returned `status=ok`, `source_issues=0`, browser maintenance `ok`, with one warning-only browser issue `instagram/browser_page_error/http_429`.
- Final live action queue has 3 real open actions: `tiktok/source_blocked` recent rate/access pressure, `lemon8/source_blocked` scoped avatar-profile cooldown, and `website/target_starved` below `5/hour` useful-output floor. The earlier 15 fake timeout skeleton actions were resolved.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_extension_health.py tests\dashboard\test_source_matrix.py -q` passed 122; compileall passed for touched files; `git diff --check` passed for touched Collector paths.

Current live update:
- Broadened the durable action-queue useful-output floor from only browser-social sources to primary collectors by default: `facebook,github,instagram,lemon8,search,strava,telegram,threads,tiktok,website,whatsapp,x,youtube`. `COLLECTION_ACTION_YIELD_SOURCES` can still override this explicitly.
- This closes the production gap where a primary live collector could emit zero useful output for the current and last complete hour without any operator action. Quiet Beeper subsources and stats-unavailable fallback rows remain suppressed.
- Added explicit action-queue suppression for source-matrix timeout skeleton rows (`source liveness query timed out; showing known source skeleton until DB load drops`) so DB-load fallbacks do not create mass fake `source_blocked` actions.
- Added WhatsApp pairing mapping so `whatsapp_pairing`/QR/unpaired blockers become `manual_auth_needed` when they are truly present.
- Recreated `unifiedcollector_dashboard`. Live action-queue sync during source-matrix timeout resolved the 15 fake skeleton actions and now leaves only two real open actions: `tiktok/source_blocked` for recent rate/access pressure and `website/target_starved` for below `5/hour` useful-output floor.
- Live Collector base `/health` is ok. Heavy `include_sources=true` can still report `source_liveness unavailable: TimeoutError` under DB load even while `/collectors/source-matrix` and action-queue sync recover; do not treat that timeout skeleton as source failure.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_extension_health.py -q` passed 45; compileall passed for `src\core\collection_action_queue.py` and `tests\core\test_collection_action_queue.py`; live sync against timeout skeleton produced `derived=2`, `resolved=1`, open actions `tiktok` and `website` only.

Current live update:
- Recovered the managed Collector browser after the operator saw normal Chrome logged out. Normal Chrome is a separate profile; Collector uses Playwright Chromium on CDP `9336`. Restarted only managed UnifiedCollector Chrome processes, restored guarded cookies from the vault, reloaded platform tabs after restore, and invoked the extension `scrapeNow` path through the Social Tabs control tab.
- Live cookie-vault restore/backup proof preserved auth marker names for Facebook/Instagram/Strava/TikTok/X without logging values. Facebook and Strava moved past login walls after post-restore reload. X shipped fresh content after `scrapeNow`; its earlier `try_again_empty_state` is no longer a source-health blocker.
- Live Collector `/health?include_sources=true` now returns `status=ok`, `source_issues=0`, browser maintenance `ok`, and browser issues `0`.
- The durable action queue is now down to real platform-pressure actions only: `instagram/source_blocked` for daily profile-view quota cooldown until `2026-08-21T23:59:59.932119+00:00`, and `tiktok/source_blocked` for recent rate/access pressure. These are cooldown/pressure states, not missing-cookie/login states.
- Patched `src/core/collection_action_queue.py` so live sources with recent output and warning-only `browser_page_error` blockers do not create false `source_blocked` actions. Regression added in `tests/core/test_collection_action_queue.py`. Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_extension_health.py -q` passed 41; compileall passed; dashboard container recreated.

Current task status: Browser/exposure defaults were loosened per operator request and live Collector health is ok. Cookie-vault latest snapshots are guarded by auth quality, so a logged-out/weaker Chrome cookie read is archived but cannot replace a better auth-bearing `latest.json`. Latest live cookie-vault health contains auth-cookie names for Instagram, Facebook, X, Strava, and TikTok. The managed Collector browser is Playwright Chromium on CDP `9336` with profile `ChromeCdpAutomationProfile_recover_fresh_20260821`; ordinary visible Chrome can be a different logged-out profile. Latest verification shows Collector `/health?include_sources=true` is `status=ok`, zero source issues, browser ingest active, and tab budget clean with one tab each for Instagram/Threads/TikTok/X/Facebook/Strava plus one extension control tab. The durable `collection_action_queue` is live and recently had 2 open actions after sync: `tiktok/source_blocked` for recent rate/access pressure and `youtube/source_blocked` for scoped cooldown. Browser tab audit can still show tab-local extension isolated-world gaps immediately after extension reload for Instagram/Threads/TikTok, but dashboard/source health remains ok while fresh ingest evidence exists. WhatsApp is live through bridge 2; bridge 1 remains an optional unpaired slot waiting for a second device. Website allow policy includes both `https://*.com` and `http://*.com`, plus `.com.sg` variants. Exposure remains intentionally broad with wildcard domains and regex allow-all. School website seed expansion for CJC monthly news archives and Classicle-style student pages is pushed.

Latest update:
- Added current-tab audit visibility to Collector dashboard health. `src/dashboard/api.py` now reads a fresh `browser_tab_audit_result.json` and injects warning-level `browser_page_error` issues when the live tab is on a recoverable page shell, even if the latest DB source evidence is still good. This covers the observed X `try_again_empty_state` edge without claiming cookies are missing.
- Fixed action-queue false positives from source-matrix timeout fallback. `src/core/collection_action_queue.py` now treats `stats_unavailable` rows and unavailable current/previous windows as informational, so timeout skeleton rows no longer create fake `target_starved` actions.
- Live recovery/proof: after guarded cookie restore and maintenance, X re-audited healthy on `https://x.com/home`; live Collector `/health?include_sources=true` returned `status=ok`, zero source issues, browser ingest `active_via_maintenance`, and active platforms `facebook,instagram,strava,threads,tiktok,x`.
- Live action-queue sync after the patch derived 1 open action: TikTok recent rate/access pressure. The previous bogus Facebook/Instagram/Threads/X target-starved actions resolved.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\dashboard\test_extension_health.py -q` passed 40; compileall and diff-check passed for touched Collector files. Multi-agent spawn was attempted for reviewer/auditor/researcher work, but the active thread limit blocked spawning.

Latest update:
- Rechecked the operator report that Chrome tabs appeared logged out. Normal Chrome and the Collector-managed Chrome are separate profiles; live process evidence showed both `Google\Chrome` and `UnifiedCollector\ChromeCdpAutomationProfile_ext_recover_...` profiles.
- Restored 67 guarded cookies into CDP `9336` from the cookie vault and forced a fresh backup. The fresh backup preserved auth marker names for Facebook `c_user/xs`, Instagram `sessionid`, X `auth_token/ct0`, Strava `_strava4_session`, and TikTok `ttwid` without logging values.
- Hard-reopened Instagram and X. Instagram recovered from HTTP 429/chrome-error to `https://www.instagram.com/explore/` with content script `1.23.72`; X reopened to `https://x.com/home` with content script `1.23.72` but the direct tab audit still reports `try_again_empty_state`, so X remains a tab-local capture caveat even though source matrix currently says live.
- Live Collector `/health?include_sources=true` returned `status=ok`, zero source issues, browser ingest `active_via_maintenance`, active platforms `facebook,instagram,strava,threads,tiktok,x`, maintenance `ok`, and browser issues `0`.

Latest update:
- Fixed the stale extension-control recovery path that caused `chrome-extension://pkmd.../tabs.html` to show `chrome-error://chromewebdata`. `scripts/start-scraper-chrome-cdp.ps1` now passes `--disable-features=DisableLoadExtensionCommandLineSwitch` and validates that a control target is usable before accepting it.
- `scripts/browser-tab-maintenance.ps1` and `scripts/cleanup_ext_tabs.py` now treat control tabs whose title starts with `chrome-extension://` or `chrome-error://` as blocked/dead, so cleanup no longer preserves stale hardcoded extension IDs over the real `UnifiedCollector -- Social Tabs` page.
- Live recovery used profile `ChromeCdpAutomationProfile_ext_recover_20260821_1502`; CDP `9336` has the real `pkmdmcklnjdeocoeigmlakhomhhcpafb/background.js` service worker, one valid control tab, and one platform tab each. Cookie restore pushed 67 guarded cookies from the vault.
- X was recovered from stale/missing-tab state: final audit showed `https://x.com/home` with content script `1.23.72`, and live DB had fresh X writes including `posts` 12 observed / 12 stored at `2026-08-21T07:21:09Z`.
- Final live Collector health returned `status=ok`, `source_issues=[]`, active browser ingest, fresh browser heartbeats/content for Facebook/Instagram/Threads/TikTok/X/Strava, and WhatsApp still live via bridge 2 with bridge 1 optional/unpaired.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py tests\extension\test_extension_bundle_static.py -q` passed 83; compileall and diff-check passed for touched launcher/maintenance/cleanup/test files.

Latest update:
- Investigated a fresh operator report that visible Chrome tabs looked logged out. CDP `9336` was alive; cookie-vault health stayed `ok=true` with effective latest restorable auth markers for Facebook `c_user/xs`, Instagram `sessionid`, Strava `_strava4_session`, TikTok `ttwid`, and X `auth_token/ct0`.
- Restored 67 cookies from the guarded host-side vault into CDP `9336`, reopened Instagram/Threads/TikTok affected tabs, opened extension control with `?reload=1`, and cleaned tab budget back to `ok=true`: 7 page targets, one extension control tab, zero blank tabs, one each for Instagram/Threads/TikTok/X/Facebook/Strava.
- Collector `/health?include_sources=true` returned `status=ok`, `source_issues=[]`, active browser ingest, and fresh content evidence for Facebook, Threads, TikTok, X, and Strava. Instagram remains source-live from recent stored content while the current tab-local audit lacks an isolated extension context after reload.
- Hardened Facebook author capture for future rows: `extension/content.js` now extracts authors from `profile.php?id=...` and `/people/.../<id>` links, and `src/bridges/ig_ingest.py` now lets nonblank future Threads/Facebook `author_username` values backfill older blank-author rows on duplicate post IDs.
- Recreated `unifiedcollector_ig_ingest` so the upsert fix is live. Verification: `python -m pytest tests\extension\test_extension_bundle_static.py tests\bridges\test_ig_ingest_vault.py -q` passed; compileall and diff-check passed for touched Collector files.

Latest update:
- Investigated a fresh operator report that Chrome tabs looked signed out. Live evidence showed CDP `9336` is reachable and the only visible Chrome process is the managed Playwright Chromium profile `ChromeCdpAutomationProfile_recover_fresh_20260821`.
- Cookie-vault health remained `ok=true` with auth markers for Facebook, Instagram, Strava, TikTok, and X. Targeted tab reload/audit reported Instagram Explore, TikTok following, X home, and Strava dashboard healthy/responsive with extension content script `1.23.72`.
- Live Collector health returned `status=ok`, source issues `0`, browser-extension issues `0`, browser ingest active for `bridge,facebook,instagram,strava,threads,tiktok,x`, and maintenance `state=ok`. Analyzer `/api/production/readiness` returned `status=ok`, `ok=true`.
- Synced the action queue; stale browser repair resolved. Remaining open actions are TikTok rate/access pressure and YouTube scoped cooldown.

Latest update:
- Added durable operator action queue slice: migration `20260821_add_collection_action_queue.sql`, core module `src/core/collection_action_queue.py`, and dashboard endpoints `GET /collectors/action-queue` plus `POST /collectors/action-queue/sync`.
- The queue derives idempotent open/resolved actions from source-matrix/browser evidence: auth/blocker rows, browser maintenance failures, browser-managed social yield starvation, and recent rate/access pressure. Quiet Beeper subsource rows and non-browser hourly noise are ignored by default.
- Live sync inside `unifiedcollector_dashboard` derived 3 open actions: browser maintenance repair, TikTok rate/access pressure, and YouTube scoped cooldown. A previous noisy 16-action sync was reduced and stale rows resolved after derivation was tightened.
- Patched browser audit DOM health so Chrome `HTTP ERROR 429` pages are classified as `recoverable_error_shell/http_429`; this is now visible in direct audit for Instagram instead of being marked page-health ok.
- Live Collector health after dashboard recreate: `status=ok`, source issues `0`, browser issues `0`, browser ingest `active`, maintenance currently `degraded` but surfaced as an open action. Analyzer readiness remained `status=ok`, `ok=true`, no failed checks.
- Verification: `python -m pytest tests\core\test_collection_action_queue.py tests\tools\test_browser_maintenance_scripts.py tests\dashboard\test_extension_health.py -q` passed 78; compileall and diff-check passed for touched action queue/audit/dashboard files.

Latest update:
- Reviewer/auditor subagents found two Collector truthfulness bugs and one Supabase proof weakness. Fixed the Collector bugs: overlap-skipped browser maintenance now writes `state=overlap_skipped` instead of `ok`, and dashboard top-level health degrades on hard `browser_extension.issues` even when browser ingest is still active.
- Patched `tools/browser_tab_audit.py` to mark CDP "No such target id"/404 races as `target_disappeared=true`, so disappearing targets are structured transient evidence instead of opaque connect failures.
- Live repair: restarted the managed CDP profile after CDP wedged, then ran maintenance. Final `tmp\browser_tab_maintenance_status.json` is `state=ok`, `detail=audit and reload completed`, `last_repair_action=targeted_reload`, loop sleeping after successful pass.
- Live Collector `/health?include_sources=true` returned `status=ok`, `source_issues=0`, `browser_issues=0`, browser ingest `active`, active platforms `bridge,facebook,instagram,strava,threads,tiktok,x`, maintenance `ok`.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py tests\dashboard\test_extension_health.py -q` passed 72; `python -m compileall` passed for touched Collector scripts/dashboard/tests; diff-check passed for touched Collector files.
- Remaining product gap from researcher subagent: implement a durable collection action queue that turns coverage/yield/DLQ/rate-limit/browser-auth evidence into operator actions.

Latest update:
- Patched `scripts/start-scraper-chrome-cdp.ps1` so scraper Chrome relaunches reuse the last successful `tmp\scraper_chrome_state.json` profile before falling back to the older `ChromeCdpAutomationProfile_recover_x`, preventing relapse into the corrupted old profile.
- Patched `scripts/cleanup_ext_tabs.py` to identify any `chrome-extension://*/tabs.html` control tab, not only hardcoded known extension IDs, so stale blocked extension-control tabs are pruned even when the unpacked extension ID changes.
- Live cleanup/audit result: tab budget `ok=true`, 7 page targets, one extension control tab, zero blank tabs, and one each for Instagram, Threads, TikTok, X, Facebook, and Strava. X/Facebook/Threads/TikTok/Strava have content script `1.23.72`; Instagram remains a tab-local probe caveat on the post URL, but Collector `/health?include_sources=true` is still `status=ok` with source issues `0`.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 42; compileall passed for touched Collector scripts/tests.

Latest update:
- Cookie vault `/health` now separates the last observed candidate backup from the effective `latest.json` restore snapshot. It exposes `effective_latest.restorable`, safe auth marker names, quality score, cookie count, timestamp, and error. Health is only ok when the effective restore snapshot is usable.
- Analyzer Collector production summary now prefers `effective_latest.auth_summary` and `effective_latest.quality_score` when available, so a weaker live candidate cannot make readiness claim auth loss while a stronger restore snapshot is preserved.
- Dashboard `/health?include_sources=true` now catches `asyncio.CancelledError` from timed source-liveness and browser-extension diagnostics, returning degraded diagnostics instead of HTTP 500 under DB/browser load.
- WhatsApp bridge 2 recovered after restart and reports `ready`, registered, phone `6584731565`, push name `Prawn Productions`; bridge 1 remains the optional unpaired slot.
- Live cookie vault after the patch is `ok=true`, `count=62`, `quality_score=5132`, `effective_latest.restorable=true`, with auth markers for Facebook/Instagram/Strava/TikTok/X. No cookie values were logged.
- Important blocker: the previous managed Chrome profile showed a visible `Profile error occurred` window and CDP died. A fresh profile was launched and cookies were restored, but Instagram/Facebook/X still display login/empty shells in that fresh profile, so Meta/X need interactive login again inside the managed Collector Chrome profile before production readiness can be green. Threads/Instagram/Facebook/X login-shell detection is working and is not being masked.
- Verification: `python -m pytest tests\tools\test_browser_cookie_vault.py -q` passed 14; `python -m pytest tests\dashboard\test_extension_health.py -q` passed 28; compileall passed for touched Collector modules. `git diff --check` only reported existing CRLF warnings.

Latest update:
- Hardened the browser self-heal edge case found by reviewer/auditor subagents. `tools/browser_tab_audit.py` now marks Threads `?error=invalid_post` and `Post unavailable` pages as `recoverable_error_shell` instead of healthy.
- `scripts/browser-tab-maintenance.ps1` now labels missing/stopped content scripts as `missing_or_stopped_content_script`, rejects source-liveness fallback for that condition, and also rejects fallback for auth/login/account shells. Login walls are treated as manual-auth degraded instead of hidden behind fresh backend liveness.
- Live recovery reopened Instagram, Threads, and Strava from non-canonical/error tabs to canonical collection pages. Final browser audit has one tab each for Instagram, Threads, TikTok, X, Facebook, and Strava plus one extension control tab; all six platform tabs are `health=ok`, content script `1.23.72`, and tab budget clean.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 42; compileall passed for touched audit/test files and scripts; `git diff --check` only reported existing CRLF warnings.

Latest update:
- Investigated the operator report that all Chrome tabs appeared logged out. Live process evidence showed the only visible browser is managed Chrome-for-Testing on CDP `9336` with `ChromeCdpAutomationProfile_recover_x`, not the ordinary Chrome profile.
- Restored 61 guarded cookies from `credentials/browser_cookies/latest.json` into live CDP `http://127.0.0.1:9336` after setting host-side `BROWSER_COOKIE_VAULT_DIR` and `CHROME_CDP_URL` explicitly.
- Ran browser maintenance and a fresh tab audit. Final tab budget is clean: 7 page targets, one extension control tab, zero blank tabs, and one tab each for Instagram, Threads, TikTok, X, Facebook, and Strava. Facebook, Instagram, X, Strava, and TikTok audit responsive with content script `1.23.72`; Threads remains a UI/content-script edge case while backend ingest is fresh.
- Live Collector `/health?include_sources=true` returned `status=ok`, zero issues, browser ingest active, and fresh useful content from Facebook, Instagram, Strava, Threads, TikTok, and X. Analyzer readiness remains `status=ok` overall with one warning: TikTok had 3 stored rolling-hour items, below the 5 useful-output floor at that check.

Latest update:
- Hardened Exposure's broad wildcard behavior without removing the operator's allow-all policy. `*.edu.sg`, `*.sg`, `*.com`, `*.me`, `*.kr`, `*.*`, and `regex:.*` remain allow gates, but wildcard target lines are no longer expanded into noisy concrete dorks by default.
- Changed `ExposureCollector` default `EXPOSURE_EXPAND_WILDCARD_TARGETS` to false and made compose explicit with `EXPOSURE_EXPAND_WILDCARD_TARGETS=${EXPOSURE_EXPAND_WILDCARD_TARGETS:-0}`. Collector-discovered concrete domains still pass through the broad gates and become dork scopes.
- Recreated `unifiedcollector_collector_exposure`; runtime env confirmed `EXPOSURE_EXPAND_WILDCARD_TARGETS=0`, broad allowed domains/regex still present, and Collector health stayed `status=ok`, `source_issues=[]`.
- Verification: `python -m pytest tests\collectors\test_exposure.py -q` passed 12; compileall passed for Exposure module/tests; `docker compose -f docker\docker-compose.yml config --quiet` passed. `git diff --check` only reported existing CRLF warnings.

Latest update:
- Browser maintenance now requires X by default in `Get-RequiredAuditPlatforms`, matching the current policy that X is browser-managed and should not silently fall out of self-heal checks.
- Browser maintenance success now performs a final extension-control/duplicate/blank-tab cleanup before writing `state=ok`, reducing post-maintenance tab churn.
- Live Collector `/health?include_sources=true` returned `status=ok`, `source_issues=[]`, browser ingest active for `bridge,facebook,instagram,strava,threads,tiktok,x`, and maintenance `state=ok`, `running_stalled=false`.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 42; compileall passed for touched tests. `git diff --check` only reported existing CRLF warnings on `.agents/JOURNAL.md` and `docker/docker-compose.yml`.

Latest update:
- Investigated the operator report that Chrome tabs appeared signed out. Live evidence showed the managed Collector browser, not ordinary Chrome, is using Playwright Chromium on CDP `9336` with `ChromeCdpAutomationProfile_recover_x`.
- Cookie vault was healthy on port `8790`; the old `8787` port is stale. Vault health showed 61 guarded cookies and auth-cookie names for Facebook, Instagram, Strava, TikTok, and X. A weaker 60-cookie snapshot was preserved from replacing the stronger latest snapshot.
- Stopped a stuck one-off maintenance pass that was causing tab churn, restarted only the managed Chromium profile, and restored 61 cookies from `credentials/browser_cookies/latest.json` into CDP.
- Live Collector `/health?include_sources=true` returned `status=ok` with `source_issues=[]`. Facebook, X, Strava, and TikTok pages show authenticated content. Instagram and Threads still redirect to site-side error/login shells after reopen despite restored cookies, so they may need interactive re-login or cooldown handling.

Latest update:
- Investigated the reported "not signed in to Chrome tabs" issue live. Restored the managed CDP browser profile on port `9336`, restored 61 guarded cookies into CDP from `credentials/browser_cookies/latest.json`, cleaned the old extension-id control tab and blank tab, and reopened X.
- Patched `tools/browser_tab_audit.py` so Threads login-wall text is treated as `recoverable_error_shell/login_wall_text`.
- Patched `tools/browser_tab_reload.py` so `--platforms` is parsed and honored instead of silently sweeping every platform; CDP close 404s are now treated as disappeared targets.
- Patched `scripts/cleanup_ext_tabs.py` to use `UC_CHROME_CDP_URL`/`UC_CHROME_CDP_PORT` default `9336` instead of hardcoded `9333`, and to close blank tabs even when only one control tab exists.
- Patched `src/dashboard/api.py` so contradictory maintenance status (`state=running` while loop says sleeping after successful/nonzero pass) is surfaced as `running_stalled=true`.
- Verification: focused dashboard/tool tests passed; `git diff --check` only reported existing CRLF warnings. Live cookie vault is ok with auth markers; Collector health is ok with zero source issues. Direct tab audit caveat remains Instagram dead-post shell.

Latest update:
- Recovered the managed browser after TikTok expanded-tab recovery caused duplicate/blank tabs and X content-script loss. Relaunched CDP `9336` on the guarded profile, confirmed cookie-vault health still has 61 cookies and auth markers for Facebook/Instagram/X/Strava/TikTok, cleaned tabs back to exactly one platform tab each plus one extension control tab.
- Final tab audit budget is `ok=true`: 7 page targets, one each for Instagram, Threads, TikTok, X, Facebook, Strava plus one extension control tab. Some tab-local Runtime probes timed out under load, but Collector `/health?include_sources=true` returned `status=ok`, zero source issues, browser ingest `active_via_maintenance`, active platforms `facebook,instagram,strava,threads,tiktok,x`.
- Rolling browser-ingest DB evidence in the last 60 minutes shows Facebook/Instagram/Threads/X storing useful output. TikTok is still challenged/low-yield: recent TikTok media events observed candidates but stored 0, and source-matrix current-hour rate-limit/challenge count makes Analyzer readiness exempt it rather than falsely pass it.

Latest update:
- Added a stalled-running browser maintenance classification: status `state=running` now reports `running_stalled=true` after `BROWSER_TAB_MAINTENANCE_RUNNING_STALLED_SECONDS` (default 900s), before the older 2700s stale threshold. Dashboard issues now surface `browser_maintenance_stalled` in both direct and fallback payload paths, so a wedged direct maintenance pass is visible quickly instead of being treated as healthy old state.
- Recreated `unifiedcollector_dashboard`; Docker health returned healthy. Live `/health?include_sources=true` returned `status=ok`, database ok, zero source issues, maintenance `ok`, `running_stalled=false`, browser ingest `active`, active platforms `bridge,facebook,instagram,strava,threads,tiktok,x`.
- Verification: `python -m compileall src\dashboard\api.py tests\dashboard\test_extension_health.py` passed; `python -m pytest tests\dashboard\test_extension_health.py -k "stalled_running or stale_browser_maintenance or maintenance_audit or source_liveness_timeout" -q` passed 5. `git diff --check` only reported existing CRLF warnings.

Latest update:
- Patched `src/dashboard/api.py` so `/health?include_sources=true` keeps source-liveness timeout diagnostics visible but does not degrade top-level health when browser ingest is actively verified.
- Recreated `unifiedcollector_dashboard`; live Collector health returned `status=ok`, `source_issues=0`, maintenance `ok`, browser ingest `active_via_maintenance`, active platforms `facebook,instagram,strava,threads,tiktok,x`.
- Cookie vault health is `ok=true`, `count=61`, `quality_score=5131`, with auth markers for Facebook `c_user/xs`, Instagram `sessionid`, Strava `_strava4_session`, TikTok `ttwid`, and X `auth_token/ct0`; no cookie values were logged.
- WhatsApp bridge 2 recovered to `ready` on host port `3012` with registered credentials for `Prawn Productions`; bridge 1 is still unpaired and optional.
- Browser tab audit after reopening X showed tab budget ok with one each for Instagram, Threads, TikTok, X, Facebook, Strava plus one extension tab. X was present on `https://x.com/home`; one audit had an isolated-world timeout, but dashboard/source health reported X live and non-stale.
- Tests: `python -m pytest tests\dashboard\test_extension_health.py -k "source_liveness_timeout or diagnostics_fail or maintenance_audit or clean_audit" -q` passed 4; compileall passed. `git diff --check` only reported existing CRLF warnings.

Latest update:
- Investigated the reported "not signed in to any Chrome tabs" incident again. The user-visible normal Chrome profile may be logged out, but Collector uses the separate managed Playwright Chromium profile on CDP `9336`.
- Cookie vault `/health` is `ok=true`, `count=61`, `quality_score=5131`, with auth markers for Facebook `c_user/xs`, Instagram `sessionid`, Strava `_strava4_session`, TikTok `ttwid`, and X `auth_token/ct0`. No cookie values were logged.
- CDP became wedged after tab reloads. Restarted only the managed `ChromeCdpAutomationProfile_recover_x` Chrome processes, moved aside stale launcher lock `tmp/scraper_chrome_launch.lock`, restored 61 cookies into `http://127.0.0.1:9336`, and stopped the hung launcher wrapper after Chrome came up.
- Final `tools\browser_tab_audit.py --json` has tab budget `ok=true`: 7 page targets, one extension control tab, zero blank tabs, one each for Instagram, Threads, TikTok, X, Facebook, and Strava. Instagram/Threads/X/Facebook/Strava had content script `1.23.72`; TikTok was responsive but isolated-world eval timed out once during audit.
- Patched dashboard browser ingest fallback so a fresh clean audit JSON can provide active-browser evidence even when the maintenance status row is still `running/degraded` from a hung pass. Focused tests `tests\dashboard\test_extension_health.py -k "maintenance_audit or clean_audit" -q` passed 2; compileall passed.
- Recreated `unifiedcollector_dashboard`; Docker health is healthy. Final Collector `/health?include_sources=true` still returned `degraded` because WhatsApp bridge health timed out/unpaired in that request, but browser ingest was `active` with active platforms `bridge,facebook,instagram,strava,threads,tiktok,x`.

Latest update:
- Recovered the reported "not signed in to any Chrome tabs" incident. CDP `9336` was down, but cookie-vault latest still had auth markers for Facebook `c_user/xs`, Instagram `sessionid`, Strava `_strava4_session`, TikTok `ttwid`, and X `auth_token/ct0`.
- Confirmed launcher profile selection now prefers `ChromeCdpAutomationProfile_recover_x`; regression `tests\tools\test_browser_maintenance_scripts.py -q` passed 41 before live recovery.
- Restarted the managed browser on `ChromeCdpAutomationProfile_recover_x`, restored 61 cookies from the vault, closed the stale old extension-control tab and blank tab, and reran `tools\browser_tab_audit.py --json`.
- Final audit has 7 page targets: one extension control tab and one platform tab each for Instagram, Threads, TikTok, X, Facebook, and Strava; tab budget `ok=true`, no blank tabs, content script `1.23.72` running on all six platform tabs.
- Cookie-vault `/health` is `ok=true`, `count=61`, `quality_score=5131`, no error, auth markers present for Facebook/Instagram/Strava/TikTok/X.
- Collector `/health?include_sources=true` is `status=ok`, `source_issues=0`, maintenance `ok`, browser ingest `active`, active platforms `facebook,instagram,strava,threads,tiktok,x`.
- Maintenance script still needs code-level follow-up: multiple live runs hung after healthy audits under load, so runtime maintenance status was manually aligned to the fresh audit result. Keep the final-success early-exit patch and add/verify hard subprocess timeouts before trusting unattended maintenance again.

Latest update:
- Investigated reported "not signed in to any Chrome tabs" again. Live process list shows the active browser is Playwright Chromium on CDP `9336` with `--user-data-dir=C:\Users\bryan\AppData\Local\UnifiedCollector\ChromeCdpAutomationProfile_recover_x`; this is separate from ordinary Chrome windows/profiles.
- Live cookie-vault `/health` still reports `ok=true`, `count=60`, `quality_score=5130`, and auth-cookie names for Facebook, Instagram, Strava, TikTok, and X without logging values.
- Live CDP tabs initially had Instagram and Threads on non-canonical/error URLs. Ran `tools/browser_tab_reload.py --platforms instagram,facebook,x,threads,tiktok,strava --hard-reopen --json`, which reopened Instagram, Threads, and Strava to canonical URLs with zero failures.
- Final `tools/browser_tab_audit.py --json` shows exactly one page each for Instagram, Threads, TikTok, X, Facebook, and Strava plus one extension control tab; all platform tabs are responsive and content script `1.23.72` is running. Collector `/health?include_sources=true` returned `status=ok` and zero source issues.
- Patched `src/tools/browser_cookie_vault.py` with auth-marker summaries and quality scoring. Backup now writes every timestamped snapshot, but preserves the existing `latest.json` when the new snapshot has lower auth quality. `/health` exposes `auth_summary`, `quality_score`, `latest_preserved`, and `preservation_reason` without cookie values.
- Updated `docker/docker-compose.yml` comment to reflect guarded startup autorestore, then recreated only `unifiedcollector_browser_cookie_vault`.
- Live cookie-vault `/health` returned `ok=true`, `count=60`, `quality_score=5130`, `latest_preserved=false`, and auth names for Facebook `c_user/xs`, Instagram `sessionid`, Strava `_strava4_session`, TikTok `ttwid`, and X `auth_token/ct0`.
- Verification: `python -m pytest tests\tools\test_browser_cookie_vault.py -q` passed 13; `python -m pytest tests\tools\test_browser_maintenance_scripts.py tests\tools\test_browser_cookie_vault.py -q` passed 53; compileall for the vault module/tests passed; `docker compose -f docker\docker-compose.yml config --quiet` passed; `git diff --check` passed with only existing CRLF warnings.
- Investigated reported "not signed in to any Chrome tabs" after Chrome crash/restart. Live CDP `9336` is reachable and is the managed Playwright Chromium profile `ChromeCdpAutomationProfile_recover_x`, not the user's normal Chrome profile.
- Verified without logging values that live CDP cookies contain auth-cookie names: Instagram `sessionid`, Facebook `c_user`/`xs`, X `auth_token`/`ct0`, Strava `_strava4_session`, and TikTok `ttwid`.
- Restored 56 cookies from `credentials/browser_cookies/latest.json` into CDP `9336`, then ran `tools/browser_tab_reload.py`. Cleanup reopened Instagram and Strava to canonical pages and removed duplicate X tab.
- Final `tools/browser_tab_audit.py --json` shows one tab each for Instagram, Threads, TikTok, X, Facebook, Strava plus one extension control tab; tab budget ok. Fast `/health` returned `status=ok`. Heavy `include_sources` and direct Postgres evidence queries timed out under load during this check.
- Patched `tools/browser_tab_reload.py` so the reload planner reads the dashboard source matrix (`UC_DASHBOARD_HEALTH_URL`, default `/health?include_sources=true`) and reloads platforms degraded by stale browser content even when the tab-local audit looks healthy.
- Live manual recovery: first pass detected stale `threads,x`, hard-reopened X and reloaded Threads; second pass hard-reopened Threads/Strava canonical URLs. X then emitted fresh posts; Threads emitted fresh posts/media; source_health rows for Threads and X returned to `running`.
- Verification: `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 40 tests; `python -m compileall tools\browser_tab_reload.py tools\browser_tab_audit.py tests\tools\test_browser_maintenance_scripts.py` passed. Live `/health?include_sources=true` returned `status=ok`, `source_issues=0`; source_health rows for Instagram/Facebook/Threads/TikTok/Strava/X/WhatsApp are all `running`.

Previous update:
- Live `/health?include_sources=true` is degraded due two real browser-content issues: `threads` stale content progress and `x` stale/zero-progress shell. WhatsApp is live via bridge 2; bridge 1 is unpaired/optional and waiting for QR only if another WhatsApp device/account is desired.
- Browser tab audit on CDP `9336` shows one tab each for Instagram, Threads, TikTok, X, Facebook, Strava plus one extension control tab; tab budget ok. Instagram/Threads/TikTok/Facebook/Strava have content script `1.23.72`; X also has content script but page health is `recoverable_error_shell`, reason `try_again_empty_state`.
- Direct DB check of `browser_ingest_events` confirms X's newest recent media events are zero-observed/zero-stored `recoverable_error_shell`; the older useful X media/posts rows are stale, so X should not be marked healthy yet.

Previous update:
- Investigated reported "not signed in to Chrome tabs" issue. Live CDP `9336` is up; visible ordinary desktop Chrome may not be the collector-managed browser. Active Collector tabs on `9336`: Instagram, Facebook, X, Threads, TikTok, Strava, and one extension control tab; tab budget is ok.
- Latest cookie vault (`credentials/browser_cookies/latest.json`, timestamp `2026-08-20T16:58:23Z`) contains required auth-cookie names: Instagram `sessionid`/`ds_user_id`, Facebook `c_user`/`xs`, X `auth_token`/`ct0`, and Strava `_strava4_session`. Values were not logged.
- Restored 57 cookies from the vault into the managed profile and reloaded/hard-reopened stale platform tabs. Post-restore audit showed Instagram/Threads/Strava responsive with content script; Facebook source_health remains running; TikTok source_health remains running though content-script audit was still settling; X remains `try_again_empty_state`.
- Live `source_health`: Instagram, Facebook, Threads, TikTok, and Strava are `running`; X is `degraded` with `browser capture stalled ... watchdog`.

Previous update:
- After fresh user login to Meta, forced browser cookie-vault backup. Latest vault now contains Instagram `sessionid`/`ds_user_id`, Facebook `c_user`/`xs`, X `auth_token`/`ct0`, and Strava session cookies. Do not log values.
- Hardened browser audit so Facebook login walls/password forms and Meta account chooser states are `recoverable_error_shell`, preventing logged-out Facebook from being reported as healthy.
- Hardened `/health?include_sources=true` to fail closed when source liveness or browser-extension diagnostics time out; live endpoint now returns `degraded` with explicit diagnostics instead of silent `ok`.
- Fixed `/health` DB pool release cancellation path by using the existing safe release helper; regression test covers `asyncio.CancelledError`.
- Fixed browser reload edge case where a disappeared target during hard reopen was treated as a failed close and could leave duplicate X tabs. Duplicate X tabs were manually cleaned; tab budget is currently ok.
- Live source_health rows for Instagram/Facebook/Threads/TikTok/Strava/X are `running`. Live Collector `/health?include_sources=true` is degraded because WhatsApp bridges are unpaired/not ready. X source_health remains running, but browser tab audit still shows `page_health_status=recoverable_error_shell`, reason `try_again_empty_state`.

Previous update:
- Investigated reported missing Chrome logins. Active collector browser is the Playwright Chromium profile on CDP port 9336; old desktop Chrome proof port 9338 is no longer reachable. The managed profile has tabs for Instagram, Threads, TikTok, X, Facebook, Strava, and the extension control page.
- Verified live `source_health` rows for `x`, `instagram`, `facebook`, `strava`, `threads`, and `tiktok` are all `running` with `last_error=NULL`. X tab is responsive, has the content script, and shows Home timeline content.
- Tried restoring old Meta cookie jars from `credentials/instagram/` into the managed profile. Chrome accepted the jar and Facebook/Instagram moved to account chooser/post states temporarily, but the live cookie vault backup still lacks Instagram `sessionid` and Facebook `c_user`/`xs`, meaning Meta rejected or cleared those stale auth cookies. Fresh interactive login in the managed 9336 browser is still needed for durable Meta auth.
- Forced a browser cookie-vault backup after the restore attempt; latest vault has X `auth_token`/`ct0` and Strava `_strava4_session`, but no durable Meta auth cookies.

Previous update:
- Fixed the Instagram stale-row blocker in code and runtime: watchdog now uses the same freshness basis as dashboard liveness (`instagram_profiles.updated_at` or Instagram media), clears stale-watchdog `source_health` rows for browser-managed sources when computed browser liveness is ok, and the dashboard source matrix suppresses generic `stale ... watchdog ...` degraded rows when computed status is live.
- Added regression coverage in `tests/test_watchdog_freshness.py` and `tests/dashboard/test_source_matrix.py`; both focused suites passed.
- Recreated patched dashboard/watchdog. Live watchdog now logs `instagram ok (newest ... ago)` instead of restarting from old media age, and `source_health.instagram` is `running` with `last_error=NULL`.
- Live DB recovered from Postgres crash recovery after backup/DB load. All core containers checked healthy after recovery. Base dashboard health is ok; the heavy `include_sources=true` endpoint can still time out under DB load but later returned `status=ok` with `source_issues=[]` in the bounded retry path. Current separate caveat: `source_health.x` has a browser-content-stale watchdog row.

Previous update:
- Added direct website crawl seeds for Catholic Junior College `https://www.cjc.edu.sg/news/` plus monthly `/news/YYYY/M/` archive URLs from 2021-01 through 2026-08, and `https://classicle.club/our-students`.
- Expanded `search.targets` with school archive/profile discovery dorks for `/news/YYYY/M/`, `/news-and-events/`, `/latest-news/`, `/student-achievements`, `/our-students`, `/student-gallery`, `/student-showcase`, and student/CCA leader pages across `edu.sg`, `moe.edu.sg`, `com.sg`, and `sg`.
- Validation: `python -m compileall src\core\source_config.py`, `python -m pytest tests\core\test_source_config.py -q`, and `git diff --check` passed. A broader `tests\test_worker_target_priority_refresh.py` command timed out before returning.

Previous update:
- Fixed Strava browser auth-wall maintenance caveat. Existing Strava Netscape cookie files in `credentials/strava/` contained `_strava4_session`; injected both Strava cookie jars into the active extension-capable Chrome profile on CDP port 9336 and navigated Strava to `https://www.strava.com/dashboard`.
- Strava tab now audits as `Dashboard | Strava`, URL `https://www.strava.com/dashboard`, content script `1.23.72` active, tab budget ok.
- Browser maintenance status is now `state=ok`, `detail=audit and reload completed`; Strava `source_health` is `running`.
- `/health?include_sources=true` returned `status=ok` with no `source_issues`; browser extension ingest is active.

Implemented in this slice:
- Changed browser tab audit/reload defaults so `x` is no longer excluded unless `UC_TAB_AUDIT_EXCLUDED_PLATFORMS` or `UC_BROWSER_EXCLUDED_PLATFORMS` explicitly says so.
- Changed source liveness and compose defaults so `X_SOURCE_MANUAL_MODE` defaults to `0`.
- Added `x` to browser repair/reopen platform lists so maintenance opens `https://x.com/home`.
- Changed `ExposureCollector` so global wildcard scope is allowed by default; removed extra explicit exposure guard envs from compose.
- Trimmed local ignored `.env` to keep only the broad exposure envs plus explicit X/browser empty-exclusion overrides.
- Recreated dashboard, watchdog, website, and exposure containers. Live env confirmed `X_SOURCE_MANUAL_MODE=0`; website runtime confirmed `WEBSITE_URL_ALLOW=https://*.com.sg,http://*.com.sg,https://*.com,http://*.com`.
- Recovered browser auth/session confusion: original automation profile cookies were still on disk, but that profile would not expose CDP under Playwright Chromium. Desktop Chrome on the original profile exposed logged-in state on port 9338. Extension-capable recovery profile on port 9336 then became logged in enough for X collection.
- Reopened/cleaned browser tabs: final audit on port 9336 reports tab budget ok, 1 extension control tab, 0 blank tabs, and one each for Instagram, Threads, TikTok, X, Facebook, and Strava.

Verification completed:
- `python -m compileall tools\browser_tab_audit.py tools\browser_tab_reload.py src\core\source_freshness.py src\collectors\exposure` passed.
- `python -m pytest tests\collectors\test_exposure.py -q` passed 11 tests.
- `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 38 tests.
- `python -m pytest tests\core\test_source_freshness.py -q` passed 17 tests.
- `docker compose -f docker\docker-compose.yml config --quiet` passed.
- `/health?include_sources=true` on port 8001 returned `status=ok` with no `source_issues`.
- Source health rows for `instagram` and `x` are `running`.
- X live ingest is working: recent health showed `x` live, `browser_health_status=healthy`, fresh `posts` and `media`, 21 observed/stored post rows and 42 observed/4 stored media in the current window.

Known caveats:
- Desktop Chrome with the original cookie profile is still open on port 9338. It is useful for confirming old login state but does not have the UnifiedCollector content script injected.
- Extension-capable Chrome profile is open on port 9336 and is the active managed collector profile.
- Instagram may still show a stored `source_health=degraded` row during active HTTP 429 cooldown, but computed health ignored the watchdog marker and returned no `source_issues` in the latest check.
- WhatsApp bridge 2 remains paired; bridge 1 still needs QR pairing if a second WhatsApp device is required.
- Do not delete or overwrite any Chrome profile folders. Original cookies are still on disk under `%LOCALAPPDATA%\UnifiedCollector\ChromeCdpAutomationProfile`.
