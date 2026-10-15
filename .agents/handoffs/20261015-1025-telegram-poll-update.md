# Telegram Poll Update Handoff — 2026-10-15 10:25 UTC

## Goal and scope

Continue the UnifiedCollector Full Plan in .agents/STATE.md and the supplied plan file. All four configured Telegram accounts, future configured accounts, and all eligible groups remain in scope. Do not expose account names, phone numbers, session data, or poll/message identifiers. Do not touch Spider/SpiderFoot files. Leave Exposure and its Search mutex untouched.

## Diagnosis

At 10:15 UTC the database had 7,325 telegram_polls: 137 public/non-anonymous and 125 marked open. Sixteen poll snapshots refreshed in the rolling 12 hours were all anonymous. telegram_poll_votes had 262 historical rows, none refreshed in 12 hours; newest refresh was Sep 28. The 12-hour Telegram log sample had no GetPollVotesRequest failed messages. Existing voter enumeration was called only from message ingest/edit paths. Telegram instead emits UpdateMessagePoll when poll results change, so later public-poll votes had no update handler. The official Telegram API documents this update; current Telethon 1.43.2 exposes optional peer and msg_id fields.

## Working-tree changes

- src/collectors/telegram/mixins/realtime.py: subscribe to UpdateMessagePoll on every connected worker; refresh stored public polls using existing voter enumeration; recover omitted peer/message references from stored message data; preserve existing rate-limit/FloodWait handling; debounce per poll for 1,800 seconds; skip anonymous or unknown polls.
- src/collectors/telegram/collector.py: configure the debounce cooldown and per-poll memory.
- docker/docker-compose.yml: set the cooldown to 1,800 seconds.
- tests/collectors/test_telegram.py: cover public poll updates, anonymous/unknown skips, debounce, missing peer/message fallback, and handler registration.

The focused Telegram and realtime test files pass. Python compilation and git diff --check pass. Changes are not yet committed, pushed, or loaded into the live collector. The Telegram Compose service bind-mounts src; after commit/push, recreate only collector_telegram to load the handler. That will reconnect its Telethon workers; existing startup delays and per-account FloodWait recovery remain in place. Then verify all connected workers register the raw poll handler without printing account names, and wait for a public poll update to create fresh voter rows. Do not count historical rows as live verification.

## Other live gates

- Lemon8 at 10:15: 29 posts/12h, 16 titled, 14 described; newest 10:15:48. Cursor remains running with last processed time 10:12:44. Do not restart until idle.
- Browser: at 10:12 CDP showed four targets, one social page, no extension target; CUA exposed no browser/app surface. Dashboard health at 10:14 showed expected extension 1.23.115, current version missing, ingest state unknown, and mismatch/stale-hook issues. User’s login is not visible in this collector surface yet.
- Search still awaits Exposure’s natural mutex release. Airbnb host retry is scheduled for 13:50:53 UTC.
- Keep one extension-managed tab per social; follow-first ordering remains in effect for Pinterest/Snapchat and followed-linked listings for Airbnb.

## Next actions

1. Inspect diff, run focused checks if code changes, hostname-scrub staged .agents blobs, commit and push the Telegram fix plus tracker updates.
2. Recreate only collector_telegram; confirm it reconnects all configured workers and registers the raw poll handler.
3. Await/verify a public UpdateMessagePoll writes fresh voter rows; no live public poll update means this gate remains pending.
4. Continue Lemon8, browser/Pinterest/Snapchat/Airbnb, Search, then the final 15-minute soak and per-container 12-hour report.
