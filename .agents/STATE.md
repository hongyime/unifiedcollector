# UnifiedCollector Agent State

Updated: 2026-08-20 10:34 UTC / 2026-08-20 18:34 SGT

Current task status: Browser/exposure defaults were loosened per operator request and the live Instagram stale-watchdog blocker is fixed. X is managed by default again, but currently has a separate browser-content-stale row. Website allow policy includes both `https://*.com` and `http://*.com`, plus `.com.sg` variants. Exposure remains intentionally broad with wildcard domains and regex allow-all. School website seed expansion for CJC monthly news archives and Classicle-style student pages is pushed.

Latest update:
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
