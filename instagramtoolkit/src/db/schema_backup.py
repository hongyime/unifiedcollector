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
        followers_count     INTEGER NOT NULL,
        following_count     INTEGER NOT NULL,
        media_count         INTEGER NOT NULL DEFAULT 0,
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
]

__all__ = ["SCHEMA_DDL"]


