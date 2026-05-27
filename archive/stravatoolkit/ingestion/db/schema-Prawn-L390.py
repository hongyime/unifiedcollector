"""Database schema definition."""
from __future__ import annotations


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS session_state (
    id INTEGER PRIMARY KEY,
    cookie_value TEXT NOT NULL,
    auth_mode TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS athletes (
    athlete_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    avatar_url TEXT,
    is_private INTEGER NOT NULL DEFAULT 0,
    is_following INTEGER NOT NULL DEFAULT 0,
    is_tracked INTEGER NOT NULL DEFAULT 1,
    first_seen_source TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    roster_refreshed_at TEXT,
    backfill_recent_cursor_before TEXT,
    backfill_deep_cursor_before TEXT,
    backfill_oldest_seen_utc TEXT,
    backfill_status TEXT NOT NULL DEFAULT 'pending',
    backfill_completed_at TEXT,
    backfill_recent_completed_at TEXT,
    backfill_last_coverage_check_at TEXT,
    last_crawl_status TEXT,
    backfill_last_issue_code TEXT,
    backfill_last_issue_message TEXT,
    backfill_last_issue_at TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    athlete_id INTEGER NOT NULL REFERENCES athletes(athlete_id),
    activity_name TEXT,
    sport_type TEXT NOT NULL,
    source TEXT NOT NULL,
    start_date_utc TEXT NOT NULL,
    start_date_local TEXT NOT NULL,
    calendar_date TEXT NOT NULL,
    elapsed_time_secs INTEGER,
    start_latlng_lat REAL,
    start_latlng_lon REAL,
    end_latlng_lat REAL,
    end_latlng_lon REAL,
    privacy_zone_start INTEGER NOT NULL DEFAULT 0,
    privacy_zone_end INTEGER NOT NULL DEFAULT 0,
    truncation_point_start_lon REAL,
    truncation_point_start_lat REAL,
    truncation_point_end_lon REAL,
    truncation_point_end_lat REAL,
    stream_status TEXT NOT NULL DEFAULT 'pending',
    streams_raw TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backfill_day_coverage (
    athlete_id INTEGER NOT NULL REFERENCES athletes(athlete_id),
    calendar_date TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    source_month TEXT,
    last_checked_at TEXT NOT NULL,
    PRIMARY KEY (athlete_id, calendar_date)
);

CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(activity_id),
    point_index INTEGER NOT NULL,
    longitude REAL NOT NULL,
    latitude REAL NOT NULL,
    abs_unix_ts INTEGER NOT NULL,
    UNIQUE(activity_id, point_index)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    target_date TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    roster_refreshed INTEGER NOT NULL DEFAULT 0,
    backfill_budget_minutes INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS athlete_photo_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id INTEGER NOT NULL REFERENCES athletes(athlete_id),
    athlete_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    md5_hash TEXT NOT NULL,
    photo_blob BLOB,
    photo_phash TEXT,
    captured_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_photos (
    photo_id TEXT PRIMARY KEY,
    activity_id INTEGER NOT NULL,
    athlete_id INTEGER NOT NULL REFERENCES athletes(athlete_id),
    athlete_name TEXT NOT NULL,
    activity_name TEXT,
    calendar_date TEXT,
    start_date_utc TEXT,
    caption TEXT,
    media_type INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    source_url_large TEXT,
    source_url_thumbnail TEXT,
    local_path TEXT,
    md5_hash TEXT,
    downloaded_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id TEXT NOT NULL UNIQUE,
    sport_type TEXT NOT NULL,
    centroid_start_lat REAL NOT NULL,
    centroid_start_lon REAL NOT NULL,
    centroid_end_lat REAL NOT NULL,
    centroid_end_lon REAL NOT NULL,
    activity_count INTEGER DEFAULT 0,
    athlete_count INTEGER DEFAULT 0,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_cluster_members (
    cluster_id TEXT NOT NULL,
    activity_id INTEGER NOT NULL REFERENCES activities(activity_id),
    PRIMARY KEY (cluster_id, activity_id)
);

CREATE TABLE IF NOT EXISTS route_overlaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id_a INTEGER NOT NULL REFERENCES activities(activity_id),
    activity_id_b INTEGER NOT NULL REFERENCES activities(activity_id),
    overlap_point_count INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(activity_id_a, activity_id_b)
);

CREATE TABLE IF NOT EXISTS co_occurrence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id_a INTEGER NOT NULL REFERENCES athletes(athlete_id),
    athlete_id_b INTEGER NOT NULL REFERENCES athletes(athlete_id),
    co_occurrence_count INTEGER DEFAULT 0,
    last_seen_date TEXT,
    computed_at TEXT NOT NULL,
    UNIQUE(athlete_id_a, athlete_id_b)
);

CREATE TABLE IF NOT EXISTS athlete_stats (
    athlete_id INTEGER PRIMARY KEY REFERENCES athletes(athlete_id),
    total_distance_m REAL DEFAULT 0,
    avg_distance_m REAL DEFAULT 0,
    activity_count INTEGER DEFAULT 0,
    common_start_lat REAL,
    common_start_lon REAL,
    common_end_lat REAL,
    common_end_lon REAL,
    monthly_counts_json TEXT DEFAULT '{}',
    computed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activities_calendar_date ON activities(calendar_date);
CREATE INDEX IF NOT EXISTS idx_activities_athlete_id ON activities(athlete_id);
CREATE INDEX IF NOT EXISTS idx_streams_activity_id ON streams(activity_id);
CREATE INDEX IF NOT EXISTS idx_athletes_is_following ON athletes(is_following);
CREATE INDEX IF NOT EXISTS idx_athletes_is_tracked ON athletes(is_tracked);
CREATE INDEX IF NOT EXISTS idx_athletes_backfill_status ON athletes(backfill_status);
CREATE INDEX IF NOT EXISTS idx_athletes_backfill_deep_cursor ON athletes(backfill_deep_cursor_before);
CREATE INDEX IF NOT EXISTS idx_athletes_backfill_recent_check ON athletes(backfill_last_coverage_check_at);
CREATE INDEX IF NOT EXISTS idx_backfill_day_coverage_status ON backfill_day_coverage(coverage_status, calendar_date);
CREATE INDEX IF NOT EXISTS idx_activity_photos_athlete_id ON activity_photos(athlete_id);
CREATE INDEX IF NOT EXISTS idx_activity_photos_calendar_date ON activity_photos(calendar_date);
CREATE INDEX IF NOT EXISTS idx_athlete_photo_history_athlete_id ON athlete_photo_history(athlete_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_route_cluster_members_activity ON route_cluster_members(activity_id);
"""
