from pathlib import Path


def test_paypal_retirement_migration_purges_only_named_records_and_drops_profiles():
    text = Path("src/db/migrations/20261105_retire_paypal.sql").read_text(encoding="utf-8")

    for table in (
        "profile_probe_queue",
        "collection_action_queue",
        "source_health",
        "source_controls",
        "service_cursors",
        "dead_letter_queue",
        "provider_webhook_events",
        "rate_limit_events",
        "source_capability_receipts",
        "backfill_lanes",
        "media_items",
    ):
        assert f"DELETE FROM {table}" in text
    assert "DROP TABLE paypal_profiles" in text
    assert "profiles_before" in text
    assert "failures 0" in text
    assert "CHECK (provider = 'strava')" in text


def test_strava_webhook_retirement_drops_the_inactive_provider_ledger():
    text = Path("src/db/migrations/20261105_retire_strava_webhooks.sql").read_text(encoding="utf-8")

    assert "DROP TABLE IF EXISTS provider_webhook_events" in text
