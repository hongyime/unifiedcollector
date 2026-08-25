from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import uuid

import pytest

os.environ.setdefault("DASHBOARD_JWT_SECRET", "test-secret-only-for-pytest-do-not-use")
os.environ.setdefault("DASHBOARD_ADMIN_PASSWORD", "x")

from src.dashboard import api as dashboard_api  # noqa: E402


def test_health_normalizes_storage_skip_and_backup_states():
    skipped = dashboard_api._normalize_backup_health_payload(  # noqa: SLF001
        {"status": "skipped"},
        include_storage=False,
    )
    missing = dashboard_api._normalize_backup_health_payload(  # noqa: SLF001
        {"status": "missing", "in_progress": False},
        include_storage=True,
    )
    running = dashboard_api._normalize_backup_health_payload(  # noqa: SLF001
        {"status": "refreshing", "in_progress": True},
        include_storage=True,
    )

    assert skipped["status"] == "skipped_by_config"
    assert missing["status"] == "missing_restorable_dump"
    assert running["status"] == "backup_running"
    assert dashboard_api._vault_health_status({"available": False}, include_storage=True) == "blocked"  # noqa: SLF001
    assert dashboard_api._vault_health_status({"available": True, "writable": True, "counts_error": "TimeoutError"}, include_storage=True) == "degraded"  # noqa: SLF001


class _Acquire:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return None


class _Pool:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = 0

    async def fetch(self, *_args):
        self.fetch_calls += 1
        return self.rows


class _InstagramHealthConn:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    async def fetchval(self, query, *args, **_kwargs):
        if "information_schema.tables" in query:
            return args[0]
        if "information_schema.columns" in query:
            return False
        return None

    async def fetch(self, query, *_args, **_kwargs):
        if "FROM collection_targets" in query:
            return [{"status": "pending", "count": 4}]
        if "FROM instagram_spider_targets" in query:
            return [{"status": "active", "count": 10}]
        if "FROM browser_ingest_events" in query:
            return [{"endpoint": "ingest", "events": 2, "observed": 5, "stored": 3, "latest_at": self.now}]
        if "FROM browser_media_revisit_queue" in query:
            return [{"status": "pending", "count": 1, "due": 1, "stale_claimed": 0, "latest_at": self.now}]
        if "FROM realtime_media_deliveries" in query:
            return [{"status": "delivered", "count": 1}]
        return []

    async def fetchrow(self, query, *_args, **_kwargs):
        if "FROM instagram_profiles" in query:
            return {
                "id": uuid.uuid4(),
                "platform_user_id": "100",
                "username": "ig_user",
                "followers_count": 120,
                "following_count": 80,
                "posts_count": 12,
                "is_private": False,
                "is_verified": False,
                "updated_at": self.now,
                "collected_at": self.now,
            }
        if "FROM instagram_posts" in query:
            return {
                "id": uuid.uuid4(),
                "platform_post_id": "POST1",
                "username": "ig_user",
                "media_type": "image",
                "likes_count": 3,
                "comments_count": 1,
                "platform_created_at": self.now,
                "collected_at": self.now,
            }
        if "FROM media_items" in query:
            return {
                "id": uuid.uuid4(),
                "entity_name": "ig_user",
                "content_type": "image",
                "content_id": "POST1_0",
                "file_size": 12345,
                "collected_at": self.now,
                "source_url": "https://www.instagram.com/p/POST1/",
                "has_vault_artifact": True,
                "has_vault_sidecar": True,
                "has_raw_payload_ref": False,
                "vault_artifact_ok": "true",
            }
        if "FROM browser_ingest_events" in query:
            return {
                "platform": "instagram",
                "endpoint": "ingest",
                "subject": "ig_user",
                "observed_count": 5,
                "stored_count": 3,
                "extension_version": "1.0.0",
                "message_type": "cycleReport",
                "health_status": "ok",
                "health_reason": None,
                "created_at": self.now,
            }
        if "FROM realtime_media_deliveries" in query:
            return {
                "content_id": "POST1_0",
                "status": "delivered",
                "reason": None,
                "file_size": 12345,
                "content_type": "image",
                "target_name": "ig_user",
                "queued_at": self.now,
                "sent_at": self.now,
                "updated_at": self.now,
            }
        if "FROM rate_limit_events" in query and "active_until" in query:
            return None
        if "FROM rate_limit_events" in query:
            return {
                "source": "instagram",
                "account": "acct1",
                "scope": "profile",
                "status_code": 200,
                "reason": "ok",
                "cooldown_seconds": None,
                "created_at": self.now,
            }
        if "FROM source_health" in query:
            return {
                "source": "instagram",
                "status": "healthy",
                "last_success_at": self.now,
                "last_error": None,
                "updated_at": self.now,
            }
        return None


class _DashboardAcquirePool:
    def __init__(self, conn):
        self.conn = conn
        self.released = False

    async def acquire(self):
        return self.conn

    async def release(self, conn):
        assert conn is self.conn
        self.released = True


class _DomainPacingConn:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    async def fetchval(self, query, *_args, **_kwargs):
        if "collector_domain_pacing_events" in query:
            return True
        return None

    async def fetch(self, query, *_args, **_kwargs):
        if "GROUP BY source, registrable_domain" in query:
            return [{
                "source": "website",
                "registrable_domain": "example.com",
                "events": 3,
                "robots_blocked": 1,
                "retry_backoff": 0,
                "http_403": 1,
                "http_429": 0,
                "media_found": 4,
                "pdfs_found": 2,
                "docs_found": 1,
                "videos_found": 1,
                "latest_event_at": self.now,
            }]
        if "GROUP BY source" in query:
            return [{
                "source": "website",
                "domains_seen": 2,
                "recently_active_domains": 1,
                "robots_blocked": 1,
                "retry_backoff": 1,
                "http_403": 1,
                "http_429": 0,
                "media_found": 4,
                "pdfs_found": 2,
                "docs_found": 1,
                "videos_found": 1,
                "latest_event_at": self.now,
            }]
        if "DISTINCT ON (source)" in query:
            return [{
                "source": "website",
                "metadata": {
                    "domain_pacing": {
                        "active_domains": 1,
                        "per_domain_inflight": {"example.com": 1},
                        "max_active_domains": 4,
                        "max_per_domain": 2,
                        "delay_seconds": 1.5,
                        "jitter_seconds": 2.0,
                        "counters": {"media_found": 4},
                    }
                },
                "created_at": self.now,
            }]
        if "SELECT source, registrable_domain, host, event_type" in query:
            return [{
                "source": "website",
                "registrable_domain": "example.com",
                "host": "example.com",
                "event_type": "robots_blocked",
                "url": "https://example.com/private",
                "status_code": None,
                "metadata": {},
                "created_at": self.now,
            }]
        return []


class _StrictNoSourceDomainPacingConn(_DomainPacingConn):
    async def fetch(self, query, *args, **kwargs):
        if "GROUP BY source, registrable_domain" in query:
            assert args == ("24", 50)
        elif "GROUP BY source" in query:
            assert args == ("24",)
        elif "SELECT source, registrable_domain, host, event_type" in query:
            assert args == ("24", 50)
        elif "DISTINCT ON (source)" in query:
            assert args == ()
        return await super().fetch(query, *args, **kwargs)


class _InstagramHealthTimeoutConn(_InstagramHealthConn):
    async def fetchrow(self, query, *args, **kwargs):
        if "FROM media_items" in query:
            raise TimeoutError()
        return await super().fetchrow(query, *args, **kwargs)


class _DomainPacingTimeoutConn(_DomainPacingConn):
    async def fetch(self, query, *args, **kwargs):
        if "FROM collector_domain_pacing_events" in query:
            raise TimeoutError()
        return await super().fetch(query, *args, **kwargs)


class _InstagramHealthHardTimeoutConn(_InstagramHealthConn):
    """Every DB call times out — models peak-load dashboard pressure."""

    async def fetchval(self, query, *args, **kwargs):
        raise TimeoutError()

    async def fetch(self, query, *args, **kwargs):
        raise TimeoutError()

    async def fetchrow(self, query, *args, **kwargs):
        raise TimeoutError()
class _ApiQuotaConn:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    async def fetchval(self, query, *args, **_kwargs):
        if "information_schema.tables" in query:
            return args[0]
        if "reltuples" in query:
            return {
                "github_users": 3,
                "github_repos": 4,
                "github_commits": 5,
                "github_issues": 6,
                "github_issue_comments": 7,
                "github_pr_reviews": 8,
                "github_pr_review_comments": 9,
                "github_edges": 10,
                "media_items": 11,
                "youtube_videos": 12,
            }.get(args[0], 0)
        if "github_users" in query:
            return 3
        if "github_repos" in query:
            return 4
        if "github_commits" in query:
            return 5
        if "github_issues" in query and "is_pull_request = true" in query:
            return 2
        if "github_issues" in query:
            return 6
        if "github_issue_comments" in query:
            return 7
        if "github_pr_reviews" in query:
            return 8
        if "github_pr_review_comments" in query:
            return 9
        if "github_edges" in query:
            return 10
        if "media_items" in query:
            return 11
        if "youtube_videos" in query:
            return 12
        return 0

    async def fetch(self, query, *_args, **_kwargs):
        if "FROM collector_api_quota_snapshots" in query:
            return [{
                "service": "youtube",
                "account": "api_key:test",
                "bucket": "search",
                "quota_date": self.now.date(),
                "reset_at": self.now,
                "used_units": 100,
                "remaining_units": 900,
                "quota_units": 1000,
                "target_units": 900,
                "target_ratio": 0.9,
                "paused": False,
                "metadata": {"endpoint": "search.list"},
                "updated_at": self.now,
            }]
        if "FROM account_quota_usage" in query:
            return [{
                "service": "youtube",
                "account": "api_key:test",
                "day": self.now.date(),
                "requests_today": 100,
                "requests_hour": 20,
                "hour_bucket": "2026-08-13 13:00",
                "requests_week": 100,
                "updated_at": self.now,
            }]
        if "github_spider_queue" in query:
            return [{"status": "pending", "count": 2}, {"status": "done", "count": 5}]
        if "youtube_spider_queue" in query:
            return [{"status": "pending", "count": 1}]
        if "youtube_profile_queue" in query:
            return [{"status": "resolved", "count": 4}]
        if "youtube_videos" in query and "media_status" in query:
            return [{"status": "pending", "count": 6}]
        if "youtube_videos" in query and "transcript_status" in query:
            return [{"status": "stored", "count": 3}]
        if "youtube_videos" in query and "comments_status" in query:
            return [{"status": "stored", "count": 2}]
        return []


def _row(source: str, status: str, created_at: datetime):
    return {
        "source": source,
        "expected_cadence": "24:00:00",
        "latest_data_at": created_at,
        "latest_run_at": created_at,
        "status": status,
        "rows_24h": 10,
        "media_24h": 4,
        "errors_24h": 0,
        "rate_limits_24h": 0,
        "private_access_failures": 0,
        "stale_targets": [],
        "seen_targets_total": 0,
        "seen_targets_backfilled": 0,
        "seen_targets_pending": 0,
        "seen_targets_fresh": 0,
        "seen_targets_stale": 0,
        "seen_targets_newly_discovered": 0,
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_collectors_coverage_refreshes_stale_snapshot(monkeypatch):
    stale_time = datetime.now(timezone.utc) - timedelta(hours=3)
    fresh_time = datetime.now(timezone.utc)
    conn = _FakeConn([_row("x", "stale", stale_time)])
    calls = {"refresh": 0}

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_refresh(refresh_conn):
        assert refresh_conn is conn
        calls["refresh"] += 1
        conn.rows = [_row("x", "fresh", fresh_time)]
        return {"written": 1}

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "build_collection_coverage_snapshot", fake_refresh)
    monkeypatch.setattr(dashboard_api, "_COVERAGE_SNAPSHOT_STALE_SECONDS", 3600)

    result = await dashboard_api.collectors_coverage(_user={})

    assert calls["refresh"] == 1
    assert conn.fetch_calls == 2
    assert result["summary"]["fresh"] == 1
    assert result["snapshot_stale"] is False
    assert result["refresh_attempted"] is True
    assert result["sources"][0]["seen_targets_total"] == 0


@pytest.mark.asyncio
async def test_collectors_coverage_reports_snapshot_age_without_refresh(monkeypatch):
    fresh_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    conn = _FakeConn([_row("telegram", "fresh", fresh_time)])

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_refresh(_conn):
        raise AssertionError("fresh snapshots should not refresh")

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "build_collection_coverage_snapshot", fake_refresh)
    monkeypatch.setattr(dashboard_api, "_COVERAGE_SNAPSHOT_STALE_SECONDS", 3600)

    result = await dashboard_api.collectors_coverage(_user={})

    assert conn.fetch_calls == 1
    assert result["total"] == 1
    assert result["summary"]["fresh"] == 1
    assert result["snapshot_age_seconds"] >= 0
    assert result["refresh_attempted"] is False


@pytest.mark.asyncio
async def test_media_realtime_feed_status_returns_safe_counts(monkeypatch):
    async def fake_status():
        return {
            "available": True,
            "queue_depth": 2,
            "failed_depth": 0,
            "local_fallback_total": 3,
            "local_fallback_by_source": {"youtube": 2, "telegram": 1},
            "local_fallback_by_reason": {"too_large": 2, "telegram_error": 1},
            "local_fallback_last": {"source": "youtube", "target_name": "big.mp4", "reason_bucket": "too_large"},
            "source_counters": {"youtube": {"sent": 4, "local_fallback": 2}},
        }

    async def fake_ledger():
        return {
            "available": True,
            "status_counts": {"delivered": 2, "too_large": 1},
            "reason_counts": {"too_large": 2},
            "by_source": [{"source": "youtube", "too_large": 1}],
            "latest": [],
        }

    monkeypatch.setattr(dashboard_api, "_realtime_feed_status_from_redis", fake_status)
    monkeypatch.setattr(dashboard_api, "_realtime_delivery_ledger_status", fake_ledger)

    result = await dashboard_api.media_realtime_feed_status(_user={})

    assert result["local_fallback_total"] == 3
    assert result["local_fallback_by_source"]["youtube"] == 2
    assert result["local_fallback_by_reason"]["too_large"] == 2
    assert result["local_fallback_last"]["target_name"] == "big.mp4"
    assert result["source_counters"]["youtube"]["local_fallback"] == 2
    assert result["delivery_ledger"]["status_counts"]["too_large"] == 1
    assert result["delivery_ledger"]["reason_counts"]["too_large"] == 2


@pytest.mark.asyncio
async def test_seen_targets_endpoint_returns_summary_and_rows(monkeypatch):
    conn = _FakeConn([])

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_summary(summary_conn, source=None):
        assert summary_conn is conn
        assert source == "instagram"
        return {"instagram": {"total": 10, "backfilled": 6, "pending": 4, "fresh": 5, "stale": 1, "newly_discovered": 2}}

    async def fake_list(list_conn, *, source=None, status=None, target_type=None, limit=200):
        assert list_conn is conn
        assert (source, status, target_type, limit) == ("instagram", "pending", "user", 25)
        return [{"source": "instagram", "target_type": "user", "target_key": "alice", "status": "pending"}]

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "seen_target_summary_by_source", fake_summary)
    monkeypatch.setattr(dashboard_api, "list_seen_targets", fake_list)

    result = await dashboard_api.seen_targets(source="instagram", status="pending", target_type="user", limit=25, _user={})

    assert result["summary"]["instagram"]["total"] == 10
    assert result["targets"][0]["target_key"] == "alice"


@pytest.mark.asyncio
async def test_optional_rollout_status_uses_guarded_monitor(monkeypatch):
    conn = _FakeConn([])

    async def fake_get_pool():
        return _Pool(conn)

    async def fake_report(report_conn, **kwargs):
        assert report_conn is conn
        assert kwargs == {
            "feature": "spiderfoot",
            "stage": "five",
            "window_hours": 24,
            "limit": 5,
        }
        return {
            "feature": "spiderfoot",
            "stage": "five",
            "can_proceed": True,
            "recommended_action": "advance_stage",
            "target_cap": 5,
            "policy": {"weak_lead_only": True},
        }

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "optional_rollout_report", fake_report)

    result = await dashboard_api.optional_rollout_status(
        feature="spiderfoot",
        stage="five",
        window_hours=24,
        limit=5,
        _user={},
    )

    assert result["recommended_action"] == "advance_stage"
    assert result["policy"]["weak_lead_only"] is True


@pytest.mark.asyncio
async def test_instagram_health_reports_operational_stuck_points(monkeypatch):
    conn = _InstagramHealthConn()

    async def fake_get_pool():
        return _Pool(conn)

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "_vault_payload", lambda: {"available": True, "writable": True})

    result = await dashboard_api.instagram_health(_user={})

    assert result["source"] == "instagram"
    assert result["stuck_stage"] == "ok"
    assert result["targets"]["pending"] == 4
    assert result["latest_profile"]["username"] == "ig_user"
    assert result["latest_post"]["platform_post_id"] == "POST1"
    assert "caption" not in result["latest_post"]
    assert result["latest_media"]["has_vault_artifact"] is True
    assert result["browser_ingest_24h"]["ingest"]["stored"] == 3
    assert result["revisit_queue"]["due"] == 1
    assert result["realtime_delivery"]["status_counts"]["delivered"] == 1
    assert result["cooldown"]["active"] is False
    assert result["tuning"]["story_sweep_cap"]["max"] == 8


@pytest.mark.asyncio
async def test_instagram_health_keeps_reporting_when_latest_media_times_out(monkeypatch):
    conn = _InstagramHealthTimeoutConn()

    async def fake_get_pool():
        return _Pool(conn)

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "_vault_payload", lambda: {"available": True, "writable": True})

    result = await dashboard_api.instagram_health(_user={})

    assert result["stuck_stage"] == "media_download"
    assert result["section_errors"]["latest_media"] == "TimeoutError"
    assert result["latest_media"] is None


@pytest.mark.asyncio
async def test_domain_pacing_status_reports_domain_pressure(monkeypatch):
    conn = _DomainPacingConn()
    pool = _DashboardAcquirePool(conn)

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)

    result = await dashboard_api.domain_pacing_status(source="website", _user={})

    assert result["available"] is True
    assert result["sources"][0]["domains_seen"] == 2
    assert result["sources"][0]["robots_blocked"] == 1
    assert result["domains"][0]["pdfs_found"] == 2
    assert result["latest_snapshots"]["website"]["per_domain_inflight"] == {"example.com": 1}
    assert result["events"][0]["event_type"] == "robots_blocked"
    assert pool.released is True


@pytest.mark.asyncio
async def test_domain_pacing_status_without_source_uses_query_specific_args(monkeypatch):
    pool = _DashboardAcquirePool(_StrictNoSourceDomainPacingConn())

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)

    result = await dashboard_api.domain_pacing_status(_user={})

    assert result["available"] is True
    assert result["source"] is None
    assert pool.released is True


@pytest.mark.asyncio
async def test_domain_pacing_status_returns_graceful_payload_on_timeout(monkeypatch):
    """DB-load timeouts must degrade to an explicit unavailable payload, not a 500."""
    pool = _DashboardAcquirePool(_DomainPacingTimeoutConn())

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)

    result = await dashboard_api.domain_pacing_status(source="website", _user={})

    assert result["available"] is False
    assert result["error"] == "timeout"
    assert result["stats_unavailable"] is True
    assert result["sources"] == []
    assert result["latest_snapshots"] == {}
    assert pool.released is True


@pytest.mark.asyncio
async def test_instagram_health_returns_degraded_report_on_hard_timeout(monkeypatch):
    """When every Instagram-health query times out, still return a report skeleton."""

    async def fake_get_pool():
        return _Pool(_InstagramHealthHardTimeoutConn())

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)
    monkeypatch.setattr(dashboard_api, "_vault_payload", lambda: {"available": True})

    result = await dashboard_api.instagram_health(_user={})

    assert result["degraded"] is True
    assert result["error"] == "timeout"
    assert result["tables"] == {}
    assert result["cooldown"] == {"active": False}


@pytest.mark.asyncio
async def test_api_quotas_status_reports_snapshots_and_progress(monkeypatch):
    conn = _ApiQuotaConn()
    pool = _DashboardAcquirePool(conn)

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(dashboard_api, "get_pool", fake_get_pool)

    result = await dashboard_api.api_quotas_status(_user={})

    assert result["available"] is True
    assert result["snapshots"][0]["service"] == "youtube"
    assert result["snapshots"][0]["metadata"]["endpoint"] == "search.list"
    assert result["account_quota_usage"][0]["requests_today"] == 100
    assert result["progress"]["github"]["queues"]["spider"]["pending"] == 2
    assert result["progress"]["github"]["quota_pusher"]["reason"] == "quota_fill_active"
    assert result["progress"]["github"]["quota_pusher"]["batch_size"] == 250
    assert result["progress"]["github"]["tables"]["commits"] == 5
    assert result["progress"]["youtube"]["queues"]["profile"]["resolved"] == 4
    assert result["progress"]["youtube"]["videos"]["total"] == 12
    assert pool.released is True
