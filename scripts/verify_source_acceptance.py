#!/usr/bin/env python3
"""Validate immutable source contracts against mutable rollout acceptance evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AcceptanceError(ValueError):
    """A source contract or its acceptance ledger is incomplete or unsafe."""


_MEDIA = {"required", "not_applicable"}
_TELEMETRY = {"expected", "not_expected"}
_KINDS = {"active", "human_blocked"}
_STATUSES = {"pending_live_verification", "verified", "blocked"}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{path} must be a JSON object")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise AcceptanceError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AcceptanceError(f"{label} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise AcceptanceError(f"{label} must include a timezone")
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise AcceptanceError(f"{label} must be a UTC timestamp")
    return parsed.astimezone(UTC)


def contract_sources(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if contract.get("schema_version") != 1 or not isinstance(contract.get("sources"), list):
        raise AcceptanceError("unsupported source-contract schema")
    sources: dict[str, dict[str, Any]] = {}
    for source in contract["sources"]:
        if not isinstance(source, dict):
            raise AcceptanceError("source contract rows must be objects")
        name = source.get("source")
        if not isinstance(name, str) or not name or name in sources:
            raise AcceptanceError("source names must be unique nonempty strings")
        kind = source.get("kind")
        if kind not in _KINDS:
            raise AcceptanceError(f"{name}: unsupported source kind")
        durable = source.get("durable")
        cohort = source.get("cohort")
        required = source.get("required_fields")
        if not isinstance(durable, dict) or not all(isinstance(durable.get(key), str) and durable[key] for key in ("table", "key")):
            raise AcceptanceError(f"{name}: durable table/key are required")
        if not isinstance(cohort, dict) or not cohort:
            raise AcceptanceError(f"{name}: exact cohort readback is required")
        if not isinstance(required, list) or not required or not all(isinstance(field, str) and field for field in required):
            raise AcceptanceError(f"{name}: required fields are required")
        if source.get("media") not in _MEDIA or source.get("telemetry") not in _TELEMETRY:
            raise AcceptanceError(f"{name}: media/telemetry contract is invalid")
        if source.get("paid_evidence") != "forbidden":
            raise AcceptanceError(f"{name}: paid evidence must be forbidden")
        sources[name] = source
    if not sources:
        raise AcceptanceError("source contract cannot be empty")
    return sources


def ledger_entries(ledger: dict[str, Any], sources: dict[str, dict[str, Any]], contract_sha256: str) -> dict[str, dict[str, Any]]:
    if ledger.get("schema_version") != 1 or ledger.get("contract_sha256") != contract_sha256:
        raise AcceptanceError("ledger is not bound to this exact source contract")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise AcceptanceError("ledger entries must be a list")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise AcceptanceError("ledger entry source is invalid")
        name = entry["source"]
        if name not in sources or name in result:
            raise AcceptanceError("ledger contains an unlisted or duplicate source")
        if entry.get("status") not in _STATUSES:
            raise AcceptanceError(f"{name}: invalid acceptance status")
        result[name] = entry
    if set(result) != set(sources):
        raise AcceptanceError("ledger must contain every contract source exactly once")
    return result


def _verify_evidence(name: str, source: dict[str, Any], entry: dict[str, Any], contract_sha256: str, since: datetime | None) -> None:
    evidence = entry.get("evidence")
    if not isinstance(evidence, dict):
        raise AcceptanceError(f"{name}: verified source lacks durable evidence")
    if evidence.get("contract_sha256") != contract_sha256:
        raise AcceptanceError(f"{name}: evidence was produced for a different contract revision")
    if evidence.get("cohort") != source["cohort"]:
        raise AcceptanceError(f"{name}: evidence cohort does not match its contract")
    if evidence.get("paid_evidence") is not False:
        raise AcceptanceError(f"{name}: paid/unknown evidence is forbidden")
    observed = _timestamp(evidence.get("observed_at_utc"), f"{name}.evidence.observed_at_utc")
    if since is not None and observed < since:
        raise AcceptanceError(f"{name}: evidence is stale for the requested --since boundary")
    fields = evidence.get("required_fields")
    if not isinstance(fields, dict) or any(not fields.get(field) for field in source["required_fields"]):
        raise AcceptanceError(f"{name}: evidence omits a required durable field")
    if source["media"] == "required" and evidence.get("media") is not True:
        raise AcceptanceError(f"{name}: evidence omits required media proof")
    if source["telemetry"] == "expected" and evidence.get("telemetry") is not True:
        raise AcceptanceError(f"{name}: evidence omits required telemetry proof")


def verify(contract_path: Path, ledger_path: Path, *, stage: str, selected: set[str], since: datetime | None) -> dict[str, Any]:
    if stage not in {"predeployment", "rollout", "final"}:
        raise AcceptanceError("stage must be predeployment, rollout, or final")
    contract = _load(contract_path)
    sources = contract_sources(contract)
    digest = _digest(contract_path)
    entries = ledger_entries(_load(ledger_path), sources, digest)
    if selected and not selected.issubset(sources):
        raise AcceptanceError("--source names an unlisted source")
    if stage == "rollout" and not selected:
        raise AcceptanceError("rollout requires at least one --source")
    targets = selected or set(sources)
    verified: list[str] = []
    blocked: list[str] = []
    pending: list[str] = []
    for name, source in sources.items():
        entry = entries[name]
        status = entry["status"]
        if source["kind"] == "human_blocked":
            if stage == "rollout" and name in targets:
                raise AcceptanceError(f"{name}: human-blocked source cannot be selected for rollout")
            if status != "blocked":
                raise AcceptanceError(f"{name}: human-blocked source must remain blocked")
            blocked.append(name)
            continue
        if status == "blocked":
            raise AcceptanceError(f"{name}: active source cannot be marked blocked")
        if status == "verified":
            _verify_evidence(name, source, entry, digest, since if name in targets else None)
            verified.append(name)
        else:
            pending.append(name)
        if stage == "rollout" and name in targets and status != "verified":
            raise AcceptanceError(f"{name}: rollout source is not verified")
        if stage == "final" and status != "verified":
            raise AcceptanceError(f"{name}: final stage requires every active source verified")
    return {"stage": stage, "contract_sha256": digest, "verified": sorted(verified), "pending": sorted(pending), "blocked": sorted(blocked)}


def _write_record(ledger_path: Path, report: dict[str, Any], selected: set[str]) -> None:
    ledger = _load(ledger_path)
    verified = set(report["verified"])
    for entry in ledger["entries"]:
        if entry["source"] in selected & verified:
            entry["last_verification"] = report
    temporary = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    temporary.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(ledger_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--stage", required=True, choices=("predeployment", "rollout", "final"))
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--since")
    parser.add_argument("--record", action="store_true", help="record this verifier report without fabricating evidence")
    parser.add_argument("--all", action="store_true", help="select all sources for an explicit rollout check")
    args = parser.parse_args()
    if args.all and args.source:
        raise SystemExit("--all and --source are mutually exclusive")
    selected = set(args.source)
    if args.all:
        selected = set(contract_sources(_load(args.contract)))
    since = _timestamp(args.since, "--since") if args.since else None
    try:
        report = verify(args.contract, args.ledger, stage=args.stage, selected=selected, since=since)
    except AcceptanceError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    if args.record:
        if args.stage == "predeployment":
            raise SystemExit("--record is not allowed for predeployment")
        _write_record(args.ledger, report, selected)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
