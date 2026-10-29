from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import recon_spiderfoot
from src.core import recon_rollout
from src.core.recon_rollout import (
    AdmissionDenied,
    admission_snapshot,
    collect_stage_metrics,
    commit_claim,
    initialize_state,
    load_policy,
    record_observation,
    reserve_claim,
    resume_after_observation,
    validate_zero_spend_modules,
)


ROOT = Path(__file__).resolve().parents[1]
GOOD_METRICS = {
    "queue_age_seconds": 30,
    "error_rate": 0,
    "storage_success_rate": 1,
    "third_party_failures": 0,
    "useful_observations": 1,
    "unresolved_targets": 0,
    "zero_spend_violation": False,
}


def test_domain_remediation_policy_preserves_r1_and_excludes_usernames():
    legacy = load_policy(ROOT / "config" / "task-37-admission-policy-r1.json")
    remediation = load_policy(ROOT / "config" / "task-37-admission-policy.json")

    assert legacy["policy_id"] == "collector-production-recovery-2026-10-23"
    assert remediation["policy_id"].endswith("-domain-remediation-r2")
    assert remediation["approved_target_types"] == ["domain"]
    assert remediation["limits"] == legacy["limits"]
    assert remediation["stage_gates"] == legacy["stage_gates"]
    assert remediation["paid_or_uncertain_modules_excluded"] == legacy["paid_or_uncertain_modules_excluded"]


def _test_policy(tmp_path, monkeypatch, *, active=True):
    tmp_path.chmod(0o700)
    policy = copy.deepcopy(load_policy(ROOT / "config" / "task-37-admission-policy.json"))
    policy["policy_id"] = f"test-{id(tmp_path)}"
    policy_path = tmp_path / "policy.json"
    state_path = tmp_path / "state.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    policy_path.chmod(0o600)
    monkeypatch.setenv("RECON_ADMISSION_POLICY_PATH", str(policy_path))
    monkeypatch.setenv("RECON_ADMISSION_STATE_PATH", str(state_path))
    initialize_state(policy, state_path, halted=not active)
    return policy, state_path


def _consume_initial_cohort(policy, state_path):
    for index in range(5):
        token = reserve_claim(
            source_identity="collector:website_targets",
            target_type="domain",
            policy=policy,
            path=state_path,
        )
        commit_claim(token, target_identity=f"canary-{index}", path=state_path)


def test_cohort_gate_advances_only_after_all_stage_metrics_pass(tmp_path, monkeypatch):
    policy, state_path = _test_policy(tmp_path, monkeypatch)
    _consume_initial_cohort(policy, state_path)
    passed = record_observation(GOOD_METRICS, policy=policy, path=state_path)
    assert passed["passed"] is True
    assert passed["state"]["cohort_limit"] == 25
    assert passed["state"]["halt"] is False
    assert len(passed["state"]["observations"]) == 1

    failing = dict(GOOD_METRICS, queue_age_seconds=policy["stage_gates"]["max_queue_age_seconds"] + 1)
    failed = record_observation(failing, policy=policy, path=state_path)
    assert failed["passed"] is False
    assert "queue_age" in failed["failures"]
    assert failed["state"]["halt"] is True
    assert failed["state"]["cohort_limit"] == 25


def test_live_canary_persists_observations_and_advances(tmp_path, monkeypatch):
    policy, state_path = _test_policy(tmp_path, monkeypatch)
    _consume_initial_cohort(policy, state_path)
    conn = type("Conn", (), {})()

    async def fetchrow(self, sql, *args):
        assert "WITH stage AS" in sql
        assert "t.created_at" in sql
        assert "FROM stage\n            WHERE status='pending'" in sql
        return {
            "queue_age_seconds": 20,
            "failed": 0,
            "completed": 5,
            "stored": 5,
            "useful_observations": 7,
            "third_party_failures": 0,
            "zero_spend_violation": False,
            "unresolved_targets": 0,
        }

    conn.fetchrow = fetchrow.__get__(conn)
    metrics = asyncio.run(collect_stage_metrics(conn, policy=policy))
    result = record_observation(metrics, policy=policy, path=state_path)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))

    assert result["passed"] is True
    assert metrics["storage_success_rate"] == 1
    assert persisted["observations"][-1]["metrics"] == metrics
    assert persisted["cohort_limit"] == 25


def test_explicit_reassessment_advances_only_a_completed_clean_cohort(tmp_path, monkeypatch):
    policy, state_path = _test_policy(tmp_path, monkeypatch)
    _consume_initial_cohort(policy, state_path)
    record_observation(
        dict(GOOD_METRICS, queue_age_seconds=policy["stage_gates"]["max_queue_age_seconds"] + 1),
        policy=policy,
        path=state_path,
    )

    result = resume_after_observation(GOOD_METRICS, policy=policy, path=state_path)

    assert result["passed"] is True
    assert result["state"]["cohort_limit"] == 25
    assert result["state"]["halt"] is False


def test_zero_useful_output_halts_instead_of_passing(tmp_path, monkeypatch):
    policy, state_path = _test_policy(tmp_path, monkeypatch)
    result = record_observation(dict(GOOD_METRICS, useful_observations=0), policy=policy, path=state_path)
    assert result["passed"] is False
    assert "zero_useful_output" in result["failures"]
    assert result["state"]["halt"] is True


def test_zero_spend_rejection_halts_before_any_claim(tmp_path, monkeypatch):
    _policy, state_path = _test_policy(tmp_path, monkeypatch)
    monkeypatch.setenv("SPIDERFOOT_MODULES", "sfp_shodan")

    class Conn:
        async def execute(self, *_args):
            raise AssertionError("worker wrote before rejecting paid module")

        async def fetchrow(self, *_args):
            raise AssertionError("worker claimed before rejecting paid module")

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    report = asyncio.run(recon_spiderfoot.run_spiderfoot_once(Conn()))
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert report["status"] == "zero_spend_rejected"
    assert state["halt"] is True
    assert "zero_spend_violation" in state["halt_reason"]
    with pytest.raises(AdmissionDenied):
        validate_zero_spend_modules(["serper"], policy=_policy)


def test_admission_gate_caps_real_worker_on_oversized_backlog(tmp_path, monkeypatch):
    policy, state_path = _test_policy(tmp_path, monkeypatch)

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Conn:
        def __init__(self):
            self.rows = [
                {
                    "id": f"00000000-0000-0000-0000-{i:012d}",
                    "target_type": "domain",
                    "target_value": f"site{i}.example.com",
                    "source": "collector:website_targets",
                    "priority": 1,
                    "scope_json": {},
                    "status": "pending",
                }
                for i in range(1, 301)
            ]
            self.persisted = []

        def transaction(self):
            return Tx()

        async def fetchrow(self, sql, *args):
            if "FOR UPDATE SKIP LOCKED" in sql:
                approved_sources = args[2]
                allowed_types = args[1]
                row = next((r for r in self.rows if r["status"] == "pending"
                            and r["source"] in approved_sources
                            and r["target_type"] in allowed_types), None)
                return dict(row) if row else None
            if "scope_json = scope_json || jsonb_build_object" in sql:
                row = next(r for r in self.rows if r["id"] == args[0] and r["status"] == "pending")
                row["status"] = "in_progress"
                row["scope_json"].update({"admission_policy_id": args[1], "admission_cohort_limit": args[2]})
                return dict(row)
            raise AssertionError(f"unexpected fetchrow: {sql}")

        async def execute(self, sql, *args):
            if "SET status = 'completed'" in sql:
                next(r for r in self.rows if r["id"] == args[0])["status"] = "completed"
            return "UPDATE 1"

        async def executemany(self, _sql, rows):
            self.persisted.extend(rows)

    async def fake_cli(target, _modules, _timeout, **_kwargs):
        return [{"module": "sfp_dnsresolve", "type": "DOMAIN_NAME", "data": target["target_value"]}]

    monkeypatch.setenv("SPIDERFOOT_MODULES", "sfp_dnsresolve")
    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    monkeypatch.setattr(recon_spiderfoot, "validate_recon_target", lambda *_args: True)
    monkeypatch.setattr(recon_spiderfoot, "_run_spiderfoot_cli", fake_cli)
    conn = Conn()
    reports = [asyncio.run(recon_spiderfoot.run_spiderfoot_once(conn)) for _ in range(6)]
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert [report["status"] for report in reports[:5]] == ["completed"] * 5
    assert reports[5]["status"] == "admission_halted"
    assert sum(row["status"] == "completed" for row in conn.rows) == 5
    assert sum(row["status"] == "pending" for row in conn.rows) == 295
    assert len(conn.persisted) == 5
    assert state["claims_used"] == 5
    assert len(state["claims"]) == 5


def test_admission_gate_counts_fp_refresh_sibling_allowance(tmp_path, monkeypatch):
    policy, state_path = _test_policy(tmp_path, monkeypatch)
    policy["sibling_work"]["maigret_fp_refresh"] = {"enabled": True, "max_claims": 2}

    first = reserve_claim(source_identity="sibling:maigret_fp_refresh", target_type="username", sibling_identity="maigret_fp_refresh", policy=policy, path=state_path)
    second = reserve_claim(source_identity="sibling:maigret_fp_refresh", target_type="username", sibling_identity="maigret_fp_refresh", policy=policy, path=state_path)
    assert first and second
    with pytest.raises(AdmissionDenied):
        reserve_claim(source_identity="sibling:maigret_fp_refresh", target_type="username", sibling_identity="maigret_fp_refresh", policy=policy, path=state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["claims_used"] == 2
    assert state["sibling_claims"]["maigret_fp_refresh"] == 2


def test_admission_gate_refuses_unapproved_source_and_account_identities(tmp_path, monkeypatch):
    _policy, state_path = _test_policy(tmp_path, monkeypatch)

    class Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Conn:
        def __init__(self):
            self.rows = [
                {"id": "00000000-0000-0000-0000-000000000011", "target_type": "domain",
                 "target_value": "manual.example.com", "source": "manual", "priority": 1,
                 "scope_json": {}, "status": "pending"},
                {"id": "00000000-0000-0000-0000-000000000012", "target_type": "domain",
                 "target_value": "account.example.com", "source": "collector:website_targets", "priority": 1,
                 "scope_json": {"account_id": "not-approved"}, "status": "pending"},
            ]

        def transaction(self):
            return Tx()

        async def fetchrow(self, sql, *args):
            if "FOR UPDATE SKIP LOCKED" in sql:
                assert "source = ANY($3::text[])" in sql
                assert "= ANY($4::text[])" in sql
                for row in self.rows:
                    if row["status"] != "pending" or row["source"] not in args[2]:
                        continue
                    account = row["scope_json"].get("account_id")
                    if account and account not in args[3]:
                        continue
                    return dict(row)
                return None
            raise AssertionError(f"unexpected fetchrow query: {sql}")

        async def execute(self, *_args):
            return "UPDATE 1"

    monkeypatch.setattr(recon_spiderfoot, "network_policy_installed", lambda: True)
    report = asyncio.run(recon_spiderfoot.run_spiderfoot_once(Conn()))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert report["status"] == "idle"
    assert state["claims_used"] == 0


def test_admission_gate_serializes_concurrent_workers_at_cohort_limit(tmp_path, monkeypatch):
    _policy, state_path = _test_policy(tmp_path, monkeypatch)

    def attempt(_index):
        try:
            return reserve_claim(
                source_identity="collector:website_targets",
                target_type="domain",
            )
        except AdmissionDenied:
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        reservations = list(pool.map(attempt, range(40)))
    admitted = [token for token in reservations if token]
    state = json.loads(state_path.read_text(encoding="utf-8"))

    assert len(admitted) == 5
    assert state["claims_used"] == 5
    assert len(state["active_reservations"]) == 5


def test_manual_seed_cli_refuses_writes_when_gate_is_halted(monkeypatch):
    from src import recon_seed_service

    monkeypatch.setattr(
        recon_seed_service,
        "admission_snapshot",
        lambda: ({"approved_source_identities": ["collector:website_targets"]}, {}, "halted"),
    )

    async def fail_if_called():
        raise AssertionError("database pool opened while manual producer was halted")

    monkeypatch.setattr(recon_seed_service, "get_pool", fail_if_called)
    args = SimpleNamespace(
        source=None, dry_run=False, no_domains=False, include_urls=False,
        no_urls=False, no_usernames=False, per_source_limit=25, limit=200,
        priority=7, json=False,
    )
    with pytest.raises(SystemExit, match=r"admission gate \(halted\)"):
        asyncio.run(recon_seed_service._run(args))


def test_rollout_cli_halt_preserves_claims_and_records_reason(monkeypatch, capsys):
    monkeypatch.setattr(recon_rollout, "load_policy", lambda: {"policy_id": "test-policy"})
    captured = {}

    def halt(reason):
        captured["reason"] = reason
        return {"policy_id": "test-policy", "halt_reason": reason}

    monkeypatch.setattr(recon_rollout, "halt_admission", halt)
    monkeypatch.setattr("sys.argv", ["recon_rollout.py", "--halt", "cohort-complete"])

    assert recon_rollout.main() == 0
    assert captured == {"reason": "cohort-complete"}
    assert json.loads(capsys.readouterr().out) == {
        "halted": True,
        "halt_reason": "cohort-complete",
        "policy_id": "test-policy",
    }
