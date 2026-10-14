# UnifiedCollector fix-list handoff — 2026-10-14 03:29 UTC

## Active task

Complete the four-item fix list from the user. Items 1–3 are complete; finish the 15-minute item 4 soak, write `/tmp/grind-summary-20261014.md`, then mark state complete. Do not touch Beeper or spider files. The `main` branch was clean and synced to `origin/main` at `ed826a18` before this handoff.

## Completed work

- YouTube fix `47c4e719` verified before this pass: recent collector logs contained successful yt-dlp runs and media ingests.
- Rate-limit fixes `7836ca41` (search), `b99b9b85` (GitHub), and `599bb8d2` (TikTok) were deployed and passed a previous 15-minute soak with no actual HTTP 429/FloodWait events.
- Airbnb profile feeder/route fix `ab6c9d8c`: direct numeric profile IDs only, `/users/show/{id}` route, and unparsed 200 pages remain errors. No current `/users/show/` URL is in `discovered_links`; `airbnb_profiles` remains empty for lack of eligible inputs.
- GitHub README fix `adbdf74d`: the API response has no `repository_id`; use the resolved internal repo UUID. A bounded mock passed and the live table gained one row after restarting only the GitHub collector.
- Bluesky feeder fix `8a3f9d23` plus validation follow-up `14686dcd`: stop fanning out bare cross-platform handles; accept direct full handles/DIDs. A rollback-only SQL feeder probe and PostgreSQL regex checks passed. Invalid pending cross-platform rows were marked error, not deleted; no pending cross-platform Bluesky rows remain.
- Item 3 classified all 75 stale and 35 empty tables. No spider files changed. Remaining tables are expected event-driven, static/control/cache, disabled or rollout-gated, missing eligible input/auth, legacy, or analyzer-owned. `pytest` is unavailable in host/container; compilation and bounded mocked/DB probes passed.

## Item 4 soak

- Soak baseline: 2026-10-14 03:24:34.590524 UTC. Target finish: 03:39:34 UTC.
- `rate_limit_events` baseline total was 33,893; latest row predates the soak (TikTok `profile_metadata`, status 200, challenge cooldown). This is not an HTTP 429. At 03:26:42 UTC there were zero new table rows, zero status-429 rows, and zero actual HTTP-429/FloodWait log markers across non-Beeper app containers.
- Baseline recent writes (prior 15 min): 61 YouTube media rows, 160 search results, 12 Facebook posts, 970 browser-ingest events, one GitHub README row. At 03:26:42, YouTube and search were still writing.
- Two error-marker log lines so far were the same transient yt-dlp “page needs to be reloaded” response; other downloads continued. No error flood observed.

## Next steps

1. Wait until at least 03:39:34 UTC, checking periodically that rate-limit rows/429 markers remain zero and YouTube downloads continue.
2. At the end, compare `rate_limit_events` against baseline total 33,893 and query rows strictly after 03:24:34.590524 UTC. Check actual 429/FloodWait markers and error counts in non-Beeper app logs; count successful YouTube yt-dlp ingests and table writes.
3. Capture final table-fill counts and worker health. Do not restart containers during the soak.
4. Write `/tmp/grind-summary-20261014.md`, including all 110 stale/empty table classifications, fixes, commit hashes, and verification evidence.
5. Update `.agents/STATE.md` and append `.agents/JOURNAL.md`; run the staged hostname scrub before committing and pushing.
