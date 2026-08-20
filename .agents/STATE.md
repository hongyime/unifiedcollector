# UnifiedCollector Agent State

Updated: 2026-08-20 05:31 UTC / 2026-08-20 13:31 SGT

Current task status: Search target expansion, browser duplicate-tab cleanup, and the new gated `exposure` collector lane are implemented locally. Low-risk collector remains live on normal search/GitHub/Strava; `exposure` is a separate Docker profile and is disabled unless explicitly enabled with allowed scope gates.

Implemented in this slice:
- Added host-side duplicate extension-control-tab cleanup to `tools/browser_tab_reload.py`, including stale historical extension ids.
- Expanded `config/sources/search.targets` with public faculty/staff profile dorks, safe public document/media dorks, CJC year/month archive seeds for 2021-2026 through 2026-08, `https://www.vcebhopal.ac.in/a/-/-/`, and Classicle-style student gallery discovery terms.
- Added `exposure` as a separate defensive audit source for credential, database dump, exposed `.git`, key, token, backup, config, open-directory, and service dorks. These are not in normal `search.targets`.
- Generated `config/sources/exposure.dorks` from upstream `spekulatius/infosec-dorks` with 322 active `[TARGET]` templates.
- Added `config/sources/exposure.targets` for concrete authorized scopes plus `regex:<pattern>` gate-only lines; env gates also support `EXPOSURE_ALLOWED_DOMAINS` and `EXPOSURE_ALLOWED_REGEX`.
- Added `exposure_findings` schema for redacted findings and registered `ExposureCollector`.
- Added `collector_exposure` Docker service under the `exposure` compose profile, default disabled with `EXPOSURE_ENABLED=0`.
- Recreated `collector_lowrisk`; it loaded 505 search targets and is crawling 72 direct seed URLs.
- Verified GitHub is not empty/blocked: live DB has 1,637,160 pending `github_spider_queue` rows and recent low-risk logs show `Collecting github/...` and spider-neighbor work.
- Cleaned 128 duplicate primary extension control tabs, later closed duplicate stale `nke...` control tabs through the reload helper.
- The original scraper Chrome profile stopped loading content scripts on non-Strava tabs and then stopped exposing CDP during restart. A fresh profile at `ChromeCdpAutomationProfile_test` is currently serving CDP on port 9336 with clean tab budget and content script `1.23.72` active on managed tabs.

Verification completed:
- `python -m compileall tools\browser_tab_reload.py` passed.
- `python -m pytest tests\tools\test_browser_maintenance_scripts.py -q` passed 38 tests.
- `python -m pytest tests\collectors\test_exposure.py tests\core\test_source_config.py -q` passed 9 tests.
- `python -m compileall src\collectors\exposure src\collectors\__init__.py src\core\source_config.py` passed.
- `docker compose -f docker\docker-compose.yml config --quiet` passed.
- `python -m src.main list` shows `exposure`; `config/sources/exposure.dorks` has 322 active dork templates.
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
- `exposure` should only be run for owned/authorized domains. It redacts obvious secret assignments and does not download evidence by default (`EXPOSURE_FETCH_EVIDENCE=0`, `EXPOSURE_SPIDER_PAGES=0`).
- To enable ad hoc: set `EXPOSURE_ENABLED=1`, configure `EXPOSURE_ALLOWED_DOMAINS` / `EXPOSURE_ALLOWED_REGEX`, add concrete domains to `config/sources/exposure.targets`, then start with `docker compose -f docker\docker-compose.yml --profile exposure up -d collector_exposure`.
