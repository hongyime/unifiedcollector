import pytest

from src.core.optional_rollout import apply_optional_rollout, optional_rollout_report


class FakeConn:
    def __init__(
        self,
        *,
        tables=None,
        candidates=None,
        health=None,
        operational=None,
        rate_limits=None,
    ):
        self.tables = set(tables or {
            "collector_seen_targets",
            "source_health",
            "collector_operational_events",
            "rate_limit_events",
        })
        self.candidates = candidates or []
        self.health = health or []
        self.operational = operational or []
        self.rate_limits = rate_limits or []
        self.executed = []

    async def fetchval(self, query, *args, **kwargs):
        if "to_regclass" in query:
            return args[0] in self.tables
        raise AssertionError(query)

    async def fetch(self, query, *args, **kwargs):
        if "FROM source_health" in query:
            return list(self.health)
        if "FROM collector_operational_events" in query:
            return list(self.operational)
        if "FROM rate_limit_events" in query:
            return list(self.rate_limits)
        if "FROM collector_seen_targets" in query:
            return list(self.candidates[: args[-1]])
        raise AssertionError(query)

    async def execute(self, query, *args, **kwargs):
        self.executed.append((query, args))
        return "INSERT 0 1"


def _candidate(**overrides):
    row = {
        "source": "instagram",
        "target_type": "user",
        "target_key": "alice",
        "target_display": "Alice",
        "origin": "social_users",
        "priority": 7,
        "evidence_count": 3,
        "last_seen_at": None,
        "status": "pending",
        "source_table": "social_users",
        "source_record_id": "ig:alice",
        "metadata": {},
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_optional_rollout_dry_run_reports_seen_registry_candidates():
    conn = FakeConn(candidates=[
        _candidate(target_type="domain", target_key="example.com", source="website"),
        _candidate(target_type="user", target_key="alice", source="instagram"),
    ])

    report = await optional_rollout_report(conn, feature="spiderfoot", stage="dry-run")

    assert report["feature"] == "spiderfoot"
    assert report["stage"] == "dry-run"
    assert report["target_cap"] == 0
    assert report["recommended_action"] == "dry_run"
    assert report["can_proceed"] is True
    assert report["candidate_count"] == 2
    assert report["candidate_preview"][0]["target_hash"]
    assert report["policy"]["weak_lead_only"] is True
    assert report["policy"]["hard_identity_links"] is False


@pytest.mark.asyncio
async def test_optional_rollout_daily100_accepts_indicator_target_types():
    conn = FakeConn(candidates=[
        _candidate(target_type="ip", target_key="203.0.113.5", source="website"),
        _candidate(target_type="phone", target_key="+14155550123", source="telegram"),
        _candidate(target_type="email", target_key="alice@example.com", source="github"),
    ])

    report = await optional_rollout_report(conn, feature="spiderfoot", stage="daily100")

    assert report["target_cap"] == 100
    assert report["candidate_count"] == 3
    assert report["policy"]["target_cap"] == 100


@pytest.mark.asyncio
async def test_optional_rollout_stops_on_health_rate_and_malformed_events():
    conn = FakeConn(
        health=[{"source": "spiderfoot", "status": "degraded", "last_error": "timeout", "updated_at": None}],
        operational=[
            {
                "source": "spiderfoot",
                "event_type": "sidecar",
                "severity": "info",
                "summary": "malformed JSON loop detected",
                "metadata": {},
                "created_at": None,
            }
        ],
        rate_limits=[
            {
                "source": "website",
                "status_code": 429,
                "reason": "cooldown",
                "cooldown_seconds": 600,
                "metadata": {},
                "created_at": None,
            }
        ],
    )

    report = await optional_rollout_report(conn, feature="spiderfoot", stage="five")

    assert report["recommended_action"] == "stop_or_rollback"
    assert report["can_proceed"] is False
    kinds = {item["kind"] for item in report["stop_reasons"]}
    assert {"source_health", "collector_operational_events", "rate_limit_events"} <= kinds


@pytest.mark.asyncio
async def test_apply_spiderfoot_rollout_queues_seen_registry_weak_leads(monkeypatch):
    from src.core import recon

    queued = []

    async def fake_queue_recon_target(conn, **kwargs):
        queued.append(kwargs)
        return {"status": "pending"}

    monkeypatch.setattr(recon, "queue_recon_target", fake_queue_recon_target)
    conn = FakeConn(candidates=[
        _candidate(target_type="user", target_key="alice", source="instagram"),
        _candidate(target_type="domain", target_key="example.com", source="website"),
    ])

    report = await apply_optional_rollout(conn, feature="spiderfoot", stage="five")

    assert report["applied"]["applied"] is True
    assert report["applied"]["queued"] == 2
    assert queued[0]["target_type"] == "username"
    assert queued[0]["source"] == "collector:collector_seen_targets"
    assert queued[0]["scope"]["weak_lead_only"] is True
    assert queued[0]["scope"]["hard_identity_link"] is False
    assert queued[1]["target_type"] == "domain"
    assert any("collector_operational_events" in call[0] for call in conn.executed)


@pytest.mark.asyncio
async def test_apply_spiderfoot_rollout_maps_ipv4_and_channel_labels(monkeypatch):
    from src.core import recon

    queued = []

    async def fake_queue_recon_target(conn, **kwargs):
        queued.append(kwargs)
        return {"status": "pending"}

    monkeypatch.setattr(recon, "queue_recon_target", fake_queue_recon_target)
    conn = FakeConn(candidates=[
        _candidate(target_type="ipv4", target_key="203.0.113.5", source="website"),
        _candidate(target_type="channel", target_key="demo-channel", source="youtube"),
    ])

    report = await apply_optional_rollout(conn, feature="spiderfoot", stage="five")

    assert report["applied"]["queued"] == 2
    assert queued[0]["target_type"] == "ip"
    assert queued[1]["target_type"] == "username"
