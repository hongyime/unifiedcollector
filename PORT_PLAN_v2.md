# Unified Collector — Port Plan v2

**Status:** Planning complete, no code written yet.
**Last updated:** 2026-05-26
**Owner:** Bryan (Prawn)
**Architect session:** Hermes Agent (Claude Opus 4.7 via Bedrock)

---

## TL;DR

Port ~142k LOC of features from 9 toolkit folders into thin `src/collectors/<x>.py` files,
backed by 8 new `src/core/` modules. Add a Beeper Matrix collector as a redundant data
path for chat platforms. Use parallel Hermes agents (max 4 concurrent) to execute.
Threshold for archiving a toolkit: 95% feature parity. Drop all outbound features.

---

## Locked-in Decisions

1. **Redundancy model:** Option A — parallel tables. `matrix_events` lives alongside
   `telegram_messages` and `whatsapp_messages`. No dedupe layer. Read-time correlation
   via SQL views if/when needed.
2. **Beeper coverage:** All 2055 rooms regardless of native overlap. Beeper is BOTH
   (a) redundant layer for WA + TG and (b) primary layer for iMessage/Signal/Discord
   DMs/etc.
3. **DROP rules (per-collector list):** No outbound features anywhere. Specifically
   drop send/reply/react/edit/delete/typing/read-receipt/bot-handler. KEEP read-only
   ingestion of all events including reactions/edits/deletes (as state changes).
4. **Wave order:** Wave 0 (cross-cutting core) → Wave 1 (Beeper, dedicated session) →
   Wave 2 (toolkit ports easy→hard) → Wave 3 (decommission at 95%).
5. **Per-wave research agents:** Each wave gets a research agent BEFORE port agents.
6. **Spider generalisation:** spider_discover.py is generalised upfront for IG, TT,
   Strava, GitHub, Lemon8 (not just GitHub).
7. **Tor proxy scope:** opt-in module, only used by github + search + website.
8. **Telegram common-chat membership:** new table `telegram_account_chat_membership`,
   refreshed daily via `iter_dialogs()` across 4 accounts.
9. **PARITY regen:** automated by each port agent at end of its task. Plus a weekly
   full regen cron.
10. **Instagram port:** split across 2 parallel agents (auth+profile / posts+spider).
11. **WSL bump to 8GB:** at the END, just before agent dispatch. Not now.

---

## Architecture Layers

```
┌──────────────────────────────────────────────────────────────┐
│ L5 — Dashboard / Query (frontend exists, query views per     │
│      use case; no JOIN-on-write per Option A)                │
├──────────────────────────────────────────────────────────────┤
│ L4 — Storage                                                 │
│      Postgres: per-platform tables + matrix_events parallel  │
│      Z: drive: media files, per-platform tree                │
├──────────────────────────────────────────────────────────────┤
│ L3 — Collectors (the thin layer — needs fattening)           │
│      src/collectors/{instagram,telegram,whatsapp,tiktok,     │
│        lemon8,strava,youtube,github,search,website,matrix}.py│
├──────────────────────────────────────────────────────────────┤
│ L2 — Shared core (mostly exists — needs 8 gaps filled)       │
│      see "Wave 0 modules" section                            │
├──────────────────────────────────────────────────────────────┤
│ L1 — Infra: docker-compose (collector, scheduler, postgres,  │
│      redis, rabbitmq, tor, dashboard, backup, wa_bridge_1+2, │
│      [matrix_collector])                                     │
└──────────────────────────────────────────────────────────────┘
```

---

## The 9 Toolkits — Port Status

| # | Source | Toolkit LOC | Unified LOC | Ratio | Beeper bridge? | Notes |
|---|--------|-----------:|-----------:|------:|----------------|-------|
| 1 | instagram | 53,746 | 1,235 | 43.5x | NO (DM bridge but we collect posts) | Boss fight |
| 2 | telegram (combined) | 22,181 | 609 | 36.4x | YES | Two source folders: telegramtoolkit + telegramcollector |
| 3 | whatsapp (collector) | 20,496 | 623 | 32.9x | YES | TS Baileys bridge |
| 4 | search | 5,282 | 159 | 33.2x | NO | Tor pattern |
| 5 | tiktok | 15,245 | 616 | 24.7x | NO | Cookie + ByteDance fingerprint |
| 6 | strava | 11,874 | 575 | 20.7x | NO | GPS streams |
| 7 | lemon8 | 16,814 | 1,262 | 13.3x | NO | ByteDance family |
| 8 | github | 3,266 | 446 | 7.3x | NO | Smallest delta |
| 9 | youtube | ~500-1500 | 763 | TBD | NO | PARITY says 0 but folder exists |
| 10 | website | ~1500-3000 | (in src) | TBD | NO | Add to PARITY |
| 11 | **matrix** | 0 (net-new) | 0 | new | (is itself) | Beeper, ~5k LOC |

**Telegram source folders (both real, complementary):**
- `telegramtoolkit/` — single-process tool. Has unique: feature_registry, account_health,
  login_verifier, parallel_processor, scan_targets, console UI
- `telegramcollector/` — microservices architecture. Has: backfill_worker + realtime_worker,
  processing_queue, topic_manager, login_bot, link_discovery, user_intelligence, richer
  tests including membership_tracker
- **Use telegramcollector as primary base; cherry-pick from telegramtoolkit**

---

## Wave 0 — Cross-Cutting Core Modules

Priority by # of consumers (most leverage first):

| Order | Module | Consumers | LOC saved | Status |
|-------|--------|----------:|----------:|--------|
| 1 | media_download.py | 7 | ~3000 | NEW (extends subprocess_downloader) |
| 2 | spider_discover.py | 6 (generalised) | ~2000 | NEW |
| 3 | adaptive_rate.py | 5 | ~1500 | NEW (extends rate_limit) |
| 4 | dedupe_hash.py | 5 | ~800 | NEW |
| 5 | account_quota.py | 4 | ~600 | NEW (extends account_pool) |
| 6 | matrix_client.py | 1 (Beeper) | n/a | NEW |
| 7 | tor_proxy.py | 3 (gh+search+web) | ~300 | PARTIAL (tor container exists) |
| 8 | auth_session.py | 1 (IG) | ~400 | DEFER (port inside IG batch) |

Existing src/core/ modules to keep/leverage:
account_pool, account_state_repository, base_collector, bot_pool, checkpoint,
circuit_breaker, dlq_consumer, drive_check, face_matcher, face_processor,
file_naming, hub_notifier, human_rate_limiter, profile_access,
profile_photo_tracker, rate_limit, resilience, search_cache,
subprocess_downloader, url_filter

---

## Wave 1 — Beeper Matrix Collector (Dedicated Session)

Phases (sequential, NOT in parallel with other waves — dedicated session per user request):

- **Phase 0** — Read-only proof (1-2 days): auth as new device, key backup recovery,
  /sync 2055 rooms, classify by bridge_type, NO DB writes
- **Phase 1** — Schema + writer (2-3 days): 4 new tables (matrix_rooms, matrix_events,
  matrix_users, matrix_sync_state); live /sync; encrypted media decrypt to Z:
- **Phase 2** — Backfill (runs for days, no dev): priority-ordered historical pull
  of 2055 rooms with rate limiting and resumable cursors
- **Phase 3** — Observability widgets (1 day): "Beeper sees, native doesn't" diff
  views; lag monitoring; bridge health dashboard

DROP for Beeper:
- All outbound (send/react/edit/redact)
- Typing indicators, read receipts (could be ADDED later as room-state events for analytics)

---

## Wave 2 — Toolkit Ports (Easy → Hard)

Each port = a research agent + one or two port agents. Output: PARITY ratio drops,
a `src/collectors/<x>.py` becomes "fat" (5-10k LOC), tests added.

| Order | Source | Strategy | Agents | Est. wall time |
|-------|--------|----------|--------|----------------|
| 1 | github | Light port + spider via core | 1 | 1-2 days |
| 2 | youtube | Polish + add OAuth/subs from toolkit | 1 | 1 day |
| 3 | website | Standard port + tor_proxy use | 1 | 1-2 days |
| 4 | search | Tor pattern, search_cache reuse | 1 | 1-2 days |
| 5 | strava | GPS streams + leaderboards | 1 | 2 days |
| 6 | lemon8 | ByteDance fingerprint stack | 1 | 2 days |
| 7 | tiktok | Reuse lemon8 fingerprint module | 1 | 2 days |
| 8 | whatsapp | Strip outbound, integrate Baileys events | 1 | 2-3 days |
| 9 | telegram | Multi-account parallel + membership | 1 | 2-3 days |
| 10 | instagram | **2 parallel agents (split)** | 2 | 4-5 days |

DROP per source (initial; refined during research):
- **All:** outbound send/reply/react/edit/delete/typing/read-receipt
- **All:** bot command handlers, conversational frameworks
- **Telegram:** send_photos, bulk_sender service
- **WhatsApp:** sendMessage, presence updates
- **Instagram:** DM/inbox features (post-only collector)

---

## Wave 3 — Decommission

Threshold: **95% feature parity** (per PARITY_MATRIX) before archiving.

Archive procedure:
1. Move `<src>toolkit/` to `archived_toolkits/<src>_archived_<YYYYMMDD>/` (don't delete)
2. Update `.gitignore` if needed
3. Regenerate PARITY_MATRIX (toolkit removed from scope)
4. Commit with message `chore: archive <src>toolkit (95% parity)`

---

## Parallelisation Plan — Agent Batches

Max 4 agents concurrent (Hermes config). Each batch = ~1-2 days wall clock.

**Batch 1 — Wave 0 cross-cutting (4 agents):**
- A: media_download.py
- B: spider_discover.py (generalised)
- C: adaptive_rate.py
- D: dedupe_hash.py

**Batch 2 — Wave 0 cont. + research (4 agents):**
- A: account_quota.py
- B: tor_proxy.py
- C: Wave 2 research agent (per-source feature gap reports)
- D: Wave 1 Phase 0 (Beeper read-only proof)

**Batch 3 — Wave 1 build + Wave 2 small (4 agents):**
- A: matrix_client.py + matrix_collector Phase 1
- B: github port
- C: youtube port
- D: website port

**Batch 4 — Wave 2 mid (4 agents):**
- A: search port
- B: strava port
- C: lemon8 port
- D: matrix_collector Phase 3 observability

**Batch 5 — Wave 2 heavy (4 agents):**
- A: tiktok port
- B: whatsapp port
- C: telegram port (incl. common-chat membership)
- D: dashboard widgets for new tables

**Batch 6 — Instagram (2 agents focused, 2 spare):**
- A: instagram auth + profile + sessions
- B: instagram posts + spider + Playwright
- C: PARITY regen + cleanup
- D: integration tests across all collectors

Total estimate: ~6 batches × 1-2 days each + Beeper Phase 2 backfill (passive) = **2-3 weeks calendar** if executed continuously.

---

## Per-Wave Research Agent Pattern

Before each batch that touches a new toolkit:
1. **Research agent** (read-only, 15-30 min): scans toolkit, classifies features by
   keep/drop/exists-in-core, outputs `WAVE_<N>_RESEARCH.md` with priority list
2. **Port agent(s)** (write, 1-2 days): consume research doc as context, implement
   features, write tests, run them
3. **Auto-regen step** at port-agent completion: regenerate PARITY_MATRIX, diff old
   vs new, fail task if ratio didn't improve meaningfully (>10% drop)

---

## Database Schema Additions

### matrix_events tables (Wave 1)
```sql
matrix_rooms (id, matrix_room_id, name, topic, bridge_type, bridge_metadata,
              member_count, is_dm, encrypted, last_event_ts, created_at, collected_at)
matrix_events (id, matrix_event_id, room_id, sender_mxid, event_type, msg_type,
               body, formatted_body, media_path, media_mime, media_size,
               reply_to_event_id, thread_root_event_id, edits_event_id, redacted,
               origin_ts, collected_at, raw_event)
matrix_users (id, mxid, display_name, avatar_mxc, avatar_local_path,
              is_bridge_user, bridge_type, external_id, metadata, collected_at)
matrix_sync_state (user_mxid, next_batch, last_sync_at, device_id,
                   access_token_encrypted)
```

### telegram_account_chat_membership (Batch 5)
```sql
telegram_account_chat_membership (
  account_id VARCHAR,
  chat_id BIGINT,
  chat_type VARCHAR,  -- 'channel' | 'group' | 'supergroup' | 'private'
  is_admin BOOLEAN,
  joined_at TIMESTAMPTZ,
  last_seen_in_dialogs TIMESTAMPTZ,
  PRIMARY KEY (account_id, chat_id)
)
```

Refreshed daily via `iter_dialogs()` across all 4 accounts.

---

## Production Schedule

See `PRODUCTION_SCHEDULE.md` for the full cron table. Highlights:

- matrix_collector + whatsapp = continuous (no cron)
- telegram = every 5 min (4 accounts in parallel)
- github = every 30 min, youtube = hourly, strava = 2h
- instagram = every 4h (cautious), tiktok = 3h, lemon8 = 6h
- search = 6h (Tor expensive)
- maintenance jobs: daily 02:00-05:00 SGT
- weekly PARITY regen: Sun 02:00 SGT

---

## Open Items (Not Yet Decided)

- Whether to KEEP Telegram + WhatsApp media (you said "drop features that send photos
  on telegram and WhatsApp we don't need" — this means RECEIVE and store ALL media,
  but never SEND. Confirm interpretation.)
- Whether matrix_events should also store typing/presence/read-receipts (probably
  no — too much noise; can be added later as state events).
- Beeper key backup setup: needs you to confirm Beeper Cloud key backup is enabled
  (Beeper Desktop → Settings → Encryption → Online Key Backup).

---

## Resume Procedure

If a future session needs to pick up here:

1. Read `PORT_PLAN_v2.md` (this file) end-to-end
2. Read `PRODUCTION_SCHEDULE.md`
3. Run `cat PARITY_MATRIX.md | head -30` for current parity status
4. Check `tmp/INVESTIGATION_REPORT.md` if present
5. Check Hermes skill `unifiedcollector-port-roadmap` for procedure details
6. Read `claude analysis of all toolkits.txt` for original toolkit inventory
7. Resume from the next un-executed batch in the parallelisation plan

Memory entries for the next agent are saved in Hermes memory store under "unifiedcollector".
