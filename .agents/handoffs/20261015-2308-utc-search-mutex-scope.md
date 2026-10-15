# Search mutex scope handoff — 2026-10-15 23:08 UTC

## Objective

Continue the full UnifiedCollector plan in `main`; Search must advance its dynamic terms, issue dork queries, and index documents. Do not touch Spider/SpiderFoot. Do not expose credentials or cookie values. Telegram must not be restarted before its four account FloodWaits expire at 2026-10-16 06:22:07 UTC.

## Confirmed Search diagnosis

The live Search worker owns `hashtext('collector_mutex:search_engine')`; its `application_name` matches the Search container ID. It has an established HTTPS socket, the DB session has been idle for several minutes, the last Search result rows appeared at 2026-10-15 23:01:47 UTC, and there is no active Search provider cooldown. The container is healthy. Search and Exposure share the result tables, so rows written after the restart cannot be attributed to Search without a per-source marker.

The mutex scope is too broad. `SearchCollector.search_query()` acquires the shared mutex around `_search_query_unlocked()`. That method calls `_finalise_query()` before returning. `_finalise_query()` persists hits, downloads each hit's asset, and—when `SEARCH_SPIDER_PAGES=1`—calls `expand_paste_sites()` to recursively crawl result pages. Live config permits 100 results, 10 concurrent downloads, and depth-2 crawling (up to 150 pages per seed). These post-search tasks can hold the Search/Exposure provider mutex for minutes and block the other source. Search's page/result crawling must move outside the shared provider mutex; keep provider calls serialized.

## Current runtime gates

- Only Search was restarted at 22:57 UTC after its prior idle lock was owner-confirmed, with no active request/cooldown. The new request is live now; leave it undisturbed until it completes or its socket closes.
- Ten dynamic terms refreshed at 22:59:11 UTC from Instagram, Pinterest, TikTok, and YouTube data. All are due and never searched; template cursor is 0. No dork-syntax query has completed.
- Both Chrome CDP endpoints have exactly 11 page targets: one for each of the 10 managed sites plus `chrome://extensions`. The extension service worker and X/Lemon8 content contexts report v1.23.119. X Home produced a successful post-fix event at 22:49 UTC. Lemon8 still needs a populated For You capture.
- Telegram has 0 recent discussion visits; four FloodWaits remain active through 06:22:07 UTC.
- The full-stack soak after the Search restart should continue through at least 23:12:17 UTC.

## Safe next steps

1. Review, commit, and push the tested source change and checklist updates to `main`.
2. Wait until the old Search request/crawl is confirmed complete, then restart only `collector_search` to load the fix.
3. Verify dynamic term cursors/search counts, fresh dork rows, result writes, and document indexing while ensuring Search and Exposure provider calls stay serialized.
4. Continue the remaining Lemon8, Telegram, Pinterest, Snapchat, and Airbnb acceptance gates, then complete a fresh 15-minute full-stack soak and report.

## 2026-10-15 23:17 UTC update

Implemented the mutex-scope fix in `src/collectors/search/__init__.py`: provider calls stay serialized, while result persistence/downloads/crawls run after releasing the shared lock. `collect()` schedules all current dynamic terms before deferred result postprocessing; direct seed crawls also run outside the provider mutex. The focused Search test file passes and `git diff --check` is clean.

Live read-only snapshot: 268 Search queries and 7,258 result rows in the rolling 12 hours; 0 dork queries and 0 documents indexed in 12 hours; 10 active/due/never-searched dynamic terms. The last result row is 23:01:47 UTC and last query row 22:32:51 UTC; Search and Exposure share these tables, so attribution is not available. The old Search worker has a DB session idle for about 15 minutes and one established HTTPS socket. The socket may be an active crawl or a keep-alive; leave the worker alone until request completion is confirmed. The final soak window through 23:12:17 UTC elapsed without dynamic-term progress. Commit/push the tested source and checklist update, then deploy only when safe; verify dork rows, term cursors, document indexing, and complete a fresh soak.

## 2026-10-15 23:28 UTC deployment update

Committed and pushed `bf3ffe12`, then restarted only `collector_search` at 23:20:41 UTC. The new runtime imports the `postprocess` keyword on both `search_query()` and `_finalise_query()`. At 23:27:50 UTC the first dynamic term advanced (`search_count=1`; 9 terms remain never searched); a subsequent lock sample identified `collector_exposure` as the Search/Exposure mutex owner. This confirms Search released the provider lock after its first query. Rolling 12h Search snapshot at 23:28: 264 query rows, 7,195 results, 0 filetype dork rows, 0 indexed documents. No active Search provider cooldown. Complete a 15-minute soak from the 23:20:41 restart (through 23:35:41 UTC), while monitoring term progress and documents.
