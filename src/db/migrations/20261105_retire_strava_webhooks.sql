-- Strava collection is browser/CDP-only. Remove the inactive external
-- provider-callback ledger and its stored delivery payloads.
DROP TABLE IF EXISTS provider_webhook_events;
