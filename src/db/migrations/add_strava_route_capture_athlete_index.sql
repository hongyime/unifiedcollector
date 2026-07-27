CREATE INDEX IF NOT EXISTS idx_strava_route_capture_by_athlete
  ON strava_activities (athlete_id, start_date DESC NULLS LAST, platform_activity_id DESC)
  WHERE (summary_polyline IS NULL OR summary_polyline = '')
    AND COALESCE(stream_status, '') NOT IN ('ok', 'incomplete', 'truncated_empty', 'ok_unverifiable');
