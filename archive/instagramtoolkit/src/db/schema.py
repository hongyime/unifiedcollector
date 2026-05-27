"""Database schema DDL statements.

All CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS statements
for the Instagram Toolkit database.
"""

SCHEMA_DDL = [
    # ── profiles ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profiles (
        username            TEXT PRIMARY KEY,
        full_name           TEXT,
        biography           TEXT,
        external_url        TEXT,
        profile_pic_url     TEXT,
        followers_count     INTEGER NOT NULL DEFAULT 0,
        following_count     INTEGER NOT NULL DEFAULT 0,
        media_count         INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER NOT NULL DEFAULT 1,
        is_verified         INTEGER NOT NULL DEFAULT 0,
        last_collected_ts   REAL NOT NULL,
        collected_by        TEXT NOT NULL,
        created_at          REAL NOT NULL DEFAULT (unixepoch()),
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_profiles_followers ON profiles(followers_count DESC)",
    "CREATE INDEX IF NOT EXISTS idx_profiles_is_public  ON profiles(is_public)",

    # ── profile_snapshots ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        username            TEXT NOT NULL REFERENCES profiles(username) ON DELETE CASCADE,
        user_id             TEXT,
        full_name           TEXT,
        biography           TEXT,
        followers_count     INTEGER NOT NULL,
        following_count     INTEGER NOT NULL,
        media_count         INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER NOT NULL DEFAULT 1,
        is_verified         INTEGER NOT NULL DEFAULT 0,
        collected_by        TEXT NOT NULL,
        snapshot_ts         REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snapshots_username ON profile_snapshots(username, snapshot_ts DESC)",

    # ── relationships ─────────────────────────────────────────────────────
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rel_source    ON relationships(source, type)",
    "CREATE INDEX IF NOT EXISTS idx_rel_target    ON relationships(target, type)",
    "CREATE INDEX IF NOT EXISTS idx_rel_collected ON relationships(collected_ts DESC)",

    # ── usernames ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS usernames (
        username            TEXT PRIMARY KEY,
        source_account      TEXT NOT NULL,
        added_ts            REAL NOT NULL DEFAULT (unixepoch()),
        last_accessed_ts    REAL,
        metadata_json       TEXT NOT NULL DEFAULT '{}',
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usernames_source ON usernames(source_account)",

    # ── username_following_status ─────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS username_following_status (
        username            TEXT NOT NULL REFERENCES usernames(username) ON DELETE CASCADE,
        account_name        TEXT NOT NULL,
        is_following        INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (username, account_name)
    )
    """,

    # ── profile_access_attempts ───────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_access_attempts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        target_username     TEXT NOT NULL,
        accessing_account   TEXT NOT NULL,
        can_access          INTEGER NOT NULL DEFAULT 0,
        is_public           INTEGER,
        is_followed         INTEGER NOT NULL DEFAULT 0,
        error_msg           TEXT,
        attempt_ts          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_access_target  ON profile_access_attempts(target_username, attempt_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_access_account ON profile_access_attempts(accessing_account)",

    # ── profile_access_summary ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS profile_access_summary (
        username                TEXT PRIMARY KEY,
        is_public               INTEGER,
        last_checked_ts         REAL,
        last_successful_ts      REAL,
        total_attempts          INTEGER NOT NULL DEFAULT 0,
        known_accessible_by_json TEXT NOT NULL DEFAULT '[]'
    )
    """,

    # ── operation_progress ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS operation_progress (
        operation_id        TEXT NOT NULL,
        username            TEXT NOT NULL,
        status              TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
        details_json        TEXT NOT NULL DEFAULT '{}',
        error_msg           TEXT,
        updated_at          REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (operation_id, username)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_progress_op_status ON operation_progress(operation_id, status)",

    # ── batch_state ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS batch_state (
        operation_id        TEXT PRIMARY KEY,
        operation_type      TEXT NOT NULL,
        state_json          TEXT NOT NULL DEFAULT '{}',
        started_at          REAL NOT NULL DEFAULT (unixepoch()),
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_cooldowns ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_cooldowns (
        account_name        TEXT PRIMARY KEY,
        until_ts            REAL NOT NULL,
        reason              TEXT NOT NULL DEFAULT 'rate-limit',
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_quotas ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS account_quotas (
        account_name        TEXT PRIMARY KEY,
        quota_date          TEXT NOT NULL,
        profile_views       INTEGER NOT NULL DEFAULT 0,
        actions             INTEGER NOT NULL DEFAULT 0,
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,

    # ── account_request_log ───────────────────────────────────────────────
    # Tracks individual request timestamps for sliding window rate limiting
    """
    CREATE TABLE IF NOT EXISTS account_request_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        account_name        TEXT NOT NULL,
        request_type        TEXT NOT NULL CHECK(request_type IN ('profile_view','download','action')),
        timestamp           REAL NOT NULL DEFAULT (unixepoch()),
        machine_id          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_request_log_account_time ON account_request_log(account_name, timestamp DESC)",

    # ── account_rate_limits ───────────────────────────────────────────────
    # Stores per-account limits for each sliding window
    """
    CREATE TABLE IF NOT EXISTS account_rate_limits (
        account_name        TEXT PRIMARY KEY,
        window_1h_limit     INTEGER NOT NULL DEFAULT 180,
        window_3h_limit     INTEGER NOT NULL DEFAULT 400,
        window_5h_limit     INTEGER NOT NULL DEFAULT 600,
        window_1d_limit     INTEGER NOT NULL DEFAULT 2000,
        last_1h_reset       REAL,
        last_3h_reset       REAL,
        last_5h_reset       REAL,
        last_1d_reset       REAL,
        updated_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
]

__all__ = ["SCHEMA_DDL"]

# Schema migration statements for adding new columns to existing tables
# These are ALTER TABLE statements that run after CREATE TABLE IF NOT EXISTS
MIGRATION_DDL = [
    # ---- profiles table migrations ----
    """
    ALTER TABLE profiles ADD COLUMN user_id TEXT
    """,
    """
    ALTER TABLE profiles ADD COLUMN user_id TEXT UNIQUE
    """,
    """
    ALTER TABLE profiles ADD COLUMN profile_pic_phash TEXT
    """,
    """
    ALTER TABLE profiles ADD COLUMN status TEXT DEFAULT 'active'
    """,

    # ---- usernames table migrations ----
    """
    ALTER TABLE usernames ADD COLUMN user_id TEXT
    """,
    """
    ALTER TABLE usernames ADD COLUMN user_id TEXT UNIQUE
    """,
    """
    ALTER TABLE usernames ADD COLUMN spider_status TEXT DEFAULT 'pending'
    """,
    """
    ALTER TABLE usernames ADD COLUMN download_status TEXT DEFAULT 'pending'
    """,
    """
    ALTER TABLE usernames ADD COLUMN filter_reason TEXT
    """,

    # ---- profile_snapshots table migrations (add user_id) ----
    """
    ALTER TABLE profile_snapshots ADD COLUMN user_id TEXT
    """,
    # ---- profile_snapshots: track bio/name/public changes per snapshot ----
    """
    ALTER TABLE profile_snapshots ADD COLUMN full_name TEXT
    """,
    """
    ALTER TABLE profile_snapshots ADD COLUMN biography TEXT
    """,
    """
    ALTER TABLE profile_snapshots ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1
    """,
    """
    ALTER TABLE profile_snapshots ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0
    """,

    # ---- username_history: track when a user_id changes its username ----
    """
    CREATE TABLE IF NOT EXISTS username_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         TEXT NOT NULL,
        username        TEXT NOT NULL,
        first_seen_ts   REAL NOT NULL DEFAULT (unixepoch()),
        last_seen_ts    REAL NOT NULL DEFAULT (unixepoch()),
        UNIQUE(user_id, username)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_uhistory_user_id  ON username_history(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_uhistory_username ON username_history(username)",

    # ---- profile_photo_history new table ----
    """
    CREATE TABLE IF NOT EXISTS profile_photo_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        user_id TEXT,
        photo_url TEXT NOT NULL,
        photo_phash TEXT NOT NULL,
        photo_blob BLOB,
        file_path TEXT,
        detected_at REAL DEFAULT (unixepoch())
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_photo_history_username ON profile_photo_history(username, detected_at DESC)",

    # ---- media_items new table ----
    """
    CREATE TABLE IF NOT EXISTS media_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        user_id TEXT,
        shortcode TEXT UNIQUE,
        media_type TEXT NOT NULL,
        media_url TEXT,
        file_path TEXT,
        file_hash TEXT,
        file_size INTEGER,
        taken_at REAL,
        downloaded_at REAL,
        download_status TEXT DEFAULT 'pending'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_media_username ON media_items(username, shortcode)",
    "CREATE INDEX IF NOT EXISTS idx_media_status ON media_items(download_status, username)",
    # ── account_sessions — per-account last-auth & next scheduled re-auth ────
    """
    CREATE TABLE IF NOT EXISTS account_sessions (
        account_name        TEXT PRIMARY KEY,
        last_auth_ts        REAL,
        next_reauth_ts      REAL,
        fingerprint_ua      TEXT,
        created_at          REAL NOT NULL DEFAULT (unixepoch())
    )
    """,
    # ── download_retry_queue — rate-limit mid-download recovery ──────────────
    """
    CREATE TABLE IF NOT EXISTS download_retry_queue (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        username            TEXT NOT NULL,
        account_name        TEXT,
        fail_ts             REAL NOT NULL DEFAULT (unixepoch()),
        retry_after_ts      REAL,
        reason              TEXT,
        attempt_count       INTEGER NOT NULL DEFAULT 1,
        status              TEXT NOT NULL DEFAULT 'pending'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_retry_queue_status ON download_retry_queue(status, retry_after_ts)",
    # ── account_access — which accounts follow which targets ─────────────────
    """
    CREATE TABLE IF NOT EXISTS account_access (
        username            TEXT NOT NULL,
        account_name        TEXT NOT NULL,
        follows             INTEGER NOT NULL DEFAULT 0,
        last_checked_ts     REAL NOT NULL DEFAULT (unixepoch()),
        PRIMARY KEY (username, account_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_account_access_user ON account_access(username)",
    "CREATE INDEX IF NOT EXISTS idx_account_access_follows ON account_access(account_name, follows)",
]


