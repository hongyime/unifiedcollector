# Agent State

Current task: Complete the UnifiedCollector Full Plan (2026-10-14). This is the active checklist. Reconciled 2026-10-15 11:28 UTC; latest runtime evidence is 11:23 UTC.

## User decisions and operating rules

- Track Pinterest, Snapchat, and Airbnb in Phase 2 against the original plan's Phase 5a–5c acceptance criteria.
- Start Pinterest/Snapchat from followed identities and Airbnb from listings linked to followed identities. Broaden only when preferred queues drain and capacity remains.
- Skip accounts only with explicit verified/blue-badge evidence on Instagram, Snapchat, Pinterest, X, Threads, TikTok, and YouTube. Unknown stays eligible.
- Telegram scope is all eligible groups and all four current accounts, plus any configured later. No allowlist.
- Keep at most one extension-managed tab per social. Respect provider cooldowns; do not interrupt Exposure or release its Search mutex. Do not touch Spider/SpiderFoot.
- `[x]` means acceptance verified; `[~]` means active or awaiting evidence; `[ ]` means not complete. Runtime facts retain their sample time.

## Master checklist — evidence through 2026-10-15 11:23 UTC

- [x] **Phase 1 — split/rename.** Exposure/Lemon8 and Search/Strava are separated; `instagram_ingest` became `browser_ingest`. Fixes are pushed. Replacement services run with separate configs/logs; no retired service names remain in active code/config/docs. Old containers are stopped orphans.
- [x] **Phase 2a — Beeper.** Fix `4648f029` is verified: 88 messages/15m and 408/1h at 10:08, with no source-health crashes. The plan's separate final soak is still open.
- [~] **Phase 2b — Lemon8 API + extension.** Fix `1f7b1fcb` is loaded. At 11:23, cursor remains `running` since 10:47 and source health last advanced 10:53; rolling 12h remains 32 posts (21 titles, 19 descriptions), below the 50+ gate. A verified wait found no progress; process is healthy but at 0% CPU. Its configured `COLLECTOR_HANG_TIMEOUT_SECONDS` is 7,200, so the watchdog's nominal timeout would be about 12:53 UTC only if no progress occurs; do not manually restart now. The 11:20 source matrix says extension ingest is inactive (expected v1.23.115; last seen v1.23.108 at 00:51). CDP has one Lemon8 page, two browser UI targets, and a Lemon8-origin service worker, not the extension. No extension candidates in 12h (2,368 historical, last Sep 7). Resolve the profile/extension path and verify API-extension dedupe.
- [~] **Phase 2c — Pinterest (plan 5a).** Fresh count by `collected_at` at 11:19: 60 pins/12h (59 titles, 1 description, 0 destinations, 0 repin/comment counts); no Pinterest browser candidates in that window. Pinterest auth is unverified: the cookie-vault auth-summary allowlist omits Pinterest, so its summary cannot establish whether a session is present. No native Playwright provenance is present. Follow queue was not drained at 10:50 (9 pending/183 failed; 715 broad profiles parked). Need authenticated native pins/boards/profile with required fields; keep follow-first.
- [~] **Phase 2c — Snapchat (plan 5b).** At 11:19: 1 story, 15 Spotlights, 23 Highlights/12h; no story/Spotlight titles, 3 Highlight titles, and no thumbnails. At 10:50, 10 followed targets pending, 3 complete, 197 failed; 10 broad targets parked. Keep follow-first and complete public content/profile fields.
- [~] **Phase 2c — Airbnb (plan 5c).** At 11:19: 3 listings/12h; all had title, description, photos, price, amenities, availability, rating, and host; 2 had location. All 30 reviews had text. Three hosts were refreshed; none had bios, all had avatars/ratings/response rates. Host retry remains scheduled for 13:50:53 UTC; do not force it.
- [~] **Phase 3 — Telegram.** At 11:19: 5 discussion visits/12h (4 completed, 1 aborted), across 2 accounts; zero fresh voter rows. At 11:01 all 4 account FloodWaits were active through Oct 16 06:02–06:22 UTC. Poll-handler fix `d00a3849` is pushed but not deployed. Wait through cooldowns, then restart only `collector_telegram` and verify all four features across authorized groups/accounts.
- [~] **Phase 4 — Search.** At 11:19, shared tables had 202 queries/8,086 results and zero documents/12h. The newest 28 queries are `waterfall` and none match the 10 active dynamic terms; all 10 remain due and never searched. Search source health is stale since Oct 14 16:51. The Search-engine advisory lock is still owned by Exposure (idle session since Oct 14 13:28); do not interrupt it. Verify Search-owned terms/results/documents after natural release.
- [~] **Cross-cutting — verified-account exclusion.** Explicit metadata filters exist for the seven requested platforms; 14 regression cases passed (`eeec5845`), with Pinterest card filtering (`22610b92`). Live verification remains open because extension ingest is inactive. Pinterest auth is unverified because the vault summary does not classify it; Snapchat uses the plan's public unauthenticated endpoints.
- [ ] **Final gate.** At 11:20 the source matrix was reachable; current hour showed 0 rate limits but 1 access error (24h: 12 rate limits, 57 access errors). At 11:18 all Compose services were Up, but this is not the required soak. After phase gates pass: verify all services, complete a fresh 15-minute no-error/no-new-rate-limit soak, refresh per-container rolling 12-hour counts, and write `/tmp/unifiedcollector-plan-complete-20261014.md`.

## Next actions, in dependency order

1. Monitor the live Lemon8 cursor and let its configured watchdog/recovery policy operate; at/after the nominal 12:53 UTC hang threshold, take a fresh sample before deciding on recovery. Resolve the extension-enabled profile without opening duplicate social tabs.
2. Verify Pinterest session use through native Playwright output; finish Snapchat public follow-target collection. Broaden either queue only after preferred targets drain.
3. Let Airbnb's scheduled 13:50:53 UTC retry occur; then check host/profile completeness.
4. Wait for all Telegram FloodWaits through Oct 16 06:22 UTC. After expiry, deploy the pushed handler fix and verify discussion visits, voters, member fields, and join/leave events across all authorized accounts/groups.
5. Wait for Exposure to release the Search mutex naturally; verify Search-owned dynamic terms and indexed documents.
6. After all above pass, run the final soak/count report and write the requested `/tmp` summary.

No runtime, browser, container, queue, or provider state was changed during this audit. No tests were run.
