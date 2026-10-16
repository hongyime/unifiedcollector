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

`eca8e429 fix: bound search targets per collection cycle` is committed on `main` and pushed. At 01:54 UTC, Search logs showed it waiting on the mutex, the exact provider lock was held by Exposure, and Search had no external TCP connection. Recreated only `collector_search`; it is healthy, its runtime imports `_select_targets_for_cycle`, and the env value is 10. Its configured 90-second startup stagger was still active at 01:55 UTC. Next verify a logged target slice, then document download/index progress. If the lock owner changes to Search, wait for the request to release naturally before any further Search restart. The latest independent cooldown snapshot still shows four Telegram waits through 06:22:07 UTC; no Telegram restart.

At 01:57 UTC the new Search runtime logged `selected 10 of 599 starting at offset 3`. At 02:00 UTC the exact search-engine advisory lock was held by Search with HTTPS connections, confirming it had begun the provider turn; do not interrupt it. Fresh aggregates: 219 queries/7,470 results/12h, 10 filetype query rows, 10 dynamic terms, 0 document rows indexed/12h; historical docs remain 28. A Pinterest own-follow refresh yielded 27 edges; one matching target completed and 26 failed with empty public pages. Snapchat has no own-follow edges. Airbnb has no followed-linked targets. Telegram still has four FloodWaits through 06:22:07 UTC. Both CDP endpoints still show one page per managed social plus Extensions.

At 02:02 UTC read-only DOM checks on both CDP endpoints found one Pinterest, Snapchat, and Airbnb page on each. Pinterest exposes a Following control and the authenticated collector refreshed 27 own-follow edges. Snapchat and Airbnb pages are ready with no login prompt but contain no Following/Friends link or text; leave broad discovery disabled. No navigation, tab, or cookie changes.

At 02:03 UTC the `search` service cursor and source heartbeat advanced, confirming at least one query completed after deployment. Search has no document index rows yet and is again waiting while Exposure owns the provider mutex. Current Pinterest completeness is partial (48 pins: 47 titles/images, 2 descriptions, no destination/board/repin/comment fields; 37 boards with names/descriptions/pin counts, no follower counts). Snapchat has expected content rows with partial title/thumbnail fields. Airbnb's 3 listing/host rows and 14 review rows have the requested fields populated. No runtime settings changed.

## 2026-10-16 02:48 UTC continuation

User approved using Airbnb Wishlists as listing seeds. The existing signed-in Airbnb page's `WishlistIndexPageQuery` returns 0 lists and 0 listing links, so no seed targets currently exist. The single Pinterest tab's own `/following/` route redirects to its profile; Snapchat `/web` still has no Following route. CDP remains at 11 pages, one per managed social plus Extensions.

Search has 9 direct document URLs/12h and 0 new Search media/documents. One PDF URL returned HTTP 200 (`application/pdf`, 262 KB) and its URL-derived content ID is not already in Search media. Search defers broad postprocessing until its full slow query batch ends; source was updated to process direct PDF/office URLs after each query completes, outside the provider mutex. `git diff --check` passes; no tests were run. The live Search cursor is still at 02:44:11, with two advisory lock holders idle on ClientRead (276s and 444s at the last sample). Do not release locks or restart Search until both provider turns end naturally; then restart only Search and watch for a direct-document pass/new document row. All four Telegram account FloodWaits remain active through 06:22:07 UTC; no Telegram restart before expiry. Cookie values were never read or logged.

At 02:53 UTC, Search's pending source patch includes strict HTTP(S) validation for document URLs and runs direct PDF/office downloads after each provider query. Search remains on its pre-patch process; its log shows it waiting since 02:51:50. One shared `collector_mutex:search_engine` advisory lock remains held by an Exposure session idle on ClientRead for 71s. Do not release it or restart Search until the request ends naturally. User approved Airbnb Wishlist seeding; the signed-in wishlist index returned 0 lists and 0 listing links, so there are no current seed URLs.

At 03:01 UTC, Airbnb Wishlist collection is implemented in the existing extension path: only `/wishlists`/`/wishlist` routes are parsed, listing cards are posted to the existing native ingest, and a targeted queue query marks them `airbnb_wishlist` with priority below followed-account links. The page's Wishlist index remains empty. Extension v1.23.122 was built and reloaded; only the existing Airbnb page was refreshed, so CDP remains at 11 pages. Restart only `collector_airbnb` when PostgreSQL load is suitable; it needs to import the new seed query. Current PG sample is 118.57% CPU / 2.291 GiB, Search's provider lock remains held (one idle ClientRead session, 105s) and Search still needs its safe post-request restart. Do not release the lock.

At 03:05 UTC, `collector_airbnb` was restarted and browser_ingest/scheduler/dashboard were recreated to load the v1.23.122 expectation. Airbnb's post-refresh heartbeat reports v1.23.122; its Wishlist target queue is empty because no saved list links exist. Dashboard and scheduler report healthy; Chrome remains at 11 pages. PostgreSQL sampled at 198.02% CPU / 2.381 GiB. A wider per-platform heartbeat query timed out, so avoid more broad aggregates. Search still has one advisory lock holder idle on ClientRead (last sample 9s); never release it, and don't restart Search until it is naturally free.

At 03:08 UTC, commits `453c4f5d` (Search direct documents) and `77255bd1` (Airbnb Wishlists) were pushed; `main` matches `origin/main`. Search still waits from 02:57:29, with one shared advisory-lock holder idle on ClientRead for 158s at the last sample. Do not release the lock or restart Search until it clears naturally. PostgreSQL sampled at 119.71% CPU / 2.443 GiB; a broad heartbeat query timed out, so avoid wide database aggregates. All other in-scope services remain healthy; CDP has 11 pages, one per managed social plus Extensions. Telegram's four FloodWaits remain through 06:22:07 UTC; run its next collection cycle after expiry and then complete the 15-minute soak.

At 03:12 UTC, state commit `d1c33f9e` is pushed and `main` matches `origin/main`. The Search lock remains granted to an idle ClientRead session (64s at the latest sample); Search has waited since 02:57:29. Do not release or interrupt it. Telegram remains gated by four FloodWaits through 06:22:07 UTC. The 15-minute final soak is still pending.
