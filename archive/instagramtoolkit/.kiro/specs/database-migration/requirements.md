# Requirements Document

## Introduction

The Instagram Toolkit currently persists all application state in JSON flat files under the `data/` directory. This feature replaces every JSON file with a relational database (SQLite by default, PostgreSQL as an optional backend) behind a thin abstraction layer. The existing manager-class public APIs remain unchanged so that no calling code outside `lib/db/` needs to be modified. A one-shot migration script safely imports existing JSON data into the new schema and renames the originals to `.bak`. A new capability — historical follower/following count snapshots — is introduced as a first-class table.

## Glossary

- **DatabaseManager**: The single entry point for all database access; manages connection lifecycle, schema creation, and backend selection.
- **BaseBackend**: Abstract base class that both `SQLiteBackend` and `PostgreSQLBackend` must implement.
- **SQLiteBackend**: Concrete backend using Python's built-in `sqlite3` module with WAL journal mode.
- **PostgreSQLBackend**: Concrete backend using `psycopg2`; selected when `DATABASE_URL` starts with `postgresql://`.
- **ProfileRepository**: Repository that replaces `UserMetadataManager` JSON I/O.
- **RelationshipRepository**: Repository that replaces `RelationshipCollector` JSON I/O and `usernames.txt` flat-file access.
- **ProfileAccessRepository**: Repository that replaces `ProfileAccessTracker` JSON I/O.
- **OperationProgressRepository**: Repository that replaces `ProgressManager` JSON I/O for progress and batch-state files.
- **AccountCooldownRepository**: Repository that replaces `AccountCooldownManager` JSON I/O.
- **AccountQuotaRepository**: Repository that replaces `AccountQuotaManager` JSON I/O.
- **UsernameRepository**: Repository that replaces `UsernameDatabase` JSON persistence and `usernames.txt`.
- **MigrationScript**: The one-shot script `lib/db/migrate_json.py` that imports JSON data into the DB.
- **WAL**: Write-Ahead Logging — SQLite journal mode that allows concurrent readers alongside a single writer.
- **DATABASE_URL**: Environment variable that controls backend selection; absent or `sqlite:///...` selects SQLite; `postgresql://...` selects PostgreSQL.
- **Parameterised query**: A SQL statement where all user-supplied values are passed as bound parameters, never interpolated into the SQL string.

## Requirements

### Requirement 1: Database Abstraction Layer

**User Story:** As a developer, I want a backend-agnostic database abstraction layer, so that the rest of the codebase can persist data without knowing whether SQLite or PostgreSQL is in use.

#### Acceptance Criteria

1. WHEN `DATABASE_URL` is absent or begins with `sqlite:///`, THE `DatabaseManager` SHALL instantiate a `SQLiteBackend`.
2. WHEN `DATABASE_URL` begins with `postgresql://`, THE `DatabaseManager` SHALL instantiate a `PostgreSQLBackend`.
3. WHEN a `SQLiteBackend` is initialised, THE `DatabaseManager` SHALL enable WAL journal mode (`PRAGMA journal_mode=WAL`).
4. WHEN `DatabaseManager.create_schema()` is called more than once, THE `DatabaseManager` SHALL apply all DDL statements idempotently without raising an error or duplicating tables.
5. THE `DatabaseManager` SHALL expose `execute`, `executemany`, `fetchone`, and `fetchall` methods that accept a SQL string and a parameters tuple.
6. THE `DatabaseManager` SHALL translate all row results to `dict` objects regardless of which backend is active.
7. WHEN a database operation raises an exception inside `get_connection()`, THE `DatabaseManager` SHALL roll back the transaction and re-raise the exception.
8. WHEN a SQLite database file is created, THE `DatabaseManager` SHALL set its file-system permissions to `0o600`.

### Requirement 2: Profile Repository

**User Story:** As a developer, I want a `ProfileRepository` that replaces `UserMetadataManager` JSON I/O, so that profile data is stored relationally with full query support.

#### Acceptance Criteria

1. WHEN `ProfileRepository.upsert_profile(username, data)` is called, THE `ProfileRepository` SHALL insert or update the corresponding row in the `profiles` table.
2. WHEN `ProfileRepository.upsert_profile(username, data)` is called, THE `ProfileRepository` SHALL insert a new row in the `profile_snapshots` table recording the current `followers_count`, `following_count`, `media_count`, `collected_by`, and timestamp.
3. WHEN `ProfileRepository.get_profile(username)` is called for a username that exists, THE `ProfileRepository` SHALL return a `dict` containing all stored profile fields.
4. IF `ProfileRepository.get_profile(username)` is called for a username that does not exist, THEN THE `ProfileRepository` SHALL return `None`.
5. WHEN `ProfileRepository.get_snapshots(username, limit)` is called, THE `ProfileRepository` SHALL return at most `limit` rows from `profile_snapshots` for that username, ordered by `snapshot_ts` descending.
6. WHEN `ProfileRepository.filter_by_follower_range(min_f, max_f)` is called, THE `ProfileRepository` SHALL return only the usernames whose `followers_count` is within the inclusive range `[min_f, max_f]`.
7. WHEN `ProfileRepository.get_top_by_followers(n)` is called, THE `ProfileRepository` SHALL return the `n` profiles with the highest `followers_count`, ordered descending.
8. WHEN `ProfileRepository.get_top_by_following(n)` is called, THE `ProfileRepository` SHALL return the `n` profiles with the highest `following_count`, ordered descending.

### Requirement 3: Relationship Repository

**User Story:** As a developer, I want a `RelationshipRepository` that replaces `RelationshipCollector` JSON I/O, so that follower/following relationships are stored with referential integrity and efficient querying.

#### Acceptance Criteria

1. WHEN `RelationshipRepository.bulk_upsert(relationships)` is called with a list containing duplicate `(source, target, type)` tuples, THE `RelationshipRepository` SHALL insert only one row per unique tuple.
2. WHEN `RelationshipRepository.bulk_upsert(relationships)` is called, THE `RelationshipRepository` SHALL return the count of unique rows inserted or updated.
3. WHEN `RelationshipRepository.get_mutual(username)` is called, THE `RelationshipRepository` SHALL return only the usernames that appear in both the `followers` and `following` sets for that username.
4. WHEN `RelationshipRepository.relationship_exists(source, target, rel_type)` is called after that relationship has been inserted, THE `RelationshipRepository` SHALL return `True`.
5. IF `RelationshipRepository.relationship_exists(source, target, rel_type)` is called for a relationship that has never been inserted, THEN THE `RelationshipRepository` SHALL return `False`.
6. WHEN `RelationshipRepository.get_followers(username)` is called, THE `RelationshipRepository` SHALL return all usernames where a row with `target=username` and `type='followers'` exists.
7. WHEN `RelationshipRepository.get_following(username)` is called, THE `RelationshipRepository` SHALL return all usernames where a row with `source=username` and `type='following'` exists.

### Requirement 4: Profile Access Repository

**User Story:** As a developer, I want a `ProfileAccessRepository` that replaces `ProfileAccessTracker` JSON I/O, so that access attempts are recorded with timestamps and the best-access account can be queried efficiently.

#### Acceptance Criteria

1. WHEN `ProfileAccessRepository.record_attempt(target, account, can_access, is_public, is_followed, error)` is called, THE `ProfileAccessRepository` SHALL insert a new row into `profile_access_attempts`.
2. WHEN `ProfileAccessRepository.record_attempt` is called, THE `ProfileAccessRepository` SHALL upsert a row in `profile_access_summary` incrementing `total_attempts` by 1 and updating `last_checked_ts`.
3. WHEN `ProfileAccessRepository.record_attempt` is called with `can_access=True`, THE `ProfileAccessRepository` SHALL update `last_successful_ts` and add `account` to `known_accessible_by_json` if not already present.
4. WHEN `ProfileAccessRepository.cleanup_old_attempts(days)` is called, THE `ProfileAccessRepository` SHALL delete only the `profile_access_attempts` rows whose `attempt_ts` is older than `days` days from the current time.
5. WHEN `ProfileAccessRepository.get_best_account(username, available)` is called, THE `ProfileAccessRepository` SHALL return the account from `available` that most recently successfully accessed `username`, or `None` if no successful access exists.
6. WHEN `ProfileAccessRepository.get_statistics()` is called, THE `ProfileAccessRepository` SHALL return a `dict` containing aggregate counts of total attempts, successful attempts, and unique profiles tracked.

### Requirement 5: Operation Progress Repository

**User Story:** As a developer, I want an `OperationProgressRepository` that replaces `ProgressManager` JSON I/O, so that operation progress and batch state are stored atomically and queryable by status.

#### Acceptance Criteria

1. WHEN `OperationProgressRepository.upsert_progress(operation_id, username, status, details, error)` is called, THE `OperationProgressRepository` SHALL insert or update the row for `(operation_id, username)` in `operation_progress` with the given status.
2. WHEN `OperationProgressRepository.get_status(operation_id, username)` is called, THE `OperationProgressRepository` SHALL return the current status string for that entry, or `None` if no row exists.
3. WHEN `OperationProgressRepository.get_remaining(operation_id, all_usernames)` is called, THE `OperationProgressRepository` SHALL return only the usernames from `all_usernames` that do not have a `completed` or `failed` status for that `operation_id`.
4. WHEN `OperationProgressRepository.archive_operation(operation_id)` is called, THE `OperationProgressRepository` SHALL delete all rows in `operation_progress` and `batch_state` for that `operation_id` only, leaving all other operation IDs unaffected.
5. WHEN `OperationProgressRepository.upsert_batch_state(operation_id, state)` is called followed by `get_batch_state(operation_id)`, THE `OperationProgressRepository` SHALL return a `dict` equivalent to the stored `state`.
6. WHEN `OperationProgressRepository.get_statistics(operation_id)` is called, THE `OperationProgressRepository` SHALL return a `dict` with counts of `pending`, `completed`, and `failed` entries for that operation.

### Requirement 6: Account Cooldown Repository

**User Story:** As a developer, I want an `AccountCooldownRepository` that replaces `AccountCooldownManager` JSON I/O, so that account cooldowns are enforced with precise timestamp comparisons.

#### Acceptance Criteria

1. WHEN `AccountCooldownRepository.is_on_cooldown(account)` is called and a row exists with `until_ts` greater than the current time, THE `AccountCooldownRepository` SHALL return `True`.
2. WHEN `AccountCooldownRepository.is_on_cooldown(account)` is called and the cooldown has expired, THE `AccountCooldownRepository` SHALL return `False` and delete the expired row.
3. WHEN `AccountCooldownRepository.get_available(accounts)` is called, THE `AccountCooldownRepository` SHALL return only the accounts from `accounts` that are not currently on cooldown.
4. WHEN `AccountCooldownRepository.put_on_cooldown(account, until_ts, reason)` is called, THE `AccountCooldownRepository` SHALL insert or replace the cooldown row for that account.
5. WHEN `AccountCooldownRepository.clear_cooldown(account)` is called, THE `AccountCooldownRepository` SHALL delete the cooldown row for that account if it exists.

### Requirement 7: Account Quota Repository

**User Story:** As a developer, I want an `AccountQuotaRepository` that replaces `AccountQuotaManager` JSON I/O, so that per-account daily usage quotas are tracked and automatically reset each day.

#### Acceptance Criteria

1. WHEN `AccountQuotaRepository.record_profile_view(account, count)` is called, THE `AccountQuotaRepository` SHALL increment `profile_views` for that account by `count` for the current quota date.
2. WHEN `AccountQuotaRepository.record_action(account, count)` is called, THE `AccountQuotaRepository` SHALL increment `actions` for that account by `count` for the current quota date.
3. WHEN `AccountQuotaRepository.reset_if_new_day(account)` is called and the stored `quota_date` differs from today's date, THE `AccountQuotaRepository` SHALL reset `profile_views` and `actions` to 0 and update `quota_date` to today.
4. WHEN `AccountQuotaRepository.get_usage(account)` is called, THE `AccountQuotaRepository` SHALL return a `dict` containing `profile_views`, `actions`, and `quota_date` for that account.

### Requirement 8: Username Repository

**User Story:** As a developer, I want a `UsernameRepository` that replaces `UsernameDatabase` JSON persistence and the `usernames.txt` flat file, so that username records are stored with source tracking and following-status per account.

#### Acceptance Criteria

1. WHEN `UsernameRepository.add_username(username, source_account, metadata)` is called for a username that does not yet exist, THE `UsernameRepository` SHALL insert the row and return `True`.
2. WHEN `UsernameRepository.add_username(username, source_account, metadata)` is called for a username that already exists, THE `UsernameRepository` SHALL return `False` without modifying the existing row.
3. WHEN `UsernameRepository.exists(username)` is called after that username has been added, THE `UsernameRepository` SHALL return `True`.
4. IF `UsernameRepository.exists(username)` is called for a username that has never been added, THEN THE `UsernameRepository` SHALL return `False`.
5. WHEN `UsernameRepository.update_following_status(username, account, following)` is called, THE `UsernameRepository` SHALL upsert the row in `username_following_status` for `(username, account)` with the given `following` value.
6. WHEN `UsernameRepository.remove(username)` is called, THE `UsernameRepository` SHALL delete the username row and all associated `username_following_status` rows via cascade.

### Requirement 9: JSON Migration Script

**User Story:** As an operator, I want a one-shot migration script that imports all existing JSON data into the database, so that I can transition to the new storage backend without losing historical data.

#### Acceptance Criteria

1. WHEN `migrate_json_to_db(data_dir, db_manager)` is called and a JSON source file exists, THE `MigrationScript` SHALL insert all records from that file into the corresponding database table.
2. WHEN `migrate_json_to_db` completes successfully for a given JSON file, THE `MigrationScript` SHALL rename that file to `<original_name>.bak`.
3. WHEN `migrate_json_to_db` is called a second time on the same data directory, THE `MigrationScript` SHALL produce the same database state as after the first run (idempotent upserts, no duplicate rows).
4. IF a single record fails to migrate, THEN THE `MigrationScript` SHALL record the error in the migration report and continue processing the remaining records without aborting.
5. WHEN `migrate_json_to_db` completes, THE `MigrationScript` SHALL return a report `dict` containing counts of migrated records per table and any per-record errors.
6. THE `MigrationScript` SHALL NOT delete or modify the `.env` file, the `sessions/` directory, or the `data/` directory itself.
7. WHEN a JSON source file does not exist, THE `MigrationScript` SHALL skip that file and record it as skipped in the migration report without raising an error.
8. THE `MigrationScript` SHALL migrate all nine data sources: `user_profiles.json`, `relationships.json`, `usernames.txt`, `username_database.json`, `profile_access.json`, `spider_progress.json`, `download_progress.json`, `account_cooldowns.json`, and `account_quotas.json`.

### Requirement 10: Manager API Backward Compatibility

**User Story:** As a developer, I want all existing manager class public APIs to remain unchanged after the migration, so that no calling code outside `lib/db/` needs to be modified.

#### Acceptance Criteria

1. THE `UserMetadataManager` SHALL expose the same public method signatures after migration as before, delegating persistence to `ProfileRepository`.
2. THE `ProfileAccessTracker` SHALL expose the same public method signatures after migration as before, delegating persistence to `ProfileAccessRepository`.
3. THE `RelationshipCollector` SHALL expose the same public method signatures after migration as before, delegating persistence to `RelationshipRepository` and `UsernameRepository`.
4. THE `ProgressManager` SHALL expose the same public method signatures after migration as before, delegating persistence to `OperationProgressRepository`.
5. THE `AccountCooldownManager` SHALL expose the same public method signatures after migration as before, delegating persistence to `AccountCooldownRepository`.
6. THE `AccountQuotaManager` SHALL expose the same public method signatures after migration as before, delegating persistence to `AccountQuotaRepository`.
7. THE `UsernameDatabase` SHALL expose the same public method signatures after migration as before, delegating persistence to `UsernameRepository`.

### Requirement 11: Security and Production Readiness

**User Story:** As a security-conscious operator, I want the database layer to follow production security practices, so that credentials are never exposed and data cannot be corrupted by SQL injection.

#### Acceptance Criteria

1. THE `DatabaseManager` SHALL use parameterised queries for all SQL statements that include user-supplied or externally-sourced values.
2. WHEN a SQLite database file is created by `DatabaseManager`, THE `DatabaseManager` SHALL set the file-system permissions of that file to `0o600`.
3. THE database schema SHALL NOT contain any columns that store account passwords, session tokens, or API credentials.
4. WHERE `PostgreSQLBackend` is used, THE `DatabaseManager` SHALL accept the full connection string only via the `DATABASE_URL` environment variable and SHALL NOT hard-code any credentials.

### Requirement 12: Environment Configuration

**User Story:** As a developer setting up the project, I want an `.env.example` template that documents the `DATABASE_URL` variable, so that I know how to configure the database backend.

#### Acceptance Criteria

1. THE project SHALL include an `.env.example` file containing a `DATABASE_URL` entry with a commented SQLite example and a commented PostgreSQL example.
2. THE `.env` file, `sessions/` directory, and `data/` directory SHALL NOT be deleted or overwritten by any part of the database migration feature.

### Requirement 13: Test Coverage

**User Story:** As a developer, I want comprehensive automated tests for the database layer, so that regressions are caught before deployment.

#### Acceptance Criteria

1. THE test suite SHALL include unit tests for each repository class using an in-memory SQLite database (`sqlite:///:memory:`).
2. THE test suite SHALL include property-based tests using Hypothesis for all repository methods that have universal correctness properties.
3. THE test suite SHALL include integration tests that exercise the full stack from manager class through repository to database.
4. WHEN property-based tests are run, THE test suite SHALL execute a minimum of 100 iterations per property.
5. THE `MigrationScript` SHALL be covered by tests that verify idempotency, error isolation, and correct `.bak` renaming using temporary directories.
