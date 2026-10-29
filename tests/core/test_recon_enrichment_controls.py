from pathlib import Path
import json
from unittest.mock import AsyncMock

import asyncpg
import pytest

from src.core import ghunt_enrich, optional_rollout, recon


@pytest.mark.parametrize("configured_modules,expected", [
    (None, None), ("", None), (" , ", None),
    ("sfp_accounts", ["sfp_accounts"]), (" maigret ", ["maigret"]),
])
async def test_seen_target_username_modules_are_operator_controlled(
    monkeypatch: pytest.MonkeyPatch, configured_modules: str | None, expected: list[str] | None,
) -> None:
    if configured_modules is None:
        monkeypatch.delenv("RECON_USERNAME_MODULES", raising=False)
    else:
        monkeypatch.setenv("RECON_USERNAME_MODULES", configured_modules)
    queue = AsyncMock()
    monkeypatch.setattr(recon, "queue_recon_target", queue)

    report = await optional_rollout._queue_spiderfoot_seen_candidates(
        AsyncMock(spec=asyncpg.Connection),
        [{"target_type": "username", "target_key": "example_handle", "source": "x"}],
        target_cap=1,
    )

    assert report == {"queued": 1, "skipped": 0}
    assert queue.await_args.kwargs["scope"].get("modules") == expected


async def test_missing_ghunt_credentials_skip_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("GHUNT_CREDS", str(tmp_path / "missing-creds.m"))
    launch = AsyncMock()
    monkeypatch.setattr(ghunt_enrich.asyncio, "create_subprocess_exec", launch)

    result = await ghunt_enrich.run_lookup("example@example.invalid")

    assert result["status"] == "skipped"
    assert "data" not in result
    launch.assert_not_awaited()


async def test_ghunt_raw_report_is_retained_with_owner_only_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    credentials = tmp_path / "creds.m"
    credentials.write_text("{}", encoding="utf-8")
    binary = tmp_path / "ghunt"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o700)
    evidence = tmp_path / "private-evidence"
    monkeypatch.setenv("GHUNT_CREDS", str(credentials))
    monkeypatch.setenv("GHUNT_BIN", str(binary))
    monkeypatch.setenv("RECON_EVIDENCE_ROOT", str(evidence))

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def launch(*args, **_kwargs):
        output = Path(args[4])
        output.write_text(json.dumps({"profile": {"name": "Example"}}), encoding="utf-8")
        return Process()

    monkeypatch.setattr(ghunt_enrich.asyncio, "create_subprocess_exec", launch)
    result = await ghunt_enrich.run_lookup("example@example.invalid")

    reports = list((evidence / "ghunt").glob("ghunt-*.json"))
    assert result["status"] == "ok"
    assert len(reports) == 1
    assert reports[0].stat().st_mode & 0o777 == 0o600
    assert (evidence / "ghunt").stat().st_mode & 0o777 == 0o700
    assert evidence.stat().st_mode & 0o777 == 0o700


def test_ghunt_readiness_requires_credentials_and_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    credentials = tmp_path / "creds.m"
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GHUNT_CREDS", str(credentials))
    monkeypatch.setenv("GHUNT_BIN", str(tmp_path / "missing-ghunt"))

    ready, _ = ghunt_enrich.is_configured()

    assert ready is False
