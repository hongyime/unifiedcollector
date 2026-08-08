from src.collectors import get_collector, list_sources
from src.collectors.instagram_dm import FEATURE_FLAG_ENV, InstagramDmCollector


def test_instagram_dm_registered_but_feature_flagged_off(monkeypatch):
    monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)
    monkeypatch.delenv("COLLECTOR_DISABLED_SOURCES", raising=False)

    collector = get_collector("instagram_dm")

    assert isinstance(collector, InstagramDmCollector)
    assert collector.SOURCE_NAME == "instagram_dm"
    assert collector._enabled is False
    assert "instagram_dm" in list_sources()
