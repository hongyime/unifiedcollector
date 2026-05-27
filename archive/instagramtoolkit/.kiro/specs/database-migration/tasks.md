# Implementation Plan: Database Migration

## Overview

Replace all JSON flat-file persistence with a relational database (SQLite default, PostgreSQL optional) behind a thin abstraction layer. All existing manager-class public APIs remain unchanged. A one-shot migration script imports existing JSON data and renames originals to `.bak` only after a successful insert.

## Tasks

- [x] 1. Database abstraction layer
  - [x] 1.1 Create `lib/db/__init__.py` — package init that exports `DatabaseManager` and all repository classes
    - _Requirements: 1.1, 1.2_

  - [x] 1.2 Create `lib/db/backends.py` — `BaseBackend` ABC and `SQLiteBackend` / `PostgreSQLBackend` concrete classes
    - Implement `connect()`, `placeholder()`, `upsert_syntax()`, and `close()` on each backend
    - `SQLiteBackend.connect()` must enable WAL mode (`PRAGMA journal_mode=WAL`) and set file permissions to `0o600` on creation
    - `PostgreSQLBackend` raises `ImportError` with install instructions when `psycopg2` is absent
    - _Requirements: 1.1, 1.2, 1.3, 11.2, 11.4_

  - [x] 1.3 Create `lib/db/schema.py` — all `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` DDL strings
    - Tables: `profiles`, `profile_snapshots`, `relationships`, `usernames`, `username_following_status`, `profile_access_attempts`, `profile_access_summary`, `operation_progress`, `batch_state`, `account_cooldowns`, `account_quotas`
    - _Requirements: 1.4_

  - [x] 1.4 Create `lib/db/manager.py` — `DatabaseManager` class
    - Parse `DATABASE_URL` env var; instantiate correct backend
    - Implement `get_connection()` context manager with commit/rollback
    - Implement `execute`, `executemany`, `fetchone`, `fetchall` — all results returned as `dict`
    - Implement `create_schema()` (idempotent; calls schema DDL from `schema.py`)
    - Implement `close()`
    - Use parameterised queries throughout; never interpolate user values into SQL strings
    - _Requirements: 1.1–1.8, 11.1_

  - [ ]* 1.5 Write property test for schema creation idempotency
    - **Property 1: Schema creation is idempotent**
    - **Validates: Requirement 1.4**

- [x] 2. Repository classes
  - [x] 2.1 Create `lib/db/repositories/profile_repository.py` — `ProfileRepository`
    - Implement `upsert_profile`, `get_profile`, `get_all_profiles`, `get_top_by_followers`, `get_top_by_following`, `filter_by_follower_range`, `get_snapshots`
    - `upsert_profile` must insert a snapshot row on every call
    - _Requirements: 2.1–2.8_

  - [ ]* 2.2 Write property tests for `ProfileRepository`
    - **Property 2: Profile upsert round-trip** — `upsert_profile` then `get_profile` returns matching fields — **Validates: Requirements 2.1, 2.3**
    - **Property 3: Every upsert_profile call produces a snapshot** — N calls → N snapshot rows — **Validates: Requirement 2.2**
    - **Property 4: get_snapshots respects ordering and limit** — **Validates: Requirements 2.4, 2.5**
    - **Property 5: filter_by_follower_range returns only in-range profiles** — **Validates: Requirement 2.6**

  - [x] 2.3 Create `lib/db/repositories/relationship_repository.py` — `RelationshipRepository`
    - Implement `upsert_relationship`, `bulk_upsert`, `get_relationships`, `get_followers`, `get_following`, `get_mutual`, `relationship_exists`, `get_all_usernames`
    - `bulk_upsert` must deduplicate within the batch before executing
    - _Requirements: 3.1–3.7_

  - [ ]* 2.4 Write property tests for `RelationshipRepository`
    - **Property 6: bulk_upsert deduplicates and returns correct count** — **Validates: Requirements 3.1, 3.2**
    - **Property 7: get_mutual returns the intersection of followers and following** — **Validates: Requirement 3.3**
    - **Property 8: relationship_exists round-trip** — **Validates: Requirements 3.4, 3.5**

  - [x] 2.5 Create `lib/db/repositories/profile_access_repository.py` — `ProfileAccessRepository`
    - Implement `record_attempt`, `get_profile_summary`, `get_accessible_accounts`, `get_best_account`, `cleanup_old_attempts`, `cleanup_inactive_profiles`, `get_statistics`
    - `record_attempt` must upsert `profile_access_summary` in the same transaction
    - _Requirements: 4.1–4.6_

  - [ ]* 2.6 Write property tests for `ProfileAccessRepository`
    - **Property 9: record_attempt increments total_attempts monotonically** — **Validates: Requirements 4.1, 4.2**
    - **Property 10: cleanup_old_attempts removes only expired rows** — **Validates: Requirement 4.4**

  - [x] 2.7 Create `lib/db/repositories/operation_progress_repository.py` — `OperationProgressRepository`
    - Implement `upsert_progress`, `get_status`, `get_completed`, `get_failed`, `get_pending`, `get_remaining`, `get_statistics`, `upsert_batch_state`, `get_batch_state`, `archive_operation`
    - _Requirements: 5.1–5.6_

  - [ ]* 2.8 Write property tests for `OperationProgressRepository`
    - **Property 11: get_remaining returns set-difference of all_usernames minus done** — **Validates: Requirement 5.3**
    - **Property 12: archive_operation isolates deletion to one operation_id** — **Validates: Requirement 5.4**
    - **Property 13: batch_state round-trip** — **Validates: Requirement 5.5**

  - [x] 2.9 Create `lib/db/repositories/account_cooldown_repository.py` — `AccountCooldownRepository`
    - Implement `put_on_cooldown`, `is_on_cooldown`, `get_remaining`, `clear_cooldown`, `get_available`
    - `is_on_cooldown` must delete the expired row as a side-effect when cooldown has passed
    - _Requirements: 6.1–6.5_

  - [ ]* 2.10 Write property tests for `AccountCooldownRepository`
    - **Property 14: is_on_cooldown reflects until_ts relative to current time** — **Validates: Requirements 6.1, 6.2**
    - **Property 15: get_available returns only non-cooldown accounts** — **Validates: Requirement 6.3**

  - [x] 2.11 Create `lib/db/repositories/account_quota_repository.py` — `AccountQuotaRepository`
    - Implement `record_profile_view`, `record_action`, `get_usage`, `reset_if_new_day`
    - _Requirements: 7.1–7.4_

  - [ ]* 2.12 Write property tests for `AccountQuotaRepository`
    - **Property 16: record_profile_view and record_action accumulate correctly** — **Validates: Requirements 7.1, 7.2**

  - [x] 2.13 Create `lib/db/repositories/username_repository.py` — `UsernameRepository`
    - Implement `add_username`, `get_by_source`, `get_all`, `update_metadata`, `update_last_accessed`, `update_following_status`, `remove`, `exists`
    - `add_username` returns `True` on insert, `False` if username already exists
    - `remove` must cascade-delete `username_following_status` rows
    - _Requirements: 8.1–8.6_

  - [ ]* 2.14 Write property tests for `UsernameRepository`
    - **Property 17: add_username is idempotent on duplicates** — **Validates: Requirements 8.1, 8.2**
    - **Property 18: exists round-trip** — **Validates: Requirements 8.3, 8.4**
    - **Property 19: update_following_status round-trip** — **Validates: Requirement 8.5**

  - [x] 2.15 Create `lib/db/repositories/__init__.py` — export all seven repository classes
    - _Requirements: 1.1_

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Migrate manager classes to use repositories
  - [x] 4.1 Migrate `UserMetadataManager` — replace `_load_metadata()` / `_save_metadata()` with `ProfileRepository`
    - All existing public method signatures must remain identical
    - Inject `DatabaseManager` via constructor or module-level singleton
    - _Requirements: 10.1_

  - [x] 4.2 Migrate `ProfileAccessTracker` — replace `_load_access_data()` / `save_access_data()` with `ProfileAccessRepository`
    - All existing public method signatures must remain identical
    - _Requirements: 10.2_

  - [x] 4.3 Migrate `RelationshipCollector` — replace JSON I/O with `RelationshipRepository` and `UsernameRepository`
    - All existing public method signatures must remain identical
    - _Requirements: 10.3_

  - [x] 4.4 Migrate `ProgressManager` — replace `_load_progress()` / `save_progress()` / `_load_batch_state()` / `save_batch_state()` with `OperationProgressRepository`
    - All existing public method signatures must remain identical
    - _Requirements: 10.4_

  - [x] 4.5 Migrate `AccountCooldownManager` — replace `_load()` / `_save()` with `AccountCooldownRepository`
    - All existing public method signatures must remain identical
    - _Requirements: 10.5_

  - [x] 4.6 Migrate `AccountQuotaManager` — replace `_load()` / `_save()` with `AccountQuotaRepository`
    - All existing public method signatures must remain identical
    - _Requirements: 10.6_

  - [x] 4.7 Migrate `UsernameDatabase` — replace JSON persistence and `usernames.txt` access with `UsernameRepository`
    - All existing public method signatures must remain identical
    - _Requirements: 10.7_

- [x] 5. JSON migration script and CLI command
  - [x] 5.1 Create `lib/db/migrate_json.py` — `migrate_json_to_db(data_dir, db_manager)` function
    - Migrate all nine sources: `user_profiles.json`, `relationships.json`, `usernames.txt`, `username_database.json`, `profile_access.json`, `spider_progress.json`, `download_progress.json`, `account_cooldowns.json`, `account_quotas.json`
    - Each file is processed in its own transaction; rename to `.bak` only after successful commit
    - A failure on one record must be caught, recorded in the report, and processing must continue
    - Missing files are skipped and recorded as skipped — no exception raised
    - MUST NOT delete or modify `.env`, `sessions/`, or the `data/` directory itself
    - Return a report dict with `migrated`, `errors`, and `skipped` keys
    - _Requirements: 9.1–9.8, 12.2_

  - [x] 5.2 Wire `python main.py db-migrate` CLI command that calls `migrate_json_to_db` and prints the report
    - _Requirements: 9.1_

  - [ ]* 5.3 Write property tests for the migration script
    - **Property 20: Migration inserts all records from JSON source files** — **Validates: Requirement 9.1**
    - **Property 21: Migration is idempotent** — running twice produces same DB state — **Validates: Requirement 9.3**

- [x] 6. `.env.example` template
  - Create `.env.example` as a new file (never touch `.env`)
  - Include a commented SQLite example and a commented PostgreSQL example for `DATABASE_URL`
  - _Requirements: 12.1, 12.2_

- [x] 7. Unit tests for all repositories
  - [x] 7.1 Create `tests/test_db_profile_repository.py` — unit tests for `ProfileRepository` using `sqlite:///:memory:`
    - Cover happy-path CRUD, idempotency of upsert, snapshot insertion on every call, `filter_by_follower_range`, `get_top_by_followers`, `get_top_by_following`
    - _Requirements: 13.1, 2.1–2.8_

  - [x] 7.2 Create `tests/test_db_relationship_repository.py` — unit tests for `RelationshipRepository`
    - Cover `bulk_upsert` deduplication, `get_mutual`, `relationship_exists`, `get_followers`, `get_following`
    - _Requirements: 13.1, 3.1–3.7_

  - [x] 7.3 Create `tests/test_db_profile_access_repository.py` — unit tests for `ProfileAccessRepository`
    - Cover `record_attempt` summary upsert, `cleanup_old_attempts`, `get_best_account`, `get_statistics`
    - _Requirements: 13.1, 4.1–4.6_

  - [x] 7.4 Create `tests/test_db_operation_progress_repository.py` — unit tests for `OperationProgressRepository`
    - Cover `upsert_progress`, `get_remaining`, `archive_operation`, `upsert_batch_state` / `get_batch_state`
    - _Requirements: 13.1, 5.1–5.6_

  - [x] 7.5 Create `tests/test_db_account_cooldown_repository.py` — unit tests for `AccountCooldownRepository`
    - Cover `is_on_cooldown` with expired row deletion, `get_available`, `put_on_cooldown`, `clear_cooldown`
    - _Requirements: 13.1, 6.1–6.5_

  - [x] 7.6 Create `tests/test_db_account_quota_repository.py` — unit tests for `AccountQuotaRepository`
    - Cover `record_profile_view`, `record_action`, `reset_if_new_day`, `get_usage`
    - _Requirements: 13.1, 7.1–7.4_

  - [x] 7.7 Create `tests/test_db_username_repository.py` — unit tests for `UsernameRepository`
    - Cover `add_username` idempotency, `exists`, `update_following_status`, `remove` cascade
    - _Requirements: 13.1, 8.1–8.6_

  - [x] 7.8 Create `tests/test_db_migration.py` — unit tests for `migrate_json_to_db`
    - Use `tmp_path` fixtures with synthetic JSON files
    - Verify idempotency, error isolation, correct `.bak` renaming, skipped-file reporting
    - Verify `.env`, `sessions/`, and `data/` directory are untouched
    - _Requirements: 13.5, 9.1–9.8_

- [x] 8. Property-based tests with Hypothesis
  - [x] 8.1 Create `tests/test_db_properties.py` — Hypothesis property tests (minimum 100 iterations per property via `@settings(max_examples=100)`)
    - Include all 21 properties from the design document
    - Use `sqlite:///:memory:` for each test; set up fresh schema per test
    - _Requirements: 13.2, 13.4_

- [x] 9. Integration tests
  - [x] 9.1 Create `tests/test_db_integration.py` — integration tests against a real SQLite file in a temp directory
    - Full round-trip: manager class → repository → DB → repository → manager class for each of the 7 managers
    - Verify `UserMetadataManager.update_profile` writes to both `profiles` and `profile_snapshots`
    - Verify `RelationshipCollector` writes to both `relationships` and `usernames`
    - Verify `ProgressManager.mark_completed` removes username from `get_remaining`
    - Verify migration script produces DB contents matching original JSON fixtures
    - _Requirements: 13.3_

- [x] 10. Final checkpoint — Ensure all tests pass
  - Run `pytest tests/test_db_*.py -v` and confirm all tests pass. Ask the user if any failures arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- NEVER delete or overwrite `.env`, `sessions/`, or `data/` — the migration script renames JSON files to `.bak` only after a successful DB insert
- `.env.example` is a new file; `.env` must never be touched
- All manager class public APIs must remain unchanged after task 4
- Each task references specific requirements for traceability
- Property tests validate universal correctness properties (Properties 1–21 from design)
- Unit tests validate specific examples and edge cases
