# Collector Vault Operations - Product Requirements Document (PRD)

## Requirements Description

### Background
- **Business Problem**: UnifiedCollector is the first system in the pipeline and must become the durable source of truth for raw digital evidence. The current system already collects large volumes of data, but growing storage, external-drive dependency, rate limits, partial files, and rebuild risk require a stronger vault contract.
- **Target Users**: The project owner, future local analyzer applications, future LLM/data tools pointed at the vault, and maintenance agents.
- **Value Proposition**: Collected data remains useful even if the live database is lost, another application wants to consume the files directly, or the external drive is temporarily unavailable.

### Feature Overview
- **Core Features**:
  - Store raw media, full Tier 1 payloads, routes, messages, and metadata under `Z:\unifiedcollector`.
  - Deduplicate physical files by checksum while preserving every source occurrence as its own sidecar/event.
  - Write one JSON sidecar per stored artifact with enough provenance to rebuild DB indexes.
  - Treat file, sidecar, DB row, and checksum consistency as the success condition for file-backed artifacts.
  - Pause file-heavy work when the vault is unavailable, keep lightweight health/session checks running, and queue pending work.
  - Run scheduled DB backups to `Z:\unifiedcollector\backups\db`.
  - Make browser/extension captures first-class auditable collector records.
  - Prioritize Tier 1 freshness before rich media, rate-limit safety, historical backfill, and broad discovery.
- **Feature Boundaries**:
  - Collector remains read-only against source platforms; it does not send, reply, react, or mutate upstream accounts.
  - Collector does not decide real-world identity truth. It can maintain collection priority per platform account and consume analyzer priority hints.
  - Analyzer-derived scores, merges, and relationship truth live in UnifiedAnalyzer, not collector.
- **User Scenarios**:
  - External drive disconnects during backfill: media downloads pause, run status shows queued/blocked, health checks continue.
  - DB is wiped: media sidecars, raw payloads, route payloads, and run manifests can rebuild `media_items` and other normalized indexes where data was persisted.
  - Same file appears on Instagram, Telegram, and WhatsApp: one blob is stored, three occurrence sidecars preserve provenance.
  - Strava API stream calls hit 429: collector retries once with longer randomized delay, then cools down that exact account/action scope; browser capture can fill GPS data.

### Detailed Requirements
- **Input/Output**:
  - Inputs: platform APIs, browser extension captures, headless scraper output, messaging live events, raw route/message/media payloads, analyzer priority hints.
  - Outputs: deduped files, per-artifact sidecars, raw payload archives, DB rows, run manifests, repair queue rows, hourly status metrics, DB backup files.
- **User Interaction**:
  - Dashboard must show vault health, free space, external-drive mounted status, partial artifact counts, repair queue size, hourly source ingest, active cooldowns, and Tier 1 freshness.
  - Telegram status must describe human-readable collection health: what collected this hour, what is blocked, what needs human action, and which rate-limit scopes are cooling down.
- **Data Requirements**:
  - Canonical roots:
    - `Z:\unifiedcollector\media`
    - `Z:\unifiedcollector\sidecars`
    - `Z:\unifiedcollector\raw`
    - `Z:\unifiedcollector\manifests`
    - `Z:\unifiedcollector\queues`
    - `Z:\unifiedcollector\backups\db`
  - DB rows should store vault-root identifiers plus relative paths, not host-specific absolute paths.
  - Each sidecar must include, when available:
    - `artifact_id`, `artifact_kind`, `source`, `ingest_path`, `collection_account`, `collection_priority`
    - platform IDs, source record IDs, parent record IDs, original URL, request URL
    - author IDs/usernames/display names
    - caption/text/message body
    - posted/discovered/collected timestamps
    - blob path, sidecar path, raw payload references
    - checksum, file size, MIME type, extension
    - scrape run ID, extension version, collector version
    - HTTP status, rate-limit scope, partial/failure flags
    - rebuild hints for target DB tables
  - Tier 1 raw payloads are kept forever. Lower tiers use compressed JSON/JSONL where practical and avoid huge duplicate HTML unless the HTML contains unique evidence.
- **Edge Cases**:
  - If file write succeeds but sidecar write fails, mark artifact partial and repair-queue it.
  - If DB insert succeeds but file or sidecar verification fails, mark artifact partial and repair-queue it.
  - If external drive is missing, do not write large files to C: as a silent fallback.
  - If a collector is rate-limited, cool down the exact source/account/action scope after one longer randomized retry.
  - If an analyzer priority hint conflicts with collector local priority, preserve both facts and compute an effective collection priority with provenance.

## Design Decisions

### Technical Approach
- **Architecture Choice**: UnifiedCollector is a vault-first ingestion system. The DB is the live index and scheduler state; the vault plus sidecars and raw payloads are the durable rebuild layer.
- **Key Components**:
  - Vault path resolver that validates `Z:\unifiedcollector` before file-heavy work.
  - Artifact writer that commits file, sidecar, checksum, and DB row as one logical operation.
  - Sidecar schema validator.
  - Blob dedupe service keyed by checksum.
  - Occurrence sidecar writer keyed by source occurrence.
  - Repair reconciler for DB-only, file-only, sidecar-only, and checksum-mismatch states.
  - Rate-limit ledger by source/account/action.
  - Priority scheduler that understands Tier 1, rich media, cooldowns, backfill, and discovery.
  - Browser capture audit store.
  - Scheduled DB backup service.
- **Data Storage**:
  - Physical media is deduped by checksum.
  - Occurrences are not deduped away; every platform/source occurrence has its own sidecar.
  - Raw browser/API captures are stored as JSON/JSONL or compressed JSONL with stable references from sidecars.
  - DB backup retention: last 7 daily, 4 weekly, 3 monthly; add hourly lightweight/schema-critical exports if feasible.
- **Interface Design**:
  - Existing collectors continue writing through shared media/artifact helpers.
  - Analyzer can read collector DB and media root, but future tools should be able to read vault sidecars directly.
  - Analyzer priority hints are imported into collector as platform-account collection priority suggestions, with provenance.

### Constraints
- **Performance Requirements**:
  - File-heavy collection must not fill C: when Z: is unavailable.
  - Sidecar writes must be lightweight enough to run for every artifact.
  - Reconciliation should be incremental and resumable.
- **Compatibility**:
  - Use Windows host path `Z:\unifiedcollector`; containers may see mounted equivalents but DB stores relative paths.
  - Existing `media_items` and collector-specific tables remain supported during migration.
- **Security**:
  - No outbound platform actions from collector.
  - Plain local JSON sidecars and backups are acceptable, but must not be committed to git.
  - Credentials remain in existing credential storage, not sidecars.
- **Scalability**:
  - The design assumes storage will keep growing past 400 GB.
  - Physical dedupe is required before rich media scale becomes unmanageable.

### Risk Assessment
- **Technical Risks**:
  - Sidecar schema drift across collectors can make rebuilds inconsistent. Mitigation: central schema helper and validation tests.
  - Partial writes can accumulate silently. Mitigation: dashboard counts, repair queue, and scheduled reconciliation.
  - Dedupe mistakes can lose provenance. Mitigation: dedupe only physical blobs, never occurrence sidecars.
  - Browser capture APIs may change. Mitigation: store raw capture provenance and extension version.
- **Dependency Risks**:
  - External drive can disconnect. Mitigation: fail closed for media, queue pending file-heavy work, keep health checks alive.
  - Platform rate limits can block Tier 1. Mitigation: scoped cooldowns, one delayed retry, browser fallback where safer.
- **Schedule Risks**:
  - Migrating every collector at once is risky. Mitigation: implement shared helpers first, then migrate source by source.

## Acceptance Criteria

### Functional Acceptance
- [x] Collector refuses file-heavy writes when `Z:\unifiedcollector` is unavailable and records queued work instead of success.
- [x] Every new file-backed artifact has a JSON sidecar with required provenance fields.
- [ ] The artifact write path verifies checksum, file size, sidecar write, and DB row consistency.
- [x] Duplicate physical files are stored once by checksum while multiple occurrence sidecars remain queryable.
- [ ] Tier 1 raw payloads, messages, and route data are persisted enough to rebuild normalized records.
- [x] Rate-limit events include source, account, action scope, retry/cooldown times, and are shown in dashboard and Telegram status.
- [x] Browser/extension captures produce auditable sidecars or raw capture records.
- [x] Scheduled collector DB backups land under `Z:\unifiedcollector\backups\db`.
- [x] A dry-run rebuild report can show what DB tables can be reconstructed from sidecars/raw payloads.

### Quality Standards
- [x] Shared artifact writer has unit tests for success, sidecar failure, checksum mismatch, missing vault, duplicate blob, and DB failure.
- [x] Reconciler has tests for DB-only, file-only, sidecar-only, and partial artifact states.
- [x] Existing collectors keep passing smoke tests after migrating to the shared artifact writer.
- [x] Dashboard and Telegram wording are clear to a non-developer operator.

### User Acceptance
- [x] Dashboard answers: what collected this hour, what is blocked, what is rate-limited, what is queued, and whether the vault is safe.
- [x] No collector silently stores large media on C: when Z: is missing.
- [x] Future tools can inspect media plus JSON sidecars without needing analyzer.

## Execution Phases

### Phase 1: Vault Foundation
**Goal**: Establish the durable storage contract before changing every collector.
- [x] Add central vault config for `Z:\unifiedcollector` with mount/free-space checks.
- [x] Add vault-root ID and relative-path conventions.
- [x] Add artifact sidecar schema and validator.
- [x] Add shared blob path strategy keyed by checksum.
- [x] Add `.gitignore` coverage for generated sidecars, raw payloads, queues, and backups.
- [x] Add dashboard/Telegram vault health fields.
- **Deliverables**: Vault resolver, sidecar schema, health checks, tests.
- **Time**: 1-2 days.

### Phase 2: Atomic Artifact Writes
**Goal**: Make file-backed collection reliable and repairable.
- [x] Build shared artifact writer: write temp file, hash, move to blob path, write sidecar, insert/update DB.
- [x] Mark any incomplete step as `partial`.
- [x] Add repair queue table or reuse existing queue mechanism.
- [x] Add reconciler dry-run: DB-only, blob-only, sidecar-only, checksum mismatch.
- [x] Migrate one low-risk source first, then Instagram/Telegram/WhatsApp/Beeper/Strava media paths.
- **Deliverables**: Shared writer, partial-state model, repair queue, first migrated sources.
- **Time**: 3-5 days.

### Phase 3: Raw Payload and Rebuild Layer
**Goal**: Preserve enough raw data to rebuild indexes.
- [x] Define raw payload path conventions for Tier 1 and lower tiers.
- [ ] Persist Tier 1 full raw payloads for messages, routes, profiles, posts, and browser captures.
- [x] Persist compressed JSON/JSONL for lower tiers where practical.
- [x] Link every sidecar to raw payload references.
- [x] Build rebuild dry-run command that reports reconstructable tables and missing fields.
- **Deliverables**: Raw archive contract, rebuild report, source coverage matrix.
- **Time**: 3-5 days.

### Phase 4: Priority and Rate-Limit Scheduler
**Goal**: Make collection methodical without losing Tier 1 freshness.
- [ ] Implement effective priority order: Tier 1 freshness, rich media, low rate-limit risk, historical backfill, broad discovery.
- [x] Add per-source/account/action rate-limit ledger.
- [x] Add one delayed randomized retry before cooldown.
- [x] Ensure cooldown stops exact scope, not unrelated safe scopes.
- [x] Import analyzer priority hints and surface provenance.
- [x] Add hourly ingest/rate-limit stats to dashboard and Telegram.
- **Deliverables**: Priority scheduler, cooldown ledger, telemetry.
- **Time**: 3-5 days.

### Phase 5: Browser Capture as First-Class Ingest
**Goal**: Make extension/browser evidence auditable and useful for routes/media.
- [x] Store extension version, tab platform/account, captured URL, request URL, response hash, and extraction IDs.
- [x] Add passive Strava GPS browser fallback for activities where API streams are missing or 429ed.
- [x] Queue browser route captures by Tier 1 priority first.
- [x] Add random delay and stop-on-challenge handling.
- [x] Add dashboard coverage for route detail pending vs route captured.
- **Deliverables**: Browser capture records, Strava GPS fallback path, coverage dashboard.
- **Time**: 4-7 days.

### Phase 6: Backups and Recovery Drills
**Goal**: Prove the system can survive failure.
- [x] Add scheduled collector DB dumps to `Z:\unifiedcollector\backups\db`.
- [x] Implement retention: 7 daily, 4 weekly, 3 monthly.
- [x] Add restore rehearsal checklist.
- [x] Run dry-run rebuild from sidecars/raw payloads into a scratch DB.
- [x] Run restore replay from a real collector DB dump into a scratch DB.
- [x] Report unrebuildable gaps by source.
- **Deliverables**: Backup job, retention, recovery report.
- **Time**: 2-4 days.

Restore rehearsal checklist:
- Verify external vault is mounted and writable, then record free space and latest backup path.
- Restore the latest collector DB dump into a scratch database, never production.
- Run `python -m src.main rebuild-report --vault-root /vault --compare-db --json` against the scratch DB context and save the report under the vault exports area.
- Run `python -m src.main rebuild-rehearsal --vault-root /vault --scratch-db <scratch.sqlite> --sidecar-limit <sample> --raw-payload-limit <sample> --json` for bounded media/raw materialization.
- Compare scratch counts for `media_items`, `raw_payloads`, rate-limit ledger rows, and Tier 1 source records against live dashboard totals.
- List unrebuildable gaps by source, artifact kind, and missing field; create repair queue rows only after review.
- Drop the scratch database and scratch SQLite file after the report is archived.

---

## Implementation Status - 2026-07-24 / 2026-07-25

- Vault foundation is implemented in `src/core/vault.py`: canonical root detection, writability/free-space checks, media-root mirror checks, vault-relative paths, sidecar schema validation, raw payload paths, and checksum blob-path conventions.
- Media/artifact/raw sidecar helpers are wired into the shared collector base path and tested. Sidecar failure is recorded on `media_items.metadata.vault_sidecar` and queued through the existing dead-letter queue.
- `BaseCollector.run()` now queues a `dead_letter_queue` pause row for the source and raises before collection when the drive is missing or the vault/media write guard fails, so file-heavy collectors do not silently record successful cycles against an absent external drive.
- `src/core/vault.py::write_atomic_artifact` is now the shared Phase-2 writer for new migrations: it writes bytes to a temp file under the vault, verifies checksum/size, moves to the canonical sha256 blob path, writes the sidecar, optionally runs a DB callback, and returns `partial=True` for sidecar/DB failures after the blob exists.
- Rebuild reporting exists in `src/core/rebuild_report.py` and covers sidecar scan, raw payload table hints, DB-only, sidecar-only, blob-only, missing file, size mismatch, and checksum mismatch states.
- Generic media reconciliation repair now canonicalizes recovered files: `_redownload` writes through `write_atomic_artifact`, then updates `media_items.file_path/file_size/sha256` and `metadata.vault_artifact` so repaired legacy rows point at vault blobs.
- Authenticated Strava club membership payloads now archive through `write_raw_payload()` under `raw/strava/...` with rebuild metadata for `strava_athletes`, replacing the old media-tree JSON write.
- Tier 1 messaging raw payloads now archive outside Postgres before/after normalized writes as applicable: Telegram chat/user/message/profile payloads, WhatsApp message/contact/delete bridge events, and Beeper account/chat/participant/message shadow payloads write through `write_raw_payload()` with rebuild table hints. Unit tests disable raw archives by default to avoid writing test fixtures into the real vault, then explicitly enable and mock the writer in raw-coverage tests.
- Strava API activity pages, individual activity payloads, and API/web GPS stream responses now archive through `write_raw_payload()` with rebuild hints for `strava_activities` and `strava_gps_streams`, so route/map reconstruction no longer depends only on normalized Postgres JSONB/columns.
- Instagram collector profile/post paths now archive raw payloads through `write_raw_payload()`: httpx profile responses, GraphQL post pages, Playwright profile/window payloads, reels-window payloads, individual normalized post nodes, and instaloader post raw dicts carry rebuild hints for `instagram_profiles` and `instagram_posts`.
- Media/raw rebuild rehearsal exists in `src/core/rebuild_rehearsal.py` and `python -m src.main rebuild-rehearsal`: media sidecars and raw-payload sidecars can be materialized into scratch SQLite tables without touching production Postgres.
- Collector DB backups are implemented with vault-mirror checks, status reporting, and retention tests.
- Dashboard and Telegram hourly status now include vault health, artifact partial/queued counts, backup status, hourly ingest, scoped rate-limit/auth failures, Telegram FloodWait events, browser extension hook/ingest counters, decoded-frame freshness, and stale Chrome extension version warnings.
- Dashboard media joins now prefer stable source keys over file paths: WhatsApp chat media resolves by `wa_<platform_message_id>` with path fallback, and YouTube channel videos expose both thumbnail and stored-video media IDs.
- Passive Strava browser route fallback is implemented in extension v1.21.24 and `/social/strava-streams`: when the real browser loads a Strava route stream, the bridge archives the raw payload and upserts `strava_gps_streams` plus `strava_activities.summary_polyline`. Browser-observed Strava stream HTTP 429/401/403 responses are now emitted even when no route JSON is parseable and are stored as durable `rate_limit_events` under `browser_strava_streams`.
- Priority-driven Strava browser route capture is implemented in extension v1.21.24, `/social/strava-route-queue`, `/social/strava-route-visit`, and `/strava/route-capture-queue`: the browser gets one missing-route activity at a time, ordered by Tier 1/2 proximity and collector target priority, while respecting active GPS-stream 429 cooldowns and recent visit TTLs.
- Analyzer priority hints are imported by `src/core/priority_hints.py`, merged into `collection_targets.metadata.analyzer_priority_hint`, used by target loading, and surfaced on the Targets dashboard.
- `src/core/scrape_pacing.py` now provides a tunable one-shot pre-cooldown retry delay (`COLLECTOR_PRE_COOLDOWN_RETRY_*`). Instagram Playwright profile fetches, Strava GPS stream fetches, and Search HTTP fetch paths use it; recovered Strava/Search 429s are logged without creating an active cooldown, while repeated 429s still open scoped cooldowns where the collector can safely skip that exact scope.
- Strava browser route capture cooldown is scoped by account: `/social/strava-route-queue` and `/strava/route-capture-queue` accept an account label, the extension sends the same Strava owner/fallback label on queue and stream events, global/no-account GPS cooldowns still pause everyone, and account-specific browser stream 429s pause only the matching browser account.
- Beeper, Telegram, WhatsApp, Lemon8, YouTube, TikTok, Website, Search, Instagram headless/extension media, GitHub direct media, GitHub bulk avatar artifacts, shared profile-photo, generic `BaseCollector.save_file`, shared `media_download` HTTP/delegated single-file calls, and Strava activity photo/route-map downloads now write physical bytes through `write_atomic_artifact` or `write_atomic_artifact_from_path` to canonical `media/blobs/<sha256>` paths while retaining per-occurrence `media_items` rows and sidecars where a concrete source occurrence should be indexed. TikTok Playwright fallback uses vault-temp intermediate files and deletes them after re-ingest, so browser fallback no longer leaves duplicate legacy MP4s. Duplicate-row cleanup in `BaseCollector.insert_media_item` preserves canonical blob files and only removes legacy per-occurrence duplicate files. The collector dashboard media resolver accepts both legacy media-root paths and vault-backed blob paths.
- `src/core/health.py` no longer imports the full DB connection module for Docker health checks; live Telegram/WhatsApp health checks dropped from roughly 50s to roughly 10s and fit the configured 30s timeout.
- Broad post-migration smoke/regression test passed on 2026-07-24: `python -m pytest -q tests\collectors tests\bridges\test_ig_ingest_vault.py tests\dashboard tests\core\test_base_collector.py tests\core\test_media_download.py tests\core\test_vault.py tests\test_reconciler.py` covered 536 collector/dashboard/vault/reconciler tests successfully.
- Real collector DB restore drill passed on 2026-07-25 and is archived at `Z:\unifiedcollector\exports\recovery_drills\collector_restore_drill_20260725_0400.json`: backup `Z:\unifiedcollector\backups\db\unifiedcollector_20260723_133814.dump` restored into scratch DB `uc_restore_drill_20260724_200036` in 15,755.531 seconds, table counts were captured, and the scratch DB was dropped. Key restored counts: `media_items` 580,866; `telegram_messages` 1,375,665; `whatsapp_messages` 49,887; `strava_activities` 43,483; `strava_gps_streams` 19,719; `instagram_posts` 24,774; `rate_limit_events` 4,568; `collection_runs` 402; `collection_targets` 1,690; `browser_ingest_events` 217.
- Restore-drill reporting was hardened after the proof run: Docker table existence checks now interpolate the actual table name, the restore timeout default is 12 hours, and the expected Beeper message table is `beeper_shadow_messages` rather than the stale `beeper_messages` name. The archived 2026-07-25 report still shows that stale Beeper expected-table gap because the fix landed after that run.
- Host-side rebuild tooling now maps container paths under `/media/...` and `/vault/...` back to the selected vault root before checking files. Re-running the same bounded rehearsal after that fix scanned 500 media sidecars and 500 raw-payload sidecars, inserted 498 media rows and 465 raw-payload rows into scratch SQLite, and reduced `file_missing` skips from 384 to 2. The two remaining misses are true Beeper/WhatsApp MXC media gaps with DB rows and sidecars but no backing file/blob. GitHub bulk avatar range writes artifact sidecars only and intentionally does not create `media_items` rows for unknown numeric IDs.
- New media/artifact sidecars normalize raw-payload provenance into a top-level `raw_payload` block. Writers accept `raw_payload_path`, `raw_payload_sidecar_path`, `raw_payload_artifact_id`, and `raw_payload_refs`; rebuild reporting counts these references without double-counting a primary path and matching ref.
- Lower-tier raw payload compression is implemented in `write_raw_payload()` for `json.gz` and `jsonl.gz`. Browser profile/posts/comments raw captures now use `json.gz`; DM captures, decoded DM payloads, and Strava route streams stay plain JSON because they are Tier 1/debug/route evidence where immediate inspection matters more than size.
- Shared artifact sidecars now expose required provenance fields directly: `ingest_path`, `collection_priority`, `raw_payload`, `provenance`, `metadata`, and `rebuild`. This applies to raw payload sidecars, generic JSON artifact sidecars, and atomic blob artifact sidecars; media sidecars already carried the same provenance block.
- Telegram status wording now avoids internal terms for operator-facing health: `DLQ rows` is reported as repair queue items, sidecar metadata failures are reported as media records waiting for sidecar repair, and non-429/auth wording is reported as login/access or other HTTP errors.
- `python -m src.main vault-inspect` provides DB-free vault inspection for future tools: it reads sidecars directly, resolves file/blob/raw references against the vault root, supports source filtering and JSON output, and does not require analyzer or collector Postgres. A real `Z:\unifiedcollector --limit 2 --json` sample completed in about 3.6 seconds after switching the scan to stream sidecars instead of sorting the whole tree.

**Document Version**: 1.0
**Created**: 2026-07-20
**Clarification Rounds**: 5
**Quality Score**: 99/100
