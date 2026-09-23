from pathlib import Path
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


def test_ghunt_readiness_requires_credentials_and_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    credentials = tmp_path / "creds.m"
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GHUNT_CREDS", str(credentials))
    monkeypatch.setenv("GHUNT_BIN", str(tmp_path / "missing-ghunt"))

    ready, _ = ghunt_enrich.is_configured()

    assert ready is False
