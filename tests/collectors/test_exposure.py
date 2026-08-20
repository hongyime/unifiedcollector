import os
from unittest.mock import AsyncMock

import pytest

from src.collectors import get_collector, list_sources
from src.collectors.exposure import (
    ExposureCollector,
    build_gate,
    classify_exposure,
    is_scope_allowed,
    redact_text,
)


def test_exposure_registered(monkeypatch):
    monkeypatch.delenv("COLLECTOR_DISABLED_SOURCES", raising=False)
    assert "exposure" in list_sources()
    assert isinstance(get_collector("exposure"), ExposureCollector)


def test_gate_allows_exact_wildcard_and_regex(monkeypatch):
    monkeypatch.setenv("EXPOSURE_ALLOWED_DOMAINS", "example.edu.sg,*.school.edu.sg")
    monkeypatch.setenv("EXPOSURE_ALLOWED_REGEX", r"^research\d+\.example\.org$")
    gate = build_gate(["regex:^lab-[a-z]+\\.example\\.net$"])

    assert is_scope_allowed("example.edu.sg", gate)
    assert is_scope_allowed("www.school.edu.sg", gate)
    assert is_scope_allowed("research7.example.org", gate)
    assert is_scope_allowed("lab-alpha.example.net", gate)
    assert not is_scope_allowed("attacker.example.com", gate)


def test_redact_text_masks_secret_assignments():
    assert redact_text("password = hunter2 and token: abcdef") == "password=[REDACTED] and token=[REDACTED]"


def test_classify_exposure_marks_git_high():
    category, severity, confidence, secret_like = classify_exposure(
        'inurl:"/.git" example.edu.sg -site:github.com',
        {"url": "https://example.edu.sg/.git/config", "snippet": "repo config"},
    )
    assert category == "exposed_git"
    assert severity == "high"
    assert confidence >= 0.8
    assert secret_like is False


@pytest.mark.asyncio
async def test_collect_expands_only_allowed_scopes(monkeypatch, tmp_path):
    dorks = tmp_path / "exposure.dorks"
    dorks.write_text("site:[TARGET] filename:.env\nsite:[TARGET] ext:sql\n", encoding="utf-8")
    monkeypatch.setenv("EXPOSURE_ENABLED", "1")
    monkeypatch.setenv("EXPOSURE_DORKS_FILE", str(dorks))
    monkeypatch.setenv("EXPOSURE_ALLOWED_DOMAINS", "example.edu.sg")
    monkeypatch.setenv("EXPOSURE_MAX_QUERIES_PER_CYCLE", "10")

    coll = ExposureCollector()
    coll.search_query = AsyncMock(return_value=[])
    coll.checkpoint.save_progress = AsyncMock()

    await coll.collect(["example.edu.sg", "other.edu.sg"])

    queries = [call.args[0] for call in coll.search_query.await_args_list]
    assert queries == [
        "site:example.edu.sg filename:.env",
        "site:example.edu.sg ext:sql",
    ]


@pytest.mark.asyncio
async def test_collect_disabled_does_not_search(monkeypatch):
    monkeypatch.setenv("EXPOSURE_ENABLED", "0")
    coll = ExposureCollector()
    coll.search_query = AsyncMock()

    await coll.collect(["example.edu.sg"])

    coll.search_query.assert_not_awaited()
