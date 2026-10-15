# Agent State

Current task: Complete the UnifiedCollector Full Plan (2026-10-14). This is the active checklist. Reconciled for visibility at 2026-10-15 11:17 UTC; latest runtime evidence below is 11:13 UTC (this edit did not refresh runtime data).

## User decisions and operating rules

- Track Pinterest, Snapchat, and Airbnb in Phase 2 against the original plan's Phase 5a–5c acceptance criteria.
- Start Pinterest/Snapchat from followed identities and Airbnb from listings linked to followed identities. Broaden only when preferred queues drain and capacity remains.
- Skip accounts only with explicit verified/blue-badge evidence on Instagram, Snapchat, Pinterest, X, Threads, TikTok, and YouTube. Unknown stays eligible.
- Telegram scope is all eligible groups and all four current accounts, plus any configured later. No allowlist.
- Keep at most one extension-managed tab per social. Respect provider cooldowns; do not interrupt Exposure or release its Search mutex. Do not touch Spider/SpiderFoot.
- `[x]` means acceptance verified; `[~]` means active or awaiting evidence; `[ ]` means not complete. Runtime facts retain their sample time.

## Master checklist — evidence through 2026-10-15 11:13 UTC

- [x] **Phase 1 — split/rename.** Exposure/Lemon8 and Search/Strava are separated; `instagram_ingest` became `browser_ingest`. Fixes are pushed. Replacement services were running with separate configs/logs; no retired service names remain in active code/config/docs. Old containers are stopped orphans.
- [x] **Phase 2a — Beeper.** Fix `4648f029` is verified: 88 messages/15m and 408/1h at 10:08, with no source-health crashes. The plan's separate final soak is still open.
- [~] **Phase 2b — Lemon8 API + extension.** Fix `1f7b1fcb` is loaded. At 11:08, the API cursor was still `running` from 10:47; source health last advanced at 10:53 with zero crashes. Rolling 12h: 32 posts (21 titles, 19 descriptions), below the 50+ gate. Same Lemon8 tab was on the configured topic feed. CDP 9336 showed no extension runtime in 8 page contexts; no bridge events in 15m and zero extension candidates in 12h (2,368 historical, last Sep 7). Profile inspection found no UnifiedCollector registration. Do not restart the running cycle; resolve the profile/extension path and verify API-extension dedupe.
- [~] **Phase 2c — Pinterest (plan 5a).** At 11:04: 143 pins/12h (142 titles, 1 description, 142 images, no destinations or repin/comment counts); zero native Playwright provenance. Collector profile has no Pinterest auth markers. Follow queue is not drained (9 pending/183 failed; 715 broad profiles parked at 10:50). Need authenticated native pins/boards/profile with required fields; keep follow-first.
- [~] **Phase 2c — Snapchat (plan 5b).** At 11:04: 1 story, 15 Spotlights, 23 Highlights/12h; story/Spotlight titles absent, 20/23 Highlight titles absent, thumbnails absent. At 10:50, 10 followed targets pending, 3 complete, 197 failed; 10 broad targets parked. Keep follow-first and complete public content/profile fields.
- [~] **Phase 2c — Airbnb (plan 5c).** At 11:04: 3 listings/12h; all had title, description, photos, price, amenities, availability, rating, and host; 2 had location. All 30 reviews had text/rating/date/reviewer ID. Six hosts had names but no bios; 3 had avatars, ratings, and response rates. Three host retries failed `graphql_query_failed`; next scheduled retry 13:50:53 UTC. Do not force retry.
- [~] **Phase 3 — Telegram.** At 10:56: 5 discussion visits/12h across 2 accounts; 5,765 membership rows refreshed (5,158 with join dates); 4 joins, no leaves, zero fresh voter rows. All 4 account FloodWaits were active at 11:01 through Oct 16 06:02–06:22 UTC. Poll-handler fix `d00a3849` is pushed but not deployed. Wait through cooldowns, then restart only `collector_telegram` and verify all four features across authorized groups/accounts.
- [~] **Phase 4 — Search.** At 11:13, source code covers Snapchat stories/Spotlights/Highlights and Airbnb listings/reviews. Current 10 terms come from Instagram, Pinterest, TikTok, YouTube; all are due and never searched. Shared tables showed 193 queries/8,051 results and zero documents at 10:56, but writes were not attributable to Search. Exposure holds `collector_mutex:search_engine`; leave it and its session untouched. Verify Search-owned terms/results/documents after natural release.
- [~] **Cross-cutting — verified-account exclusion.** Explicit metadata filters exist for the seven requested platforms; 14 regression cases passed (`eeec5845`), with Pinterest card filtering (`22610b92`). Live verification remains open because the extension is inactive in the collector profile and Pinterest/Snapchat auth is absent there.
- [ ] **Final gate.** After phase gates pass: verify all services, complete a fresh 15-minute no-error/no-new-rate-limit soak, refresh per-container rolling 12-hour counts, and write `/tmp/unifiedcollector-plan-complete-20261014.md`. At 10:56 all Compose services were up; that point check is not the final soak.

## Next actions, in dependency order

1. Let Lemon8 finish naturally; then refresh output, rate-limit state, extension capture, and dedupe. Fix/point to the extension-enabled profile without opening duplicate social tabs.
2. Reconcile Pinterest/Snapchat login state in the collector profile and finish native follow-target collection; broaden only after preferred queues drain.
3. Let Airbnb's scheduled 13:50:53 UTC retry occur; then check host/profile completeness.
4. Wait for all Telegram FloodWaits through Oct 16 06:22 UTC. After expiry, deploy the pushed handler fix and verify discussion visits, voters, member fields, and join/leave events across all authorized accounts/groups.
5. Wait for Exposure to release the Search mutex naturally; verify Search-owned dynamic terms and indexed documents.
6. After all above pass, run the final soak/count report and write the requested `/tmp` summary.

No runtime, browser, container, queue, or provider state was changed during this checklist reconciliation. No tests were run.
