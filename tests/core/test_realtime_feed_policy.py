from src.core.realtime_feed_policy import get_realtime_feed_policy


def test_realtime_feed_policy_requires_both_whatsapp_sources_for_group_media_exclusion(monkeypatch):
    monkeypatch.setenv("REALTIME_POST_FEED_ENABLED", "1")
    monkeypatch.setenv("REALTIME_POST_FEED_EXCLUDED_SOURCES", "beeper_whatsapp, whatsapp")

    policy = get_realtime_feed_policy()

    assert policy["enabled"] is True
    assert policy["excluded_sources"] == ["beeper_whatsapp", "whatsapp"]
    assert policy["whatsapp_group_media_excluded"] is True
