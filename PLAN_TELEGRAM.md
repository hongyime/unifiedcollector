# Telegram Collector — Build Plan

**Status:** Ralph loop in progress. Started 2026-05-28. Resume from first non-completed checkbox.

## Vision (Bryan, verbatim)

Connect 4+ Telegram user accounts (login via dashboard OR via 3 bots: `bryanseahbot`, `shotsbyseahbot`, `prawnproductionsbot` using `/startcollector`). Scrape all messages and chats and channels per account (even new account) — full backfill. Then monitoring/cron job to monitor and incremental backfill. Spider:
- Each chat → forwarded messages → username + ID + media + text + documents + voice
- Group chats → member list (only if admin enabled or we are admin) + forwarded messages
- Channels → linked discussion groups → JOIN → scrape members + messages → LEAVE immediately (anti-suspicion)
- Reactions → poll vote tallies → reactors (user discovery surface)
- Join/leave events on chats

## Decisions

- **Q1** chat_members.chat_id type → **UUID FK telegram_chats(id)**
- **Q2** reactions cap → **store emoji counts always, store individual reactor list capped at `TELEGRAM_REACTION_USER_CAP=500` per emoji per message**
- **Q3** discussion groups → **always leave**, never persistent join
- **Q4** API_ID/API_HASH → **shared across 3 bots** (per-app, not per-bot)
- **Q5** order → Phase 1 → Phase 2 → run + monitor → Phase 3 (dashboard + bots, blocked on user delivering API creds)

## Existing assets (do NOT rebuild)

- `src/collectors/telegram.py` — 67KB, 40+ methods, has: account pool, multi-worker, backfill, realtime, edit/delete tracking, join/leave events, member list scrape (function exists, not wired), media download (photo/document/voice), stories scan, user profile fetch, dialog enumeration, FloodWait handling.
- `src/core/spider_discover.py` — 622 LOC, generic graph crawler with Redis-backed visited set.
- `src/core/user_change_tracker.py` — 281 LOC, exists but not wired into `_upsert_user_full`.
- `src/core/account_pool.py` — env-based account loader.
- archived `archive/telegramtoolkit/src/managers/{join_groups.py, leave_groups.py}` — has reference logic for join+leave-cleanup pattern. Reuse patterns.

## Known bugs (Phase 1 stops the bleeding)

1. `telegram_chat_members.chat_id` is `bigint` but `telegram_chats.platform_chat_id` is `varchar` → cannot join. Members never get written.
2. `telegram_chats.members_count` always 0 — not populated from `entity.participants_count` in `_upsert_chat`.
3. `telegram_user_changes` has 0 rows — `UserChangeTracker.diff_and_record` never called from `_upsert_user_full`.
4. `telegram_spider_queue` has 0 rows — forward extraction not feeding spider edges.
5. Reactions have no storage table.
6. Polls have no storage table — only "is this a poll?" detection.

## Phase 1 — Schema + wiring fixes (blocking)

- [x] **1.1** Migration: drop `telegram_chat_members`, recreate with `chat_id UUID FK telegram_chats(id) ON DELETE CASCADE, user_id UUID FK telegram_users(id) ON DELETE CASCADE, role varchar(32), joined_at timestamptz, last_seen_at timestamptz, refreshed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (chat_id, user_id)`. Indexes on user_id and refreshed_at.
- [x] **1.2** Migration: create `telegram_reactions` (id UUID PK, message_id UUID FK telegram_messages(id) ON DELETE CASCADE, user_id UUID FK telegram_users(id) ON DELETE SET NULL, emoji varchar(64), is_big bool, added_at timestamptz, refreshed_at timestamptz DEFAULT now(), UNIQUE(message_id, user_id, emoji)). Index on user_id.
- [x] **1.3** Migration: create `telegram_reaction_counts` (message_id UUID PK FK telegram_messages(id) ON DELETE CASCADE, counts jsonb NOT NULL DEFAULT '{}', total_reactions int DEFAULT 0, refreshed_at timestamptz DEFAULT now()). For high-volume reaction summary.
- [x] **1.4** Migration: create `telegram_polls` (message_id UUID PK FK telegram_messages(id) ON DELETE CASCADE, poll_id varchar(255), question text, options jsonb, total_voters int, vote_counts jsonb, is_closed bool, is_anonymous bool, allows_multiple bool, refreshed_at timestamptz DEFAULT now()). Index on poll_id.
- [x] **1.5** Migration: create `telegram_discussion_visits` (id UUID PK, channel_chat_id UUID FK telegram_chats(id), discussion_chat_id UUID FK telegram_chats(id), joined_at timestamptz NOT NULL, left_at timestamptz, members_collected int DEFAULT 0, messages_collected int DEFAULT 0, abort_reason varchar(64)). Index on (channel_chat_id, joined_at DESC).
- [x] **1.6** Apply all migrations in single SQL file `src/db/migrations/add_telegram_phase1.sql`. Verify table creation in postgres.
- [x] **1.7** Patch `_upsert_chat`: write `members_count` from `getattr(entity, 'participants_count', 0)`.
- [x] **1.8** Patch `_upsert_user_full`: call `UserChangeTracker.diff_and_record(old_row, new_row)` before UPSERT, write changes to `telegram_user_changes` if any field differs.
- [x] **1.9** Patch `collect_chat_members`: actually wire it to be called per-chat during backfill cycle. Use new UUID schema.
- [x] **1.10** Patch realtime forward extraction: when `message.forward_from` set, write `forward_from_chat_id`/`forward_from_message_id` AND enqueue `(chat_id_or_user_id, EdgeType.FORWARD, parent_node_id=current_chat_id)` into `telegram_spider_queue`.
- [x] **1.11** Patch realtime listener: register `events.MessageReactionUpdate` handler → upsert `telegram_reactions` rows + bump `telegram_reaction_counts.counts[emoji]++`.
- [x] **1.12** Patch poll detection: when `message.poll` exists, run `GetPollResultsRequest` → write to `telegram_polls`.
- [x] **1.13** Run pytest. Fix per-iteration. AST parse + import bar.
- [x] **1.14** Smoke: docker rebuild collector image, restart, watch one cycle, confirm rows appear in: chat_members, reactions, reaction_counts, polls, user_changes, spider_queue.
- [x] **1.15** Commit Phase 1.

## Phase 2 — Spider deeper (non-blocking, runs after Phase 1 verified)

- [x] **2.1** Discussion group spider: detect `entity.linked_chat_id` for channel entities → if not joined, call `JoinChannelRequest(linked_chat)` → wait random `60-180s` (`TELEGRAM_DISCUSSION_DWELL_MIN/MAX`) → `collect_chat_members(linked_chat)` + `backfill_chat(linked_chat, limit=2000)` → `LeaveChannelRequest` → write `telegram_discussion_visits` row.
- [x] **2.2** Reaction-driven user discovery: for any message with reactions, call `GetMessageReactionsListRequest(limit=TELEGRAM_REACTION_USER_CAP=500)` → enqueue each reactor as spider seed of type USER.
- [x] **2.3** Forward-driven channel/user discovery: when forward source is a chat, enqueue chat as spider seed; when forward source is a user, enqueue user.
- [x] **2.4** Auto-backfill on new account: at startup `_load_accounts()` compares known account names vs DB-tracked-accounts → for any new name, after worker connects, call `collect_dialogs()` then enqueue every chat for full `backfill_chat`.
- [x] **2.5** Periodic monitor cron: scheduler tick every 15min runs incremental `backfill_chat(min_id=last_seen_message_id)` for every known chat. Realtime listener handles new chats not yet known. (Note: scheduler uses 1h interval by default; 15min requires changing `collection_schedules.interval_hours` column to support fractions.)
- [ ] **2.6** Tests + smoke + commit.

## RUN + MONITOR (between Phase 2 and Phase 3)

- [ ] **3.0** Restart collector. Watch logs `docker logs -f unifiedcollector_collector` for ~30 min. Confirm: spider queue depth grows, discussion_visits rows appear, reactions tick, no error spam.
- [ ] **3.1** Document any silent fails / new bugs into BUGS_TELEGRAM.md and fix in-loop.

## Phase 3 — Bot onboarding + dashboard (BLOCKED on user delivering API_ID/API_HASH)

- [ ] **4.1** Create `src/db/migrations/add_telegram_user_accounts.sql` table `telegram_user_accounts` (name varchar PK, api_id int, api_hash varchar, phone varchar, session_string text, owner_bot varchar, created_at, last_connected_at, status varchar). NOTE session_string encrypted-at-rest TBD.
- [ ] **4.2** Create `src/bots/onboard_bot.py` — single process running `python-telegram-bot` Application that polls all 3 bot tokens (`BRYANSEAH_BOT_TOKEN`, `SHOTSBYSEAH_BOT_TOKEN`, `PRAWNPRODUCTIONS_BOT_TOKEN`).
- [ ] **4.3** `/startcollector` handler — DM-only. ConversationHandler states: ASK_PHONE → ASK_CODE → MAYBE_ASK_2FA → DONE. Uses `telethon.TelegramClient(StringSession(), api_id, api_hash)` for the auth ladder. On success, persist to `telegram_user_accounts`.
- [ ] **4.4** Add docker-compose service `unifiedcollector_onboard_bot` running the bot.
- [ ] **4.5** Patch `TelegramCollector._load_accounts` to ALSO load from `telegram_user_accounts` table (env still works for legacy).
- [ ] **4.6** Hot-reload: on new row insert (use postgres LISTEN/NOTIFY or 60s poll), spawn worker for new account, fire backfill.
- [ ] **4.7** Dashboard API: `GET /api/telegram/accounts`, `POST /api/telegram/accounts/add` (phone+code+password), `POST /api/telegram/accounts/{name}/refresh`, `DELETE /api/telegram/accounts/{name}`.
- [ ] **4.8** Dashboard React: account-add modal with phone/code/2FA stepper, accounts table.
- [ ] **4.9** Tests + smoke (with user-provided API creds) + commit.

## Notes / pitfalls (filled in as ralph loop discovers them)

- (none yet — append below as discovered)

## Resume instructions

If context compacted, resume by reading this file top-to-bottom, finding first unchecked box, doing it. Each box is mechanical enough that the loop can drive it. Mark complete with `- [x]` after verification.
