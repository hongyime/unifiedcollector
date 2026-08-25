from unittest.mock import AsyncMock

import pytest

from src.collectors import get_collector, list_sources
from src.collectors.exposure import (
    ExposureCollector,
    build_gate,
    classify_exposure,
    is_scope_allowed,
    redact_url,
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


def test_redact_url_masks_sensitive_query_values():
    redacted = redact_url("https://example.edu.sg/a?token=abc123&view=public&signature=deadbeef")
    assert redacted == "https://example.edu.sg/a?token=%5BREDACTED%5D&view=public&signature=%5BREDACTED%5D"


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
    saved = [call.args[0] for call in coll.checkpoint.save_progress.await_args_list]
    assert len(saved) == 2
    assert all(item.startswith("dork:") for item in saved)
    assert all(len(item) < 100 for item in saved)


@pytest.mark.asyncio
async def test_collect_treats_wildcards_as_gate_only_by_default(monkeypatch, tmp_path):
    dorks = tmp_path / "exposure.dorks"
    dorks.write_text('site:[TARGET] filename:.env\nintext:[TARGET] password\n', encoding="utf-8")
    monkeypatch.setenv("EXPOSURE_ENABLED", "1")
    monkeypatch.setenv("EXPOSURE_DORKS_FILE", str(dorks))
    monkeypatch.setenv("EXPOSURE_ALLOWED_DOMAINS", "*.edu.sg,*.*")
    monkeypatch.setenv("EXPOSURE_ALLOWED_REGEX", ".*")
    monkeypatch.setenv("EXPOSURE_MAX_QUERIES_PER_CYCLE", "10")

    coll = ExposureCollector()
    coll.search_query = AsyncMock(return_value=[])
    coll.checkpoint.save_progress = AsyncMock()
    coll._seed_scopes_from_collector = AsyncMock(return_value=["smu.edu.sg", "nus.edu.sg"])

    await coll.collect(["*.edu.sg", "*.*", "regex:.*"])

    queries = [call.args[0] for call in coll.search_query.await_args_list]
    assert queries == [
        "site:smu.edu.sg filename:.env",
        "intext:smu.edu.sg password",
        "site:nus.edu.sg filename:.env",
        "intext:nus.edu.sg password",
    ]


@pytest.mark.asyncio
async def test_collect_can_expand_wildcards_when_explicitly_enabled(monkeypatch, tmp_path):
    dorks = tmp_path / "exposure.dorks"
    dorks.write_text('site:[TARGET] filename:.env\nintext:[TARGET] password\n', encoding="utf-8")
    monkeypatch.setenv("EXPOSURE_ENABLED", "1")
    monkeypatch.setenv("EXPOSURE_DORKS_FILE", str(dorks))
    monkeypatch.setenv("EXPOSURE_ALLOWED_DOMAINS", "*.edu.sg,*.*")
    monkeypatch.setenv("EXPOSURE_ALLOWED_REGEX", ".*")
    monkeypatch.setenv("EXPOSURE_EXPAND_WILDCARD_TARGETS", "1")
    monkeypatch.setenv("EXPOSURE_MAX_QUERIES_PER_CYCLE", "10")

    coll = ExposureCollector()
    coll.search_query = AsyncMock(return_value=[])
    coll.checkpoint.save_progress = AsyncMock()
    coll._seed_scopes_from_collector = AsyncMock(return_value=["smu.edu.sg"])

    await coll.collect(["*.edu.sg", "*.*", "regex:.*"])

    queries = [call.args[0] for call in coll.search_query.await_args_list]
    assert queries[:4] == [
        "site:*.edu.sg filename:.env",
        "intext:*.edu.sg password",
        "site:*.* filename:.env",
        "intext:*.* password",
    ]
    assert "site:smu.edu.sg filename:.env" in queries


@pytest.mark.asyncio
async def test_collect_can_make_wildcards_gate_only(monkeypatch, tmp_path):
    dorks = tmp_path / "exposure.dorks"
    dorks.write_text("site:[TARGET] filename:.env\n", encoding="utf-8")
    monkeypatch.setenv("EXPOSURE_ENABLED", "1")
    monkeypatch.setenv("EXPOSURE_DORKS_FILE", str(dorks))
    monkeypatch.setenv("EXPOSURE_ALLOWED_DOMAINS", "*.edu.sg")
    monkeypatch.setenv("EXPOSURE_EXPAND_WILDCARD_TARGETS", "0")

    coll = ExposureCollector()
    coll.search_query = AsyncMock(return_value=[])
    coll.checkpoint.save_progress = AsyncMock()
    coll._seed_scopes_from_collector = AsyncMock(return_value=["smu.edu.sg"])

    await coll.collect(["*.edu.sg"])

    assert [call.args[0] for call in coll.search_query.await_args_list] == [
        "site:smu.edu.sg filename:.env",
    ]


@pytest.mark.asyncio
async def test_collect_disabled_does_not_search(monkeypatch):
    monkeypatch.setenv("EXPOSURE_ENABLED", "0")
    coll = ExposureCollector()
    coll.search_query = AsyncMock()

    await coll.collect(["example.edu.sg"])

    coll.search_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_upsert_exposure_finding_serializes_metadata(monkeypatch):
    coll = ExposureCollector()
    calls = []

    class Conn:
        async def execute(self, *args):
            calls.append(args)

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *_exc):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    coll.pool = Pool()
    coll._has_url_hash_column = True

    await coll._upsert_exposure_finding(
        "site:example.edu.sg filename:.env",
        {
            "url": "https://example.edu.sg/.env?token=abc123",
            "domain": "example.edu.sg",
            "title": "password=hunter2",
            "snippet": "token: abcdef",
            "engine": "test",
        },
        inserted=True,
    )

    assert calls
    assert calls[0][3] == "https://example.edu.sg/.env?token=%5BREDACTED%5D"
    assert isinstance(calls[0][-2], str)
    assert "$11::jsonb" in calls[0][0]
    assert '"search_result_inserted": true' in calls[0][-2]
    assert len(calls[0][-1]) == 64


def test_query_key_is_compact_and_stable():
    query = "site:example.edu.sg " + ("filename:.env " * 100)
    key = ExposureCollector._query_key(query)
    assert key == ExposureCollector._query_key(query)
    assert key.startswith("exposure:")
    assert len(key) < 100
