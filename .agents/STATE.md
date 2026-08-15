# UnifiedCollector Agent State

Updated: 2026-08-15 18:45 UTC / 2026-08-16 02:45 SGT

Current task status: website policy, SpiderFoot daily500, realtime public-media dedupe, Pi-hole nebula-sync, Docker healthchecks, and the scraper browser/Threads recovery are implemented and live-verified. The active scraper browser is Playwright Chromium on `127.0.0.1:9336`; stale duplicate recovery windows were closed, leaving one CDP listener.

Implemented in this slice:
- Website URL policy now allows `https://*.com.sg`, `http://*.com.sg`, `https://*.com`, and `http://*.com`, with anchored `allow_regex:` equivalents in `config/sources/website.url-policy.txt`.
- SpiderFoot optional rollout supports `daily250` and `daily500`, with CLI/API limit caps raised to 1000.
- Applied SpiderFoot `daily500` live earlier in this slice; recon queue now shows completed 57, failed 22, in_progress 4, pending 441.
- Loosened SpiderFoot/realtime healthcheck timeouts so Docker does not mark loaded-but-running containers unhealthy during CPU-heavy runs.
- Realtime Telegram media dedupe is now global for public/social sources by sha/source URL/media-family keys while private chat sources remain source-scoped.
- Browser recovery scripts prefer Chrome for Testing/Playwright Chromium without broad recursive scans, pass the selected Chrome path through maintenance, tolerate audit exit code 2, and default profile restart threshold to one unhealthy platform unless explicitly disabled.
- Local runtime is on CDP port 9336; startup/cookie-vault env outside git were updated to use that port.
- Pi-hole nebula-sync was moved into the main Pi-hole compose stack outside this repo; both `pihole` and `nebula-sync` are healthy and sync completed.

Verification completed:
- `python -m compileall src\core\optional_rollout.py src\dashboard\api.py src\main.py src\notifications\realtime_feed.py` passed.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin tests\notifications\test_realtime_feed.py tests\core\test_url_filter.py tests\core\test_optional_rollout.py tests\tools\test_browser_maintenance_scripts.py -q` passed 100 tests.
- Compose config validation passed for Collector recon profile, main Pi-hole compose, and legacy nebula-sync compose.
- Live env readback: website allowlist includes `https://*.com`; realtime global dedupe sources include `threads,lemon8`; cookie vault points at `host.docker.internal:9336`.
- `collector_spiderfoot` and `realtime_feed` were recreated and are healthy with the new healthcheck timeouts.
- Browser audit on port 9336: 8 page targets, 1 control tab, no blank tabs, one tab per platform; Threads is responsive with content script `1.23.70` running.
- Ingest bridge is alive under load: `/health` returned 200, browser heartbeats returned 200, and logs show a Threads ingest batch saving 38/55 plus later `/social/ingest` 200s.

Known caveats:
- `ig_ingest` still has timeout spikes under load, especially revisit/DM endpoints, although it continues accepting heartbeats and ingest.
- X still shows a recoverable `Try again`/blank shell; Threads, Instagram, TikTok, and Facebook had active content scripts in the final audit.
- SpiderFoot is intentionally weak-lead recon only; failed/timeout targets are expected and should not block the whole rollout by themselves.
