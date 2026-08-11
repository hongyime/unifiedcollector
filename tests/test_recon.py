import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from src.core.recon import normalize_recon_target, queue_recon_target
from src.core import recon_spiderfoot
from src.core.recon_spiderfoot import (
    allowed_modules,
    normalize_observation,
    parse_spiderfoot_stdout,
    normalize_spiderfoot_payload,
    run_spiderfoot_once,
    spiderfoot_max_threads,
    target_allowed_by_policy,
    target_in_scope,
)
from src.core.recon_seed import seed_recon_targets_from_collector


ROOT = Path(__file__).resolve().parents[1]


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


def test_queue_recon_target_requeues_existing_non_running_rows():
    class Conn:
        async def fetchrow(self, sql, *args):
            assert "status = CASE" in sql
            assert "ELSE 'pending'" in sql
            assert "error = CASE" in sql
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
        target_value="example.com",
        source="retry",
        scope={"allowlist": ["example.com"]},
    ))

    assert row["status"] == "pending"


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


def test_recon_policy_allows_collector_derived_without_static_allowlist(monkeypatch):
    monkeypatch.delenv("RECON_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECON_ALLOW_UNSCOPED", raising=False)
    monkeypatch.delenv("RECON_ALLOWED_DOMAIN_SUFFIXES", raising=False)
    monkeypatch.setenv("RECON_ALLOWED_SOURCES", "telegram")

    allowed, reason = target_allowed_by_policy({
        "target_type": "username",
        "target_value": "alice",
        "source": "collector:social_users",
        "scope_json": {
            "collector_derived": True,
            "collector_source": "telegram",
        },
    })

    assert allowed
    assert "collector-derived" in reason


def test_recon_policy_blocks_manual_without_allowlist(monkeypatch):
    monkeypatch.delenv("RECON_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECON_ALLOW_UNSCOPED", raising=False)

    allowed, reason = target_allowed_by_policy({
        "target_type": "domain",
        "target_value": "example.com",
        "source": "manual",
        "scope_json": {},
    })

    assert not allowed
    assert reason == "manual target outside RECON_ALLOWLIST"


def test_recon_policy_honors_domain_suffix_for_collector_urls(monkeypatch):
    monkeypatch.delenv("RECON_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECON_ALLOW_UNSCOPED", raising=False)
    monkeypatch.setenv("RECON_ALLOWED_SOURCES", "telegram")
    monkeypatch.setenv("RECON_COLLECTOR_TARGET_TYPES", "domain,url,username")
    monkeypatch.setenv("RECON_ALLOWED_DOMAIN_SUFFIXES", "example.com")

    allowed, _reason = target_allowed_by_policy({
        "target_type": "url",
        "target_value": "https://sub.example.com/path",
        "source": "collector:discovered_links",
        "scope_json": {"collector_derived": True, "collector_source": "telegram"},
    })
    blocked, reason = target_allowed_by_policy({
        "target_type": "url",
        "target_value": "https://other.net/path",
        "source": "collector:discovered_links",
        "scope_json": {"collector_derived": True, "collector_source": "telegram"},
    })

    assert allowed
    assert not blocked
    assert reason == "collector-derived target outside RECON_ALLOWED_DOMAIN_SUFFIXES"


def test_recon_policy_blocks_unapproved_collector_source_table(monkeypatch):
    monkeypatch.delenv("RECON_ALLOWLIST", raising=False)
    monkeypatch.delenv("RECON_ALLOW_UNSCOPED", raising=False)
    monkeypatch.setenv("RECON_ALLOWED_SOURCES", "telegram")
    monkeypatch.setenv("RECON_ALLOWED_SOURCE_TABLES", "discovered_links")

    allowed, reason = target_allowed_by_policy({
        "target_type": "username",
        "target_value": "alice",
        "source": "collector:raw_messages",
        "scope_json": {
            "collector_derived": True,
            "collector_source": "telegram",
            "source_table": "raw_messages",
        },
    })

    assert not allowed
    assert reason == "collector source table not allowed: raw_messages"


def test_spiderfoot_max_threads_is_bounded(monkeypatch):
    monkeypatch.delenv("SPIDERFOOT_MAX_THREADS", raising=False)
    assert spiderfoot_max_threads() == 4

    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "0")
    assert spiderfoot_max_threads() == 1

    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "999")
    assert spiderfoot_max_threads() == 20

    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "bad")
    assert spiderfoot_max_threads() == 4


def test_spiderfoot_module_guardrails_and_payload_shapes(monkeypatch):
    monkeypatch.setenv("SPIDERFOOT_MODULES", "sfp_dnsresolve,sfp_portscan_tcp,sfp_whois,sfp_names")
    monkeypatch.setenv("SPIDERFOOT_MAX_MODULES", "2")
    monkeypatch.delenv("SPIDERFOOT_ALLOW_INTRUSIVE", raising=False)

    assert allowed_modules() == ["sfp_dnsresolve", "sfp_whois"]
    assert normalize_spiderfoot_payload({"events": [{"module": "a"}, "skip"]}) == [{"module": "a"}]
    assert normalize_spiderfoot_payload({"data": [{"module": "b"}]}) == [{"module": "b"}]
    assert normalize_spiderfoot_payload("not-json") == []


def test_spiderfoot_parser_tolerates_mixed_stdout():
    stdout = (
        b'[{"module":"sfp_dnsresolve","type":"DOMAIN_NAME","data":"example.com"}]'
        b'non-json progress text'
        b'{"module":"sfp_whois","type":"WHOIS_REGISTRAR","data":"Example Registrar"}'
    )

    rows = parse_spiderfoot_stdout(stdout)

    assert [row["module"] for row in rows] == ["sfp_dnsresolve", "sfp_whois"]


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
    assert conn.observation_batches[0][0][4] == hashlib.sha256(b"www.example.com").hexdigest()
    assert conn.reclaimed
    assert conn.health[-1][0] == "spiderfoot"
    assert conn.health[-1][1] == "running"


def test_recon_observation_value_hash_migration_matches_upsert():
    migration = (
        ROOT
        / "src"
        / "db"
        / "migrations"
        / "20260811_fix_recon_observation_value_hash.sql"
    ).read_text(encoding="utf-8")

    assert "value_hash TEXT" in migration
    assert "ux_recon_observations_target_module_type_value_hash" in migration
    assert "ON recon_observations (target_id, module, observation_type, value_hash)" in migration
    assert "idx_recon_observations_type_value_hash" in migration


def test_recon_seed_dry_run_builds_collector_candidates():
    class Conn:
        async def fetchval(self, sql, *args):
            assert "to_regclass" in sql
            return True

        async def fetch(self, sql, *args):
            if "NULLIF(domain, '')" in sql:
                return [{
                    "source_record_id": "link-domain-1",
                    "collector_source": "telegram",
                    "target_value": "example.com",
                    "seen_at": None,
                }]
            if "NULLIF(url, '')" in sql:
                return [{
                    "source_record_id": "link-url-1",
                    "collector_source": "telegram",
                    "target_value": "https://example.com/post",
                    "seen_at": None,
                }]
            if "FROM social_users" in sql:
                return [{
                    "source_record_id": "telegram:alice",
                    "collector_source": "telegram",
                    "target_value": "alice",
                    "seen_at": None,
                }]
            return []

    report = asyncio.run(seed_recon_targets_from_collector(
        Conn(),
        include_urls=True,
        per_source_limit=3,
        total_limit=10,
        dry_run=True,
    ))

    assert report["dry_run"] is True
    assert report["candidates"] == 3
    assert report["queued"] == 0
    assert report["sources"] == ["telegram"]
    assert report["types"] == {"domain": 1, "url": 1, "username": 1}
    assert "target_value" not in report["sample"][0]
    assert "target_hash" in report["sample"][0]


def test_recon_seed_scopes_username_targets_to_account_module(monkeypatch):
    class Conn:
        def __init__(self):
            self.scopes = []

        async def fetchval(self, sql, *args):
            assert "to_regclass" in sql
            return True

        async def fetch(self, sql, *args):
            if "FROM social_users" in sql:
                return [{
                    "source_record_id": "telegram:alice",
                    "collector_source": "telegram",
                    "target_value": "alice",
                    "seen_at": None,
                }]
            return []

        async def fetchrow(self, sql, *args):
            assert "INSERT INTO recon_targets" in sql
            self.scopes.append(json.loads(args[4]))
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "target_type": args[0],
                "target_value": args[1],
                "source": args[2],
                "priority": args[3],
                "status": "pending",
            }

    monkeypatch.delenv("RECON_USERNAME_MODULES", raising=False)
    conn = Conn()

    report = asyncio.run(seed_recon_targets_from_collector(
        conn,
        include_urls=False,
        per_source_limit=3,
        total_limit=10,
        dry_run=False,
    ))

    assert report["queued"] == 1
    assert conn.scopes[0]["modules"] == ["sfp_accounts"]


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
    assert any("manual target outside RECON_ALLOWLIST" in str(args) for _sql, args in conn.updates)
