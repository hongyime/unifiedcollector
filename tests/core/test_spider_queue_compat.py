from src.core.spider_queue_compat import (
    LEGACY_QUEUE_SPECS,
    mirror_legacy_queue_batch,
    normalize_legacy_status,
)
from src.core.spider_graph_store import bounded_limit, shared_spider_enabled


def test_legacy_queue_status_aliases_are_normalized():
    assert normalize_legacy_status("pending") == "pending"
    assert normalize_legacy_status("active") == "pending"
    assert normalize_legacy_status("processing") == "in_progress"
    assert normalize_legacy_status("done") == "completed"
    assert normalize_legacy_status("unresolvable") == "failed"
    assert normalize_legacy_status("unavailable") == "failed"
    assert normalize_legacy_status("operator_paused") == "unknown"


def test_legacy_queue_specs_cover_current_dedicated_queues():
    assert {
        "instagram", "instagram_targets", "tiktok", "github", "strava",
        "lemon8", "youtube", "x", "telegram",
    } == set(LEGACY_QUEUE_SPECS)
    assert LEGACY_QUEUE_SPECS["github"].target_type_field == "target_type"
    assert not LEGACY_QUEUE_SPECS["instagram_targets"].record_id_is_uuid
    assert LEGACY_QUEUE_SPECS["x"].table == "x_profile_targets"
    instagram = LEGACY_QUEUE_SPECS["instagram"]
    assert instagram.table == "spider_queue"
    assert instagram.record_id_field == "node_id"
    assert instagram.filter_by_platform is True


class _FakeConn:
    def __init__(self, queries):
        self.queries = queries

    async def fetch(self, query, *args):
        self.queries.append(query)
        return []


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool._conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def test_instagram_mirror_query_filters_by_platform():
    queries: list[str] = []
    pool = _FakePool(_FakeConn(queries))
    await mirror_legacy_queue_batch(pool, "instagram", limit=10)
    assert len(queries) == 1
    assert "q.platform = 'instagram'" in queries[0]


async def test_tiktok_mirror_query_has_no_platform_filter():
    queries: list[str] = []
    pool = _FakePool(_FakeConn(queries))
    await mirror_legacy_queue_batch(pool, "tiktok", limit=10)
    assert "q.platform" not in queries[0]


def test_shared_spider_rollout_is_per_platform_and_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GITHUB_SHARED_SPIDER_ENABLED", raising=False)
    monkeypatch.delenv("YOUTUBE_SHARED_SPIDER_ENABLED", raising=False)
    monkeypatch.delenv("LEMON8_SHARED_SPIDER_ENABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_SHARED_SPIDER_ENABLED", raising=False)
    monkeypatch.delenv("FACEBOOK_SHARED_SPIDER_ENABLED", raising=False)
    monkeypatch.setenv("TIKTOK_SHARED_SPIDER_ENABLED", "true")
    assert shared_spider_enabled("github") is False
    assert shared_spider_enabled("youtube") is False
    assert shared_spider_enabled("lemon8") is False
    assert shared_spider_enabled("telegram") is False
    assert shared_spider_enabled("facebook") is False
    assert shared_spider_enabled("tiktok") is True


def test_bounded_spider_limits_clamp_invalid_and_excessive_values(monkeypatch):
    monkeypatch.setenv("GITHUB_SHARED_SPIDER_EDGE_CAP", "9999")
    assert bounded_limit("GITHUB_SHARED_SPIDER_EDGE_CAP", 100, maximum=500) == 500
    monkeypatch.setenv("GITHUB_SHARED_SPIDER_EDGE_CAP", "invalid")
    assert bounded_limit("GITHUB_SHARED_SPIDER_EDGE_CAP", 100, maximum=500) == 100
