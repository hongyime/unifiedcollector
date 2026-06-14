"""Tests for src/collectors/github.py — Wave 2 batch.

Pure unit. No network, no docker, no real DB. The httpx client is fully
mocked at the ``_make_client`` boundary; the asyncpg pool is replaced with
an AsyncMock chain.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ``GithubCollector.__init__`` constructs a ProfilePhotoTracker which only
# reaches into env, so no extra setup is needed at import time. We *do*
# need DRIVE_PATH to be writable for ``account_media_dir``; tests below
# either avoid touching it or monkeypatch the property on the instance.

from src.collectors import github as github_mod
from src.collectors.github import (
    AVATAR_CDN_BASE,
    GithubCollector,
    GithubEdgeFetcher,
    _parse_iso,
)
from src.core.spider_discover import EdgeType


# ── helpers ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_drive(tmp_path, monkeypatch):
    """Redirect DRIVE_PATH so media_dir lands somewhere writable per-test."""
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    # Disable Tor so _make_client returns a vanilla httpx (we never call it
    # for real anyway, but we don't want it to import the proxy plumbing).
    monkeypatch.setenv("GITHUB_USE_TOR", "0")
    monkeypatch.setenv("TOR_PROXY_ENABLED", "0")
    yield


def _make_pool() -> MagicMock:
    """Mock asyncpg pool whose ``acquire()`` is an async-context-manager
    yielding a connection with all the methods the collector calls.
    """
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    pool._conn = conn  # test-side handle for assertions
    return pool


def _make_response(
    *, status: int = 200, json_body: Any = None, content: bytes = b"",
    headers: dict[str, str] | None = None,
):
    """Build a MagicMock that quacks like an httpx.Response."""
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json = MagicMock(return_value=json_body if json_body is not None else {})
    r.content = content
    r.raise_for_status = MagicMock()
    return r


def _patch_make_client(collector: GithubCollector, http_mock: MagicMock):
    """Replace ``_make_client`` so it returns ``http_mock`` as the
    async-context-manager body. The returned client exposes ``.get``."""

    @asynccontextmanager
    async def _cm():
        yield http_mock

    # _make_client is called as ``self._make_client()`` returning a
    # context manager — i.e. a non-async callable that yields a CM.
    collector._make_client = _cm  # type: ignore[assignment]


def _clear_github_env(monkeypatch):
    """Strip any GITHUB_*/PAT vars so constructor sees a clean env."""
    for key in (
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GITHUB_PATS",
        "GITHUB_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)


def _new_collector(monkeypatch=None) -> GithubCollector:
    """Construct a collector with no PATs and a fake pool wired.

    Pass `monkeypatch` so the container's ambient GITHUB_TOKEN does not
    leak into the constructor; tests that pre-set their own env should
    still call `_clear_github_env(monkeypatch)` before populating.
    """
    if monkeypatch is not None:
        _clear_github_env(monkeypatch)
    else:
        # Last-resort fallback for callers that don't pass monkeypatch:
        # blow away the ambient env on the *real* os.environ. Pytest
        # fixtures aren't available, so this leaks across tests if abused.
        for key in ("GITHUB_TOKEN", "GITHUB_PAT", "GITHUB_PATS", "GITHUB_TOKENS"):
            os.environ.pop(key, None)
    coll = GithubCollector()
    coll.set_pool(_make_pool())
    return coll


# ── module-level helpers ──────────────────────────────────────────────────


def test_parse_iso_handles_z_suffix():
    out = _parse_iso("2024-01-02T03:04:05Z")
    assert out is not None
    assert out.year == 2024 and out.hour == 3


def test_parse_iso_returns_none_on_garbage():
    assert _parse_iso(None) is None
    assert _parse_iso("") is None
    assert _parse_iso("not-a-date") is None
    assert _parse_iso(12345) is None  # type: ignore[arg-type]


# ── construction / configuration ──────────────────────────────────────────


def test_constructor_defaults_no_pats():
    coll = _new_collector()
    assert coll.SOURCE_NAME == "github"
    assert coll._pats == []
    assert coll._current_pat() is None
    # No PAT → no Authorization header
    h = coll._headers()
    assert "Authorization" not in h
    assert "User-Agent" in h


def test_constructor_loads_pats_from_env(monkeypatch):
    monkeypatch.setenv(
        "GITHUB_TOKEN", "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaa,ghp_bbbbbbbbbbbbbbbbbbbbbbb"
    )
    coll = GithubCollector()
    assert len(coll._pats) == 2
    assert coll._headers()["Authorization"].startswith("token ghp_")


def test_constructor_falls_back_to_github_pat_var(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_PAT", "ghp_xxxxxxxxxxxxxxxxxxxxxxxx")
    coll = GithubCollector()
    assert len(coll._pats) == 1


def test_pat_helpers_mask_and_validate():
    assert GithubCollector.get_pat_display("") == "****"
    assert GithubCollector.get_pat_display("short") == "****"
    masked = GithubCollector.get_pat_display("ghp_abcdefgh12345678zzzz")
    assert masked.startswith("ghp_") and "****" in masked

    assert GithubCollector.validate_pat_format("ghp_aaaaaaaaaaaaaaaaaaaaaaaaaa") is True
    assert GithubCollector.validate_pat_format("not_a_token") is False
    assert GithubCollector.validate_pat_format("") is False
    assert GithubCollector.validate_pat_format("xxx_abcdefghijklmnopqrstuvwx") is False


def test_rotate_pat_no_op_with_zero_or_one_token():
    coll = _new_collector()  # 0 pats
    assert coll._rotate_pat() is False
    coll._pats = ["only-one"]
    assert coll._rotate_pat() is False


def test_rotate_pat_advances_index_with_two_tokens():
    coll = _new_collector()
    coll._pats = ["a-token-aaaa", "b-token-bbbb"]
    assert coll._pat_idx == 0
    assert coll._rotate_pat() is True
    assert coll._pat_idx == 1
    assert coll._rotate_pat() is True
    assert coll._pat_idx == 0  # wraps


def test_account_media_dir_isolated_per_pat(tmp_path):
    coll = _new_collector()
    p1 = coll.account_media_dir
    assert p1.exists()
    assert p1.name == "token_1"
    assert p1.parent.name == "github"


# ── _update_rate_limit ────────────────────────────────────────────────────


def test_update_rate_limit_extracts_headers():
    coll = _new_collector()
    coll._update_rate_limit({
        "X-RateLimit-Remaining": "42",
        "X-RateLimit-Reset": "1700000000",
        "X-RateLimit-Limit": "5000",
    })
    assert coll.rate_limit_remaining == 42
    assert coll.rate_limit_reset == 1700000000
    assert coll.rate_limit_limit == 5000

    # Bad input: silent
    coll._update_rate_limit({"X-RateLimit-Remaining": "not-a-number"})
    # remaining sticks at the previous good value
    assert coll.rate_limit_remaining == 42


def test_get_rate_limit_status_snapshot():
    coll = _new_collector()
    coll.rate_limit_remaining = 99
    coll.rate_limit_limit = 5000
    coll.rate_limit_reset = 1700000000
    coll.requests_made = 7
    snap = coll.get_rate_limit_status()
    assert snap["remaining"] == 99
    assert snap["limit"] == 5000
    assert snap["reset_time"]  # iso-rendered
    assert snap["requests_made"] == 7
    assert snap["pat_count"] == 0


# ── high-level GET wrappers ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_returns_json_on_200():
    coll = _new_collector()
    http = MagicMock()
    http.get = AsyncMock(return_value=_make_response(
        status=200, json_body={"id": 1, "login": "octocat"}
    ))
    _patch_make_client(coll, http)

    out = await coll.get_user("octocat")
    assert out == {"id": 1, "login": "octocat"}
    http.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_user_returns_none_on_404():
    coll = _new_collector()
    http = MagicMock()
    http.get = AsyncMock(return_value=_make_response(status=404))
    _patch_make_client(coll, http)
    out = await coll.get_user("ghost")
    assert out is None


@pytest.mark.asyncio
async def test_get_user_by_id_returns_json():
    coll = _new_collector()
    http = MagicMock()
    http.get = AsyncMock(return_value=_make_response(
        status=200, json_body={"id": 5, "login": "u5"}
    ))
    _patch_make_client(coll, http)
    assert (await coll.get_user_by_id(5))["login"] == "u5"


@pytest.mark.asyncio
async def test_get_followers_paginates_and_terminates_short_page():
    coll = _new_collector()
    # First page returns 2 items (< PER_PAGE=100), so loop ends immediately.
    http = MagicMock()
    http.get = AsyncMock(return_value=_make_response(
        status=200, json_body=[{"login": "a"}, {"login": "b"}]
    ))
    _patch_make_client(coll, http)
    out = await coll.get_followers("octocat")
    assert [u["login"] for u in out] == ["a", "b"]
    http.get.assert_awaited_once()


# ── _api_get error paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_api_get_returns_none_on_404():
    coll = _new_collector()
    http = MagicMock()
    http.get = AsyncMock(return_value=_make_response(status=404))
    out = await coll._api_get(http, "https://api.github.com/users/nope")
    assert out is None


@pytest.mark.asyncio
async def test_api_get_returns_none_on_401():
    coll = _new_collector()
    http = MagicMock()
    http.get = AsyncMock(return_value=_make_response(status=401))
    out = await coll._api_get(http, "https://api.github.com/users/x")
    assert out is None


# ── upserts (DB seam) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_user_writes_expected_columns():
    coll = _new_collector()
    user = {
        "id": 100,
        "login": "octocat",
        "name": "Octo",
        "company": "GH",
        "blog": "blog",
        "location": "loc",
        "email": "e@e",
        "bio": "hi",
        "public_repos": 3,
        "followers": 4,
        "following": 5,
        "created_at": "2010-01-01T00:00:00Z",
    }
    await coll._upsert_user(user)
    coll.pool._conn.execute.assert_awaited_once()
    args = coll.pool._conn.execute.await_args.args
    sql = args[0]
    assert "INSERT INTO github_users" in sql
    # Positional args: id, login, name, company, blog, location, email,
    # bio, public_repos, followers, following, created_at
    assert args[1] == 100
    assert args[2] == "octocat"
    assert args[10] == 4  # followers (column $10)


@pytest.mark.asyncio
async def test_upsert_repo_serialises_metadata_and_handles_no_license():
    coll = _new_collector()
    repo = {
        "id": 7, "name": "r", "full_name": "u/r",
        "description": "d", "homepage": "h",
        "language": "Python", "stargazers_count": 1,
        "watchers_count": 2, "forks_count": 3,
        "open_issues_count": 0, "topics": ["a"],
        "license": None, "created_at": None, "updated_at": None,
    }
    await coll._upsert_repo(repo)
    args = coll.pool._conn.execute.await_args.args
    assert "INSERT INTO github_repos" in args[0]
    # license-derived value (positional 13: name, ...) — None on missing
    # We just sanity-check the metadata column at the end is JSON.
    assert args[-1].startswith("{")


@pytest.mark.asyncio
async def test_upsert_commit_tolerates_none_author_blocks():
    """Null commit/author/gh-author are common on imported / signed commits."""
    coll = _new_collector()
    commit = {"sha": "abc", "commit": None, "author": None}
    await coll._upsert_commit(0, commit)
    coll.pool._conn.execute.assert_awaited_once()
    args = coll.pool._conn.execute.await_args.args
    # Param order: (sha, repo_id, author_name, author_email, ...)
    assert args[1] == "abc"  # sha
    assert args[2] == 0      # repo_id
    assert args[3] is None   # author_name (null commit/author coalesced)


@pytest.mark.asyncio
async def test_upsert_issue_flags_pull_request():
    coll = _new_collector()
    issue = {
        "id": 1, "number": 2, "title": "t", "body": "b",
        "state": "open", "pull_request": {"url": "..."},
        "labels": [{"name": "bug"}], "comments": 0,
        "created_at": None, "updated_at": None,
    }
    await coll._upsert_issue(0, issue)
    args = coll.pool._conn.execute.await_args.args
    # Positional 6 is is_pull_request bool
    assert args[6] is True
    assert args[7] == ["bug"]  # labels list


# ── _enqueue_user behaviour ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_user_returns_true_on_insert():
    coll = _new_collector()
    coll.pool._conn.execute.return_value = "INSERT 0 1"
    assert await coll._enqueue_user("alice", priority=2) is True


@pytest.mark.asyncio
async def test_enqueue_user_returns_false_on_conflict():
    coll = _new_collector()
    coll.pool._conn.execute.return_value = "INSERT 0 0"
    assert await coll._enqueue_user("alice", priority=2) is False


@pytest.mark.asyncio
async def test_enqueue_user_swallows_db_errors():
    coll = _new_collector()
    coll.pool._conn.execute.side_effect = RuntimeError("db down")
    # Must not propagate — the queue is best-effort.
    assert await coll._enqueue_user("alice", 2) is False


# ── collect() error path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_swallows_per_target_exceptions(monkeypatch, caplog):
    coll = _new_collector()
    # Disable spider drain so we don't traverse the queue path.
    monkeypatch.setenv("GITHUB_SPIDER_ENABLED", "false")

    http = MagicMock()
    _patch_make_client(coll, http)

    # Force the underlying per-target call to blow up; collect() must still
    # return cleanly and route through send_to_dlq instead of raising.
    coll._collect_user = AsyncMock(side_effect=RuntimeError("kaboom"))
    coll._spider_depth = 0  # don't try social-graph after
    coll.send_to_dlq = AsyncMock()

    with caplog.at_level("ERROR", logger="src.collectors.github"):
        await coll.collect(["octocat"])

    coll.send_to_dlq.assert_awaited_once()
    assert any("Failed github/octocat" in r.getMessage() for r in caplog.records)


# ── EdgeFetcher ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edge_fetcher_yields_followers():
    coll = _new_collector()

    # Stub out _paginate so we don't go near httpx.
    async def fake_paginate(client, url, **kw):
        assert "/followers" in url
        return [{"login": "x"}, {"login": "y"}, {}]  # last is dropped

    coll._paginate = AsyncMock(side_effect=fake_paginate)

    # _make_client must yield *something*; the EdgeFetcher just needs the CM.
    http = MagicMock()
    _patch_make_client(coll, http)

    fetcher = coll.make_edge_fetcher()
    edges = []
    async for e in fetcher.fetch_edges("octocat", EdgeType.FOLLOWER):
        edges.append(e)

    assert [e.target for e in edges] == ["x", "y"]
    assert all(e.source == "octocat" for e in edges)


@pytest.mark.asyncio
async def test_edge_fetcher_rejects_unsupported_type():
    coll = _new_collector()
    fetcher = coll.make_edge_fetcher()
    with pytest.raises(NotImplementedError):
        async for _ in fetcher.fetch_edges("octocat", EdgeType.STAR):
            pass


# ── reconcile_avatars ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_avatars_no_pool_short_circuits():
    coll = GithubCollector()  # no set_pool → pool is None
    out = await coll.reconcile_avatars()
    assert out == {"total": 0, "missing": 0, "redownloaded": 0, "errors": 0}


@pytest.mark.asyncio
async def test_reconcile_avatars_counts_missing(tmp_path):
    coll = _new_collector()
    missing = tmp_path / "does_not_exist.jpg"
    coll.pool._conn.fetch.return_value = [
        {"entity_id": "1", "entity_name": "u1",
         "file_path": str(missing), "source_url": "u",
         "metadata": None},
    ]
    coll.track_avatar_changes = AsyncMock(return_value=(True, missing))
    stats = await coll.reconcile_avatars()
    assert stats["total"] == 1
    assert stats["missing"] == 1
    assert stats["redownloaded"] == 1


# ── cleanup ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_is_a_noop_returning_none():
    coll = _new_collector()
    out = await coll.cleanup()
    assert out is None


# ── module surface smoke ──────────────────────────────────────────────────


def test_module_exposes_main_classes():
    assert hasattr(github_mod, "GithubCollector")
    assert hasattr(github_mod, "GithubEdgeFetcher")
    assert github_mod.AVATAR_CDN_BASE.startswith("https://avatars.")
