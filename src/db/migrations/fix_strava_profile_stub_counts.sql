UPDATE strava_athletes
SET follower_count = NULL
WHERE follower_count = 0;

UPDATE strava_athletes
SET following_count = NULL
WHERE following_count = 0;

UPDATE strava_athletes
SET username = NULL,
    firstname = NULL,
    lastname = NULL
WHERE lower(COALESCE(username, '')) LIKE 'signup for free to see more about %'
   OR lower(COALESCE(firstname, '')) = 'signup';
