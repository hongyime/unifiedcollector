# Full plan audit handoff — 2026-10-14 16:20 UTC (updated 2026-10-15 16:54 UTC)

## Scope

The active work follows the user's five-phase UnifiedCollector plan. The user clarified that Pinterest/Snapchat/Airbnb WIP belongs in Phase 2 and said they would reload the Lemon8 extension. Preserve Spider files. Do not print credentials or tokens.

## Pushed follow-ups

- `22acfcef fix: bound Beeper chat refresh`: maps the API's `lastActivity`, bounds chat-list refresh to three pages by default, and prioritizes recent chats. Beeper tests: 52 passed. After restart, the latest rolling 15-minute DB window had 75 message rows and 229 chat refreshes; current rolling 12h has 287 message rows.
- `5a88905a fix: ignore removed services in watchdog sweep`: liveness restart now requires a current Compose service label. Retired `collector_light`, `collector_lowrisk`, and `instagram_ingest` containers stayed exited for at least 15 minutes after 16:38:51 UTC; watchdog remained healthy.
- `efa6a2c5 feat: persist Snapchat Spotlight and Highlights`: adds additive tables and routes public story/Spotlight/Highlight writes to separate tables. Native scraper/bridge tests: 23 passed. The migration is recorded in Postgres; all three native content tables remain empty.
- `ea8791b5 fix: release search mutex between queries`: query-scoped shared provider lock. Focused operation-lock and sidecar tests: 3 passed. `collector_search` was restarted and logs show it waiting for the shared lock.

## Current runtime and blockers

- Exposure still owns `collector_mutex:search_engine` from its current long batch (DB session idle under a granted advisory lock for more than 3h27m at 16:54 UTC). Do not stop or recreate exposure to release it. Restart exposure only after natural release, before the next old-code cycle reacquires it. Search is already on the new code.
- Phase 1 obsolete-container check passed for 15 minutes; a final full-stack soak is still pending after remaining workers reload.
- Lemon8 has 14 posts in the rolling 12h DB window. The last 15-minute extension window had 53 events, all with stored_count 0; recent media events remain empty, and no recent Lemon8 browser media candidates exist. User was asked to open For You for capture verification.
- Telegram member refreshes continue and there is one recent join/leave row; no poll-vote or discussion-visit rows in 12h. The discussion join/scrape/leave path is disabled because `TELEGRAM_SPIDER_ACCOUNTS` is unset and no linked-discussion field is stored. User was asked which existing worker account scope to use.
- Pinterest has boards but no pins. Snapchat has profiles but no story, Spotlight, or Highlight rows. Airbnb has listings/hosts but no reviews; current public GraphQL responses yield no usable data. Do not bypass provider responses or access private Snapchat content.
- Search has 10 dynamic terms with `search_count=0`, no document-index rows, and recent document URLs. Wait for exposure's natural lock release before judging the query-level fix.

## Resume steps

1. Check for user replies about Lemon8 For You and Telegram discussion account scope.
2. Monitor the exposure advisory lock without disturbing its worker. Once naturally released, restart exposure to load per-query locking and verify search resumes.
3. Refresh rolling 12-hour per-container counts and source-specific row counts; run the final 15-minute stack health/rate-limit window after all required workers have loaded current source.
4. Update `/tmp/unifiedcollector-plan-complete-20261014.md` with current split service names, results, and blockers.
5. Keep `.agents` hostname scrub checks in place before staging; never stage ignored `docker/env/*.env` files.
