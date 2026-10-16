# Full-plan resume checkpoint — 2026-10-16 05:15 UTC

## Task and repository state

Continue the user-approved UnifiedCollector full plan from the attachment. The user's plan authorizes commits and pushes to `main`; do not touch Spider/SpiderFoot. Latest pushed checkpoint is `6f2722e3`. `.agents/STATE.md` and `.agents/JOURNAL.md` have newer local updates and need a hostname-scrubbed commit after this handoff is added.

## Active constraints

- One tab per social platform; CDP has 11 pages across 11 hosts (ten social platforms plus Extensions).
- All 10 platform heartbeats and the bridge report extension v1.23.123 as running. No duplicate host tabs.
- Keep cookie values, session strings, and account identifiers out of output and repository state. Cookie vault is healthy, snapshots every five minutes, and has zero consecutive failures. At 05:06 the newest snapshot had 157 cookies including Pinterest 12, Snapchat 23, and Airbnb 29. The effective restorable 161-cookie snapshot from 04:36 remains preserved (Pinterest 12, Snapchat 24, Airbnb 29). No cookie values were read.
- All four Telegram accounts and eligible groups are authorized. Four active FloodWaits run through 06:22:07 UTC. Do not restart Telegram before expiry and a fresh cooldown check. Never vote in polls.
- Do not manually release the Search/Exposure advisory lock or change provider cooldowns/backoffs. A provider query was recorded at 05:12 UTC; allow natural completion before considering any restart.
- Public discovery for Pinterest, Snapchat, and Airbnb is disabled (`NATIVE_PUBLIC_DISCOVERY_ENABLED=0`). PostgreSQL recently sampled at 221.92% CPU and 2.449/4 GiB memory while other workloads were also busy. Keep optional spidering off until resource headroom is demonstrated.

## Checklist snapshot

- Phase 1 split/rename is deployed; no old `collector_light`, `collector_lowrisk`, or `instagram_ingest` references were found in `docker/` or `src/`.
- Beeper: 10,811 messages/12h.
- Lemon8: 108 posts/12h. Extension v1.23.123 last recorded 165 stored media candidates and 257 deduplicated repeats/12h.
- Pinterest: 24 pins and 37 boards/12h; 2 pins have descriptions and none have destination, repin, or comment fields. The user's own following graph has 27 edges (one profile completed; 26 failed/empty public pages). 715 generic profile targets are parked while public discovery is disabled; zero live native claims.
- Snapchat: 13 stories, 40 Spotlights, and 19 highlights/12h. No Snapchat following edges exist; `/web` exposes no following route and private Friends remain out of scope. Nine generic profile targets are parked.
- Airbnb: 3 listings, 3 hosts, and 14 reviews/12h. User explicitly approved Wishlist seeding, but the existing signed-in `/wishlists` tab currently exposes zero room links and zero Wishlist cards. The existing extension capture path handles that route. A user prompt asks them to open a populated Wishlist in the same tab if one exists; response is pending. No Wishlist-sourced rows exist yet.
- Telegram: 4 active DB accounts; account filter unset. 0 discussion visits/12h and 5/24h (4 complete, 1 `ChannelPrivateError`), with a 24h per-channel dedup gate. 639 member rows refreshed across 104 chats/12h (roles 639, join dates 180, usernames 482, display names 553). Seven join/leave rows exist; one was added/updated in the recent window. Poll-voter table has 262 historical rows, last refreshed 2026-09-28. Three polls refreshed/12h were all anonymous; 125 open non-anonymous polls are old and none were refreshed/12h. Recheck after cooldown; do not cast votes.
- Search: 10 active terms exactly (3 usernames, 3 keywords, 2 hashtags, 2 full names), none zero-result. Last 12h: 166 queries, 29 `filetype:` queries, 7,622 results, 42 indexed documents (41 with text). All-time documents: 64 text-extracted PDFs, 11 text files, 8 JPG metadata rows, 1 metadata-only DOC. Queue progressed to 56 complete / 233 pending / 0 claimed; latest query was 05:12 UTC.
- Verified-account filtering is deployed for Instagram, Snapchat, Pinterest, X, Threads, TikTok, and YouTube. Explicit badge markers are excluded; unknown badge state remains eligible (best effort).

## Next steps

1. Continue monitoring Search queue progress and rate-limit events with read-only aggregates; leave provider coordination untouched.
2. Await the user's Wishlist response. If a populated list is opened in the existing Airbnb tab, verify extension capture counts only; do not print list names or account data.
3. Shortly after 06:22:07 UTC, recheck Telegram FloodWaits/cooldowns. If none remain active, restart only `collector_telegram` to load current source, then verify four workers, discussion visits, member refreshes, join/leave events, and any public poll-voter update. Never cast votes.
4. Keep Pinterest/Snapchat/Airbnb public discovery disabled while PostgreSQL/host load is high. Preserve follow-first behavior and the one-tab limit.
5. After all gates, run the required 15-minute health/rate-limit soak and a per-container 12h row report. Write the plan summary to `/tmp/unifiedcollector-plan-complete-20261014.md` only when verification is complete.

## 05:19 UTC addendum

Search queue has advanced to 63 complete / 226 pending / 0 claimed, with the latest query at 05:18 UTC. No Instagram 401 or Lemon8 429 events occurred in the sampled last 15 minutes; the latest Lemon8 429 was at 02:38 UTC and its 903-second cooldown expired. Three advisory locks were granted system-wide; leave them and provider coordination untouched. PostgreSQL remained at 271–280% CPU across two 37-second samples, with host CPU/I/O pressure and other high-CPU workloads; keep optional native spidering disabled.

## 05:22 UTC addendum

Database confirms four Telegram FloodWait rows expire at 06:22:07 UTC. Five discussion-visit dedup rows expire between 06:11:31 and 06:41:46 UTC, so after the restart at least the older channel rows can become eligible; the latest dedup entries age out by 06:41:46.

## 2026-10-16 05:26 UTC addendum

Search health is good; the 05:25 UTC query has 9 documents claimed (63 complete / 217 pending). Let this batch finish without restart; no rate-limit events occurred in the sampled last 15 minutes.

## 2026-10-16 05:27 UTC addendum

Search batch advanced from 9 to 2 live claims: 68 complete / 219 pending. Let the last two finish naturally.

## 2026-10-16 05:27 UTC addendum

The 05:25 Search batch settled: 68 complete / 221 pending / 0 claimed. Two rows returned to pending retry. Search stayed healthy; the sampled last 15 minutes had 2 queries and no rate-limit events.
