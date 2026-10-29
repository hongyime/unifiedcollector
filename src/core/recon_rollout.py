"""Persistent, fail-closed admission and staged rollout state for recon."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


COHORTS = (5, 25, 100, 250)
MAX_EGRESS_BYTES = 1_073_741_824
MAX_DEADLINE_SECONDS = 3_600
PAID_OR_UNCERTAIN_MODULES = frozenset({
    "sfp_badpackets", "sfp_c99", "sfp_censys", "sfp_dehashed",
    "sfp_fsecure_riddler", "sfp_haveibeenpwned", "sfp_projectdiscovery",
    "sfp_seon", "sfp_shodan", "sfp_sociallinks", "sfp_spur",
    "sfp_whoisology", "sfp_whoxy", "serper",
})
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "task-37-admission-policy.json"
DEFAULT_STATE_PATH = Path("/var/lib/unifiedcollector/recon-admission/task-37-admission-state.json")
SUPPORTED_SIBLINGS = frozenset({"maigret_fp_refresh"})


class AdmissionPolicyError(RuntimeError):
    """The admission policy or its private persistent state is missing/invalid."""


class AdmissionDenied(RuntimeError):
    """The current target or sibling work is outside the active admission."""


def policy_path() -> Path:
    return Path(os.getenv("RECON_ADMISSION_POLICY_PATH", str(DEFAULT_POLICY_PATH)))


def state_path() -> Path:
    return Path(os.getenv("RECON_ADMISSION_STATE_PATH", str(DEFAULT_STATE_PATH)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AdmissionPolicyError("admission timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise AdmissionPolicyError("admission timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_policy(path: Path | None = None) -> dict[str, Any]:
    source = path or policy_path()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionPolicyError("admission policy is missing or invalid") from exc
    if not isinstance(value, dict):
        raise AdmissionPolicyError("admission policy must be a JSON object")
    validate_policy(value)
    return value


def validate_policy(policy: dict[str, Any]) -> None:
    if policy.get("schema_version") != 1 or not str(policy.get("policy_id") or "").strip():
        raise AdmissionPolicyError("unsupported admission policy schema or missing policy_id")
    sources = policy.get("approved_source_identities")
    accounts = policy.get("approved_account_identities")
    target_types = policy.get("approved_target_types")
    if not isinstance(sources, list) or not sources or any(not isinstance(x, str) or not x or "*" in x for x in sources):
        raise AdmissionPolicyError("approved_source_identities must be a nonempty exact-identity list")
    if len(sources) != len(set(sources)):
        raise AdmissionPolicyError("approved_source_identities must be unique")
    if not isinstance(accounts, list) or any(not isinstance(x, str) or not x or "*" in x for x in accounts):
        raise AdmissionPolicyError("approved_account_identities must be an exact-identity list")
    if len(accounts) != len(set(accounts)):
        raise AdmissionPolicyError("approved_account_identities must be unique")
    if not isinstance(target_types, list) or not target_types or any(x not in {"domain", "username", "ip"} for x in target_types):
        raise AdmissionPolicyError("approved_target_types must be a nonempty public target-type list")
    if len(target_types) != len(set(target_types)):
        raise AdmissionPolicyError("approved_target_types must be unique")
    if policy.get("cohorts") != list(COHORTS):
        raise AdmissionPolicyError("cohort stages must be exactly 5, 25, 100, and 250")
    limits = policy.get("limits")
    if not isinstance(limits, dict):
        raise AdmissionPolicyError("admission limits are required")
    try:
        max_claims = int(limits["max_claims"])
        max_egress = int(limits["max_egress_bytes"])
        deadline = int(limits["deadline_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdmissionPolicyError("admission limits are incomplete") from exc
    if max_claims < COHORTS[-1] or max_claims > COHORTS[-1]:
        raise AdmissionPolicyError("max_claims must cap the staged plan at 250")
    if max_egress != MAX_EGRESS_BYTES:
        raise AdmissionPolicyError("max_egress_bytes must match the enforced 1 GiB namespace quota")
    if not 30 <= deadline <= MAX_DEADLINE_SECONDS:
        raise AdmissionPolicyError("deadline_seconds exceeds the hard one-hour guard")
    gates = policy.get("stage_gates")
    if not isinstance(gates, dict):
        raise AdmissionPolicyError("stage gates are required")
    required_gates = {
        "max_queue_age_seconds", "max_error_rate", "min_storage_success_rate",
        "max_third_party_failures", "min_useful_observations", "max_unresolved_targets",
    }
    if not required_gates.issubset(gates):
        raise AdmissionPolicyError("stage gates are incomplete")
    try:
        if float(gates["max_queue_age_seconds"]) < 0:
            raise ValueError
        if not 0 <= float(gates["max_error_rate"]) <= 1:
            raise ValueError
        if not 0 <= float(gates["min_storage_success_rate"]) <= 1:
            raise ValueError
        if int(gates["max_third_party_failures"]) < 0:
            raise ValueError
        if int(gates["min_useful_observations"]) < 1:
            raise ValueError
        if int(gates["max_unresolved_targets"]) < 0:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise AdmissionPolicyError("stage gate thresholds are invalid") from exc
    siblings = policy.get("sibling_work")
    if not isinstance(siblings, dict):
        raise AdmissionPolicyError("sibling_work must be specified")
    for identity, config in siblings.items():
        if identity not in SUPPORTED_SIBLINGS:
            raise AdmissionPolicyError(f"unsupported sibling work identity: {identity}")
        if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
            raise AdmissionPolicyError(f"invalid sibling policy: {identity}")
        try:
            sibling_cap = int(config.get("max_claims", -1))
        except (TypeError, ValueError) as exc:
            raise AdmissionPolicyError(f"invalid sibling cap: {identity}") from exc
        if sibling_cap < 0 or sibling_cap > max_claims:
            raise AdmissionPolicyError(f"invalid sibling cap: {identity}")
        if not config["enabled"] and sibling_cap != 0:
            raise AdmissionPolicyError(f"disabled sibling must have a zero cap: {identity}")
    excluded = policy.get("paid_or_uncertain_modules_excluded")
    if not isinstance(excluded, list) or not PAID_OR_UNCERTAIN_MODULES.issubset(set(excluded)):
        raise AdmissionPolicyError("paid/uncertain module exclusions are incomplete")


def _secure_state_parent(path: Path) -> None:
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise AdmissionPolicyError("admission state directory is missing")
    mode = stat.S_IMODE(parent.stat().st_mode)
    if mode not in {0o700, 0o710, 0o2710, 0o2770}:
        raise AdmissionPolicyError("admission state directory must be private or shared only with the configured group")
    if mode in {0o710, 0o2710, 0o2770} and parent.stat().st_gid != _shared_gid():
        raise AdmissionPolicyError("admission state directory group does not match the configured shared group")


def _shared_gid() -> int:
    raw = os.getenv("RECON_ADMISSION_SHARED_GID")
    try:
        return int(raw) if raw is not None else os.getgid()
    except ValueError as exc:
        raise AdmissionPolicyError("shared admission group id is invalid") from exc


def _state_mode() -> int:
    raw = os.getenv("RECON_ADMISSION_STATE_MODE", "0600")
    try:
        mode = int(raw, 8)
    except ValueError as exc:
        raise AdmissionPolicyError("admission state mode is invalid") from exc
    if mode not in {0o600, 0o660}:
        raise AdmissionPolicyError("admission state mode must be 600 or 660")
    return mode


@contextmanager
def _locked_state(path: Path) -> Iterator[dict[str, Any]]:
    _secure_state_parent(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, _state_mode())
        os.fchmod(fd, _state_mode())
    except FileExistsError:
        fd = os.open(lock_path, os.O_RDWR)
    try:
        lock_info = os.fstat(fd)
        lock_mode = stat.S_IMODE(lock_info.st_mode)
        if lock_mode not in {0o600, 0o660}:
            raise AdmissionPolicyError("admission lock file must have mode 600 or 660")
        if lock_mode == 0o660 and lock_info.st_gid != _shared_gid():
            raise AdmissionPolicyError("admission lock file group does not match the configured shared group")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise AdmissionPolicyError("admission state is not initialized") from exc
        file_mode = stat.S_IMODE(info.st_mode)
        if file_mode not in {0o600, 0o660}:
            raise AdmissionPolicyError("admission state file must have mode 600 or 660")
        if file_mode == 0o660 and info.st_gid != _shared_gid():
            raise AdmissionPolicyError("admission state file group does not match the configured shared group")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdmissionPolicyError("admission state is invalid") from exc
        if not isinstance(state, dict):
            raise AdmissionPolicyError("admission state must be a JSON object")
        yield state
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".admission-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, _state_mode())
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, _state_mode())
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def initialize_state(
    policy: dict[str, Any] | None = None,
    path: Path | None = None,
    *,
    halted: bool = True,
) -> dict[str, Any]:
    config = policy or load_policy()
    validate_policy(config)
    target = path or state_path()
    _secure_state_parent(target)
    if target.exists():
        with _locked_state(target) as state:
            if state.get("policy_id") != config["policy_id"]:
                raise AdmissionPolicyError("existing state belongs to a different policy_id")
            return dict(state)
    state = {
        "schema_version": 1,
        "policy_id": config["policy_id"],
        "cohort_limit": config["cohorts"][0],
        "claims_used": 0,
        "sibling_claims": {},
        "halt": halted,
        "halt_reason": "awaiting explicit canary activation" if halted else None,
        "started_at": _utc_now().isoformat(),
        "active_reservations": {},
        "observations": [],
    }
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _state_mode())
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(target, _state_mode())
    return state


def admission_snapshot(
    policy: dict[str, Any] | None = None,
    path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    config = policy or load_policy()
    target = path or state_path()
    with _locked_state(target) as state:
        if state.get("schema_version") != 1 or state.get("policy_id") != config["policy_id"]:
            raise AdmissionPolicyError("admission state does not match the installed policy")
        reason = None
        if not config.get("enabled", False):
            reason = "policy_disabled"
        elif state.get("halt"):
            reason = str(state.get("halt_reason") or "halted")
        else:
            try:
                stage_limit = int(state["cohort_limit"])
                claims_used = int(state["claims_used"])
                started = _parse_time(state["started_at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AdmissionPolicyError("admission state counters are invalid") from exc
            if stage_limit not in config["cohorts"]:
                raise AdmissionPolicyError("current cohort is not an installed stage")
            if claims_used >= stage_limit:
                reason = "cohort_cap_reached"
            elif _utc_now() >= started + timedelta(seconds=int(config["limits"]["deadline_seconds"])):
                reason = "deadline_expired"
        return config, dict(state), reason


def activate_admission(*, policy: dict[str, Any] | None = None, path: Path | None = None) -> dict[str, Any]:
    config = policy or load_policy()
    target = path or state_path()
    with _locked_state(target) as state:
        if state.get("policy_id") != config["policy_id"]:
            raise AdmissionPolicyError("admission state does not match the installed policy")
        if state.get("claims_used", 0) >= state.get("cohort_limit", 0):
            raise AdmissionPolicyError("the existing cohort allowance is exhausted")
        state["halt"] = False
        state["halt_reason"] = None
        state["started_at"] = _utc_now().isoformat()
        _write_state(target, state)
        return dict(state)


def halt_admission(reason: str, *, path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    with _locked_state(target) as state:
        state["halt"] = True
        state["halt_reason"] = str(reason)[:500]
        state.setdefault("halt_events", []).append({"at": _utc_now().isoformat(), "reason": state["halt_reason"]})
        state["halt_events"] = state["halt_events"][-50:]
        _write_state(target, state)
        return dict(state)


def reserve_claim(
    *,
    source_identity: str,
    target_type: str,
    account_identity: str | None = None,
    sibling_identity: str | None = None,
    policy: dict[str, Any] | None = None,
    path: Path | None = None,
) -> str:
    config = policy or load_policy()
    target = path or state_path()
    if sibling_identity is None:
        if source_identity not in config["approved_source_identities"]:
            raise AdmissionDenied("source identity is not approved")
        if target_type not in config["approved_target_types"]:
            raise AdmissionDenied("target type is not approved")
        if account_identity is not None and account_identity not in config["approved_account_identities"]:
            raise AdmissionDenied("account identity is not approved")
    else:
        sibling = config["sibling_work"].get(sibling_identity)
        if not sibling or not sibling["enabled"] or sibling["max_claims"] <= 0:
            raise AdmissionDenied("sibling work is disabled")
    token = uuid.uuid4().hex
    with _locked_state(target) as state:
        if state.get("schema_version") != 1 or state.get("policy_id") != config["policy_id"]:
            raise AdmissionPolicyError("admission state does not match the installed policy")
        if not config.get("enabled", False) or state.get("halt"):
            raise AdmissionDenied(str(state.get("halt_reason") or "policy disabled or halted"))
        stage_limit = int(state.get("cohort_limit", 0))
        claims_used = int(state.get("claims_used", 0))
        if stage_limit not in config["cohorts"] or claims_used >= min(stage_limit, int(config["limits"]["max_claims"])):
            raise AdmissionDenied("cohort claim allowance exhausted")
        started = _parse_time(str(state.get("started_at") or ""))
        if _utc_now() >= started + timedelta(seconds=int(config["limits"]["deadline_seconds"])):
            state["halt"] = True
            state["halt_reason"] = "deadline_expired"
            _write_state(target, state)
            raise AdmissionDenied("admission deadline expired")
        if sibling_identity is not None:
            used = int(state.setdefault("sibling_claims", {}).get(sibling_identity, 0))
            sibling = config["sibling_work"][sibling_identity]
            if used >= int(sibling["max_claims"]):
                raise AdmissionDenied("sibling claim allowance exhausted")
            state["sibling_claims"][sibling_identity] = used + 1
        state["claims_used"] = claims_used + 1
        state.setdefault("active_reservations", {})[token] = {
            "source_identity": source_identity,
            "sibling_identity": sibling_identity,
            "cohort_limit": stage_limit,
            "reserved_at": _utc_now().isoformat(),
        }
        _write_state(target, state)
    return token


def release_claim(token: str, *, path: Path | None = None) -> None:
    target = path or state_path()
    with _locked_state(target) as state:
        reservations = state.setdefault("active_reservations", {})
        reservation = reservations.pop(token, None)
        if reservation is None:
            return
        state["claims_used"] = max(0, int(state.get("claims_used", 0)) - 1)
        sibling = reservation.get("sibling_identity")
        if sibling:
            siblings = state.setdefault("sibling_claims", {})
            siblings[sibling] = max(0, int(siblings.get(sibling, 0)) - 1)
        _write_state(target, state)


def commit_claim(token: str, *, target_identity: str | None = None, path: Path | None = None) -> None:
    target = path or state_path()
    with _locked_state(target) as state:
        reservation = state.setdefault("active_reservations", {}).pop(token, None)
        if reservation is None:
            raise AdmissionPolicyError("claim reservation disappeared")
        item = {
            "at": _utc_now().isoformat(),
            "source_identity": reservation.get("source_identity"),
            "sibling_identity": reservation.get("sibling_identity"),
            "target_hash": target_identity,
        }
        state.setdefault("claims", []).append(item)
        state["claims"] = state["claims"][-250:]
        _write_state(target, state)


def validate_zero_spend_modules(
    modules: list[str], *, policy: dict[str, Any] | None = None,
) -> None:
    config = policy or load_policy()
    blocked = sorted(set(modules) & set(config["paid_or_uncertain_modules_excluded"]))
    if blocked:
        raise AdmissionDenied("paid or uncertain-charge modules are excluded: " + ",".join(blocked))


def evaluate_stage(metrics: dict[str, Any], *, policy: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    config = policy or load_policy()
    gates = config["stage_gates"]
    failures: list[str] = []
    try:
        queue_age = float(metrics["queue_age_seconds"])
        error_rate = float(metrics["error_rate"])
        storage_rate = float(metrics["storage_success_rate"])
        third_party = int(metrics["third_party_failures"])
        useful = int(metrics["useful_observations"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdmissionPolicyError("rollout observation is incomplete") from exc
    if queue_age > float(gates["max_queue_age_seconds"]):
        failures.append("queue_age")
    if error_rate > float(gates["max_error_rate"]):
        failures.append("error_rate")
    if storage_rate < float(gates["min_storage_success_rate"]):
        failures.append("storage_success_rate")
    if third_party > int(gates["max_third_party_failures"]):
        failures.append("third_party_failures")
    if useful < int(gates["min_useful_observations"]):
        failures.append("zero_useful_output")
    if metrics.get("zero_spend_violation"):
        failures.append("zero_spend_violation")
    if int(metrics.get("unresolved_targets", 0)) > int(gates["max_unresolved_targets"]):
        failures.append("unresolved_targets")
    return not failures, failures


def record_observation(
    metrics: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    config = policy or load_policy()
    target = path or state_path()
    passed, failures = evaluate_stage(metrics, policy=config)
    with _locked_state(target) as state:
        if state.get("policy_id") != config["policy_id"]:
            raise AdmissionPolicyError("admission state does not match the installed policy")
        item = {
            "at": _utc_now().isoformat(),
            "cohort_limit": state.get("cohort_limit"),
            "metrics": metrics,
            "passed": passed,
            "failures": failures,
        }
        state.setdefault("observations", []).append(item)
        state["observations"] = state["observations"][-50:]
        current = int(state.get("cohort_limit", 0))
        if passed and (
            int(state.get("claims_used", 0)) < current
            or bool(state.get("active_reservations"))
            or int(metrics.get("unresolved_targets", 0)) > 0
        ):
            passed = False
            failures = ["cohort_incomplete"]
            item["passed"] = False
            item["failures"] = failures
        if not passed:
            if failures != ["cohort_incomplete"]:
                state["halt"] = True
                state["halt_reason"] = ",".join(failures)
        elif state.get("halt"):
            # Passing observations never silently override a manual/policy halt.
            state["halt_reason"] = state.get("halt_reason") or "manual_halt"
        else:
            next_stages = [stage for stage in config["cohorts"] if stage > current]
            if next_stages:
                state["cohort_limit"] = next_stages[0]
                state["started_at"] = _utc_now().isoformat()
            else:
                state["halt"] = True
                state["halt_reason"] = "maximum_cohort_complete"
        _write_state(target, state)
        return {"passed": passed, "failures": failures, "state": dict(state)}


def resume_after_observation(
    metrics: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Explicitly reassess a halted, completed cohort after remediation."""
    config = policy or load_policy()
    passed, failures = evaluate_stage(metrics, policy=config)
    if not passed:
        raise AdmissionPolicyError("stage gates still fail: " + ",".join(failures))
    target = path or state_path()
    with _locked_state(target) as state:
        if state.get("policy_id") != config["policy_id"]:
            raise AdmissionPolicyError("admission state does not match the installed policy")
        if not state.get("halt"):
            raise AdmissionPolicyError("admission is not halted")
        if state.get("active_reservations") or int(state.get("claims_used", 0)) < int(state.get("cohort_limit", 0)):
            raise AdmissionPolicyError("cohort is not complete")
        state["halt"] = False
        state["halt_reason"] = None
        _write_state(target, state)
    return record_observation(metrics, policy=config, path=target)


async def collect_stage_metrics(conn, *, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    config, state, _block = admission_snapshot(policy)
    row = await conn.fetchrow(
        """
        WITH stage AS (
            SELECT t.id, t.status, t.error, t.scope_json, t.created_at,
                   EXISTS (SELECT 1 FROM recon_observations o WHERE o.target_id=t.id) AS has_observation
            FROM recon_targets t
            WHERE t.scope_json->>'admission_policy_id'=$1
              AND t.scope_json->>'admission_cohort_limit'=$2
        ), pending AS (
            SELECT max(EXTRACT(EPOCH FROM (now()-created_at))) AS age_seconds
            FROM stage
            WHERE status='pending'
        )
        SELECT COALESCE((SELECT age_seconds FROM pending),0)::double precision AS queue_age_seconds,
               count(*) FILTER (WHERE status='failed')::int AS failed,
               count(*) FILTER (WHERE status='completed')::int AS completed,
               count(*) FILTER (WHERE status='in_progress')::int AS unresolved_targets,
               count(*) FILTER (WHERE status='completed' AND has_observation)::int AS stored,
               (SELECT count(*)::int FROM stage s JOIN recon_observations o ON o.target_id=s.id)
                   AS useful_observations,
               count(*) FILTER (WHERE status='failed' AND error ~* '(http|dns|timeout|connection|rate.?limit|tls|ssl)')::int
                   AS third_party_failures,
               EXISTS (
                   SELECT 1 FROM stage s
                   JOIN LATERAL jsonb_array_elements_text(
                       CASE WHEN jsonb_typeof(s.scope_json->'modules')='array'
                            THEN s.scope_json->'modules' ELSE '[]'::jsonb END
                   ) module_name ON TRUE
                   WHERE module_name IN ('sfp_shodan','sfp_censys','serper')
               ) AS zero_spend_violation
        FROM stage
        """,
        config["policy_id"],
        str(state["cohort_limit"]),
    )
    failed = int(row["failed"] or 0)
    completed = int(row["completed"] or 0)
    processed = failed + completed
    return {
        "queue_age_seconds": float(row["queue_age_seconds"] or 0),
        "error_rate": failed / processed if processed else 0.0,
        "storage_success_rate": int(row["stored"] or 0) / completed if completed else 0.0,
        "third_party_failures": int(row["third_party_failures"] or 0),
        "useful_observations": int(row["useful_observations"] or 0),
        "unresolved_targets": int(row["unresolved_targets"] or 0),
        "zero_spend_violation": bool(row["zero_spend_violation"]),
    }


async def observe_live_stage(
    conn, *, policy: dict[str, Any] | None = None, resume: bool = False,
) -> dict[str, Any]:
    config = policy or load_policy()
    metrics = await collect_stage_metrics(conn, policy=config)
    if resume:
        return resume_after_observation(metrics, policy=config)
    return record_observation(metrics, policy=config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or control the persistent recon rollout gate")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init-state", action="store_true")
    group.add_argument("--activate", action="store_true")
    group.add_argument("--observe-live", action="store_true", help="Collect current stage metrics from PostgreSQL and persist them")
    group.add_argument("--resume-after-observation", action="store_true", help="Explicitly reassess a halted completed cohort")
    group.add_argument("--halt", metavar="REASON", help="Stop new claims without altering existing evidence")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args()
    try:
        policy = load_policy()
        if args.init_state:
            state = initialize_state(policy)
            result = {"initialized": True, "policy_id": state["policy_id"], "halt": state["halt"]}
        elif args.activate:
            state = activate_admission(policy=policy)
            result = {"activated": True, "policy_id": state["policy_id"], "cohort_limit": state["cohort_limit"]}
        elif args.halt:
            state = halt_admission(args.halt)
            result = {"halted": True, "policy_id": state["policy_id"], "halt_reason": state["halt_reason"]}
        elif args.observe_live or args.resume_after_observation:
            import asyncio
            from src.db.connection import close_pool, get_pool

            async def _observe():
                pool = await get_pool()
                try:
                    async with pool.acquire() as conn:
                        return await observe_live_stage(
                            conn, policy=policy, resume=args.resume_after_observation,
                        )
                finally:
                    await close_pool()

            result = asyncio.run(_observe())
        else:
            config, state, reason = admission_snapshot(policy)
            result = {"policy_id": config["policy_id"], "state": state, "admission_block": reason}
    except (AdmissionPolicyError, AdmissionDenied, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
