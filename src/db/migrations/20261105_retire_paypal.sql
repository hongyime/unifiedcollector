-- Irreversible retirement. Apply only after the replacement runtime has passed
-- smoke checks; this transaction reports aggregate counts without retaining IDs.
DO $$
DECLARE
    before_count bigint := 0;
    deleted_count bigint := 0;
    remaining_count bigint := 0;
    affected bigint := 0;
BEGIN
    SELECT count(*) INTO before_count FROM paypal_profiles;

    DELETE FROM profile_probe_queue WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM collection_action_queue WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM source_health WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM source_controls WHERE source_name = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM service_cursors WHERE service = 'paypal' OR service LIKE 'paypal:%';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM dead_letter_queue WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM provider_webhook_events WHERE provider = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM rate_limit_events WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM source_capability_receipts WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM backfill_lanes WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM media_items WHERE source = 'paypal';
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DELETE FROM paypal_profiles;
    GET DIAGNOSTICS affected = ROW_COUNT; deleted_count := deleted_count + affected;
    DROP TABLE paypal_profiles;

    SELECT count(*) INTO remaining_count FROM profile_probe_queue WHERE source = 'paypal';
    RAISE LOG 'PayPal retirement: profiles_before %, generic_rows_deleted %, queue_rows_remaining %, failures 0',
        before_count, deleted_count, remaining_count;
END $$;

ALTER TABLE provider_webhook_events
    DROP CONSTRAINT IF EXISTS provider_webhook_events_provider_check;
ALTER TABLE provider_webhook_events
    ADD CONSTRAINT provider_webhook_events_provider_check CHECK (provider = 'strava');
