# UnifiedCollector full-plan handoff

Updated: 2026-10-15 22:45 UTC

## Scope and state

Continue the full UnifiedCollector plan supplied in the conversation; do not reduce completion scope. Refresh the existing `/tmp/unifiedcollector-plan-complete-20261014.md` report. Keep one tab per social site. Do not touch Spider/SpiderFoot or read/print credentials.

## Current evidence

- Branch `main` was clean and at `f10137de` before this turn's changes. Earlier pushed commits include the Pinterest follow roster and the same-platform follow-source restriction.
- All 30 UnifiedCollector containers are running; 24 have healthy checks and 6 have no healthcheck.
- Both CDP ports 9222 and 9336 have 11 page targets: one for each of the 10 managed sites plus `chrome://extensions`. No tabs were created.
- Pinterest started 22:24:07 UTC and Snapchat 22:20:52 UTC. Their 15-minute bounded soak completed 22:39:07 with no native rate event or traceback; bounded log scans had 14 Pinterest and 16 Snapchat error-level lines. One Instagram 401 occurred during the same interval. Latest 12h: Pinterest 48 pins/1 description/0 boards; Snapchat 12 Stories/40 Spotlights/20 Highlights. Pinterest has 27 own-following edges, one preferred target completed, one pending, and 314 failed. Snapchat has 0 own-following edges and 359 quarantined legacy guesses. Airbnb has 3 listings, 3 hosts, and 30 reviews.
- Beeper has 12,018 messages/12h; Lemon8 API has 110 posts. Lemon8 extension media events in the 12h window have 62 stored rows and 968 dedupe outcomes; the most recent partial For You event is 18:51 UTC. The current visible page is not producing fresh For You content.
- Telegram has 0 discussion visits/12h, 7 polls refreshed/12h with 0 voter rows, 710 refreshed members, and 1 join/leave event. All four Telegram accounts remain FloodWait-limited through 2026-10-16 06:22:07 UTC. No account allowlist is set; all configured groups/accounts remain eligible. Do not restart before expiry.
- Search has 283 queries and 7,599 results/12h, 0 documents indexed/12h, and 10 due/unsearched terms. Exposure was restarted alone at 22:34:11 UTC; the old idle backend session disappeared. Search acquired the advisory key and currently holds it while a live HTTPS connection is open. At 22:45 it had not written query/result/document rows or advanced a dynamic term in the latest 5m; no rate/error signal appeared. Leave the live request alone and recheck its completion. Exposure's last success was 22:41:16; Search's last success remains 2026-10-14 16:51:50.
- X's live content script v1.23.118 repeatedly reported `try_again_empty_state`. The previous form guard did not prevent X's retry-button handler from detaching the form during the click's default action. Updated only this X recovery reason to use the existing paced navigation fallback, avoiding the native button. Extension v1.23.119 is built; the regression assertion failed against the old bundle, then passed after the change. All 45 `tests/extension/test_extension_bundle_static.py` tests pass. Compose expects 1.23.119, but the live Chrome browser still runs 1.23.118. CUA exposes no browser control; user needs to reload the existing extension and then the existing X tab before runtime verification.

## Next actions

1. Let Search's current HTTPS request finish, then verify Search `last_success_at`, dynamic terms, query/results, and documents. Do not restart while its provider socket is live.
2. Recreate only the three Compose services that carry `UC_EXTENSION_EXPECTED_VERSION` (`browser_ingest`, `scheduler`, `dashboard`) when operational load permits; their containers still have 1.23.118 until recreated.
3. Ask user to reload UnifiedCollector Bridge in the existing `chrome://extensions` tab and reload the existing X tab; verify active content script 1.23.119 and no new form-cancellation event. Preserve the one-tab-per-site setup and do not restart Chrome.
4. After Telegram FloodWait expiry (2026-10-16 06:22:07 UTC), restart Telegram to load current retry/discussion/poll code, then verify all four advanced features across all eligible accounts/groups.
5. Obtain Snapchat Following from its signed-in app/export if available; the web tab has no Following list. Leave Lemon8 on a populated For You feed for an extension capture check.
6. Refresh per-container 12h counts and perform the final full-stack 15-minute soak after all required reloads/restarts; keep the final checklist/report incomplete until every original gate is proven.
