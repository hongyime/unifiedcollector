# UnifiedCollector Agent State

Updated: 2026-08-20 04:08 UTC / 2026-08-20 12:08 SGT

Current task status: Search target expansion and browser duplicate-tab cleanup are implemented and live-verified. Low-risk collector was recreated and is actively running the expanded search/GitHub/Strava workload.

Implemented in this slice:
- Added host-side duplicate extension-control-tab cleanup to `tools/browser_tab_reload.py`, including stale historical extension ids.
- Expanded `config/sources/search.targets` with public faculty/staff profile dorks, safe public document/media dorks, CJC year/month archive seeds for 2021-2026 through 2026-08, `https://www.vcebhopal.ac.in/a/-/-/`, and Classicle-style student gallery discovery terms.
- Deliberately did not add credential, database dump, exposed `.git`, key, token, or exploit dorks to the normal collector query list.
- Recreated `collector_lowrisk`; it loaded 505 search targets and is crawling 72 direct seed URLs.
- Verified GitHub is not empty/blocked: live DB has 1,637,160 pending `github_spider_queue` rows and recent low-risk logs show `Collecting github/...` and spider-neighbor work.
- Cleaned 128 duplicate primary extension control tabs, later closed duplicate stale `nke...` control tabs through the reload helper.
- The original scraper Chrome profile stopped loading content scripts on non-Strava tabs and then stopped exposing CDP during restart. A fresh profile at `ChromeCdpAutomationProfile_test` is currently serving CDP on port 9336 with clean tab budget and content script `1.23.72` active on managed tabs.

Verification completed:
- `python -m compileall tools\browser_tab_reload.py` passed.
- `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 38 tests.
- `python tools\browser_tab_audit.py --cdp http://127.0.0.1:9336 --json` on the fresh profile reports 6 page targets, exactly 1 extension control tab, no blank tabs, one each for Instagram/Threads/TikTok/Facebook/Strava, X missing, and tab budget `ok=true`.
- Content script `1.23.72` is active on the fresh-profile managed tabs in the latest audit.
- Collector Docker services are up/healthy where healthchecks exist; `collector_lowrisk` is healthy after recreate.
- Collector `/health` is ok. Browser cookie vault `/health` is ok with recent backup and zero consecutive failures.
- Analyzer `/api/health` is ok and `/api/indicators/export/supabase/status` reports `postgres_direct`, compact normalized rows only, and `ready_to_export=651`.

Known caveats:
- Fresh scraper Chrome profile is stable but may not carry all previous logged-in sessions from the old profile. Re-pair/re-login through the managed tabs if platform auth is missing.
- Old scraper Chrome profile likely has extension/CDP corruption. Do not switch back without first repairing or replacing its extension state.
- WhatsApp bridge 2 remains paired. Bridge 1 still needs QR pairing; `/qr` currently reports `needs_scan=true` but `qr_available=false` after prior QR attempts expired.
- `WEBSITE_URL_ALLOW` already includes `https://*.com` and `http://*.com` in runtime env and the text policy.
- Normal collector search list intentionally excludes credential-leak/exploit dorks; build a separate authorized defensive audit lane if those are required.
