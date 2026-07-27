CREATE INDEX IF NOT EXISTS idx_browser_ingest_strava_route_subject
  ON browser_ingest_events (platform, endpoint, subject, created_at DESC)
  WHERE platform = 'strava' AND endpoint = 'strava_route_visit';

CREATE INDEX IF NOT EXISTS idx_strava_route_capture_candidates
  ON strava_activities (start_date DESC, platform_activity_id DESC)
  WHERE (summary_polyline IS NULL OR summary_polyline = '')
    AND COALESCE(stream_status, '') NOT IN ('ok', 'incomplete', 'truncated_empty', 'ok_unverifiable');

CREATE INDEX IF NOT EXISTS idx_strava_route_capture_candidates_recent
  ON strava_activities (start_date DESC NULLS LAST, platform_activity_id DESC)
  WHERE (summary_polyline IS NULL OR summary_polyline = '')
    AND COALESCE(stream_status, '') NOT IN ('ok', 'incomplete', 'truncated_empty', 'ok_unverifiable')
    AND lower(COALESCE(sport_type, type, '')) NOT IN (
      'crossfit', 'elliptical', 'hiit', 'pilates',
      'stairstepper', 'weighttraining', 'workout', 'yoga'
    )
    AND lower(COALESCE(sport_type, type, '')) NOT LIKE 'virtual%'
    AND lower(COALESCE(sport_type, type, '')) NOT LIKE 'indoor%'
    AND (
      start_latlng IS NOT NULL
      OR lower(COALESCE(sport_type, type, '')) ~
         '(run|ride|walk|hike|trail|bike|cycle|ski|snowboard|kayak|canoe|row|paddle|surf|sail|skate|wheelchair|velomobile)'
    );
