from __future__ import annotations

from pathlib import Path

import pytest

from scripts.recovery.record_task50_runtime_verification import SPECS, RuntimeVerificationError, ServiceSpec, collect


def _artifact(name: str, image: str) -> dict:
    return {
        "name": name,
        "image": {"id": image},
        "expected_in_container": {"source_sha256": "a" * 64, "artifact_sha256": "b" * 64},
    }


def _inspect(image: str) -> dict:
    return {"Image": image, "State": {"Running": True}, "Config": {"Env": ["SAFE=1"], "Cmd": ["run"]}, "Mounts": []}


def test_runtime_receipt_binds_running_container_to_artifact_image_and_hashes() -> None:
    spec = ServiceSpec("collector_whatsapp", "wa", "collector", "/src", "/artifact")
    receipt = collect(
        {"schema_version": 1, "task": 44, "artifacts": [_artifact("collector", "sha256:collector")]},
        {"schema_version": 1, "task": 49, "selected_branch": "recovered"},
        inspect=lambda _: _inspect("sha256:collector"),
        container_sha=lambda _container, path: "a" * 64 if path == "/src" else "b" * 64,
        specs=(spec,),
    )
    assert receipt["services"][0]["in_container"] == {"source_sha256": "a" * 64, "artifact_sha256": "b" * 64}


def test_runtime_receipt_rejects_stale_image_or_in_container_hash() -> None:
    spec = ServiceSpec("collector_whatsapp", "wa", "collector", "/src", "/artifact")
    runtime = {"schema_version": 1, "task": 44, "artifacts": [_artifact("collector", "sha256:collector")]}
    activation = {"schema_version": 1, "task": 49}
    with pytest.raises(RuntimeVerificationError, match="image"):
        collect(runtime, activation, inspect=lambda _: _inspect("sha256:stale"), specs=(spec,))
    with pytest.raises(RuntimeVerificationError, match="hashes"):
        collect(runtime, activation, inspect=lambda _: _inspect("sha256:collector"), container_sha=lambda *_: "c" * 64, specs=(spec,))


def test_runtime_receipt_covers_the_recon_services_in_the_approved_set() -> None:
    specs = {spec.service: spec for spec in SPECS}
    assert specs["recon_egress_guard"].artifact == "recon-egress-guard"
    assert specs["collector_spiderfoot"].artifact == "spiderfoot"
    assert specs["browser_ingest"].artifact == "collector"
    assert specs["scheduler"].artifact == "scheduler"


def test_runtime_receipt_uses_baseline_artifacts_for_unchanged_services() -> None:
    spec = ServiceSpec("collector_paypal", "paypal", "collector", "/src", "/artifact")
    receipt = collect(
        {"schema_version": 1, "task": 44, "artifacts": [_artifact("collector", "sha256:refreshed")]},
        {"schema_version": 1, "task": 49},
        baseline_runtime={"schema_version": 1, "task": 44, "artifacts": [_artifact("collector", "sha256:baseline")]},
        inspect=lambda _: _inspect("sha256:baseline"),
        container_sha=lambda _container, path: "a" * 64 if path == "/src" else "b" * 64,
        specs=(spec,),
    )
    assert receipt["services"][0]["release"] == "baseline"


def test_production_whatsapp_bridge_retains_source_for_runtime_hashing() -> None:
    dockerfile = Path("src/bridges/whatsapp/Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=builder /app/src ./src" in dockerfile
