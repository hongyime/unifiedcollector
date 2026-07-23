CREATE INDEX IF NOT EXISTS idx_strava_route_repair_candidates
    ON strava_activities (start_date DESC)
    WHERE summary_polyline IS NULL
       OR summary_polyline = ''
       OR stream_status IS NULL
       OR start_latlng IS NULL
       OR end_latlng IS NULL
       OR privacy_zone_start IS NULL
       OR privacy_zone_end IS NULL;
