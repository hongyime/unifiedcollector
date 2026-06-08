ALTER TABLE strava_activities ADD COLUMN IF NOT EXISTS page_scraped_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_strava_activities_page_scraped_at ON strava_activities (page_scraped_at) WHERE page_scraped_at IS NULL;
