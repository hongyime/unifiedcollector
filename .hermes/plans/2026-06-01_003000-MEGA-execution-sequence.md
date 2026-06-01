# MEGA TASK LIST -- interwoven execution sequence for all 3 plans

**Date:** 2026-06-01
**Repo:** C:\unifiedcollector
**Status:** PLAN ONLY (master sequencer). Execution starts when Bryan says go.

Merges:
- P1 = `2026-06-01_000000-zero-progress-autoheal.md` (watchdog auto-heal)
- P2 = `2026-06-01_001500-collector-semantics-and-dashboard-overhaul.md` (targeting + dashboard)
- P3 = `2026-06-01_002500-whatsapp-standalone-ingest.md` (WhatsApp ingest)

## Sequencing logic (why this order)
1. **Safety + observability first.** Back up, then make errors VISIBLE (verbose errors,
   watchdog gap) before changing collection behavior — so when re-targeting breaks
   something we can actually see it.
2. **Diagnose before fixing.** WhatsApp + dashboard 500s need root-cause confirmation
   first (cheap, read-only) so fixes are targeted.
3. **Config-only changes before code changes.** File-authoritative target/env edits
   (restart) are low-risk and reversible; batch them. Code + image rebuilds (Pattern B)
   are heavier; batch those into single rebuilds per image.
4. **Frontend last in each track.** One `npm build` + dashboard image bake covers ALL
   dashboard fixes at once (don't rebuild per-fix).
5. **Famous-filter = code.** Needs collector code (sub/star/follower count fetch). Ships
   in the collector-image rebuild alongside Strava/Telegram code.

## Decisions baked in
- Famous caps OVERRIDE seeds (no allowlist): YouTube <4k subs, GitHub <1k stars,
  Lemon8 <1k followers, Instagram <1k followers, TikTok <1k followers (spider + seed).
- Strava = NO famous filter (spider everyone w/ media/map).
- Telegram dialogs-spider approved (FloodWait-safe, bounded backfill).
- Schema/data reset AUTHORIZED (purge wrong rows, re-seed clean, pg_dump first).
- Search terms (139 yearbook/OSINT dorks) already written to config/sources/search.targets.
- Stay inside C:\unifiedcollector. Bedrock/OpenCode unaffected.

---

## PHASE 0 — Safety net (do first, blocks nothing downstream until done)
- [ ] 0.1  pg_dump full backup of `unifiedcollector` DB (per backup hardening) before ANY
          row purge or schema change. Verify dump size > 0 and restorable.
- [ ] 0.2  `git status` clean / commit current state as a restore point. Confirm on `main`,
          pushed (last known good = 30e3773).
- [ ] 0.3  Snapshot current `collection_targets` (COPY to a backup table) so re-seeding is
          reversible.

## PHASE 1 — Observability (P1 + P2-B1/B7) — make failures visible FIRST
- [ ] 1.1  [P2-B1] Add global exception handler in `src/dashboard/api.py`: when
          `_AUTH_DISABLED`, return real exc type+msg+traceback tail as JSON (localhost
          only). Structured WARNING+ error logging (method/path/exc/stack).
- [ ] 1.2  [P2-B7] Frontend: log failed requests (status+URL+body) to console in dev.
          (Ships with the Phase 5 frontend bake.)
- [ ] 1.3  [P1] Inspect collector `run()` contract — read `src/collectors/__init__.py`
          base + youtube/lemon8/tiktok `run()` to decide persisted-count signal
          (approach A return-int vs B before/after COUNT). READ-ONLY.
- [ ] 1.4  [P1] Implement zero-progress detection in `src/worker/__init__.py`:
          `_zero_progress_streak`, knobs `COLLECTOR_ZERO_PROGRESS_LIMIT=5` /
          `_HARD_LIMIT=12`; run-loop accounting (targets>0 & persisted==0 -> increment;
          progress OR no-targets -> reset); watchdog soft tier (rebuild collector +
          relaunch) and hard tier (process-exit -> Docker restart, guarded).
- [ ] 1.5  [P1] **Critical:** move `get_collector(source)` INTO the relaunch path so an
          escalated relaunch gets a fresh collector (clears wedged pool/session handle).
- [ ] 1.6  [P1] Tests: extend `tests/test_watchdog_autoheal.py` (streak inc/reset,
          soft+hard escalation, dedup-exhausted no-thrash). `pytest` on host.
- [ ] 1.7  [P2-A3 dedup root fix] Make sources mark fully-collected targets `completed` so
          dedup-exhausted idle becomes no-targets reset (prevents false-positive
          escalation). Verify `_load_targets` filter (pending/error only).

## PHASE 2 — Diagnostics (P3 + P2-B2) — root-cause before fixing, all READ-ONLY
- [ ] 2.1  [P3] Check main collector logs: is a `whatsapp` source task alive? `WhatsApp
          RabbitMQ connected` vs `no collection mode` vs crash. Confirm worker launches it.
- [ ] 2.2  [P3] RabbitMQ inspect (rabbitmq container): exchanges (whatsapp.events?),
          queues (unifiedcollector.messages depth + consumer count), wac_user vhost.
- [ ] 2.3  [P3] Bridge inspect: env (rabbit url/vhost/exchange, publish enabled?), logs for
          publish attempts/errors. Confirm bridge image source path (archive vs live).
- [ ] 2.4  [P3] Send a test WhatsApp message -> watch bridge log -> queue depth ->
          collector log -> whatsapp_messages count. Identify the broken stage.
- [ ] 2.5  [P2-B2] Confirm `wa_user_profiles` missing (already verified NULL) — note for
          the graceful-empty guard in Phase 5.

## PHASE 3 — Config-only re-targeting (P2 Track A) — file edits + restart, reversible
- [ ] 3.1  [P2 reset] Purge wrong `collection_targets` + their collected rows: telegram
          public channels (CoinDesk/durov/SpaceX/...), youtube 4 demo channels, github
          famous repos. (Backup taken in 0.3.)
- [ ] 3.2  [P2-A1] telegram.targets -> `dialogs` sentinel (enumerate connected accounts'
          real chats), remove predefined channels. Confirm run_targets honors `dialogs`.
- [ ] 3.3  [P2-A2] strava.targets -> add `feed`; strava.env -> `STRAVA_FOLLOW_SPIDER=1`,
          `STRAVA_USE_WEB=1`.
- [ ] 3.4  [P2-A3] youtube.targets -> import all 492 channel IDs from
          `archive/youtubetoolkit/data/subscriptions.json`. youtube.env ->
          `YOUTUBE_FAMOUS_SUB_CAP=4000`.
- [ ] 3.5  [P2-A4] github.targets -> replace famous repos with your follows/owned/starred
          graph. github.env -> `GITHUB_FAMOUS_STAR_CAP=1000`.
- [ ] 3.6  [P2-A5] lemon8.env -> `LEMON8_FAMOUS_FOLLOWER_CAP=1000` (keep 60 personal
          handles; DB seed was empty).
- [ ] 3.7  [P2-A8] tiktok.env -> `TIKTOK_FAMOUS_FOLLOWER_CAP=1000` (spider-discovered).
          instagram.env -> `INSTAGRAM_FAMOUS_FOLLOWER_CAP=1000` (for when re-enabled).
- [ ] 3.8  [P2-A6] search.targets -> DONE (139 terms written). Verify loader picks them up.
- [ ] 3.9  Restart affected collectors per Option-A; env_file changes need `compose up`,
          NOT restart. Sequential restarts (youtube/tiktok never concurrent w/ pg_dump).

## PHASE 4 — Collector code (P2 Track A code + P3 fixes) — ONE collector-image rebuild
- [ ] 4.1  [P2-A2] Strava: confirm/extend `collect_following_roster()` does BOTH following
          AND followers; media/map-only filter on spidered activities.
- [ ] 4.2  [P2-A1] Telegram: ensure forward-source + reaction-list + participant seed
          enqueue ENABLED; FloodWait handling + bounded backfill depth.
- [ ] 4.3  [P2 famous-filter] Implement sub/star/follower count fetch + cap skip in
          youtube/github/lemon8/tiktok/instagram collectors (filter OVERRIDES seed).
- [ ] 4.4  [P3] Apply WhatsApp fix from Phase-2 diagnosis (bridge publish / consumer start
          / persist schema). Handle `@newsletter` (channel) JIDs in `_upsert_chat`.
- [ ] 4.5  [P3] Optional: split `collector_whatsapp` container (mirror youtube/tiktok);
          add whatsapp to main's COLLECTOR_DISABLED_SOURCES + env parity.
- [ ] 4.6  Verify: `ast.parse` + `ruff E9,F821` on all touched .py. Rebuild collector
          image(s) (Pattern B). `compose up` collectors sequentially.

## PHASE 5 — Dashboard frontend (P2 Track B) — ONE npm build + dashboard-image bake
- [ ] 5.1  [P2-B2] `/whatsapp/users` + history: guard missing tables with `to_regclass`,
          return [] + meta hint. (Also bake the Phase-1.1 backend error handler now.)
- [ ] 5.2  [P2-B3] TargetsPage: render `t.target_id` (not `t.target`); add columns
          target_id/source/status/priority/last_collected. Fix types.ts.
- [ ] 5.3  [P2-B4] CollectorsPage: show last-seen + alive/stale indicator; REMOVE
          pagination (all ~11 fit one table).
- [ ] 5.4  [P2-B5] Strava on Dashboard tab: SOURCES includes strava; StravaFeedPage shows
          athletes+activities (populated once Phase-3.3 feed-spider runs).
- [ ] 5.5  [P2-B6] MediaBrowser: filter dropdowns (source, media type, date range,
          has-media); wire to `/media` query params (add backend params if missing).
- [ ] 5.6  `npm run build` (tsc+vite) — pre-broken TelegramAccountsPage already fixed
          earlier; build must stay green. Rebuild dashboard image, `compose up dashboard`.

## PHASE 6 — Verify, observe, commit
- [ ] 6.1  Per-source production check: telegram pulling YOUR dialogs (not @coindesk);
          strava feed activities w/ media/map; youtube/github/lemon8/tiktok skipping
          famous (count rows, eyeball entities). WhatsApp: test msg -> row in seconds;
          image -> disk + media_items.
- [ ] 6.2  Dashboard: raw errors on failure; targets show target_id; collectors one page
          w/ liveness; media filters work; strava on dashboard; WA Users/Links/Stats
          populated.
- [ ] 6.3  Auto-heal: confirm no false-positive restarts on dedup-idle over multi-hour
          window; (if reproducible) force a wedge -> confirm soft/hard recovery.
- [ ] 6.4  Observe 1+ full cycle per source. Backfill (telegram + whatsapp) rate-limited,
          no FloodWait/ban.
- [ ] 6.5  Update ops skill ref (autoheal "KNOWN GAP" -> FIXED w/ new knobs; targeting
          semantics; whatsapp ingest pipeline). Commit + push each phase; final push.

---

## Critical-path dependencies
- 0.1 backup BEFORE 3.1 purge (non-negotiable).
- 1.1 error handler BEFORE 5.1 (5.6 bakes it).
- 1.7 dedup-completed fix BEFORE 1.4 hard-tier goes live (prevents thrash).
- 2.x diagnosis BEFORE 4.4 whatsapp fix (don't fix blind).
- 3.3 strava feed-spider BEFORE 5.4 (dashboard needs data to show).
- Phase 4 = one collector image rebuild; Phase 5 = one dashboard image rebuild. Don't
  rebuild per-task.

## Rollback
- Targets: restore from 0.3 snapshot table.
- DB: restore 0.1 pg_dump.
- Code: git revert to 0.2 restore point (30e3773).
- Each phase committed separately so partial rollback is clean.
