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
- [ ] Collector refuses file-heavy writes when `Z:\unifiedcollector` is unavailable and records queued work instead of success.
- [ ] Every new file-backed artifact has a JSON sidecar with required provenance fields.
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
- [ ] Existing collectors keep passing smoke tests after migrating to the shared artifact writer.
- [ ] Dashboard and Telegram wording are clear to a non-developer operator.

### User Acceptance
- [x] Dashboard answers: what collected this hour, what is blocked, what is rate-limited, what is queued, and whether the vault is safe.
- [x] No collector silently stores large media on C: when Z: is missing.
- [ ] Future tools can inspect media plus JSON sidecars without needing analyzer.

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
- [ ] Migrate one low-risk source first, then Instagram/Telegram/WhatsApp/Beeper/Strava media paths.
- **Deliverables**: Shared writer, partial-state model, repair queue, first migrated sources.
- **Time**: 3-5 days.

### Phase 3: Raw Payload and Rebuild Layer
**Goal**: Preserve enough raw data to rebuild indexes.
- [x] Define raw payload path conventions for Tier 1 and lower tiers.
- [ ] Persist Tier 1 full raw payloads for messages, routes, profiles, posts, and browser captures.
- [ ] Persist compressed JSON/JSONL for lower tiers where practical.
- [ ] Link every sidecar to raw payload references.
- [x] Build rebuild dry-run command that reports reconstructable tables and missing fields.
- **Deliverables**: Raw archive contract, rebuild report, source coverage matrix.
- **Time**: 3-5 days.

### Phase 4: Priority and Rate-Limit Scheduler
**Goal**: Make collection methodical without losing Tier 1 freshness.
- [ ] Implement effective priority order: Tier 1 freshness, rich media, low rate-limit risk, historical backfill, broad discovery.
- [x] Add per-source/account/action rate-limit ledger.
- [x] Add one delayed randomized retry before cooldown.
- [ ] Ensure cooldown stops exact scope, not unrelated safe scopes.
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

## Implementation Status - 2026-07-24

- Vault foundation is implemented in `src/core/vault.py`: canonical root detection, writability/free-space checks, media-root mirror checks, vault-relative paths, sidecar schema validation, raw payload paths, and checksum blob-path conventions.
- Media/artifact/raw sidecar helpers are wired into the shared collector base path and tested. Sidecar failure is recorded on `media_items.metadata.vault_sidecar` and queued through the existing dead-letter queue.
- `src/core/vault.py::write_atomic_artifact` is now the shared Phase-2 writer for new migrations: it writes bytes to a temp file under the vault, verifies checksum/size, moves to the canonical sha256 blob path, writes the sidecar, optionally runs a DB callback, and returns `partial=True` for sidecar/DB failures after the blob exists.
- Rebuild reporting exists in `src/core/rebuild_report.py` and covers sidecar scan, raw payload table hints, DB-only, sidecar-only, blob-only, missing file, size mismatch, and checksum mismatch states.
- Generic media reconciliation repair now canonicalizes recovered files: `_redownload` writes through `write_atomic_artifact`, then updates `media_items.file_path/file_size/sha256` and `metadata.vault_artifact` so repaired legacy rows point at vault blobs.
- Media/raw rebuild rehearsal exists in `src/core/rebuild_rehearsal.py` and `python -m src.main rebuild-rehearsal`: media sidecars and raw-payload sidecars can be materialized into scratch SQLite tables without touching production Postgres.
- Collector DB backups are implemented with vault-mirror checks, status reporting, and retention tests.
- Dashboard and Telegram hourly status now include vault health, artifact partial/queued counts, backup status, hourly ingest, scoped rate-limit/auth failures, Telegram FloodWait events, browser extension hook/ingest counters, decoded-frame freshness, and stale Chrome extension version warnings.
- Dashboard media joins now prefer stable source keys over file paths: WhatsApp chat media resolves by `wa_<platform_message_id>` with path fallback, and YouTube channel videos expose both thumbnail and stored-video media IDs.
- Passive Strava browser route fallback is implemented in extension v1.21.23 and `/social/strava-streams`: when the real browser loads a Strava route stream, the bridge archives the raw payload and upserts `strava_gps_streams` plus `strava_activities.summary_polyline`. Browser-observed Strava stream HTTP 429/401/403 responses are now emitted even when no route JSON is parseable and are stored as durable `rate_limit_events` under `browser_strava_streams`.
- Priority-driven Strava browser route capture is implemented in extension v1.21.23, `/social/strava-route-queue`, `/social/strava-route-visit`, and `/strava/route-capture-queue`: the browser gets one missing-route activity at a time, ordered by Tier 1/2 proximity and collector target priority, while respecting active GPS-stream 429 cooldowns and recent visit TTLs.
- Analyzer priority hints are imported by `src/core/priority_hints.py`, merged into `collection_targets.metadata.analyzer_priority_hint`, used by target loading, and surfaced on the Targets dashboard.
- `src/core/scrape_pacing.py` now provides a tunable one-shot pre-cooldown retry delay (`COLLECTOR_PRE_COOLDOWN_RETRY_*`). Instagram Playwright profile fetches, Strava GPS stream fetches, and Search HTTP fetch paths use it; recovered Strava/Search 429s are logged without creating an active cooldown, while repeated 429s still open scoped cooldowns where the collector can safely skip that exact scope.
- Beeper, Telegram, WhatsApp, Lemon8, YouTube, TikTok, Website, Search, Instagram headless/extension media, GitHub direct media, GitHub bulk avatar artifacts, shared profile-photo, generic `BaseCollector.save_file`, and Strava activity photo/route-map downloads now write physical bytes through `write_atomic_artifact` or `write_atomic_artifact_from_path` to canonical `media/blobs/<sha256>` paths while retaining per-occurrence `media_items` rows and sidecars where a concrete source occurrence should be indexed. TikTok Playwright fallback uses vault-temp intermediate files and deletes them after re-ingest, so browser fallback no longer leaves duplicate legacy MP4s. Duplicate-row cleanup in `BaseCollector.insert_media_item` preserves canonical blob files and only removes legacy per-occurrence duplicate files. The collector dashboard media resolver accepts both legacy media-root paths and vault-backed blob paths.
- `src/core/health.py` no longer imports the full DB connection module for Docker health checks; live Telegram/WhatsApp health checks dropped from roughly 50s to roughly 10s and fit the configured 30s timeout.
- Still not complete: source-by-source migration proof, full Tier 1 raw payload coverage, and restore replay from a real DB dump. GitHub bulk avatar range writes artifact sidecars only and intentionally does not create `media_items` rows for unknown numeric IDs.

**Document Version**: 1.0
**Created**: 2026-07-20
**Clarification Rounds**: 5
**Quality Score**: 99/100
