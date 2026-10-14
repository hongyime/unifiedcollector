# UnifiedCollector full-plan handoff — 2026-10-14 17:41 UTC

## Objective and constraints

Continue the complete execution checklist in the user's `pasted-text-1.txt`: verify phases 1–5, satisfy every specified collection surface/field, then pass a clean 15-minute soak and publish a fresh per-container rolling 12-hour report. Keep all work on `main`, commit and push phase changes, update shared state, do not modify Spider files, do not print credentials/tokens, and honor provider backoff/`Retry-After`.

## Repository and runtime baseline

- Current code HEAD before this handoff: `76baee5e`; worktree was clean.
- Compose runtime has the split services (`collector_exposure`, `collector_lemon8`, `collector_search`, `collector_strava`) and `browser_ingest`; all current UnifiedCollector containers were running at 17:38 UTC.
- Beeper: 1,239 `beeper_shadow_messages` in 12h, 181 in 15m.
- Lemon8: 15 posts and 33 media rows in 12h, 0 browser candidates. Reload of extension 1.23.108 is verified by a 17:36:10 `content_script_boot`; its page returned `recoverable_error_shell`, then an empty media event. The current visible feed is still not verified as populated.
- Telegram: 11,936 messages, 1,639 member refresh touches, 16 polls, 0 poll-voter rows, 1 join/leave row, and 0 discussion visits in 12h. `TELEGRAM_SPIDER_ACCOUNTS` is unset. The July feature commits are present (`8128becf` poll voters, `d5d296c1` member inference); the recent discussion fix is `53f68397`. Do not select an account scope without the operator's choice.
- Search: 7,453 results in 12h; 10 active dynamic terms are all unsearched; `search_document_index` has 0 rows. Exposure still holds `collector_mutex:search_engine` (advisory key matches `hashtext`; the session is idle and the lock age was about 4h13m). Do not stop/recreate exposure or release its lock. Restart only after natural release so the deployed per-query search mutex fix is loaded.
- Phase 5 tables: Pinterest pins 0; Snapchat stories/Spotlights/Highlights 0; Airbnb reviews 0. Airbnb has 4 listings and 3 hosts touched in 12h.
- Native queue evidence: Pinterest has 727 pending profile targets, 6 pending board targets, 304 completed boards, 10 failed empty profiles, and 1 claimed board. Snapchat has 9 pending profiles, 79 failed empty profiles, 426 failed unavailable profiles, and no public story rows. Airbnb browser-queue work has 11 runtime-error failures; the direct GraphQL collector last touched a listing at 17:37 UTC.
- The recent 15-minute log scan had no tracebacks, but it is not a clean window: 53 Website 429 lines were from 53 distinct hosts (one each), with additional browser-ingest/TikTok/WhatsApp/Pinterest/Snapchat 429s and native worker errors. `rate_limit_events` had one new HTTP 401 event (Instagram profile fetch), not a 429.

## Phase 5 implementation audit

- Pinterest already imports Playwright and reads the cookie vault, but it fetches the page HTML with `httpx` before handing that HTML to the browser context; verify browser availability and make the Playwright page perform the actual dynamic page load/scroll. The user-required fields still need a field-by-field audit (profile monthly views/following; board description/followers; pin repins/comments).
- Snapchat has a paced `requests` public-page path and profile probing. Keep it public-only; improve endpoint/parse diagnostics and verify story/highlight/Spotlight types only from eligible public content.
- Airbnb already calls public GraphQL persisted queries directly (no Apify), but the current parser/schema omits requested listing description/photos/price/amenities/availability and host bio/response rate. Existing queue failures are generic and no review rows are stored. Audit the current `pyairbnb` reference for refreshable hashes/request shape; add the missing schema/parser fields and non-sensitive diagnostics before live verification.
- References inspected: official GitHub repos for `pinterest-dl`, `SnapScraper.py`, `snaposint`, and `pyairbnb`. The SnapScraper README says its public story/highlight/Spotlight path uses Requests and BeautifulSoup; SnapOSINT describes public, unauthenticated profile metadata/media only. The `pyairbnb` README documents direct Airbnb listing/review/host extraction and current persisted-query/search helpers. Do not use paid Apify or private Snapchat content.

## Next steps

1. Finish field/schema audit for phase 5, then implement the missing requested fields and correct Pinterest Playwright page navigation without exposing cookies.
2. Check dependency/browser availability in the Pinterest container and use bounded, low-rate live probes only after local tests pass.
3. Audit Airbnb GraphQL errors by type/status without printing response bodies, then refresh persisted-query mechanics from the upstream source.
4. Confirm Telegram member completeness by joining member rows to `telegram_users`; keep discussion scraping gated until account scope is selected. Poll voters require eligible public-voter polls and a capable existing worker account.
5. Keep observing the exposure lock until it releases naturally; then restart exposure, let search queries/docs run, and verify the dynamic-term cap/expiry behavior.
6. After code deploys and operator-visible Lemon8 feed/account inputs arrive, refresh rolling counts and require a new clean 15-minute window before writing the final summary.
