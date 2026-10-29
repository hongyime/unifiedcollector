from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts.verify_source_acceptance import AcceptanceError, main, verify


def _contract() -> dict:
    return {
        "schema_version": 1,
        "sources": [
            {
                "source": "paypal_profiles",
                "kind": "active",
                "durable": {"table": "profiles", "key": "source,profile_id"},
                "cohort": {"source": "paypal", "stage": "rollout"},
                "required_fields": ["profile_id", "profile_url"],
                "media": "not_applicable",
                "telemetry": "not_expected",
                "paid_evidence": "forbidden",
            },
            {
                "source": "website_media",
                "kind": "active",
                "durable": {"table": "media_items", "key": "source,content_id"},
                "cohort": {"source": "website", "stage": "rollout"},
                "required_fields": ["content_id", "file_path", "sha256"],
                "media": "required",
                "telemetry": "expected",
                "paid_evidence": "forbidden",
            },
            {
                "source": "telegram_login",
                "kind": "human_blocked",
                "durable": {"table": "telegram_accounts", "key": "account_id"},
                "cohort": {"source": "telegram", "stage": "human"},
                "required_fields": ["account_id"],
                "media": "not_applicable",
                "telemetry": "not_expected",
                "paid_evidence": "forbidden",
            },
        ],
    }


def _write(tmp_path: Path, contract: dict, entries: list[dict]) -> tuple[Path, Path]:
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps({"schema_version": 1, "contract_sha256": digest, "entries": entries}), encoding="utf-8")
    return contract_path, ledger_path


def _entry(source: str, status: str, cohort: dict | None = None, fields: dict | None = None) -> dict:
    entry = {"source": source, "status": status}
    if status == "verified":
        entry["evidence"] = {
            "contract_sha256": "placeholder",
            "cohort": cohort,
            "paid_evidence": False,
            "observed_at_utc": "2026-10-26T00:00:00Z",
            "required_fields": fields or {},
        }
    return entry


def _bind_evidence(contract_path: Path, ledger_path: Path) -> None:
    ledger = json.loads(ledger_path.read_text())
    digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    for entry in ledger["entries"]:
        if "evidence" in entry:
            entry["evidence"]["contract_sha256"] = digest
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")


def test_predeployment_allows_pending_active_and_blocked_human_rows(tmp_path: Path) -> None:
    contract, ledger = _write(tmp_path, _contract(), [
        _entry("paypal_profiles", "pending_live_verification"),
        _entry("website_media", "pending_live_verification"),
        _entry("telegram_login", "blocked"),
    ])
    report = verify(contract, ledger, stage="predeployment", selected=set(), since=None)
    assert report["pending"] == ["paypal_profiles", "website_media"]
    assert report["blocked"] == ["telegram_login"]


def test_rollout_requires_exact_source_evidence_and_final_rejects_pending(tmp_path: Path) -> None:
    contract_doc = _contract()
    contract, ledger = _write(tmp_path, contract_doc, [
        _entry("paypal_profiles", "verified", contract_doc["sources"][0]["cohort"], {"profile_id": "x", "profile_url": "x"}),
        _entry("website_media", "pending_live_verification"),
        _entry("telegram_login", "blocked"),
    ])
    _bind_evidence(contract, ledger)
    assert verify(contract, ledger, stage="rollout", selected={"paypal_profiles"}, since=None)["verified"] == ["paypal_profiles"]
    with pytest.raises(AcceptanceError, match="final stage"):
        verify(contract, ledger, stage="final", selected=set(), since=None)


def test_rejects_wrong_cohort(tmp_path: Path) -> None:
    contract_doc = _contract()
    contract, ledger = _write(tmp_path, contract_doc, [
        _entry("paypal_profiles", "verified", {"wrong": True}, {"profile_id": "x", "profile_url": "x"}),
        _entry("website_media", "pending_live_verification"),
        _entry("telegram_login", "blocked"),
    ])
    _bind_evidence(contract, ledger)
    with pytest.raises(AcceptanceError, match="cohort"):
        verify(contract, ledger, stage="rollout", selected={"paypal_profiles"}, since=None)


def test_rejects_required_media_or_telemetry_evidence_that_is_missing(tmp_path: Path) -> None:
    contract_doc = _contract()
    entry = _entry(
        "website_media", "verified", contract_doc["sources"][1]["cohort"],
        {"content_id": "x", "file_path": "x", "sha256": "x"},
    )
    contract, ledger = _write(tmp_path, contract_doc, [
        _entry("paypal_profiles", "pending_live_verification"),
        entry,
        _entry("telegram_login", "blocked"),
    ])
    _bind_evidence(contract, ledger)
    with pytest.raises(AcceptanceError, match="media"):
        verify(contract, ledger, stage="rollout", selected={"website_media"}, since=None)
    entry["evidence"]["media"] = True
    entry["evidence"]["contract_sha256"] = hashlib.sha256(contract.read_bytes()).hexdigest()
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "entries": [
            _entry("paypal_profiles", "pending_live_verification"), entry,
            _entry("telegram_login", "blocked"),
        ],
    }), encoding="utf-8")
    with pytest.raises(AcceptanceError, match="telemetry"):
        verify(contract, ledger, stage="rollout", selected={"website_media"}, since=None)


@pytest.mark.parametrize(
    ("mutate", "since", "error"),
    [
        (
            lambda evidence: evidence.__setitem__("observed_at_utc", "2026-10-25T23:59:59Z"),
            "2026-10-26T00:00:00Z",
            "stale",
        ),
        (
            lambda evidence: evidence.__setitem__("paid_evidence", True),
            None,
            "paid/unknown",
        ),
        (
            lambda evidence: evidence.__setitem__("observed_at_utc", "2026-10-26T08:00:00+08:00"),
            None,
            "UTC timestamp",
        ),
    ],
)
def test_rejects_stale_paid_or_non_utc_evidence(tmp_path: Path, mutate, since: str | None, error: str) -> None:
    contract_doc = _contract()
    contract, ledger = _write(tmp_path, contract_doc, [
        _entry("paypal_profiles", "verified", contract_doc["sources"][0]["cohort"], {"profile_id": "x", "profile_url": "x"}),
        _entry("website_media", "pending_live_verification"),
        _entry("telegram_login", "blocked"),
    ])
    _bind_evidence(contract, ledger)
    payload = json.loads(ledger.read_text())
    mutate(payload["entries"][0]["evidence"])
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AcceptanceError, match=error):
        verify(contract, ledger, stage="rollout", selected={"paypal_profiles"}, since=(
            datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
        ))


def test_rollout_rejects_human_blocked_source_even_if_ledger_is_correct(tmp_path: Path) -> None:
    contract, ledger = _write(tmp_path, _contract(), [
        _entry("paypal_profiles", "pending_live_verification"),
        _entry("website_media", "pending_live_verification"),
        _entry("telegram_login", "blocked"),
    ])
    with pytest.raises(AcceptanceError, match="cannot be selected"):
        verify(contract, ledger, stage="rollout", selected={"telegram_login"}, since=None)


def test_cli_all_and_record_only_writes_verified_active_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract_doc = _contract()
    website = _entry(
        "website_media", "verified", contract_doc["sources"][1]["cohort"],
        {"content_id": "x", "file_path": "x", "sha256": "x"},
    )
    website["evidence"].update({"media": True, "telemetry": True})
    contract, ledger = _write(tmp_path, contract_doc, [
        _entry("paypal_profiles", "verified", contract_doc["sources"][0]["cohort"], {"profile_id": "x", "profile_url": "x"}),
        website,
        _entry("telegram_login", "blocked"),
    ])
    _bind_evidence(contract, ledger)
    monkeypatch.setattr(sys, "argv", [
        "verify_source_acceptance.py", "--contract", str(contract), "--ledger", str(ledger),
        "--stage", "final", "--all", "--record",
    ])
    assert main() == 0
    entries = {entry["source"]: entry for entry in json.loads(ledger.read_text())["entries"]}
    assert "last_verification" in entries["paypal_profiles"]
    assert "last_verification" in entries["website_media"]
    assert "last_verification" not in entries["telegram_login"]
