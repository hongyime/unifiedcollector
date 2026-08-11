import asyncio

import pytest

from src.core.recon import normalize_recon_target, queue_recon_target
from src.core import recon_spiderfoot
from src.core.recon_spiderfoot import (
    allowed_modules,
    normalize_observation,
    normalize_spiderfoot_payload,
    run_spiderfoot_once,
    target_in_scope,
)


def test_normalize_recon_target_validates_domain_and_email():
    assert normalize_recon_target("domain", "Example.COM") == ("domain", "example.com")
    assert normalize_recon_target("email", "A@Example.COM") == ("email", "a@example.com")
    with pytest.raises(ValueError):
        normalize_recon_target("domain", "not-a-domain")
    with pytest.raises(ValueError):
        normalize_recon_target("unknown", "example.com")


def test_queue_recon_target_returns_row():
    class Conn:
        async def fetchrow(self, sql, *args):
            assert "INSERT INTO recon_targets" in sql
            assert args[0] == "domain"
            assert args[1] == "example.com"
            assert args[4] == "{}"
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "target_type": args[0],
                "target_value": args[1],
                "source": args[2],
                "priority": args[3],
                "status": "pending",
            }

    row = asyncio.run(queue_recon_target(
        Conn(),
        target_type="domain",
        target_value="Example.COM",
        source="test",
        priority=2,
    ))

    assert row["target_type"] == "domain"
    assert row["target_value"] == "example.com"


def test_queue_recon_target_persists_scope_json():
    class Conn:
        async def fetchrow(self, sql, *args):
            assert "INSERT INTO recon_targets" in sql
            assert args[4] == '{"allowlist": ["example.com"], "modules": ["sfp_dnsresolve"]}'
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "target_type": args[0],
                "target_value": args[1],
                "source": args[2],
                "priority": args[3],
                "status": "pending",
            }

    row = asyncio.run(queue_recon_target(
        Conn(),
        target_type="domain",
        target_value="Example.COM",
        source="test",
        priority=2,
        scope={"allowlist": ["example.com"], "modules": ["sfp_dnsresolve"]},
    ))

    assert row["target_value"] == "example.com"


def test_spiderfoot_scope_modules_and_observation_normalization(monkeypatch):
    monkeypatch.delenv("SPIDERFOOT_MODULES", raising=False)
    monkeypatch.delenv("SPIDERFOOT_ALLOW_INTRUSIVE", raising=False)

    assert allowed_modules() == ["sfp_dnsresolve", "sfp_whois", "sfp_names"]
    assert target_in_scope("sub.example.com", "example.com")
    assert target_in_scope("https://sub.example.com/path", "example.com")
    assert target_in_scope("a@example.com", "example.com")
    assert not target_in_scope("other.net", "example.com")
    assert not target_in_scope("badexample.com", "example.com")

    obs = normalize_observation("00000000-0000-0000-0000-000000000001", {
        "module": "sfp_dnsresolve",
        "type": "DOMAIN_NAME",
        "data": "example.com",
        "confidence": "0.5",
    })

    assert obs["module"] == "sfp_dnsresolve"
    assert obs["observation_type"] == "DOMAIN_NAME"
    assert obs["confidence"] == 0.5


def test_spiderfoot_requires_scope_unless_explicitly_allowed(monkeypatch):
    monkeypatch.delenv("RECON_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECON_ALLOW_UNSCOPED", raising=False)

    assert not target_in_scope("example.com")

    monkeypatch.setenv("RECON_ALLOW_UNSCOPED", "1")
    assert target_in_scope("example.com")


def test_spiderfoot_module_guardrails_and_payload_shapes(monkeypatch):
    monkeypatch.setenv("SPIDERFOOT_MODULES", "sfp_dnsresolve,sfp_portscan_tcp,sfp_whois,sfp_names")
    monkeypatch.setenv("SPIDERFOOT_MAX_MODULES", "2")
    monkeypatch.delenv("SPIDERFOOT_ALLOW_INTRUSIVE", raising=False)

    assert allowed_modules() == ["sfp_dnsresolve", "sfp_whois"]
    assert normalize_spiderfoot_payload({"events": [{"module": "a"}, "skip"]}) == [{"module": "a"}]
    assert normalize_spiderfoot_payload({"data": [{"module": "b"}]}) == [{"module": "b"}]
    assert normalize_spiderfoot_payload("not-json") == []


def test_run_spiderfoot_once_stores_observations(monkeypatch):
    target_id = "00000000-0000-0000-0000-000000000001"

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Conn:
        def __init__(self):
            self.statuses = []
            self.health = []
            self.reclaimed = False
            self.observation_batches = []

        def transaction(self):
            return Tx()

        async def fetchrow(self, sql, *args):
            assert "UPDATE recon_targets" in sql
            return {
                "id": target_id,
                "target_type": "domain",
                "target_value": "example.com",
                "source": "test",
                "priority": 1,
                "scope_json": {"allowlist": ["example.com"], "modules": ["sfp_dnsresolve", "sfp_portscan_tcp"]},
            }

        async def execute(self, sql, *args):
            if "source_health" in sql:
                self.health.append(args)
            elif "status = 'in_progress'" in sql:
                self.reclaimed = True
            elif "UPDATE recon_targets" in sql:
                self.statuses.append(sql)
            return "UPDATE 1"

        async def executemany(self, sql, rows):
            assert "INSERT INTO recon_observations" in sql
            self.observation_batches.append(rows)

    async def fake_cli(target, modules, timeout_seconds):
        assert modules == ["sfp_dnsresolve"]
        assert target["target_value"] == "example.com"
        assert timeout_seconds > 0
        return [{
            "module": "sfp_dnsresolve",
            "type": "DOMAIN_NAME",
            "data": "www.example.com",
            "confidence": 0.8,
        }]

    monkeypatch.delenv("SPIDERFOOT_ALLOW_INTRUSIVE", raising=False)
    monkeypatch.setattr(recon_spiderfoot, "_run_spiderfoot_cli", fake_cli)
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "completed"
    assert report["observations"] == 1
    assert len(conn.observation_batches) == 1
    assert conn.observation_batches[0][0][3] == "www.example.com"
    assert conn.reclaimed
    assert conn.health[-1][0] == "spiderfoot"
    assert conn.health[-1][1] == "running"


def test_run_spiderfoot_once_blocks_unscoped_target_by_default(monkeypatch):
    target_id = "00000000-0000-0000-0000-000000000002"

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Conn:
        def __init__(self):
            self.updates = []

        def transaction(self):
            return Tx()

        async def fetchrow(self, sql, *args):
            return {
                "id": target_id,
                "target_type": "domain",
                "target_value": "example.com",
                "source": "test",
                "priority": 1,
                "scope_json": {},
            }

        async def execute(self, sql, *args):
            self.updates.append((sql, args))
            return "UPDATE 1"

    monkeypatch.delenv("RECON_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECON_ALLOW_UNSCOPED", raising=False)
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "blocked"
    assert any("target outside RECON_ALLOWLIST" in args for _sql, args in conn.updates)
