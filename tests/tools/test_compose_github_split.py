"""GitHub, Strava, and search run in independent worker containers."""
from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"
DEV_COMPOSE = COMPOSE.parents[1] / "compose.dev.yaml"
WATCH_COMPOSE = COMPOSE.parents[1] / "compose.watch.yaml"


def _service_block(name: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match is not None, name
    return match.group(1)


def test_github_has_its_own_worker() -> None:
    block = _service_block("collector_github")
    assert "python -m src.main worker --source github\n" in block
    assert "github.env" in block
    assert "strava.env" not in block
    assert "search.env" not in block
    assert "GITHUB_API_DELAY:" in block


def test_search_and_strava_have_independent_workers_and_env_files() -> None:
    search = _service_block("collector_search")
    strava = _service_block("collector_strava")
    assert "python -m src.main worker --source search\n" in search
    assert "search.env" in search
    assert "strava.env" not in search
    assert "python -m src.main worker --source strava\n" in strava
    assert "strava.env" in strava
    assert "search.env" not in strava


def test_exposure_and_lemon8_have_independent_workers_and_env_files() -> None:
    exposure = _service_block("collector_exposure")
    lemon8 = _service_block("collector_lemon8")
    assert "python -m src.main worker --source exposure\n" in exposure
    assert "exposure.env" in exposure
    assert "EXPOSURE_ENABLED:" not in exposure
    assert "EXPOSURE_ALLOWED_DOMAINS:" not in exposure
    assert "LEMON8_FYP_DETAIL_FETCH" not in exposure
    assert "python -m src.main worker --source lemon8\n" in lemon8
    assert "lemon8.env" in lemon8
    assert "EXPOSURE_ENABLED" not in lemon8


def test_browser_bridge_has_its_new_service_and_container_name() -> None:
    block = _service_block("browser_ingest")
    assert "unifiedcollector_browser_ingest" in block
    assert "python -m src.bridges.ig_ingest" in block


def test_development_compose_uses_the_split_worker_names() -> None:
    dev = DEV_COMPOSE.read_text(encoding="utf-8")
    watch = WATCH_COMPOSE.read_text(encoding="utf-8")
    for service in ("collector_exposure", "collector_lemon8", "collector_search", "collector_strava", "browser_ingest"):
        assert f"  {service}:" in dev
    for service in ("collector_search", "collector_strava", "browser_ingest"):
        assert f"  {service}:" in watch
