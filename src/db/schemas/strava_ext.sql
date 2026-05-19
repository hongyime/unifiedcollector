-- Strava extended schema: GPS streams, day-level coverage tracking

CREATE TABLE IF NOT EXISTS strava_gps_streams (
    id SERIAL PRIMARY KEY,
    athlete_id VARCHAR(100) NOT NULL,
    activity_id VARCHAR(100) NOT NULL,
    stream_type VARCHAR(30) NOT NULL,
    data JSONB NOT NULL,
    collected_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(activity_id, stream_type)
);

CREATE INDEX IF NOT EXISTS idx_sgs_athlete ON strava_gps_streams(athlete_id);
CREATE INDEX IF NOT EXISTS idx_sgs_activity ON strava_gps_streams(activity_id);

CREATE TABLE IF NOT EXISTS strava_day_coverage (
    athlete_id VARCHAR(100) NOT NULL,
    date DATE NOT NULL,
    has_data BOOLEAN NOT NULL DEFAULT TRUE,
    activity_count INT DEFAULT 0,
    PRIMARY KEY (athlete_id, date)
);
