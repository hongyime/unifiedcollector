# Design Document: Database Migration

## Overview

The Instagram Toolkit currently persists all state in a collection of JSON flat files scattered across the `data/` directory. While simple to implement, this approach has several limitations: no referential integrity, no efficient querying, no historical time-series data, and fragile concurrent access that requires hand-rolled file locking. This design replaces every JSON file with a relational database (SQLite by default, PostgreSQL as an optional backend) behind a thin abstraction layer, so the rest of the codebase continues to call the same manager-class APIs it uses today.

The migration introduces three new layers: a **database abstraction layer** (`lib/db/`) that exposes a backend-agnostic connection and query interface; a set of **repository classes** that replace the direct JSON read/write logic inside each existing manager; and a **one-shot migration script** (`lib/db/migrate_json.py`) that imports existing JSON data into the new schema. A new capability — **historical follower/following count snapshots** — is added as a first-class table, something the flat-file approach could not support without duplicating entire files.

## Architecture

```mermaid
graph TD
    subgraph "Existing Manager Layer (unchanged API)"
        UMM[UserMetadataManager]
        PAT[ProfileAccessTracker]
        RC[RelationshipCollector]
        PM[ProgressManager]
        ACM[AccountCooldownManager]
        AQM[AccountQuotaManager]
        UDB[UsernameDatabase]
    end

    subgraph "Repository Layer (new)"
        PR[ProfileRepository]
        RR[RelationshipRepository]
        PAR[ProfileAccessRepository]
        OPR[OperationProgressRepository]
        ACR[AccountCooldownRepository]
        AQR[AccountQuotaRepository]
        UNR[UsernameRepository]
    end

    subgraph "Database Abstraction Layer (new)"
        DM[DatabaseManager]
        SB[SQLiteBackend]
        PB[PostgreSQLBackend]
        BA[BaseBackend ABC]
    end

    UMM --> PR
    PAT --> PAR
    RC --> RR
    RC --> UNR
    PM --> OPR
    ACM --> ACR
    AQM --> AQR
    UDB --> UNR

    PR --> DM
    RR --> DM
    PAR --> DM
    OPR --> DM
    ACR --> DM
    AQR --> DM
    UNR --> DM

    DM --> BA
    BA --> SB
    BA --> PB
```

### Key Design Decisions

- **SQLite default, PostgreSQL optional.** SQLite requires zero setup and ships with Python. The backend is selected via a `DATABASE_URL` environment variable: absent or `sqlite:///...` → SQLite; `postgresql://...` → PostgreSQL (requires `psycopg2`).
- **Repository pattern.** Each manager class delegates all persistence to a dedicated repository. Managers keep their existing public method signatures; only their `_load_*` / `_save_*` internals change.
- **No ORM.** Raw SQL with parameterised queries keeps the dependency footprint minimal and makes the PostgreSQL upgrade path explicit.
- **WAL mode for SQLite.** `PRAGMA journal_mode=WAL` allows concurrent readers alongside a single writer, replacing the hand-rolled `FileLock` mechanism.
- **Historical snapshots.** A `profile_snapshots` table records follower/following counts every time a profile is updated, enabling time-series queries that were impossible with the flat-file approach.
- **Backward-compatible migration.** The migration script reads existing JSON files and inserts their data into the DB. Original JSON files are renamed to `*.json.bak` after a successful migration so they can be recovered if needed.

## Sequence Diagrams

### Profile Update Flow (post-migration)

```mermaid
sequenceDiagram
    participant RC as RelationshipCollector
    participant UMM as UserMetadataManager
    participant PR as ProfileRepository
    participant DB as DatabaseManager

    RC->>UMM: update_profile(username, profile_obj, account)
    UMM->>PR: upsert_profile(username, metadata_dict)
    PR->>DB: execute(UPSERT profiles ...)
    PR->>DB: execute(INSERT profile_snapshots ...)
    DB-->>PR: rowcount
    PR-->>UMM: ok
    UMM-->>RC: ok
```

### Operation Progress Flow

```mermaid
sequenceDiagram
    participant PP as InstagramProcessor
    participant PM as ProgressManager
    participant OPR as OperationProgressRepository
    participant DB as DatabaseManager

    PP->>PM: mark_pending(username)
    PM->>OPR: upsert_progress(op_id, username, "pending")
    OPR->>DB: execute(UPSERT operation_progress ...)
    DB-->>OPR: ok

    PP->>PM: mark_completed(username, details)
    PM->>OPR: upsert_progress(op_id, username, "completed", details)
    OPR->>DB: execute(UPSERT operation_progress ...)
    DB-->>OPR: ok
```


## Components and Interfaces

### DatabaseManager

**Purpose**: Single entry point for all database access. Manages connection lifecycle, schema creation, and backend selection.

**Interface**:
```python
class DatabaseManager:
    def __init__(self, database_url: str | None = None) -> None: ...
    def get_connection(self) -> contextlib.AbstractContextManager: ...
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor | psycopg2.cursor: ...
    def executemany(self, sql: str, params_seq: list[tuple]) -> None: ...
    def fetchone(self, sql: str, params: tuple = ()) -> dict | None: ...
    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]: ...
    def create_schema(self) -> None: ...
    def close(self) -> None: ...
```

**Responsibilities**:
- Parse `DATABASE_URL` and instantiate the correct backend
- Expose a `get_connection()` context manager that handles commit/rollback
- Translate row results to `dict` regardless of backend
- Apply schema DDL on first run (idempotent `CREATE TABLE IF NOT EXISTS`)

### BaseBackend (ABC)

**Purpose**: Contract that both SQLite and PostgreSQL backends must satisfy.

**Interface**:
```python
class BaseBackend(ABC):
    @abstractmethod
    def connect(self) -> Any: ...
    @abstractmethod
    def placeholder(self) -> str: ...   # "?" for SQLite, "%s" for psycopg2
    @abstractmethod
    def upsert_syntax(self) -> str: ...  # "INSERT OR REPLACE" vs "INSERT ... ON CONFLICT DO UPDATE"
    @abstractmethod
    def close(self) -> None: ...
```

### ProfileRepository

**Purpose**: Replaces `UserMetadataManager._load_metadata()` / `_save_metadata()`.

**Interface**:
```python
class ProfileRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def upsert_profile(self, username: str, data: dict) -> None: ...
    def get_profile(self, username: str) -> dict | None: ...
    def get_all_profiles(self) -> dict[str, dict]: ...
    def get_top_by_followers(self, n: int) -> list[dict]: ...
    def get_top_by_following(self, n: int) -> list[dict]: ...
    def filter_by_follower_range(self, min_f: int, max_f: int | None) -> list[str]: ...
    def get_snapshots(self, username: str, limit: int = 90) -> list[dict]: ...
```

### RelationshipRepository

**Purpose**: Replaces `RelationshipCollector._load_relationships()` / `_save_relationships()` and the `usernames.txt` flat file.

**Interface**:
```python
class RelationshipRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def upsert_relationship(self, source: str, target: str, rel_type: str,
                            collected_by: str, source_is_public: bool) -> None: ...
    def bulk_upsert(self, relationships: list[dict]) -> int: ...
    def get_relationships(self, source: str | None = None,
                          rel_type: str | None = None) -> list[dict]: ...
    def get_followers(self, username: str) -> list[str]: ...
    def get_following(self, username: str) -> list[str]: ...
    def get_mutual(self, username: str) -> list[str]: ...
    def relationship_exists(self, source: str, target: str, rel_type: str) -> bool: ...
    def get_all_usernames(self) -> list[str]: ...
```

### ProfileAccessRepository

**Purpose**: Replaces `ProfileAccessTracker._load_access_data()` / `save_access_data()`.

**Interface**:
```python
class ProfileAccessRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def record_attempt(self, target: str, account: str, can_access: bool,
                       is_public: bool | None, is_followed: bool, error: str | None) -> None: ...
    def get_profile_summary(self, username: str) -> dict: ...
    def get_accessible_accounts(self, username: str) -> list[str]: ...
    def get_best_account(self, username: str, available: list[str]) -> str | None: ...
    def cleanup_old_attempts(self, days: int = 30) -> int: ...
    def cleanup_inactive_profiles(self, days: int = 30) -> int: ...
    def get_statistics(self) -> dict: ...
```

### OperationProgressRepository

**Purpose**: Replaces `ProgressManager._load_progress()` / `save_progress()` / `_load_batch_state()` / `save_batch_state()`.

**Interface**:
```python
class OperationProgressRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def upsert_progress(self, operation_id: str, username: str,
                        status: str, details: dict | None = None,
                        error: str | None = None) -> None: ...
    def get_status(self, operation_id: str, username: str) -> str | None: ...
    def get_completed(self, operation_id: str) -> list[str]: ...
    def get_failed(self, operation_id: str) -> list[str]: ...
    def get_pending(self, operation_id: str) -> list[str]: ...
    def get_remaining(self, operation_id: str, all_usernames: list[str]) -> list[str]: ...
    def get_statistics(self, operation_id: str) -> dict: ...
    def upsert_batch_state(self, operation_id: str, state: dict) -> None: ...
    def get_batch_state(self, operation_id: str) -> dict: ...
    def archive_operation(self, operation_id: str) -> None: ...
```

### AccountCooldownRepository

**Purpose**: Replaces `AccountCooldownManager._load()` / `_save()`.

**Interface**:
```python
class AccountCooldownRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def put_on_cooldown(self, account: str, until_ts: float, reason: str) -> None: ...
    def is_on_cooldown(self, account: str) -> bool: ...
    def get_remaining(self, account: str) -> float: ...
    def clear_cooldown(self, account: str) -> None: ...
    def get_available(self, accounts: list[str]) -> list[str]: ...
```

### AccountQuotaRepository

**Purpose**: Replaces `AccountQuotaManager._load()` / `_save()`.

**Interface**:
```python
class AccountQuotaRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def record_profile_view(self, account: str, count: int = 1) -> None: ...
    def record_action(self, account: str, count: int = 1) -> None: ...
    def get_usage(self, account: str) -> dict: ...
    def reset_if_new_day(self, account: str) -> None: ...
```

### UsernameRepository

**Purpose**: Replaces `UsernameDatabase` JSON persistence and the `usernames.txt` flat file.

**Interface**:
```python
class UsernameRepository:
    def __init__(self, db: DatabaseManager) -> None: ...
    def add_username(self, username: str, source_account: str,
                     metadata: dict | None = None) -> bool: ...
    def get_by_source(self, source_account: str) -> list[dict]: ...
    def get_all(self) -> list[dict]: ...
    def update_metadata(self, username: str, metadata: dict) -> bool: ...
    def update_last_accessed(self, username: str) -> bool: ...
    def update_following_status(self, username: str, account: str, following: bool) -> bool: ...
    def remove(self, username: str) -> bool: ...
    def exists(self, username: str) -> bool: ...
```

## Data Models

### Table: `profiles`

Replaces `data/user_profiles.json`.

```sql
CREATE TABLE IF NOT EXISTS profiles (
    username            TEXT PRIMARY KEY,
    full_name           TEXT,
    biography           TEXT,
    external_url        TEXT,
    profile_pic_url     TEXT,
    followers_count     INTEGER NOT NULL DEFAULT 0,
    following_count     INTEGER NOT NULL DEFAULT 0,
    media_count         INTEGER NOT NULL DEFAULT 0,
    is_public           INTEGER NOT NULL DEFAULT 1,   -- BOOLEAN (0/1)
    is_verified         INTEGER NOT NULL DEFAULT 0,
    last_collected_ts   REAL NOT NULL,                -- Unix timestamp
    collected_by        TEXT NOT NULL,
    created_at          REAL NOT NULL DEFAULT (unixepoch()),
    updated_at          REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_profiles_followers ON profiles(followers_count DESC);
CREATE INDEX IF NOT EXISTS idx_profiles_is_public  ON profiles(is_public);
```

**Validation Rules**:
- `username` must match `^[a-zA-Z0-9._]+$`
- `followers_count`, `following_count`, `media_count` >= 0
- `is_public`, `is_verified` are 0 or 1

### Table: `profile_snapshots`

New capability — historical follower/following count tracking.

```sql
CREATE TABLE IF NOT EXISTS profile_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL REFERENCES profiles(username) ON DELETE CASCADE,
    followers_count     INTEGER NOT NULL,
    following_count     INTEGER NOT NULL,
    media_count         INTEGER NOT NULL DEFAULT 0,
    collected_by        TEXT NOT NULL,
    snapshot_ts         REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_snapshots_username ON profile_snapshots(username, snapshot_ts DESC);
```

### Table: `relationships`

Replaces `data/relationships.json`.

```sql
CREATE TABLE IF NOT EXISTS relationships (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    source                      TEXT NOT NULL,
    target                      TEXT NOT NULL,
    type                        TEXT NOT NULL CHECK(type IN ('followers','following')),
    collected_by                TEXT NOT NULL,
    source_is_public            INTEGER NOT NULL DEFAULT 1,
    source_followed_by_collector INTEGER NOT NULL DEFAULT 0,
    collected_ts                REAL NOT NULL DEFAULT (unixepoch()),
    UNIQUE(source, target, type)
);
CREATE INDEX IF NOT EXISTS idx_rel_source      ON relationships(source, type);
CREATE INDEX IF NOT EXISTS idx_rel_target      ON relationships(target, type);
CREATE INDEX IF NOT EXISTS idx_rel_collected   ON relationships(collected_ts DESC);
```

### Table: `usernames`

Replaces `data/usernames.txt` and `data/username_database.json`.

```sql
CREATE TABLE IF NOT EXISTS usernames (
    username            TEXT PRIMARY KEY,
    source_account      TEXT NOT NULL,
    added_ts            REAL NOT NULL DEFAULT (unixepoch()),
    last_accessed_ts    REAL,
    metadata_json       TEXT NOT NULL DEFAULT '{}',   -- JSON blob for extensible metadata
    created_at          REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_usernames_source ON usernames(source_account);

CREATE TABLE IF NOT EXISTS username_following_status (
    username            TEXT NOT NULL REFERENCES usernames(username) ON DELETE CASCADE,
    account_name        TEXT NOT NULL,
    is_following        INTEGER NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (username, account_name)
);
```

### Table: `profile_access_attempts`

Replaces `data/profile_access.json`.

```sql
CREATE TABLE IF NOT EXISTS profile_access_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_username     TEXT NOT NULL,
    accessing_account   TEXT NOT NULL,
    can_access          INTEGER NOT NULL DEFAULT 0,
    is_public           INTEGER,                      -- NULL = unknown
    is_followed         INTEGER NOT NULL DEFAULT 0,
    error_msg           TEXT,
    attempt_ts          REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_access_target  ON profile_access_attempts(target_username, attempt_ts DESC);
CREATE INDEX IF NOT EXISTS idx_access_account ON profile_access_attempts(accessing_account);

-- Materialised summary updated by triggers or application logic
CREATE TABLE IF NOT EXISTS profile_access_summary (
    username                TEXT PRIMARY KEY,
    is_public               INTEGER,
    last_checked_ts         REAL,
    last_successful_ts      REAL,
    total_attempts          INTEGER NOT NULL DEFAULT 0,
    known_accessible_by_json TEXT NOT NULL DEFAULT '[]'  -- JSON array of account names
);
```

### Table: `operation_progress`

Replaces `data/spider_progress.json`, `data/download_progress.json`, and `data/following_media_download_progress.json`.

```sql
CREATE TABLE IF NOT EXISTS operation_progress (
    operation_id        TEXT NOT NULL,   -- e.g. "spider_20240115_143022"
    username            TEXT NOT NULL,
    status              TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
    details_json        TEXT NOT NULL DEFAULT '{}',
    error_msg           TEXT,
    updated_at          REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (operation_id, username)
);
CREATE INDEX IF NOT EXISTS idx_progress_op_status ON operation_progress(operation_id, status);

CREATE TABLE IF NOT EXISTS batch_state (
    operation_id        TEXT PRIMARY KEY,
    operation_type      TEXT NOT NULL,
    state_json          TEXT NOT NULL DEFAULT '{}',
    started_at          REAL NOT NULL DEFAULT (unixepoch()),
    updated_at          REAL NOT NULL DEFAULT (unixepoch())
);
```

### Table: `account_cooldowns`

Replaces `data/account_cooldowns.json`.

```sql
CREATE TABLE IF NOT EXISTS account_cooldowns (
    account_name        TEXT PRIMARY KEY,
    until_ts            REAL NOT NULL,
    reason              TEXT NOT NULL DEFAULT 'rate-limit',
    created_at          REAL NOT NULL DEFAULT (unixepoch())
);
```

### Table: `account_quotas`

Replaces `data/account_quotas.json`.

```sql
CREATE TABLE IF NOT EXISTS account_quotas (
    account_name        TEXT PRIMARY KEY,
    quota_date          TEXT NOT NULL,   -- YYYY-MM-DD
    profile_views       INTEGER NOT NULL DEFAULT 0,
    actions             INTEGER NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL DEFAULT (unixepoch())
);
```

## Algorithmic Pseudocode

### Algorithm: DatabaseManager.get_connection()

```pascal
PROCEDURE get_connection()
  OUTPUT: context manager that yields a DB connection

  SEQUENCE
    conn <- backend.connect()

    TRY
      YIELD conn
      conn.commit()
    EXCEPT Exception AS e
      conn.rollback()
      RAISE e
    FINALLY
      IF backend IS SQLiteBackend THEN
        // SQLite: return to connection pool (thread-local)
        pass
      ELSE
        conn.close()
      END IF
    END TRY
  END SEQUENCE
END PROCEDURE
```

**Preconditions**: `backend` is initialised and connected  
**Postconditions**: All statements within the context are committed atomically, or rolled back on error  
**Loop Invariants**: N/A

### Algorithm: RelationshipRepository.bulk_upsert()

```pascal
PROCEDURE bulk_upsert(relationships)
  INPUT:  relationships — list of dicts with keys: source, target, type, collected_by, source_is_public
  OUTPUT: count of rows inserted or updated

  SEQUENCE
    IF relationships IS EMPTY THEN
      RETURN 0
    END IF

    // Deduplicate within the batch before hitting the DB
    seen <- empty set
    deduped <- empty list
    FOR each r IN relationships DO
      key <- (r.source, r.target, r.type)
      IF key NOT IN seen THEN
        seen.add(key)
        deduped.append(r)
      END IF
    END FOR

    // Single transaction for atomicity
    WITH db.get_connection() AS conn DO
      sql <- "INSERT OR REPLACE INTO relationships
              (source, target, type, collected_by, source_is_public, collected_ts)
              VALUES (?, ?, ?, ?, ?, ?)"
      params_seq <- [(r.source, r.target, r.type, r.collected_by,
                      r.source_is_public, current_time()) FOR r IN deduped]
      conn.executemany(sql, params_seq)
    END WITH

    RETURN len(deduped)
  END SEQUENCE
END PROCEDURE
```

**Preconditions**: `relationships` is a list (may be empty); each dict has required keys  
**Postconditions**: All unique (source, target, type) tuples are present in the DB; duplicates within the batch are collapsed  
**Loop Invariants**: `seen` contains only keys already processed; `deduped` contains only unique entries

### Algorithm: ProfileRepository.upsert_profile()

```pascal
PROCEDURE upsert_profile(username, data)
  INPUT:  username — string; data — dict of profile fields
  OUTPUT: none (side-effect: DB updated, snapshot inserted)

  SEQUENCE
    now <- current_unix_timestamp()

    WITH db.get_connection() AS conn DO
      // Upsert main profile row
      conn.execute(
        "INSERT INTO profiles (username, full_name, biography, external_url,
          profile_pic_url, followers_count, following_count, media_count,
          is_public, is_verified, last_collected_ts, collected_by, updated_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(username) DO UPDATE SET
           full_name=excluded.full_name,
           followers_count=excluded.followers_count,
           following_count=excluded.following_count,
           media_count=excluded.media_count,
           is_public=excluded.is_public,
           is_verified=excluded.is_verified,
           last_collected_ts=excluded.last_collected_ts,
           collected_by=excluded.collected_by,
           updated_at=excluded.updated_at",
        (username, data.full_name, data.biography, ...)
      )

      // Always insert a snapshot for time-series tracking
      conn.execute(
        "INSERT INTO profile_snapshots
           (username, followers_count, following_count, media_count, collected_by, snapshot_ts)
         VALUES (?,?,?,?,?,?)",
        (username, data.followers_count, data.following_count,
         data.media_count, data.collected_by, now)
      )
    END WITH
  END SEQUENCE
END PROCEDURE
```

**Preconditions**: `username` is non-empty; `data` contains at minimum `followers_count`, `following_count`, `collected_by`  
**Postconditions**: `profiles` row for `username` reflects latest data; a new row exists in `profile_snapshots`  
**Loop Invariants**: N/A

### Algorithm: OperationProgressRepository.get_remaining()

```pascal
PROCEDURE get_remaining(operation_id, all_usernames)
  INPUT:  operation_id — string; all_usernames — list of strings
  OUTPUT: list of usernames not yet completed or failed

  SEQUENCE
    rows <- db.fetchall(
      "SELECT username FROM operation_progress
       WHERE operation_id = ? AND status IN ('completed','failed')",
      (operation_id,)
    )
    done <- set(row.username FOR row IN rows)

    remaining <- [u FOR u IN all_usernames IF u NOT IN done]
    RETURN remaining
  END SEQUENCE
END PROCEDURE
```

**Preconditions**: `operation_id` is a valid string; `all_usernames` is a list  
**Postconditions**: Returns only usernames not in `done`; order of `all_usernames` is preserved  
**Loop Invariants**: `done` is immutable after the DB query; each username is checked exactly once

### Algorithm: JSON Migration Script

```pascal
PROCEDURE migrate_json_to_db(data_dir, db_manager)
  INPUT:  data_dir — path to data/ directory; db_manager — initialised DatabaseManager
  OUTPUT: migration report dict

  SEQUENCE
    report <- {migrated: {}, errors: {}, skipped: {}}

    // 1. Profiles (user_profiles.json)
    profiles_path <- data_dir / "user_profiles.json"
    IF file_exists(profiles_path) THEN
      data <- json_load(profiles_path)
      FOR each username, profile IN data.items() DO
        TRY
          ProfileRepository(db_manager).upsert_profile(username, profile)
          report.migrated["profiles"] += 1
        EXCEPT Exception AS e
          report.errors["profiles"].append({username: str(e)})
        END TRY
      END FOR
      rename(profiles_path, profiles_path + ".bak")
    END IF

    // 2. Relationships (relationships.json)
    rels_path <- data_dir / "relationships.json"
    IF file_exists(rels_path) THEN
      rels <- json_load(rels_path)
      count <- RelationshipRepository(db_manager).bulk_upsert(rels)
      report.migrated["relationships"] <- count
      rename(rels_path, rels_path + ".bak")
    END IF

    // 3. Usernames (usernames.txt)
    usernames_path <- data_dir / "usernames.txt"
    IF file_exists(usernames_path) THEN
      lines <- read_lines(usernames_path)
      repo <- UsernameRepository(db_manager)
      FOR each line IN lines DO
        username <- line.strip()
        IF username IS NOT EMPTY THEN
          repo.add_username(username, source_account="migrated", metadata={migrated: true})
          report.migrated["usernames"] += 1
        END IF
      END FOR
      rename(usernames_path, usernames_path + ".bak")
    END IF

    // 4. Profile access (profile_access.json)
    // 5. Progress files (spider_progress.json, download_progress.json, batch_state.json)
    // 6. Account cooldowns / quotas
    // ... (same pattern: load JSON, insert rows, rename to .bak)

    RETURN report
  END SEQUENCE
END PROCEDURE
```

**Preconditions**: `db_manager` has been initialised and schema created; `data_dir` exists  
**Postconditions**: All migrated JSON data is present in the DB; original files renamed to `*.bak`; report contains counts and any errors  
**Loop Invariants**: Each file is processed independently; a failure in one file does not abort others

## Key Functions with Formal Specifications

### `DatabaseManager.__init__(database_url)`

```python
def __init__(self, database_url: str | None = None) -> None
```

**Preconditions**:
- `database_url` is `None`, a `sqlite:///...` URI, or a `postgresql://...` URI
- If PostgreSQL URL, `psycopg2` is installed

**Postconditions**:
- `self.backend` is an instance of `SQLiteBackend` or `PostgreSQLBackend`
- Schema DDL has been applied (all tables exist)
- WAL mode enabled for SQLite backends

**Loop Invariants**: N/A

---

### `ProfileRepository.get_snapshots(username, limit)`

```python
def get_snapshots(self, username: str, limit: int = 90) -> list[dict]
```

**Preconditions**:
- `username` is a non-empty string
- `limit` >= 1

**Postconditions**:
- Returns at most `limit` rows from `profile_snapshots` for `username`
- Rows are ordered by `snapshot_ts DESC` (most recent first)
- Returns `[]` if no snapshots exist for `username`
- No mutations to any table

**Loop Invariants**: N/A

---

### `ProfileAccessRepository.record_attempt(target, account, can_access, ...)`

```python
def record_attempt(self, target: str, account: str, can_access: bool,
                   is_public: bool | None, is_followed: bool,
                   error: str | None) -> None
```

**Preconditions**:
- `target` and `account` are non-empty strings
- `can_access`, `is_followed` are booleans

**Postconditions**:
- A new row is inserted into `profile_access_attempts`
- `profile_access_summary` row for `target` is upserted:
  - `total_attempts` incremented by 1
  - `last_checked_ts` set to current time
  - If `can_access` is True: `last_successful_ts` updated; `account` added to `known_accessible_by_json` if not already present
  - If `is_public` is not None: `is_public` field updated

**Loop Invariants**: N/A

---

### `AccountCooldownRepository.is_on_cooldown(account)`

```python
def is_on_cooldown(self, account: str) -> bool
```

**Preconditions**:
- `account` is a non-empty string

**Postconditions**:
- Returns `True` if a row exists in `account_cooldowns` for `account` AND `until_ts > current_time()`
- Returns `False` otherwise
- If cooldown has expired, the row is deleted as a side-effect (lazy cleanup)
- No other tables are mutated

**Loop Invariants**: N/A

---

### `OperationProgressRepository.archive_operation(operation_id)`

```python
def archive_operation(self, operation_id: str) -> None
```

**Preconditions**:
- `operation_id` is a non-empty string

**Postconditions**:
- All rows in `operation_progress` with `operation_id` are deleted
- The corresponding row in `batch_state` is deleted
- No other operation IDs are affected

**Loop Invariants**: N/A

## Example Usage

```python
# --- Initialisation (replaces direct JSON file access) ---
from db.manager import DatabaseManager
from db.repositories import (
    ProfileRepository, RelationshipRepository,
    ProfileAccessRepository, OperationProgressRepository,
    AccountCooldownRepository, AccountQuotaRepository,
    UsernameRepository,
)

# SQLite (default — no env var needed)
db = DatabaseManager()

# PostgreSQL (set DATABASE_URL in .env)
# db = DatabaseManager("postgresql://user:pass@localhost/instagram_toolkit")

profile_repo = ProfileRepository(db)
rel_repo     = RelationshipRepository(db)

# --- UserMetadataManager.update_profile() after migration ---
profile_repo.upsert_profile("alice", {
    "full_name": "Alice Smith",
    "followers_count": 1500,
    "following_count": 300,
    "is_public": True,
    "is_verified": False,
    "collected_by": "account_1",
    "last_collected_ts": time.time(),
})

# --- Historical snapshot query (new capability) ---
snapshots = profile_repo.get_snapshots("alice", limit=30)
# [{"snapshot_ts": 1705000000, "followers_count": 1500, ...}, ...]

# --- Relationship bulk insert ---
new_rels = [
    {"source": "alice", "target": "bob", "type": "following",
     "collected_by": "account_1", "source_is_public": True},
    {"source": "alice", "target": "carol", "type": "following",
     "collected_by": "account_1", "source_is_public": True},
]
count = rel_repo.bulk_upsert(new_rels)

# --- Mutual connections query ---
mutuals = rel_repo.get_mutual("alice")
# ["bob"] if bob also follows alice

# --- Progress tracking ---
op_repo = OperationProgressRepository(db)
op_id = "spider_20240115_143022"
op_repo.upsert_progress(op_id, "alice", "pending")
op_repo.upsert_progress(op_id, "alice", "completed", details={"account_used": "account_1"})
remaining = op_repo.get_remaining(op_id, ["alice", "bob", "carol"])
# ["bob", "carol"]

# --- One-shot JSON migration ---
from db.migrate_json import migrate_json_to_db
report = migrate_json_to_db(data_dir="data", db_manager=db)
print(report)
# {"migrated": {"profiles": 450, "relationships": 12000, "usernames": 500, ...}, "errors": {}}
```

## Correctness Properties

- For all usernames `u`, if `upsert_profile(u, data)` is called, then `get_profile(u)` returns a dict where `followers_count == data["followers_count"]` and a new row exists in `profile_snapshots` for `u`.
- For all `(source, target, type)` triples, `bulk_upsert` is idempotent: calling it twice with the same data produces the same number of rows in `relationships` as calling it once.
- For all accounts `a`, `is_on_cooldown(a)` returns `False` after `until_ts` has passed, and the expired row is removed.
- For all operation IDs `op`, `get_remaining(op, all_usernames)` returns a list that is a subset of `all_usernames` and contains no username whose status is `"completed"` or `"failed"`.
- `migrate_json_to_db` is idempotent: running it twice on the same JSON files (before renaming) produces the same DB state as running it once, because all inserts use `INSERT OR REPLACE` / `ON CONFLICT DO UPDATE`.
- For all usernames `u` in `relationships` (as source or target), `u` is also present in `usernames` after migration (enforced by the migration script inserting into `usernames` before `relationships`).

## Error Handling

### Scenario 1: Database file locked / connection failure

**Condition**: SQLite file is locked by another process, or PostgreSQL is unreachable at startup  
**Response**: `DatabaseManager.__init__` raises `RuntimeError` with a descriptive message; the calling manager class catches it and falls back to a read-only in-memory SQLite instance for the duration of the process  
**Recovery**: On next process start, the lock will have cleared; no data is lost because the in-memory instance does not persist

### Scenario 2: Schema migration on existing DB (version upgrade)

**Condition**: A new column is added to a table in a future version; the existing DB file lacks that column  
**Response**: `create_schema()` uses `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for additive changes; destructive changes require an explicit versioned migration script  
**Recovery**: The schema version is stored in a `schema_version` table; `DatabaseManager` checks it on startup and applies pending migrations in order

### Scenario 3: JSON migration fails mid-way

**Condition**: Process is killed while `migrate_json_to_db` is running  
**Response**: Each file is migrated inside its own transaction; a file is only renamed to `.bak` after its transaction commits successfully  
**Recovery**: Re-running the script skips files already renamed to `.bak`; partially-inserted data is overwritten by `INSERT OR REPLACE`

### Scenario 4: Corrupt or missing JSON file during migration

**Condition**: A JSON file is malformed or absent  
**Response**: The migration script logs a warning and continues with the remaining files; the report's `errors` dict records the failure  
**Recovery**: The original file (if present) is not renamed; the operator can fix it and re-run

### Scenario 5: PostgreSQL driver not installed

**Condition**: `DATABASE_URL` is a `postgresql://` URI but `psycopg2` is not installed  
**Response**: `DatabaseManager.__init__` raises `ImportError` with instructions to run `pip install psycopg2-binary`  
**Recovery**: Install the driver and restart

## Testing Strategy

### Unit Testing Approach

Each repository class is tested in isolation using an in-memory SQLite database (`sqlite:///:memory:`). Tests cover:
- Happy-path CRUD for every public method
- Idempotency of upsert operations
- Correct filtering (e.g. `get_remaining` excludes completed/failed)
- Lazy cooldown expiry in `AccountCooldownRepository.is_on_cooldown`
- Snapshot insertion on every `upsert_profile` call

### Property-Based Testing Approach

**Property Test Library**: `hypothesis`

Key properties to test:
- `bulk_upsert(rels)` is idempotent: `bulk_upsert(rels); bulk_upsert(rels)` produces the same row count as `bulk_upsert(rels)` alone
- `get_remaining(op_id, usernames)` always returns a subset of `usernames`
- For any sequence of `upsert_profile` calls on the same username, `get_snapshots` returns exactly as many rows as there were calls
- `migrate_json_to_db` applied twice to the same data produces the same DB state as applying it once

### Integration Testing Approach

Integration tests run against a real SQLite file (temp directory) and verify:
- Full round-trip: manager class → repository → DB → repository → manager class
- `UserMetadataManager.update_profile` writes to `profiles` and `profile_snapshots`
- `RelationshipCollector.collect_for_user` writes to `relationships` and `usernames`
- `ProgressManager.mark_completed` correctly removes username from `get_remaining`
- Migration script produces a DB whose contents match the original JSON files

## Performance Considerations

- **WAL mode** (SQLite) allows concurrent reads during writes, eliminating the need for `FileLock` in most cases.
- **Bulk inserts** via `executemany` for relationship collection (up to 1000 rows per call) are orders of magnitude faster than individual `INSERT` statements.
- **Indexed columns**: `profiles.followers_count`, `relationships.(source,type)`, `relationships.(target,type)`, `profile_access_attempts.(target_username, attempt_ts)` cover the most common query patterns.
- **Snapshot pruning**: A background cleanup (called at the end of each spider run) deletes `profile_snapshots` rows older than 365 days to prevent unbounded growth.
- **Connection pooling**: For PostgreSQL, `psycopg2`'s connection is reused within a process; a simple thread-local pool is sufficient for the CLI use case.

## Security Considerations

- **Parameterised queries only**: No string interpolation in SQL. All user-supplied values go through the `params` tuple to prevent SQL injection.
- **Database file permissions**: The SQLite file is created with `0o600` permissions (owner read/write only) to protect scraped data.
- **No credentials in DB**: Instagram account passwords remain in `.env`; only account names are stored in the database.
- **PostgreSQL connection string**: Loaded from `DATABASE_URL` environment variable, never hard-coded.

## Dependencies

| Dependency | Purpose | Already present? |
|---|---|---|
| `sqlite3` | SQLite backend | Yes (Python stdlib) |
| `psycopg2-binary` | PostgreSQL backend (optional) | No — install if needed |
| `python-dotenv` | Load `DATABASE_URL` from `.env` | Yes |
| `hypothesis` | Property-based tests | Yes (`.hypothesis/` dir present) |

No new runtime dependencies are required for the default SQLite path.


## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Schema creation is idempotent

For any `DatabaseManager` instance, calling `create_schema()` two or more times SHALL produce the same set of tables as calling it once, with no errors and no duplicate table definitions.

**Validates: Requirement 1.4**

### Property 2: Profile upsert round-trip

For any valid username and profile data dict, calling `upsert_profile(username, data)` followed by `get_profile(username)` SHALL return a dict whose fields are equivalent to the values in `data`.

**Validates: Requirements 2.1, 2.3**

### Property 3: Every upsert_profile call produces a snapshot

For any username and any sequence of N calls to `upsert_profile(username, data)`, the `profile_snapshots` table SHALL contain exactly N rows for that username.

**Validates: Requirement 2.2**

### Property 4: get_snapshots respects ordering and limit

For any username with M snapshots and any limit L where 1 <= L <= M, `get_snapshots(username, L)` SHALL return exactly L rows ordered by `snapshot_ts` descending.

**Validates: Requirements 2.4, 2.5**

### Property 5: filter_by_follower_range returns only in-range profiles

For any min_f, max_f, and any set of profiles with varying follower counts, every username returned by `filter_by_follower_range(min_f, max_f)` SHALL have `followers_count` in the inclusive range `[min_f, max_f]`, and no in-range profile SHALL be omitted.

**Validates: Requirement 2.6**

### Property 6: bulk_upsert deduplicates and returns correct count

For any list of relationship dicts containing duplicate `(source, target, type)` tuples, `bulk_upsert(relationships)` SHALL insert exactly one DB row per unique tuple and return a count equal to the number of unique tuples.

**Validates: Requirements 3.1, 3.2**

### Property 7: get_mutual returns the intersection of followers and following

For any username and any set of relationships, `get_mutual(username)` SHALL return exactly the set of usernames that appear in both `get_followers(username)` and `get_following(username)`.

**Validates: Requirement 3.3**

### Property 8: relationship_exists round-trip

For any `(source, target, rel_type)` triple, `relationship_exists` SHALL return `True` after that relationship has been inserted and `False` before it has been inserted.

**Validates: Requirements 3.4, 3.5**

### Property 9: record_attempt increments total_attempts monotonically

For any target username and any sequence of N calls to `record_attempt`, the `total_attempts` field in `profile_access_summary` for that target SHALL equal N.

**Validates: Requirements 4.1, 4.2**

### Property 10: cleanup_old_attempts removes only expired rows

For any set of access attempts with varying timestamps and any threshold `days`, `cleanup_old_attempts(days)` SHALL delete exactly those rows whose `attempt_ts` is strictly older than `days` days ago, leaving all newer rows intact.

**Validates: Requirement 4.4**

### Property 11: get_remaining returns the set-difference of all_usernames minus done

For any `operation_id` and any list `all_usernames`, `get_remaining(operation_id, all_usernames)` SHALL return exactly the usernames from `all_usernames` that have no row with status `completed` or `failed` for that operation, preserving the original order.

**Validates: Requirement 5.3**

### Property 12: archive_operation isolates deletion to one operation_id

For any two distinct operation IDs A and B with progress rows, calling `archive_operation(A)` SHALL delete all rows for A and leave all rows for B unaffected.

**Validates: Requirement 5.4**

### Property 13: batch_state round-trip

For any `operation_id` and any JSON-serialisable state dict, `upsert_batch_state(operation_id, state)` followed by `get_batch_state(operation_id)` SHALL return a dict equivalent to `state`.

**Validates: Requirement 5.5**

### Property 14: is_on_cooldown reflects until_ts relative to current time

For any account, `is_on_cooldown` SHALL return `True` when `until_ts` is in the future and `False` when `until_ts` is in the past, and an expired row SHALL be deleted as a side-effect of the `False` return.

**Validates: Requirements 6.1, 6.2**

### Property 15: get_available returns only non-cooldown accounts

For any list of accounts with mixed cooldown states, every account returned by `get_available(accounts)` SHALL have `is_on_cooldown` return `False`, and every account not returned SHALL have `is_on_cooldown` return `True`.

**Validates: Requirement 6.3**

### Property 16: record_profile_view and record_action accumulate correctly

For any account and any sequence of N calls to `record_profile_view(account, 1)` on the same quota date, `get_usage(account)["profile_views"]` SHALL equal N. The same invariant holds for `record_action`.

**Validates: Requirements 7.1, 7.2**

### Property 17: add_username is idempotent on duplicates

For any username, the first call to `add_username` SHALL return `True` and insert a row; every subsequent call with the same username SHALL return `False` and leave the row unchanged.

**Validates: Requirements 8.1, 8.2**

### Property 18: exists round-trip

For any username, `exists(username)` SHALL return `True` after `add_username` has been called for it and `False` before.

**Validates: Requirements 8.3, 8.4**

### Property 19: update_following_status round-trip

For any `(username, account, following)` triple, calling `update_following_status` and then querying the `username_following_status` table SHALL reflect the stored `following` value.

**Validates: Requirement 8.5**

### Property 20: Migration inserts all records from JSON source files

For any JSON source file with N valid records, after `migrate_json_to_db` completes the corresponding DB table SHALL contain at least N rows whose data matches the source records.

**Validates: Requirement 9.1**

### Property 21: Migration is idempotent

For any data directory, running `migrate_json_to_db` twice SHALL produce the same DB row count and content as running it once (no duplicate rows introduced on the second run).

**Validates: Requirement 9.3**
