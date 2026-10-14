# Phase 5 Native Scraping Handoff

Task: continue the user-provided UnifiedCollector plan. The user explicitly expanded Phase 2 to include the Pinterest/Snapchat/Airbnb WIP. Do not edit Spider files.

## Current status

- Phase 4 fix `ff263967` and Phase 5 commit `8c38459e` are pushed to `origin/main`.
- Phase 5 implementation is committed and pushed as `8c38459e`. Pinterest uses Playwright and reads only Pinterest cookies from the shared cookie vault. Snapchat uses Requests against allowlisted public pages and parses Spotlight/Discover/profile story data. Airbnb uses direct GraphQL queries for public listings, host cards, and up to ten reviews per listing; `airbnb_reviews` has a new migration.
- Native target claims now take `browser-native-scrape:<platform>` as a transaction advisory lock, matching the browser bridge claim path.
- Review follow-up tightened Pinterest board visibility further: a board requires an anonymous primary heading and pin count from that heading's DOM region. Cookie-rendered board fields are discarded; pin IDs still require anonymous matches. Pinterest redirects are fetched with redirects disabled, checked hop-by-hop before continuing, then validated HTML is fulfilled into Playwright; later main-frame navigations are aborted. Pin ownership requires exact profile/board membership links within each pin card, and profile attribution requires visible identity evidence. Cookie-rendered pages only confirm anonymous IDs; anonymous fields remain authoritative. Snapchat checks every redirect before following. Airbnb computes remaining listing queue capacity under the platform advisory lock, with full/partial behavior covered.
- Verification passes: 44 focused tests; Python compileall; production Compose config; `scripts/verify_env_split.py` (30 services, no missing keys); `git diff --check`. Follow-up review identified that a profile scrape with verified owner but no board context must preserve a previously verified board association. The upsert now does so, with regression coverage; final review found no remaining issues. The shared image has been rebuilt from the latest source changes.
- Migration `20261014_add_airbnb_reviews.sql` was applied and recorded in `schema_migrations`. Only the Pinterest, Snapchat, and Airbnb profile containers were recreated at 15:48 UTC; all three were running with zero restarts at 15:52 UTC. Live counts are Pinterest 75 boards/0 pins, Snapchat 0 stories, Airbnb 4 listings/1 host/0 reviews. Pinterest has a prior shared queue claim through 16:06 UTC. Snapchat public profile probes returned 404 and Spotlight/Discover returned no visible items. Airbnb GraphQL still returns a generic error with no data. Airbnb `StaysPdpSections` returned HTTP 200 with a generic GraphQL error and no `data` for a current listing. Respect this response and do not attempt a challenge bypass.
- After the user's manual reload, the latest 60-minute Lemon8 window has 13 `media` events, all `empty`, and zero new candidates. The latest event metadata still points to the Singapore topic URL; For You capture remains unverified. Heartbeats continue.
- Search dynamic terms and cursor are live (10 terms), but the document index remains empty while exposure holds the shared search mutex (idle session lock age about 2h22m at 15:52 UTC). Existing 15-minute soak and per-container 12-hour counts remain open.

## Next steps

1. Complete the 15-minute soak from 15:48:51 UTC and verify live table deltas, service status, logs, and rate-limit handling.
2. Recheck Pinterest after the shared lease expires, then inspect new Pinterest/Snapchat/Airbnb table writes. Do not claim rows until database evidence exists.
3. Recheck Lemon8 after the extension is on the For You feed, and recheck the search mutex/document index without cancelling the active exposure cycle.
4. Complete per-container 12-hour counts and `/tmp/unifiedcollector-plan-complete-20261014.md` report.
