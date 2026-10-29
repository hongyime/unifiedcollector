import asyncio
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from src.core.recon import normalize_recon_target, queue_recon_target
from src.core import recon_spiderfoot
from src.core.recon_spiderfoot import (
    _is_target_level_failure,
    _safe_worker_label,
    _spiderfoot_env,
    allowed_modules,
    effective_modules_for_target,
    normalize_observation,
    parse_spiderfoot_stdout,
    normalize_spiderfoot_payload,
    _run_spiderfoot_cli,
    _run_maigret_subprocess,
    run_spiderfoot_once,
    spiderfoot_max_threads,
)
from src.core.recon_seed import seed_recon_targets_from_collector
from src.core.recon_rollout import initialize_state, load_policy


ROOT = Path(__file__).resolve().parents[1]


def _allow_test_admission(monkeypatch, tmp_path, *, target_types=("domain",), sources=("test",)):
    tmp_path.chmod(0o700)
    policy = copy.deepcopy(load_policy(ROOT / "config" / "task-37-admission-policy.json"))
    policy["policy_id"] = f"test-{id(tmp_path)}"
    policy["approved_source_identities"] = list(sources)
    policy["approved_target_types"] = list(target_types)
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    policy_path.chmod(0o600)
    monkeypatch.setenv("RECON_ADMISSION_POLICY_PATH", str(policy_path))
    monkeypatch.setenv("RECON_ADMISSION_STATE_PATH", str(state_path))
    initialize_state(policy, state_path, halted=False)


def test_normalize_recon_target_validates_domain_and_email():
    assert normalize_recon_target("domain", "Example.COM") == ("domain", "example.com")
    assert normalize_recon_target("email", "A@Example.COM") == ("email", "a@example.com")
    assert normalize_recon_target("ipv4", "203.0.113.5") == ("ip", "203.0.113.5")
    assert normalize_recon_target("channel", "ExampleChannel") == ("username", "ExampleChannel")
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


def test_queue_recon_target_requeues_legacy_blocked_target():
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

    assert allowed_modules() == [
        "sfp_dnsresolve", "sfp_whois", "sfp_names", "sfp_dnsbrute",
        "sfp_portscan_tcp",
    ]

    obs = normalize_observation("00000000-0000-0000-0000-000000000001", {
        "module": "sfp_dnsresolve",
        "type": "DOMAIN_NAME",
        "data": "example.com",
        "confidence": "0.5",
    })

    assert obs["module"] == "sfp_dnsresolve"
    assert obs["observation_type"] == "DOMAIN_NAME"
    assert obs["confidence"] == 0.5


def test_manual_and_collector_targets_share_public_input_validation(monkeypatch):
    from src.core.recon_egress_guard import validate_recon_target

    monkeypatch.setenv("RECON_ALLOWED_SOURCES", "only-one-source")
    monkeypatch.setenv("RECON_ALLOWED_DOMAIN_SUFFIXES", "example.org")

    def public_resolver(_host, _port, _family, _socktype):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    assert validate_recon_target("domain", "unlisted.example", resolver=public_resolver)
    assert validate_recon_target("username", "alice")


def test_spiderfoot_max_threads_is_bounded(monkeypatch):
    monkeypatch.delenv("SPIDERFOOT_MAX_THREADS", raising=False)
    assert spiderfoot_max_threads() == 4

    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "0")
    assert spiderfoot_max_threads() == 1

    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "999")
    assert spiderfoot_max_threads() == 20

    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "bad")
    assert spiderfoot_max_threads() == 4


def test_spiderfoot_allows_instagram_spider_queue_targets(monkeypatch):
    from src.core.recon_egress_guard import validate_recon_target

    assert validate_recon_target("username", "alice")


def test_spiderfoot_env_isolates_worker_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SPIDERFOOT_STATE_ROOT", str(tmp_path))

    env = _spiderfoot_env("worker:1")

    assert _safe_worker_label("worker:1") == "worker_1"
    assert env["HOME"] == str(tmp_path / "worker_1")
    assert (tmp_path / "worker_1").is_dir()


def test_spiderfoot_target_timeout_does_not_degrade_source(monkeypatch, tmp_path):
    _allow_test_admission(monkeypatch, tmp_path)
    target_id = "00000000-0000-0000-0000-000000000003"

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Conn:
        def __init__(self):
            self.health = []
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
                "scope_json": {"allowlist": ["example.com"]},
            }

        async def execute(self, sql, *args):
            if "source_health" in sql:
                self.health.append(args)
            elif "UPDATE recon_targets" in sql:
                self.updates.append(args)
            return "UPDATE 1"

    async def fake_cli(*args, **kwargs):
        raise RuntimeError("SpiderFoot timed out after 180s")

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    monkeypatch.setattr(recon_spiderfoot, "validate_recon_target", lambda *_args: True)
    monkeypatch.setattr(recon_spiderfoot, "_run_spiderfoot_cli", fake_cli)
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "failed"
    assert _is_target_level_failure(report["error"])
    assert conn.health[-1][1] == "running"
    assert conn.health[-1][2] is None


def test_spiderfoot_module_guardrails_and_payload_shapes(monkeypatch):
    monkeypatch.setenv(
        "SPIDERFOOT_MODULES",
        "sfp_dnsresolve,sfp_portscan_tcp,sfp_whois,sfp_names,sfp_dnsbrute",
    )
    monkeypatch.setenv("SPIDERFOOT_MAX_MODULES", "10")

    assert allowed_modules() == [
        "sfp_dnsresolve", "sfp_portscan_tcp", "sfp_whois", "sfp_names",
        "sfp_dnsbrute",
    ]
    assert normalize_spiderfoot_payload({"events": [{"module": "a"}, "skip"]}) == [{"module": "a"}]
    assert normalize_spiderfoot_payload({"data": [{"module": "b"}]}) == [{"module": "b"}]
    assert normalize_spiderfoot_payload("not-json") == []


@pytest.mark.parametrize("module", ["sfp_shodan", "sfp_censys", "sfp_badpackets", "sfp_projectdiscovery"])
def test_paid_or_uncertain_modules_are_explicitly_rejected(module):
    with pytest.raises(ValueError, match="zero-spend policy"):
        allowed_modules(f"sfp_dnsresolve,{module}")


def test_intrusive_free_modules_are_available():
    modules = allowed_modules("sfp_dnsbrute,sfp_portscan_tcp")
    assert modules == ["sfp_dnsbrute", "sfp_portscan_tcp"]


def test_per_target_module_overrides_cannot_narrow_global_coverage(monkeypatch):
    monkeypatch.setenv(
        "SPIDERFOOT_MODULES",
        "sfp_dnsresolve,sfp_whois,sfp_names,sfp_dnsbrute,sfp_portscan_tcp",
    )
    monkeypatch.setenv("SPIDERFOOT_MAX_MODULES", "10")
    assert effective_modules_for_target(["sfp_dnsresolve"]) == allowed_modules()
    assert effective_modules_for_target(["sfp_accounts"]) == [
        *allowed_modules(), "sfp_accounts",
    ]


def test_effective_module_set_is_passed_to_spiderfoot_command(monkeypatch, tmp_path):
    monkeypatch.setenv("SPIDERFOOT_CLI", sys.executable)
    monkeypatch.setenv("SPIDERFOOT_MAX_THREADS", "3")
    monkeypatch.setenv("SPIDERFOOT_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("RECON_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    calls = {}

    class Process:
        pid = 12345
        returncode = 0

        async def communicate(self):
            return b"[]", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls["args"] = args
        return Process()

    monkeypatch.setattr(
        recon_spiderfoot.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    modules = effective_modules_for_target(["sfp_dnsresolve"])
    asyncio.run(_run_spiderfoot_cli(
        {"target_value": "public.example"}, modules, timeout_seconds=180,
    ))

    args = list(calls["args"])
    assert args[args.index("-m") + 1] == ",".join(modules)
    assert args[args.index("-max-threads") + 1] == "3"
    assert args[args.index("-o") + 1] == "json"
    raw_reports = list((tmp_path / "evidence" / "spiderfoot").glob("*.stdout.json"))
    assert len(raw_reports) == 1
    assert raw_reports[0].read_bytes() == b"[]"
    assert raw_reports[0].stat().st_mode & 0o777 == 0o600
    assert raw_reports[0].parent.stat().st_mode & 0o777 == 0o700


def test_maigret_raw_report_is_retained_privately(monkeypatch, tmp_path):
    evidence = tmp_path / "private"
    monkeypatch.setenv("RECON_EVIDENCE_ROOT", str(evidence))
    calls = {}

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        output_dir = Path(args[args.index("-fo") + 1])
        calls["args"] = args
        (output_dir / "report_example_ndjson.json").write_text(
            '{"sitename":"example","status":"Claimed"}\n',
            encoding="utf-8",
        )
        return Process()

    monkeypatch.setenv("MAIGRET_CLI", sys.executable)
    monkeypatch.setenv("MAIGRET_TOP_SITES", "9999")
    monkeypatch.setenv("MAIGRET_NUM_REQUESTS", "9999")
    monkeypatch.setenv("MAIGRET_HTTP_TIMEOUT_SECONDS", "9999")
    monkeypatch.setattr(
        recon_spiderfoot.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    keep_dir = evidence / "maigret"
    rows = asyncio.run(_run_maigret_subprocess(
        "private-handle", timeout_seconds=10, keep_dir=str(keep_dir),
    ))

    reports = list(keep_dir.glob("*.json"))
    assert rows[0]["sitename"] == "example"
    assert len(reports) == 1
    assert evidence.stat().st_mode & 0o777 == 0o700
    assert keep_dir.stat().st_mode & 0o777 == 0o700
    assert reports[0].stat().st_mode & 0o777 == 0o600
    args = list(calls["args"])
    assert args[args.index("--top-sites") + 1] == "100"
    assert args[args.index("-n") + 1] == "10"
    assert args[args.index("--timeout") + 1] == "30"


def test_unavailable_free_module_is_not_silently_dropped():
    with pytest.raises(ValueError, match="unavailable in the installed SpiderFoot version.*sfp_portscan_udp"):
        allowed_modules("sfp_dnsresolve,sfp_portscan_udp")


def test_requested_modules_are_not_silently_truncated(monkeypatch):
    monkeypatch.setenv("SPIDERFOOT_MAX_MODULES", "2")
    with pytest.raises(ValueError, match="raise the limit or explicitly reduce"):
        allowed_modules("sfp_dnsresolve,sfp_whois,sfp_names")


def test_spiderfoot_parser_tolerates_mixed_stdout():
    stdout = (
        b'[{"module":"sfp_dnsresolve","type":"DOMAIN_NAME","data":"example.com"}]'
        b'non-json progress text'
        b'{"module":"sfp_whois","type":"WHOIS_REGISTRAR","data":"Example Registrar"}'
    )

    rows = parse_spiderfoot_stdout(stdout)

    assert [row["module"] for row in rows] == ["sfp_dnsresolve", "sfp_whois"]


def test_run_spiderfoot_once_stores_observations(monkeypatch, tmp_path):
    _allow_test_admission(monkeypatch, tmp_path)
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
            if "FOR UPDATE SKIP LOCKED" in sql:
                return {
                    "id": target_id,
                    "target_type": "domain",
                    "target_value": "example.com",
                    "source": "test",
                    "priority": 1,
                    "scope_json": {"modules": ["sfp_dnsresolve", "sfp_portscan_tcp"]},
                }
            assert "UPDATE recon_targets" in sql
            return {
                "id": target_id,
                "target_type": "domain",
                "target_value": "example.com",
                "source": "test",
                "priority": 1,
                "scope_json": {"modules": ["sfp_dnsresolve", "sfp_portscan_tcp"]},
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

    async def fake_cli(target, modules, timeout_seconds, **kwargs):
        assert modules == [
            "sfp_dnsresolve", "sfp_whois", "sfp_names", "sfp_dnsbrute",
            "sfp_portscan_tcp",
        ]
        assert target["target_value"] == "example.com"
        assert timeout_seconds > 0
        return [{
            "module": "sfp_dnsresolve",
            "type": "DOMAIN_NAME",
            "data": "www.example.com",
            "confidence": 0.8,
        }]

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    monkeypatch.setattr(recon_spiderfoot, "validate_recon_target", lambda *_args: True)
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


def test_run_spiderfoot_once_fails_when_scan_has_no_useful_observations(monkeypatch, tmp_path):
    _allow_test_admission(monkeypatch, tmp_path)
    target_id = "00000000-0000-0000-0000-000000000004"

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Conn:
        def __init__(self):
            self.updates = []
            self.health = []

        def transaction(self):
            return Tx()

        async def fetchrow(self, sql, *args):
            return {
                "id": target_id,
                "target_type": "domain",
                "target_value": "public.example",
                "source": "test",
                "priority": 1,
                "scope_json": {},
            }

        async def execute(self, sql, *args):
            if "source_health" in sql:
                self.health.append(args)
            else:
                self.updates.append((sql, args))
            return "UPDATE 1"

    async def no_rows(*_args, **_kwargs):
        return []

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    monkeypatch.setattr(recon_spiderfoot, "validate_recon_target", lambda *_args: True)
    monkeypatch.setattr(recon_spiderfoot, "_run_spiderfoot_cli", no_rows)
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "failed"
    assert report["observations"] == 0
    assert "no useful observations" in report["error"]
    assert any("status = 'failed'" in sql for sql, _args in conn.updates)
    assert conn.health[-1][1] == "degraded"


def test_halted_admission_writes_a_healthy_idle_heartbeat(monkeypatch, tmp_path):
    _allow_test_admission(monkeypatch, tmp_path)
    state_path = Path(os.environ["RECON_ADMISSION_STATE_PATH"])
    recon_spiderfoot.halt_admission("error_rate", path=state_path)

    class Conn:
        def __init__(self):
            self.health = []

        async def execute(self, sql, *args):
            self.health.append((sql, args))
            return "INSERT 0 1"

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "admission_halted"
    assert conn.health[-1][1] == ("spiderfoot", "running", None, True)


def test_run_spiderfoot_once_dry_run_only_reads_pending_target(monkeypatch, tmp_path):
    _allow_test_admission(monkeypatch, tmp_path)
    class Conn:
        def __init__(self):
            self.reads = []

        async def fetchrow(self, sql, *args):
            self.reads.append(sql)
            assert "SELECT" in sql
            assert "status = 'pending'" in sql
            return {
                "id": "00000000-0000-0000-0000-000000000005",
                "target_type": "domain",
                "target_value": "public.example",
                "source": "test",
                "priority": 1,
                "scope_json": {},
            }

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("dry-run attempted a database mutation")

        def transaction(self):
            raise AssertionError("dry-run attempted a write transaction")

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    monkeypatch.setattr(recon_spiderfoot, "validate_recon_target", lambda *_args: True)
    report = asyncio.run(run_spiderfoot_once(Conn(), dry_run=True))

    assert report["status"] == "dry_run"
    assert report["dry_run"] is True
    assert report["modules"] == allowed_modules()


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
            if "FROM discovered_links" in sql and "NULLIF(domain, '')" in sql:
                return [{
                    "source_record_id": "link-domain-1",
                    "collector_source": "telegram",
                    "target_value": "example.com",
                    "seen_at": None,
                }]
            if "FROM discovered_links" in sql and "NULLIF(url, '')" in sql:
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


def test_recon_seed_covers_discovery_sources_and_does_not_requeue_existing_targets():
    source_rows = {
        "FROM website_targets": [{
            "source_record_id": "website-1", "collector_source": "website",
            "target_value": "website.example.com", "seen_at": None,
        }],
        "FROM search_results": [{
            "source_record_id": "search-1", "collector_source": "search",
            "target_value": "search.example.com", "seen_at": None,
        }],
        "FROM exposure_findings": [{
            "source_record_id": "exposure-1", "collector_source": "exposure",
            "target_value": "exposure.example.com", "seen_at": None,
        }],
        "FROM browser_native_scrape_queue": [{
            "source_record_id": "native-1", "collector_source": "pinterest",
            "platform": "pinterest", "target_value": "nativeuser", "seen_at": None,
        }],
        "FROM collection_targets": [{
            "source_record_id": "collector-1", "collector_source": "telegram",
            "target_type": "user", "target_value": "collectoruser", "seen_at": None,
        }],
    }

    class Conn:
        def __init__(self):
            self.targets = {}

        async def fetchval(self, sql, *args):
            return True

        async def fetch(self, sql, *args):
            for marker, rows in source_rows.items():
                if marker in sql:
                    return rows
            return []

        async def fetchrow(self, sql, *args):
            assert "INSERT INTO recon_targets" in sql
            assert args[5] is False  # seed cycles preserve the prior queue state
            identity = (args[0], args[1])
            inserted = identity not in self.targets
            if inserted:
                self.targets[identity] = {
                    "status": "pending",
                    "scope": json.loads(args[4]),
                }
            return {
                "id": f"target-{len(self.targets)}",
                "target_type": args[0], "target_value": args[1],
                "source": args[2], "priority": args[3],
                "status": self.targets[identity]["status"],
                "inserted": inserted,
            }

    conn = Conn()
    fair = asyncio.run(seed_recon_targets_from_collector(
        conn, include_urls=False, per_source_limit=5, total_limit=3, dry_run=True,
    ))
    assert fair["sources"] == ["exposure", "search", "website"]
    assert len(conn.targets) == 0
    scoped = asyncio.run(seed_recon_targets_from_collector(
        conn,
        include_urls=False,
        per_source_limit=5,
        total_limit=20,
        dry_run=True,
        approved_source_identities=["collector:website_targets"],
    ))
    assert scoped["candidates"] == 1
    assert scoped["sources"] == ["website"]
    first = asyncio.run(seed_recon_targets_from_collector(
        conn, include_urls=False, per_source_limit=5, total_limit=20,
    ))
    assert first["candidates"] == 5
    assert first["queued"] == 5
    assert first["queued_by_source"] == {
        "website_targets": 1, "search_results": 1, "exposure_findings": 1,
        "browser_native_scrape_queue": 1, "collection_targets": 1,
    }
    assert len(conn.targets) == 5
    assert conn.targets[("username", "nativeuser")]["scope"]["platform"] == "pinterest"

    # A completed target remains completed and is not made pending by a later
    # periodic seeding pass.
    for target in conn.targets.values():
        target["status"] = "completed"
    second = asyncio.run(seed_recon_targets_from_collector(
        conn, include_urls=False, per_source_limit=5, total_limit=20,
    ))
    assert second["candidates"] == 5
    assert second["queued"] == 0
    assert len(conn.targets) == 5
    assert {target["status"] for target in conn.targets.values()} == {"completed"}


@pytest.mark.parametrize("configured_modules,expected", [
    (None, None), ("", None), (" , ", None),
    ("sfp_accounts", ["sfp_accounts"]), ("maigret", ["maigret"]),
])
def test_recon_seed_username_modules_are_operator_controlled(monkeypatch, configured_modules, expected):
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
                "inserted": True,
            }

    if configured_modules is None:
        monkeypatch.delenv("RECON_USERNAME_MODULES", raising=False)
    else:
        monkeypatch.setenv("RECON_USERNAME_MODULES", configured_modules)
    conn = Conn()

    report = asyncio.run(seed_recon_targets_from_collector(
        conn,
        include_urls=False,
        per_source_limit=3,
        total_limit=10,
        dry_run=False,
    ))

    assert report["queued"] == 1
    assert conn.scopes[0].get("modules") == expected


def test_run_spiderfoot_once_rejects_private_ip_before_engine(monkeypatch, tmp_path):
    _allow_test_admission(monkeypatch, tmp_path, target_types=("ip",))
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
                "target_type": "ip",
                "target_value": "127.0.0.1",
                "source": "test",
                "priority": 1,
                "scope_json": {},
            }

        async def execute(self, sql, *args):
            self.updates.append((sql, args))
            return "UPDATE 1"

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    monkeypatch.setattr(
        recon_spiderfoot,
        "_run_spiderfoot_cli",
        lambda *_args, **_kwargs: pytest.fail("private target reached SpiderFoot"),
    )
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "blocked"
    assert "non-public destination" in report["error"]
    assert any("non-public destination" in str(args) for _sql, args in conn.updates)


def test_run_spiderfoot_once_fails_closed_without_network_policy(monkeypatch):
    class Conn:
        def __init__(self):
            self.claimed = False
            self.health = []

        async def execute(self, sql, *args):
            if "source_health" in sql:
                self.health.append(args)

        async def fetchrow(self, sql, *args):
            self.claimed = True
            pytest.fail("target was claimed without the egress guard")

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: False)
    conn = Conn()

    report = asyncio.run(run_spiderfoot_once(conn))

    assert report["status"] == "guard_unavailable"
    assert not conn.claimed
