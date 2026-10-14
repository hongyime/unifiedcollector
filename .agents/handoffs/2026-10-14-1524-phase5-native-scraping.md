# Phase 5 Native Scraping Handoff

Task: continue the user-provided UnifiedCollector plan. The user explicitly expanded Phase 2 to include the Pinterest/Snapchat/Airbnb WIP. Do not edit Spider files.

## Current status

- Local `main` is at `ff263967`, one commit ahead of `origin/main`. The last two pushes of `ff263967` were rejected by GitHub with an internal server error; fetch showed no remote divergence.
- Phase 5 implementation is uncommitted. Pinterest uses Playwright and reads only Pinterest cookies from the shared cookie vault. Snapchat uses Requests against allowlisted public pages and parses Spotlight/Discover/profile story data. Airbnb uses direct GraphQL queries for public listings, host cards, and up to ten reviews per listing; `airbnb_reviews` has a new migration.
- Native target claims now take `browser-native-scrape:<platform>` as a transaction advisory lock, matching the browser bridge claim path.
- Review follow-up tightened Pinterest board visibility further: a board requires an anonymous primary heading and pin count from that heading's DOM region. Cookie-rendered board fields are discarded; pin IDs still require anonymous matches. Pinterest redirects are fetched with redirects disabled, checked hop-by-hop before continuing, then validated HTML is fulfilled into Playwright; later main-frame navigations are aborted. Pin ownership requires exact profile/board membership links within each pin card, and profile attribution requires visible identity evidence. Cookie-rendered pages only confirm anonymous IDs; anonymous fields remain authoritative. Snapchat checks every redirect before following. Airbnb computes remaining listing queue capacity under the platform advisory lock, with full/partial behavior covered.
- Verification passes: 44 focused tests; Python compileall; production Compose config; `scripts/verify_env_split.py` (30 services, no missing keys); `git diff --check`. Follow-up review identified that a profile scrape with verified owner but no board context must preserve a previously verified board association. The upsert now does so, with regression coverage; final review found no remaining issues. The shared image has been rebuilt from the latest source changes.
- Pinterest/Snapchat/Airbnb live writes are not yet verified. Airbnb `StaysPdpSections` returned HTTP 200 with a generic GraphQL error and no `data` for a current listing. Respect this response and do not attempt a challenge bypass.
- After the user's manual reload, the latest 60-minute Lemon8 window has 9 `media` events, all `empty`, and zero new candidates. The latest event metadata still points to the Singapore topic URL; For You capture remains unverified. Heartbeats continue.
- Search dynamic terms and cursor are live, but the document index is still waiting for the shared search mutex. Existing 15-minute soak and per-container 12-hour counts remain open.

## Next steps

1. Finish the read-only code review; resolve any Critical/Important findings and rerun affected checks.
2. Commit Phase 5 as one focused commit, preserving the prior `ff263967` commit. Before staging `.agents/`, replace the machine hostname with `dev-host-3.example` and verify staged `.agents/STATE.md`, `.agents/JOURNAL.md`, and handoff blobs contain zero real-hostname occurrences.
3. Push both local commits after confirming the remote state. Do not rewrite history.
4. Build the shared collector image using the configured image-building service; run the migration runner from that image; recreate only Pinterest, Snapchat, and Airbnb profile services.
5. Verify live table deltas, service health, logs, and retry/rate-limit handling. Do not claim rows until database evidence exists.
6. Recheck Lemon8 after the extension is on the For You feed, and recheck the search mutex/document index without cancelling the active exposure cycle.
7. Complete the requested 15-minute soak, per-container 12-hour counts, and `/tmp/unifiedcollector-plan-complete-20261014.md` report.
