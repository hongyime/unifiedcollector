# Full-plan handoff — 2026-10-16 10:30 UTC

Continue the user's full plan from the attachment and keep its requirements open until verified. Do not touch Spider/SpiderFoot. Keep one browser tab per social platform, do not read or print cookie values, respect rate limits, and do not add or run tests unless the user asks.

## User direction

- Airbnb has no saved listings yet. Continue seeding from the homepage in the existing Airbnb tab and revisit a populated Wishlist in that same tab later.
- Telegram may use the four connected authorized accounts and permitted groups. Never cast a vote.

## Current progress

- Phase 1 and Beeper are complete.
- Lemon8: 47 posts/12h (target 50+), 4 posts/15m, 346 profiles updated/12h, 8 profiles/15m. Its worker is healthy; do not release the shared lock manually.
- Native collectors: Pinterest has 37 boards/1 pin in 12h; Snapchat has 4 Stories/16 Spotlights/19 Highlights. Airbnb has 23 homepage-sourced listing refreshes in 12h, latest 10:19:32 UTC, and no Wishlist rows.
- Telegram: 4 accounts active, 0 cooldowns. Two discussion visits/24h, both left, with 200 messages. `telegram_poll_votes` has 262 rows all-time and none refreshed in 12h. There are 113 non-anonymous group polls with voters. The only non-anonymous channel poll is API-forbidden.
- Search: 193 documents indexed/12h (158 PDFs, 156 with text; 26 JPG metadata; 1 each DOC, DOCX, PPTX, XLSX; 5 TXT), with 3 documents indexed/15m. Queue has 167 complete and 128 pending; most pending rows have generic `document_not_indexed` errors.
- CDP 9222 has 11 page tabs/11 hosts: one per ten social platforms plus Extensions. Do not create tabs.
- Cookie vault is healthy; latest safe archive is 10:26:12 UTC with 162 social cookies. Values remain unread.
- Instagram request at 10:21 UTC returned HTTP 429; the secondary account returned 401 and needs 2FA. Do not retry before the 900-second cooldown expires around 10:36:46 UTC. The final soak needs a later successful provider request and 15 minutes with no new rate-limit event.

## Pending Telegram change

Uncommitted source changes are in `src/collectors/telegram/collector.py` and `src/db/migrations/20261016_add_telegram_poll_voter_refresh.sql`. The background pass checks at most one existing non-anonymous group poll per connected account each hour and records `voters_checked_at` to avoid repeat requests for 24 hours. Broadcast channels and anonymous polls are excluded; no votes are cast. Review the diff, deploy by restarting only Telegram, then confirm the migration applied, four account sessions reconnect, discussion visits still leave cleanly, and voter checks produce rows or documented API restrictions. Do not restart Telegram again if the accounts are on cooldown.

Deployment checkpoint (10:37 UTC): The migration is applied and the container is healthy. Four accounts reconnected, then all four hit Telegram FloodWaits of about 70,000–71,000 seconds during the initial target pass. No further Telegram API requests until expiry. The voter-refresh batch has not run yet; it is gated on available accounts. Startup discussion sweep found no eligible pending channels (the prior two visits remain left). Keep this gate open and continue other phases.

10:38 UTC follow-up: Telegram has 0 voter attempts after restart and has not reached realtime; keep the code deployed but do not issue more account requests during cooldown. The vault is healthy with a 10:36:12 safe archive (164 social cookies; values unread). The browser still has one tab per social plus Extensions, Airbnb is on `/`, Pinterest is restored to its profile page, and no tab was added. Search has 10 unexpired terms; queue is 167 complete/128 pending. No tests were run.

Update `.agents/STATE.md` and append `.agents/JOURNAL.md` after the deployment checkpoint. Before staging any `.agents` changes, replace the actual machine hostname with `dev-host-3.example` if it appears, and verify the staged blobs. Commit and push only after the change is reviewed and runtime evidence is recorded.
