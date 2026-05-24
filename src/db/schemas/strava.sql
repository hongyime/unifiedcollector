-- Strava V2 Schema

CREATE TABLE IF NOT EXISTS strava_athletes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_athlete_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(255),
    firstname VARCHAR(255),
    lastname VARCHAR(255),
    profile TEXT, -- URL to profile picture
    city VARCHAR(255),
    state VARCHAR(255),
    country VARCHAR(100),
    sex VARCHAR(1), -- 'M', 'F'
    weight FLOAT, -- kg
    height FLOAT, -- cm
    follower_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_athlete_strava UNIQUE (platform_athlete_id)
);

CREATE TABLE IF NOT EXISTS strava_activities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_activity_id BIGINT UNIQUE NOT NULL,
    athlete_id UUID REFERENCES strava_athletes(id) ON DELETE SET NULL,
    name VARCHAR(500),
    type VARCHAR(100),
    sport_type VARCHAR(100),
    workout_type INTEGER,
    description TEXT,
    distance FLOAT, -- meters
    moving_time INTEGER, -- seconds
    elapsed_time INTEGER, -- seconds
    total_elevation_gain FLOAT, -- meters
    average_speed FLOAT,
    max_speed FLOAT,
    average_heartrate INTEGER,
    max_heartrate INTEGER,
    average_cadence FLOAT,
    max_cadence FLOAT,
    average_temp FLOAT,
    weighted_average_watts INTEGER,
    max_watts INTEGER,
    kilojoules FLOAT,
    calories INTEGER,
    average_watts INTEGER,
    start_date TIMESTAMP,
    start_latlng VARCHAR(50),
    end_latlng VARCHAR(50),
    timezone VARCHAR(100),
    utc_offset INTEGER,
    platform_created_at TIMESTAMP,
    collected_at TIMESTAMP DEFAULT NOW(),
    metadata JSONB,
    CONSTRAINT unique_platform_activity_strava UNIQUE (platform_activity_id)
);

CREATE TABLE IF NOT EXISTS strava_gps_streams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id UUID REFERENCES strava_activities(id) ON DELETE CASCADE UNIQUE,
    latlng JSONB, -- Array of [lat, lng] arrays
    altitude JSONB, -- Array of altitudes
    distance JSONB, -- Cumulative distance
    time JSONB, -- Elapsed time
    heartrate JSONB,
    cadence JSONB,
    watts JSONB,
    speed JSONB,
    grade_smooth JSONB,
    collected_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strava_day_coverage (
    athlete_id BIGINT,
    date DATE,
    has_data BOOLEAN DEFAULT false,
    PRIMARY KEY (athlete_id, date)
);

CREATE TABLE IF NOT EXISTS strava_segments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_segment_id BIGINT UNIQUE NOT NULL,
    name VARCHAR(500),
    activity_type VARCHAR(50),
    distance FLOAT,
    average_grade FLOAT,
    maximum_grade FLOAT,
    elevation_high FLOAT,
    elevation_low FLOAT,
    start_latlng VARCHAR(50),
    end_latlng VARCHAR(50),
    climb_category INTEGER,
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_platform_segment_strava UNIQUE (platform_segment_id)
);

CREATE TABLE IF NOT EXISTS strava_spider_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_athlete_id BIGINT UNIQUE NOT NULL,
    source VARCHAR(50), -- 'followers', 'following', 'kudos', 'manual'
    source_activity_id BIGINT,
    priority INTEGER DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    collected_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT unique_spider_athlete_strava UNIQUE (platform_athlete_id)
);

CREATE INDEX IF NOT EXISTS idx_strava_activities_athlete ON strava_activities(athlete_id);
