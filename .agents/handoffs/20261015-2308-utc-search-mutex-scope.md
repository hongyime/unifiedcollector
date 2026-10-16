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

## 2026-10-15 23:36 UTC soak result

The 15-minute soak from the 23:20:41 Search restart completed. Two dynamic terms advanced by 23:35:02; eight remain never searched. Search health updated at 23:35:15. The lock owner changed to Exposure between Search requests, confirming the provider mutex is no longer held during Search postprocessing. No Search 429/cooldown has appeared since restart. The recent query/result rows are shared with Exposure; no `filetype:` dork row or indexed document is present yet. Both Search and Exposure collector test files pass. All 30 Compose services are running, and CDP ports 9222/9336 each have exactly one page per managed social plus Extensions. Continue monitoring the remaining dynamic terms and documents; the other phase gates remain open.


## 2026-10-15 23:59 UTC dork-order update

The rolling Search snapshot has 5/10 terms searched, 5 never searched, and no filetype query/document yet. `_expand_dynamic_dorks()` places six cloud-bucket queries before the six person/document templates; filetype templates begin at cursor 5. Reordered the document templates first, placing filetype dorks at cursors 0–4 and keeping the second existing cursor on a filetype query. Added an ordering regression; the complete Search test file passes and `git diff --check` is clean. Change `cb1721a3` is pushed to `main` and not yet loaded by the Search worker. Defer a worker restart until after the Pinterest soak and lower Postgres load.

- Commit `cb1721a3` is pushed. Deploy only `collector_search` after the Pinterest soak ending 00:06:13 UTC Oct 16, and only when Search has no active request/cooldown and PostgreSQL load has settled.

2026-10-16 01:35 UTC full-plan continuation: Search remains short of its document gate. At 01:29 UTC there were 9 filetype queries/12h and 576 associated search-result rows, but 0 result URLs ending in document extensions; search_documents has 0 rows indexed/12h (28 historical, latest 2026-10-14 20:45 UTC). Runtime SEARCH_SPIDER_PAGES and document indexing/download defaults are enabled. At 01:32 UTC the search_engine advisory lock was held by collector_exposure while Search was waiting; leave it alone and observe the natural batch.
Telegram remains behind four active FloodWaits through 06:22:07 UTC. At 01:33 UTC: 0 discussion visits, 0 recent voter rows, 619 member refreshes, and 2 join/0 leave events. The Telegram container started 2026-10-14 and has not loaded the latest collector code. Original voter/member features are in commits 8128becf/d5d296c1; linked discussion and join/leave fixes are in 53f68397; next deploy only after cooldown expiry. Telegram API limits confirmed from official docs: voter IDs require non-anonymous polls; channel poll voter retrieval may be forbidden, and API can require the current account to have voted. Do not cast votes. Non-admin full member enumeration can be restricted. Focused pytest command was unavailable (`pytest` not installed); no tests or services changed.


2026-10-16 01:39 UTC continuation: the repo master checklist now reflects current 12h metrics. Search tests pass; the existing search_documents rows are historical (28 total; 20 with extracted text), and there were no document index rows in 12h or direct document-extension URLs among 576 results from nine filetype queries. Keep observing the natural Search/Exposure lock turn. Telegram focused mocks pass (12 tests); live visits/voters remain unverified until the four FloodWaits expire 06:22:07 UTC. Both CDP endpoints have exactly 11 unique page hosts. No code, DB, service, cookie, or browser mutations were made.

## 2026-10-16 01:43 UTC Search batch diagnosis

The current Search worker and PostgreSQL are healthy. Read-only counts show 599 pending Search targets, zero targets with `last_collection_at`, 225 `search_queries` rows/12h (9/1h), 10 active dynamic terms (6 searched in 45m), and 28 historical `search_documents` rows with no indexing in 12h. `Worker._load_targets()` returns every pending/error target without a limit. `SearchCollector.collect()` executes the full query set sequentially at `SEARCH_QUERY_DELAY_SECONDS=30` and defers all result postprocessing until the entire set completes. A bounded round-robin target slice is the next implementation step; dynamic queries should remain first and the slice must advance via the existing persisted checkpoint. Do not reduce provider delays, release the Search mutex, interrupt Exposure, or touch Spider/SpiderFoot. Do not restart Search while a provider request is active; verify a safe deployment window after the focused tests.

## 2026-10-16 01:47 UTC implementation checkpoint

Added a configurable `SEARCH_MAX_TARGETS_PER_CYCLE` limit (default 10, bounded to 100) and checkpoint-based round-robin selection across query targets and direct seed URLs. The ten dynamic terms remain first; `SearchCollector.collect()` now defers postprocessing only for the bounded slice instead of the whole 599-target pending set. Added a regression covering the cursor rotation and exposed the setting in `collector_search`. Search/parser tests pass; Compose config and diff checks pass. The runtime has not loaded this change. Before deploying, verify the Search provider call has ended; do not touch Exposure's held mutex.
