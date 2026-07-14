# Collector<->Analyzer sync — progress (resume here)

Goal: analyzer consume collector identity data. Blocker: entity creation gated on collection_targets.
Decisions: entity scope = corroborated + target-contacts (NOT lone group randoms). Beeper unmatched senders = create own entity.
Repos: C:\unifiedcollector + C:\unifiedanalyzer (both editable this task). Commit to main each.
Deploy analyzer: `docker compose -f C:\unifiedanalyzer\docker\docker-compose.yml up -d <svc>` (analyzer + scheduler live-src-mounted; no --build). Collector: restart container.

## Phases
- [ ] P1 = #30 broaden entity creation (analyzer entity_resolver.py Phase1 ~:422-460). IN PROGRESS.
- [ ] P2 = #31 beeper bridge (analyzer timeline_builder + entity_resolver) + #37 beeper.network backfill (collector).
- [ ] P3 = #32 wa @lid (timeline_builder:79-98 + whatsapp_lid_map) | #33 social_users signal | #34 threads/x ingest+handle_fanout promote.
- [ ] P4 = #35 phone+link signals | #36 face missing-file | #38 media tombstone | #39 lemon8 id | #40 contract/bot flags | #41 doc §14.

## Key facts (verified live)
- entity_platform_links source keys: instagram=numeric platform_user_id, telegram=numeric, whatsapp=phone||@s.whatsapp.net, youtube=UC channel id, tiktok=numeric, strava=numeric, github=numeric, lemon8=username.
- beeper sender_id = `@telegram_<id>` / `@whatsapp_lid-<id>` (sender_name=phone) / `@instagram_<id>`; network on beeper_shadow_chats.network (msg.network='unknown'). 1509 tg senders -> 1292 match telegram_users.
- whatsapp_lid_map = 15,901 rows (lid->phone_jid). timeline_builder wa query does NOT use it (78% wa msgs NULL phone).
- social_users 71,189; 512 usernames on >=2 platforms. Used only in graph.py UI route.
- collection_targets: ig 7, youtube 492, tiktok 240, lemon8 60, whatsapp 8, strava 2, telegram 1, beeper 1, github 1.
- media on disk: lemon8 OLD gone (150/150), instagram+telegram intact (0/150).

## Subagent log
(append: agentId | task | result summary | files touched)
- a829926fa9a8121ee | #31 beeper bridge | RUNNING (fable, bg). Owns: NEW src/pipeline/beeper_bridge.py + incremental_runner.py wiring + timeline_builder.py beeper block. Do NOT edit those files until it returns. Spec: parse beeper sender_id native ids (@telegram_/@instagram_/@whatsapp_lid-), matched->attribute existing entity, unmatched->new native-source entity+link (link_method='beeper_bridge'). Baseline links tg347/ig149.

## Done log
(append commit hashes per task)

## DONE LOG (updated)
- #30 entity broadening: analyzer 2095d39. VERIFIED links 1400->1933; instagram 9->149, telegram 125->347, threads 0->32, x 0->5. (resolve_entities hit a transient asyncpg TimeoutError on one query but committed.)
- #37 beeper network backfill: UPDATE 473,552 (Discord 319k/Telegram 124k/WhatsApp 18k/IG 2.8k). collector DB done; forward-fill on new msgs = analyzer joins beeper_shadow_chats.network (robust).
- #32 wa @lid: analyzer ec91bcc. timeline_builder resolves @lid via whatsapp_lid_map.
- #41 doc §14 telegram-ceiling correction: analyzer 16deb57.
- #36 face_worker tombstone gone/unreadable media: analyzer a81c87b. Root-mounted->tombstone(status='missing'); root-absent->skip (graceful offline).
- #31 beeper bridge: analyzer 16313cb (beeper_bridge.py + incremental_runner wiring). RAN: +3450 entities/links (tg+1049, wa+2348, ig+53; 342 matched; 1136 bare-id skipped). entities 1348->5039, links 1933->5383 (wa3000/tg1396/yt491/ig202/lemon105/strava58/tiktok53/gh41/threads32/x5).
- #34 threads/x timeline: analyzer 5fcd1bc (timeline_builder threads+x CONTENT_PUBLISHED blocks; handle_fanout already spans threads/x). 
- #33 social_users signal: analyzer entity_resolver.py EDITED (feed usernames on >=2 platforms into clustering; threads/x key on handle, else platform_user_id). COMPILES. resolve_entities re-running (task b160xf359) to apply+measure — NOT yet committed. VERIFY link growth then commit.
- #33 social_users: analyzer 32fbe40 (entity_resolver + link-upsert dedupe bugfix). VERIFIED links 5383->5853 (ig 202->432, tg 1396->1549, threads 32->73). NOTE: first resolve crashed CardinalityViolation (same platform_id in 2 clusters); fixed by deduping upsert batch by (source,platform_id).
- #35 cross-source signals: analyzer 101d14f (NEW cross_source_signals.py + runner wiring). tg<->wa phone_match (16 rows=8 pairs x2) + IG external_url shared_website (21). Idempotent via metadata.emitter='cross_source_signals_v1'. wa_discovered_links DEFERRED (content not identity).
- STATUS done: #30,#31,#32,#33,#34,#35,#36,#37,#41. REMAINING: #38 media tombstone (collector, metadata-based sweep, media_items has no status col), #39 lemon8 stable id (lemon8 platform_user_id=vanity handle not numeric; ~half are 'userNNNN' stable, half vanity), #40 identity-key contract doc + telegram is_bot (telegram_users has NO is_bot col, only is_deleted; 283 bot-suffix usernames leak as entities). COLLECTOR CHANGES = FRAGILE (migration drift + no-rebuild rules): prefer doc + metadata sweeps over schema/migration.
- FINAL entity graph: entities 1348->5248, entity_platform_links 1400->5853 (wa3000/tg1549/yt491/ig432/lemon109/tiktok79/threads73/strava71/gh41/x8).
- #40 identity contract: collector 61647c4 (IDENTITY_KEYS.md). telegram is_bot capture documented as known-gap (NOT implemented — would need new migration + live-collector recreate; too fragile for P3). bot pollution left documented, not heuristic-filtered (would exclude real '%bot' users).
- #39 lemon8 stable id: collector fd20c82. Multi-marker stable-id extraction + prior-stable-id reuse; backward-compat; applies on next lemon8 restart. lemon8 IS actively collecting (media today).
- #38 media tombstone: IN PROGRESS. Host sweep (beu2ff5t2) checking 529,626 media_items file_paths against Z:\unifiedcollector\media (root ONLINE, so misses=genuinely gone). Missing ids -> tmp/media_missing_ids.txt. THEN batch UPDATE media_items SET metadata||{'missing_at':now} (NO status col; metadata-based, no migration). lemon8 NOT blanket-gone (34k files exist, old rows point at deleted files -> per-file sweep needed).
- STATUS: 11/12 done (#30-37,#39,#40,#41). ONLY #38 pending (sweep running).
