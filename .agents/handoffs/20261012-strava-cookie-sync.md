# Strava cookie sync handoff

Task: Enable the Chrome extension to sync live Strava cookies through the existing `/social/cookies` endpoint so the headless collector can auto-discover `strava_<account>.txt`.

Starting point:
- Branch `main` was clean and aligned with `origin/main` at `cf5a4c2e`.
- `cookies_handler` currently accepts only Instagram and writes Netscape jars below `CREDENTIALS_ROOT`.
- Extension `syncCookies()` and both call sites are in `extension/src/background.ts`; keep the function itself unchanged.
- `browser_ingest` and `collector_strava` both mount `../credentials` at `/app/credentials`; the image works from `/app`. With `STRAVA_COOKIES_FILE` unset, collector discovery uses `/app/credentials/strava`.
- No dedicated cookie handler tests exist. `extension/package-lock.json` exists, but `extension/node_modules` is absent.

Implemented: shared cookie writing/account sanitization helpers; Strava handler requires `_strava4_session` and writes `strava_<account>.txt`; extension sync is called at both existing sync sites; focused handler tests cover Strava, Instagram, and unsupported platforms. Build entry paths were updated from nonexistent `.js` files to the tracked `.ts` sources.

Verification so far: `npm ci` and `npm run build` pass after correcting the TypeScript entry paths. The host Python lacks pytest/aiohttp. A synthetic smoke against the running bridge container passed handler write, Netscape session parsing, and collector `_load_all_cookie_accounts()` discovery. Runtime env files do not set `CREDENTIALS_ROOT`, so constants use `/app/credentials`.

Verification complete: 77 tests pass across `test_ig_ingest_cookies.py` and the existing `test_ig_ingest_vault.py` in a disposable collector-image container. `npm run build` passes. Synthetic bridge-to-collector write, Netscape parsing, and auto-discovery smoke passes.

Completed and pushed as commit `2e7554a8` on `main`. The extension bundle builds, 77 cookie and ig_ingest bridge tests pass, and synthetic handler-to-collector parsing and auto-discovery pass. Instagram success behavior is unchanged. Both services share `/app/credentials`; no Compose cookie-path override was needed.
