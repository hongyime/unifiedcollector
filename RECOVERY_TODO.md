# Z-Drive Recovery — TODO + Monitoring Plan

> Started 2026-06-19. Z media drive reformatted (exFAT→NTFS); all 148,156 media
> files lost. DB survived (`unifiedcollector_pgdata` volume). Refill driven by
> `COLLECTOR_RECOVER_MISSING=1` (root `.env`). See memory `z-drive-recovery`.

## Phase A — Selective wipe (telegram + whatsapp), GATED on safety dump
Decision (user, 06-19): wipe ONLY telegram + whatsapp; other 8 sources keep refilling.
Rationale: telegram re-enumerates live with valid file refs (clean win); whatsapp
history isn't re-fetchable anyway; the other 8 refill fine from surviving metadata.

- [ ] **BLOCKER:** wait for `backups/unifiedcollector_postZformat_*.dump` to finish (safety net).
- [ ] Stop the two collectors: `docker compose stop collector_telegram collector_whatsapp`.
- [ ] **PRESERVE `telegram_user_accounts`** — holds session_string/api_id/api_hash/phone
      for the 4 accounts. Wiping = telegram can't reconnect. DO NOT TRUNCATE.
- [ ] TRUNCATE telegram collected data: telegram_messages, telegram_chats,
      telegram_chat_members, telegram_discussion_visits, telegram_polls,
      telegram_reaction_counts, telegram_reactions, telegram_spider_queue,
      telegram_user_changes, telegram_users.
- [ ] TRUNCATE whatsapp: whatsapp_messages, whatsapp_chats, whatsapp_lid_map,
      whatsapp_users, wa_discovered_links, wa_face_embeddings, wa_face_identities.
- [ ] `DELETE FROM media_items WHERE source IN ('telegram','whatsapp');` (13,438 + 988).
- [ ] Reset cursors: `DELETE FROM service_cursors WHERE service IN ('telegram','whatsapp');`
- [ ] Reset targets to pending: `UPDATE collection_targets SET status='pending' WHERE source IN ('telegram','whatsapp');`
- [ ] WhatsApp needs the `wa-bridge` service running (HTTP-poll host currently unresolved).
- [ ] Restart: `docker compose up -d collector_telegram collector_whatsapp`.

## Phase B — Monitor backfill vs live-scraping (the key question)
Worry: re-downloading 148k media files starves the LIVE scrape (new rows) of CPU/
bandwidth/rate-limit budget. Architecture claims separation (audit:105 "4h lanes not
starved by media"; commit d9a9781 telegram "backfill while listening live") — validate it.

Two distinct progress signals — watch BOTH:
- **SCRAPE progress** (new data): row growth in telegram_messages / whatsapp_messages /
  *_videos etc.; `service_cursors.last_processed_at` advancing; max(collected_at) recent.
- **MEDIA-download progress** (Z refill): `Z:/unifiedcollector/media` file count rising.

Commands:
- Z growth: `Get-ChildItem Z:\unifiedcollector\media -Recurse -File | Measure-Object`
- Cursor freshness: `SELECT service,status,last_processed_at FROM service_cursors;`
- DLQ depth (failed downloads piling up): check dead-letter table per source.
- Prometheus `/metrics`: `uc_source_health_age_seconds`, `uc_account_requests_*`.
Cadence: snapshot every ~15-30 min; compare scrape-cursor delta vs Z-file delta.

## Phase C — Prior throughput TODOs (from collector_audit.md:382, NOT done)
- [ ] Verify Telegram/WhatsApp/Beeper backfill paths actually run once DB stable.
- [ ] Telethon rate-underutilization review (was deferred until session desync fixed).
- [ ] Confirm Strava spidering enqueues discovery.
- [ ] (New) Add a backfill concurrency cap / low-priority lane so media refill cannot
      starve live scraping — the knob that resolves Phase B if it trips.

## Phase D — TRIPWIRE: when to "restart all progress" instead
Restart-all (full wipe, collect-forward-only, media backfill capped/deferred) IF:
- After ~2-4h, live scrape cursors are NOT advancing (new content not collected) because
  workers are pinned on media backfill, OR
- realtime sources (telegram/whatsapp/beeper) fall materially behind live, OR
- DLQ for media downloads grows unbounded (refill not even succeeding).
Otherwise: let refill run (hours/days); turn OFF `COLLECTOR_RECOVER_MISSING` when Z full.
