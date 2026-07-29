"""Tests for src/collectors/strava.py — Wave 2 Phase 2 collector.

Pure-unit. httpx, the DB pool, and the photo tracker are all mocked; no
network and no database I/O. We exercise:

  * constructor + auth-mode flags (`_use_api`, `_use_web`)
  * account_media_dir shape
  * cookie-jar parsing helper (`_load_session_cookie_from_file`)
  * `_ensure_token` happy path against a stubbed token endpoint
  * activity / athlete upserts (DB write shape)
  * collect() dispatch table (me + API → authenticated, me + cookie →
    cookies path, etc.)
  * download_media (happy path + DLQ on httpx error)
  * download_route_maps (sidecar JSON)
  * collect_following_roster cookie gate
  * cleanup() (no-op contract)
  * set_pool() also propagates to ProfilePhotoTracker

Each test isolates the collector by monkeypatching environment vars
through the constructor, then patching `httpx.AsyncClient` at the module
level so we can assert request shape without opening sockets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collectors import strava as strava_mod
from src.collectors.strava import StravaCollector


# ── helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _disable_tier1_raw_archives(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "0")


def _set_api_env(monkeypatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "cid12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "csec")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "rtok")
    monkeypatch.delenv("STRAVA_SESSION_COOKIE", raising=False)
    # Disable cookie-file fallback by pointing at a path that won't exist.
    monkeypatch.setenv("STRAVA_COOKIES_FILE", "/nonexistent/strava_cookies.txt")
    # Zero out delays so tests don't actually sleep.
    monkeypatch.setenv("STRAVA_API_DELAY_MIN", "0")
    monkeypatch.setenv("STRAVA_API_DELAY_MAX", "0")
    monkeypatch.setenv("STRAVA_FEED_DELAY_MIN", "0")
    monkeypatch.setenv("STRAVA_FEED_DELAY_MAX", "0")


def _set_web_env(monkeypatch):
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("STRAVA_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("STRAVA_SESSION_COOKIE", "FAKE_SESSION_COOKIE_VALUE")
    monkeypatch.setenv("STRAVA_COOKIES_FILE", "/nonexistent/strava_cookies.txt")
    monkeypatch.setenv("STRAVA_API_DELAY_MIN", "0")
    monkeypatch.setenv("STRAVA_API_DELAY_MAX", "0")
    monkeypatch.setenv("STRAVA_FEED_DELAY_MIN", "0")
    monkeypatch.setenv("STRAVA_FEED_DELAY_MAX", "0")


def _make_pool():
    """Build an asyncpg-style pool whose acquire() yields an AsyncMock conn."""
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value={"id": "athlete-uuid"})
    conn.fetchval = AsyncMock(return_value=None)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    pool._conn = conn  # for assertions in tests
    return pool


def _stub_async_client(*, get_responses=None, post_responses=None):
    """Patchable factory that returns a context-manager httpx.AsyncClient
    whose .get/.post can be queued with a list of MagicMock responses
    (or a single response, broadcast for every call)."""
    client = MagicMock()

    def _consume(queue):
        if queue is None:
            return AsyncMock(return_value=MagicMock(status_code=200, json=MagicMock(return_value={}), text=""))
        if isinstance(queue, list):
            it = iter(queue)
            return AsyncMock(side_effect=lambda *a, **kw: next(it))
        return AsyncMock(return_value=queue)

    client.get = _consume(get_responses)
    client.post = _consume(post_responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── constructor + feature gate ─────────────────────────────────────────────


def test_constructor_no_creds_disables_both_modes(monkeypatch):
    for var in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                "STRAVA_REFRESH_TOKEN", "STRAVA_SESSION_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRAVA_COOKIES_FILE", "/nonexistent/strava_cookies.txt")

    coll = StravaCollector()
    assert coll._use_api is False
    assert coll._use_web is False
    assert coll.SOURCE_NAME == "strava"


def test_constructor_api_mode(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    assert coll._use_api is True
    assert coll._use_web is False


def test_constructor_web_mode(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    assert coll._use_api is False
    assert coll._use_web is True


def test_set_pool_propagates_to_photo_tracker(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    coll._photo_tracker = MagicMock()
    coll._photo_tracker.set_pool = MagicMock()

    pool = _make_pool()
    coll.set_pool(pool)

    assert coll.pool is pool
    coll._photo_tracker.set_pool.assert_called_once_with(pool)


def test_gps_stream_429_event_dedupes_same_activity(monkeypatch):
    _set_web_env(monkeypatch)
    monkeypatch.setattr(strava_mod, "_GPS_429_EVENT_DEDUPE_SECONDS", 9999.0)
    coll = StravaCollector()
    coll._note_rate_limit = MagicMock()

    coll._set_gps_stream_cooldown("123", "page")
    coll._set_gps_stream_cooldown("123", "page-fallback")
    coll._recent_gps_429s["123"] -= 10000.0
    coll._set_gps_stream_cooldown("123", "page")

    assert coll._note_rate_limit.call_count == 2


def test_strava_429_sleeps_use_jittered_helper():
    source = Path(strava_mod.__file__).read_text(encoding="utf-8")

    assert "await asyncio.sleep(self._ratelimit_sleep)" not in source
    assert "await asyncio.sleep(_heavy)" not in source


def test_extract_strava_profile_from_server_html():
    html = """
    <html>
      <head>
        <title>Wrong Nav User | Strava Runner Profile</title>
        <meta content='https://dgalywyr863hv.cloudfront.net/pictures/athletes/42/avatar/full.jpg' property='og:image'>
        <meta content='Alice Runner | Strava Runner Profile' property='og:title'>
        <meta content='Alice Runner is a runner from Singapore, Singapore. Join Strava to track your activities.' property='og:description'>
      </head>
      <body>
        <div class='page container'><div id='athlete-profile'>
          <div class='row profile-heading'>
            <img alt='' class='avatar-img' src='https://example.com/alice-large.jpg'>
            <h1 class='text-title1 athlete-name'>Alice Runner</h1>
          </div>
          <div class='section connections'>
            <ul class='inline-stats'>
              <li><span class='label static-label'>Following</span> <a href='/x'>1,234</a></li>
              <li><span class='label static-label'>Followers</span> <strong>56</strong></li>
            </ul>
          </div>
        </div></div>
      </body>
    </html>
    """

    out = strava_mod._extract_strava_profile_from_html(html, "42")

    assert out["id"] == "42"
    assert out["username"] == "Alice Runner"
    assert out["firstname"] == "Alice"
    assert out["lastname"] == "Runner"
    assert out["profile"] == "https://dgalywyr863hv.cloudfront.net/pictures/athletes/42/avatar/full.jpg"
    assert out["following_count"] == 1234
    assert out["follower_count"] == 56
    assert out["city"] == "Singapore"
    assert out["country"] == "Singapore"


@pytest.mark.asyncio
async def test_note_rate_limit_can_record_transient_without_cooldown(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    coll.pool = object()
    record_event = AsyncMock()
    monkeypatch.setattr(strava_mod, "record_rate_limit_event", record_event)

    coll._note_rate_limit(
        scope="gps_streams",
        account="acct1",
        cooldown_seconds=None,
        cooldown_active=False,
        reason="streams 429 recovered by retry",
    )
    await asyncio.sleep(0)

    record_event.assert_awaited_once()
    assert record_event.await_args.kwargs["cooldown_seconds"] is None


@pytest.mark.asyncio
async def test_fetch_streams_retries_web_429_before_gps_cooldown(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    coll._cookie_accounts = [("acct1", "FAKE_SESSION_COOKIE_VALUE")]
    coll._use_web = True
    coll._note_rate_limit = MagicMock()
    monkeypatch.setattr(
        strava_mod,
        "sleep_before_pre_cooldown_retry",
        AsyncMock(return_value=0.0),
    )

    first = MagicMock(status_code=429)
    second = MagicMock(status_code=200)
    second.json.return_value = {
        "latlng": [[1.0, 2.0], [1.1, 2.1]],
        "time": [0, 60],
        "altitude": [10, 11],
    }
    fake_client = _stub_async_client(get_responses=[first, second])
    monkeypatch.setattr(strava_mod.httpx, "AsyncClient", MagicMock(return_value=fake_client))

    latlng, times, altitude = await coll._fetch_streams(MagicMock(), "123")

    assert latlng == [[1.0, 2.0], [1.1, 2.1]]
    assert times == [0, 60]
    assert altitude == [10, 11]
    assert fake_client.get.await_count == 2
    assert coll._gps_stream_cooling_down() is False
    coll._note_rate_limit.assert_called_once()
    assert coll._note_rate_limit.call_args.kwargs["cooldown_active"] is False


@pytest.mark.asyncio
async def test_fetch_streams_archives_web_raw_payload(monkeypatch):
    _set_web_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    coll = StravaCollector()
    coll._cookie_accounts = [("acct1", "FAKE_SESSION_COOKIE_VALUE")]
    coll._use_web = True
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return MagicMock(ok=True)

    monkeypatch.setattr(strava_mod, "write_raw_payload", fake_write_raw_payload)
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "latlng": [[1.0, 2.0], [1.1, 2.1]],
        "time": [0, 60],
        "altitude": [10, 11],
    }
    fake_client = _stub_async_client(get_responses=[response])
    monkeypatch.setattr(strava_mod.httpx, "AsyncClient", MagicMock(return_value=fake_client))

    latlng, _times, _altitude = await coll._fetch_streams(MagicMock(), "123")

    assert latlng == [[1.0, 2.0], [1.1, 2.1]]
    assert calls
    assert calls[0]["artifact_id"] == "gps_streams/web/acct1/123"
    assert calls[0]["target_tables"] == ["strava_gps_streams", "strava_activities"]
    assert calls[0]["metadata"]["collection_account"] == "acct1"


@pytest.mark.asyncio
async def test_fetch_streams_sets_gps_cooldown_after_retry_still_429(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    coll._cookie_accounts = [("acct1", "FAKE_SESSION_COOKIE_VALUE")]
    coll._use_web = True
    coll._note_rate_limit = MagicMock()
    monkeypatch.setattr(
        strava_mod,
        "sleep_before_pre_cooldown_retry",
        AsyncMock(return_value=0.0),
    )

    fake_client = _stub_async_client(
        get_responses=[MagicMock(status_code=429), MagicMock(status_code=429)]
    )
    monkeypatch.setattr(strava_mod.httpx, "AsyncClient", MagicMock(return_value=fake_client))

    latlng, times, altitude = await coll._fetch_streams(MagicMock(), "123")

    assert (latlng, times, altitude) == (None, None, None)
    assert fake_client.get.await_count == 2
    assert coll._gps_stream_cooling_down() is True
    coll._note_rate_limit.assert_called_once()
    assert coll._note_rate_limit.call_args.kwargs["cooldown_seconds"] == coll._gps_stream_cooldown_seconds


@pytest.mark.asyncio
async def test_sync_persisted_gps_stream_cooldown_restores_after_restart(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    pool._conn.fetchrow = AsyncMock(return_value={
        "created_at": datetime.now(timezone.utc),
        "cooldown_seconds": 1800,
        "reason": "streams 429 for 123 via web:bryanseah234",
    })
    coll.set_pool(pool)

    restored = await coll._sync_persisted_gps_stream_cooldown()

    assert restored is True
    assert coll._gps_stream_cooling_down() is True
    pool._conn.fetchrow.assert_awaited_once()
    assert "browser_strava_streams" in pool._conn.fetchrow.await_args.args[0]


def test_is_truncated_accepts_stored_latlng_strings():
    assert strava_mod._is_truncated("1.300000,103.800000", [1.300001, 103.800001]) is False
    assert strava_mod._is_truncated("1.300000,103.800000", [1.310000, 103.810000]) is True


def test_derive_gps_route_fields_from_existing_stream_json():
    fields = strava_mod._derive_gps_route_fields(
        None,
        None,
        json.dumps([[1.37507, 103.750999], [1.376439, 103.75308]]),
    )

    assert fields["stream_status"] == "ok"
    assert fields["start_latlng"] == "1.37507,103.750999"
    assert fields["end_latlng"] == "1.376439,103.75308"
    assert fields["privacy_zone_start"] is True
    assert fields["privacy_zone_end"] is True
    assert fields["summary_polyline"]
    assert fields["point_count"] == 2


@pytest.mark.asyncio
async def test_repair_existing_gps_stream_routes_backfills_activity_row(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    pool._conn.fetch = AsyncMock(return_value=[
        {
            "id": "activity-uuid",
            "platform_activity_id": 19283135496,
            "start_latlng": None,
            "end_latlng": None,
            "summary_polyline": None,
            "stream_status": None,
            "privacy_zone_start": None,
            "privacy_zone_end": None,
            "latlng": json.dumps([[1.37507, 103.750999], [1.376439, 103.75308]]),
        }
    ])
    coll.set_pool(pool)

    repaired = await coll._repair_existing_gps_stream_routes(batch_size=10)

    assert repaired == 1
    pool._conn.fetch.assert_awaited_once()
    fetch_sql = pool._conn.fetch.await_args.args[0]
    assert "jsonb_array_length" not in fetch_sql
    assert "ORDER BY a.start_date DESC NULLS LAST" in fetch_sql
    pool._conn.execute.assert_awaited_once()
    args = pool._conn.execute.await_args.args
    assert "summary_polyline = COALESCE" in args[0]
    assert args[1] == "1.37507,103.750999"
    assert args[2] == "1.376439,103.75308"
    assert args[3] == "ok"
    assert args[8]
    assert args[9] == "activity-uuid"


# ── account_media_dir ──────────────────────────────────────────────────────


def test_account_media_dir_uses_first_8_of_client_id(monkeypatch, tmp_path):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    # drive_check imports DRIVE_PATH at module load; force re-eval by
    # rebinding here.
    import importlib
    import src.core.drive_check as dc
    importlib.reload(dc)
    import src.core.base_collector as bc
    importlib.reload(bc)
    importlib.reload(strava_mod)

    coll = strava_mod.StravaCollector()
    p = coll.account_media_dir
    assert p.exists()
    # client_id = "cid12345"; first 8 chars sanitized
    assert "account_cid12345" in str(p)


def test_account_media_dir_no_creds_uses_web(monkeypatch, tmp_path):
    for var in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                "STRAVA_REFRESH_TOKEN", "STRAVA_SESSION_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRAVA_COOKIES_FILE", "/nonexistent/strava_cookies.txt")
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    import importlib
    import src.core.drive_check as dc
    importlib.reload(dc)
    import src.core.base_collector as bc
    importlib.reload(bc)
    importlib.reload(strava_mod)

    coll = strava_mod.StravaCollector()
    p = coll.account_media_dir
    assert p.exists()
    assert "account_web" in str(p)


# ── cookie file loader ─────────────────────────────────────────────────────


def test_load_session_cookie_from_missing_file_returns_empty(tmp_path):
    out = StravaCollector._load_session_cookie_from_file(str(tmp_path / "nope.txt"))
    assert out == ""


def test_load_session_cookie_from_jar(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        ".strava.com\tTRUE\t/\tTRUE\t9999999999\t_strava4_session\tABCDEF12345\n"
        ".strava.com\tTRUE\t/\tFALSE\t9999999999\tother\tnope\n",
        encoding="utf-8",
    )
    out = StravaCollector._load_session_cookie_from_file(str(jar))
    assert out == "ABCDEF12345"


def test_load_session_cookie_jar_without_strava_cookie(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        ".other.com\tTRUE\t/\tFALSE\t9999999999\tfoo\tbar\n",
        encoding="utf-8",
    )
    out = StravaCollector._load_session_cookie_from_file(str(jar))
    assert out == ""


# ── _ensure_token ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_token_fetches_and_caches(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()

    token_resp = MagicMock()
    token_resp.json = MagicMock(return_value={"access_token": "AT-NEW", "refresh_token": "RT-NEW"})
    token_resp.raise_for_status = MagicMock()

    client = _stub_async_client(post_responses=token_resp)
    with patch.object(strava_mod.httpx, "AsyncClient", return_value=client):
        await coll._ensure_token()

    assert coll._access_token == "AT-NEW"
    assert coll._refresh_token == "RT-NEW"
    client.post.assert_awaited_once()
    # Second call is a no-op (already cached).
    client.post.reset_mock()
    await coll._ensure_token()
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_token_noop_when_no_api(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    # Should return immediately without any client call.
    with patch.object(strava_mod.httpx, "AsyncClient") as client_cls:
        await coll._ensure_token()
        client_cls.assert_not_called()
    assert coll._access_token == ""


# ── DB upserts ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_athlete_writes_expected_columns(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)

    athlete = {
        "id": 42, "username": "alice", "firstname": "A", "lastname": "L",
        "profile": "https://e.com/p.jpg", "city": "SF", "state": "CA",
        "country": "US", "sex": "F", "follower_count": 7, "friend_count": 3,
    }
    await coll._upsert_athlete(athlete)

    pool._conn.execute.assert_awaited_once()
    args = pool._conn.execute.await_args.args
    sql = args[0]
    assert "INSERT INTO strava_athletes" in sql
    # positional args after sql: id, username, firstname, lastname, profile, ...
    assert args[1] == 42
    assert args[2] == "alice"
    assert args[5] == "https://e.com/p.jpg"
    assert args[10] == 7
    assert args[11] == 3


@pytest.mark.asyncio
async def test_upsert_athlete_does_not_write_fake_zero_counts_or_placeholders(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)

    await coll._upsert_athlete({"id": 42, "username": "athlete_42"})

    args = pool._conn.execute.await_args.args
    sql = args[0]
    assert "firstname      = COALESCE(EXCLUDED.firstname" in sql
    assert "following_count= COALESCE(EXCLUDED.following_count" in sql
    assert args[2] is None
    assert args[10] is None
    assert args[11] is None


@pytest.mark.asyncio
async def test_upsert_activity_resolves_athlete_uuid(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)

    activity = {
        "id": 999, "name": "Run", "type": "Run", "sport_type": "Run",
        "distance": 5000.0, "moving_time": 1800, "elapsed_time": 1900,
        "total_elevation_gain": 50.0, "average_speed": 2.7, "max_speed": 4.0,
        "average_heartrate": 140, "calories": 300,
        "start_date": "2026-04-15T08:00:00Z",
    }
    await coll._upsert_activity(activity, "42")

    pool._conn.fetchrow.assert_awaited_once()
    pool._conn.execute.assert_awaited_once()
    sql = pool._conn.execute.await_args.args[0]
    assert "INSERT INTO strava_activities" in sql


@pytest.mark.asyncio
async def test_upsert_activity_archives_raw_payload(monkeypatch):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_TIER1_RAW_PAYLOADS_ENABLED", "1")
    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    calls = []

    def fake_write_raw_payload(**kwargs):
        calls.append(kwargs)
        return MagicMock(ok=True)

    monkeypatch.setattr(strava_mod, "write_raw_payload", fake_write_raw_payload)
    activity = {
        "id": 999,
        "name": "Run",
        "type": "Run",
        "sport_type": "Run",
        "start_date": "2026-04-15T08:00:00Z",
    }

    await coll._upsert_activity(activity, "42")

    assert calls
    assert calls[0]["source"] == "strava"
    assert calls[0]["artifact_id"] == "activities/999"
    assert calls[0]["target_tables"] == ["strava_activities"]
    assert calls[0]["metadata"]["platform_athlete_id"] == "42"


# ── collect dispatch ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_no_auth_skips_target(monkeypatch, caplog):
    for var in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                "STRAVA_REFRESH_TOKEN", "STRAVA_SESSION_COOKIE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRAVA_COOKIES_FILE", "/nonexistent/strava_cookies.txt")
    monkeypatch.setenv("STRAVA_SPIDER_ENABLED", "false")
    monkeypatch.setenv("STRAVA_FOLLOW_SCRAPE_ENABLED", "false")

    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.checkpoint.save_progress = AsyncMock()

    with caplog.at_level("WARNING", logger="src.collectors.strava"):
        await coll.collect(["12345"])

    coll.checkpoint.save_progress.assert_awaited_once_with("12345")
    assert any("no auth available" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_dispatches_me_to_authenticated_path(monkeypatch):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("STRAVA_SPIDER_ENABLED", "false")
    monkeypatch.setenv("STRAVA_FOLLOW_SCRAPE_ENABLED", "false")

    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.checkpoint.save_progress = AsyncMock()
    coll._ensure_token = AsyncMock()
    coll._collect_authenticated_athlete = AsyncMock()

    await coll.collect(["me"])

    coll._collect_authenticated_athlete.assert_awaited_once()
    coll.checkpoint.save_progress.assert_awaited_once_with("me")


@pytest.mark.asyncio
async def test_collect_handles_per_target_exception_and_dlq(monkeypatch):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("STRAVA_SPIDER_ENABLED", "false")
    monkeypatch.setenv("STRAVA_FOLLOW_SCRAPE_ENABLED", "false")

    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll._ensure_token = AsyncMock()
    coll._collect_athlete = AsyncMock(side_effect=RuntimeError("boom"))
    coll.send_to_dlq = AsyncMock()
    coll.checkpoint.save_progress = AsyncMock()

    await coll.collect(["12345"])

    coll.send_to_dlq.assert_awaited_once()
    args = coll.send_to_dlq.await_args.args
    assert args[0] == "12345" and "boom" in args[2]


# ── download_media ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_media_happy_path(monkeypatch, tmp_path):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    monkeypatch.setenv("COLLECTOR_VAULT_ROOT", str(tmp_path))
    import importlib
    import src.core.drive_check as dc
    importlib.reload(dc)
    import src.core.base_collector as bc
    importlib.reload(bc)
    import src.core.vault as vault_mod
    importlib.reload(vault_mod)
    importlib.reload(strava_mod)

    coll = strava_mod.StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.insert_media_item = AsyncMock(return_value=True)
    coll.save_json = MagicMock(return_value=tmp_path / "metadata.json")

    img_resp = MagicMock(status_code=200, content=b"\xff\xd8\xff\xe0fakejpg")
    img_resp.raise_for_status = MagicMock()
    client = _stub_async_client(get_responses=img_resp)

    with patch.object(strava_mod.httpx, "AsyncClient", return_value=client), patch.dict(
        coll.download_media.__func__.__globals__,
        {"assert_media_write_allowed": lambda *args, **kwargs: None},
    ):
        await coll.download_media({
            "entity_id": "42", "entity_name": "alice",
            "content_type": "activity", "content_id": "ACT_1",
            "url": "https://example.com/p.jpg", "extension": "jpg",
            "raw": {"id": 1},
        })

    coll.insert_media_item.assert_awaited_once()
    kwargs = coll.insert_media_item.await_args.kwargs
    digest = hashlib.sha256(b"\xff\xd8\xff\xe0fakejpg").hexdigest()
    stored_path = Path(kwargs["file_path"])
    assert kwargs["entity_id"] == "42"
    assert kwargs["content_id"] == "ACT_1"
    assert stored_path == tmp_path / "media" / "blobs" / digest[:2] / digest[2:4] / f"{digest}.jpg"
    assert stored_path.read_bytes() == b"\xff\xd8\xff\xe0fakejpg"
    assert kwargs["sha256"] == digest
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True
    assert "ACT_1" in coll._known_ids


@pytest.mark.asyncio
async def test_download_media_skips_known(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    coll._known_ids.add("ACT_DUP")
    coll.insert_media_item = AsyncMock()

    with patch.object(strava_mod.httpx, "AsyncClient") as client_cls:
        await coll.download_media({
            "entity_id": "42", "entity_name": "alice",
            "content_type": "activity", "content_id": "ACT_DUP",
            "url": "https://example.com/p.jpg",
        })
        client_cls.assert_not_called()
    coll.insert_media_item.assert_not_called()


@pytest.mark.asyncio
async def test_download_media_error_routes_to_dlq(monkeypatch, tmp_path):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    monkeypatch.setenv("COLLECTOR_VAULT_ROOT", str(tmp_path))
    import importlib
    import src.core.drive_check as dc
    importlib.reload(dc)
    import src.core.base_collector as bc
    importlib.reload(bc)
    import src.core.vault as vault_mod
    importlib.reload(vault_mod)
    importlib.reload(strava_mod)

    coll = strava_mod.StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.send_to_dlq = AsyncMock()

    bad = MagicMock(status_code=500)
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("boom", request=None, response=bad))
    client = _stub_async_client(get_responses=bad)

    with patch.object(strava_mod.httpx, "AsyncClient", return_value=client), patch.dict(
        coll.download_media.__func__.__globals__,
        {"assert_media_write_allowed": lambda *args, **kwargs: None},
    ):
        await coll.download_media({
            "entity_id": "42", "entity_name": "alice",
            "content_type": "activity", "content_id": "ACT_FAIL",
            "url": "https://example.com/p.jpg",
        })

    coll.send_to_dlq.assert_awaited_once()


# ── download_route_maps ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_route_maps_writes_sidecar(monkeypatch, tmp_path):
    _set_api_env(monkeypatch)
    monkeypatch.setenv("COLLECTOR_DRIVE_PATH", str(tmp_path))
    monkeypatch.setenv("COLLECTOR_VAULT_ROOT", str(tmp_path))
    import importlib
    import src.core.drive_check as dc
    importlib.reload(dc)
    import src.core.base_collector as bc
    importlib.reload(bc)
    import src.core.vault as vault_mod
    importlib.reload(vault_mod)
    importlib.reload(strava_mod)

    coll = strava_mod.StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)
    coll.insert_media_item = AsyncMock(return_value=True)

    activity = {
        "id": 12345,
        "name": "Morning Run",
        "distance": 5000.0,
        "map": {"summary_polyline": "abc_FAKE_POLY", "bounds": [[1, 2], [3, 4]]},
        "start_latlng": [1.0, 2.0], "end_latlng": [3.0, 4.0],
    }
    with patch.dict(
        coll.download_route_maps.__func__.__globals__,
        {"assert_media_write_allowed": lambda *args, **kwargs: None},
    ):
        await coll.download_route_maps(activity, athlete_id="42")

    coll.insert_media_item.assert_awaited_once()
    kwargs = coll.insert_media_item.await_args.kwargs
    assert kwargs["content_type"] == "route_map"
    assert kwargs["content_id"] == "12345"
    stored_path = Path(kwargs["file_path"])
    assert stored_path.exists()
    assert stored_path.suffix == ".json"
    assert "media" in stored_path.parts and "blobs" in stored_path.parts
    payload = json.loads(stored_path.read_text(encoding="utf-8"))
    assert payload["polyline"] == "abc_FAKE_POLY"
    assert kwargs["metadata"]["vault_artifact"]["ok"] is True


@pytest.mark.asyncio
async def test_download_route_maps_skip_no_polyline(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    coll.insert_media_item = AsyncMock()
    await coll.download_route_maps({"id": 1, "map": {}}, athlete_id="x")
    coll.insert_media_item.assert_not_called()


# ── collect_clubs ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_clubs_skips_when_no_api(monkeypatch, caplog):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    with caplog.at_level("INFO", logger="src.collectors.strava"):
        await coll.collect_clubs("42")
    assert any("requires API auth" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_clubs_archives_raw_payload(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    coll._ensure_token = AsyncMock()
    coll._access_token = "access-token"
    response = MagicMock(
        status_code=200,
        json=MagicMock(return_value=[{"id": 1, "name": "Run Club"}]),
    )
    client = _stub_async_client(get_responses=response)
    seen: dict[str, object] = {}

    def fake_write_raw_payload(**kwargs):
        seen.update(kwargs)
        return MagicMock(ok=True, error=None)

    monkeypatch.setattr(strava_mod.httpx, "AsyncClient", MagicMock(return_value=client))
    monkeypatch.setattr(strava_mod, "write_raw_payload", fake_write_raw_payload)

    await coll.collect_clubs("42")

    coll._ensure_token.assert_awaited_once()
    client.get.assert_awaited_once()
    assert seen["source"] == "strava"
    assert seen["artifact_id"] == "clubs/42"
    assert seen["target_tables"] == ["strava_athletes"]
    assert seen["payload"]["clubs"] == [{"id": 1, "name": "Run Club"}]
    assert seen["metadata"]["payload_type"] == "strava_clubs"


# ── collect_following_roster ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_following_roster_no_web_returns_zero(monkeypatch):
    _set_api_env(monkeypatch)  # API only, no web
    coll = StravaCollector()
    out = await coll.collect_following_roster("42")
    assert out == 0


# ── cleanup ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_is_a_noop(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    # Must not raise; returns None.
    assert await coll.cleanup() is None


# ── feed playback ──────────────────────────────────────────────────────────


def test_day_bounds_returns_utc_seconds():
    start, end = StravaCollector._day_bounds("2026-04-15")
    # 2026-04-15 00:00:00 UTC
    expected = int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp())
    assert start == expected
    assert end == start + 86399


def test_normalize_feed_activity_picks_fields_from_nested_activity():
    raw = {
        "activity": {
            "id": 12345,
            "name": "Morning Run",
            "type": "Run",
            "sport_type": "Run",
            "distance": 5000.0,
            "elapsed_time": 1900,
            "moving_time": 1800,
            "start_date": "2026-04-15T08:00:00Z",
            "athlete": {"id": 42, "username": "alice"},
        }
    }
    norm = StravaCollector._normalize_feed_activity(raw)
    assert norm is not None
    assert norm["id"] == 12345
    assert norm["name"] == "Morning Run"
    assert norm["start_date"] == "2026-04-15T08:00:00Z"
    assert norm["_athlete_id"] == 42
    assert norm["_source"] == "following_feed"


def test_normalize_feed_activity_returns_none_when_no_id():
    assert StravaCollector._normalize_feed_activity({}) is None
    assert StravaCollector._normalize_feed_activity({"activity": {"name": "x"}}) is None


@pytest.mark.asyncio
async def test_fetch_feed_for_date_no_web_returns_empty(monkeypatch):
    _set_api_env(monkeypatch)  # API only, no cookies
    coll = StravaCollector()
    out = await coll.fetch_feed_for_date(42, "2026-04-15")
    assert out == []


@pytest.mark.asyncio
async def test_fetch_feed_for_date_filters_to_day_window_and_upserts(monkeypatch):
    _set_web_env(monkeypatch)
    monkeypatch.setenv("STRAVA_FEED_MAX_PAGES", "2")
    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)

    # Build one feed page with 3 activities: one in the day, one too old,
    # one too new. Server payload format is a JSON list at top level.
    page_payload = [
        {"activity": {"id": 100, "name": "OnDay", "type": "Run",
                      "start_date": "2026-04-15T10:00:00Z",
                      "athlete": {"id": 42, "username": "alice"}}},
        {"activity": {"id": 200, "name": "TooOld", "type": "Run",
                      "start_date": "2026-04-14T10:00:00Z",
                      "athlete": {"id": 42, "username": "alice"}}},
        {"activity": {"id": 300, "name": "TooNew", "type": "Run",
                      "start_date": "2026-04-16T10:00:00Z",
                      "athlete": {"id": 42, "username": "alice"}}},
    ]
    resp = MagicMock(status_code=200, json=MagicMock(return_value=page_payload),
                     headers={"content-type": "application/json"})
    client = _stub_async_client(get_responses=resp)
    with patch.object(strava_mod.httpx, "AsyncClient", return_value=client):
        out = await coll.fetch_feed_for_date(42, "2026-04-15")

    assert len(out) == 1
    assert out[0]["id"] == 100
    # Pool was used to upsert athlete + activity. _conn.execute called at
    # least twice (athlete insert + activity insert).
    assert pool._conn.execute.await_count >= 2


@pytest.mark.asyncio
async def test_fetch_feed_for_date_stops_on_non_200(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    coll.set_pool(pool)

    resp = MagicMock(status_code=403, json=MagicMock(return_value={}),
                     headers={"content-type": "text/html"})
    client = _stub_async_client(get_responses=resp)
    with patch.object(strava_mod.httpx, "AsyncClient", return_value=client):
        out = await coll.fetch_feed_for_date(42, "2026-04-15")

    assert out == []


@pytest.mark.asyncio
async def test_backfill_feed_history_no_web_returns_zero(monkeypatch):
    _set_api_env(monkeypatch)
    coll = StravaCollector()
    out = await coll.backfill_feed_history(42, days_back=7)
    assert out == 0


@pytest.mark.asyncio
async def test_backfill_feed_history_walks_n_days_and_skips_covered(monkeypatch):
    _set_web_env(monkeypatch)
    coll = StravaCollector()
    pool = _make_pool()
    # First day fetchval returns True (covered) → skip; remaining return None.
    pool._conn.fetchval = AsyncMock(side_effect=[True, None, None])
    coll.set_pool(pool)

    fetched_dates: list[str] = []

    async def fake_fetch(athlete_id, date_string):
        fetched_dates.append(date_string)
        return [{"id": 1}]  # one fake activity

    coll.fetch_feed_for_date = fake_fetch
    total = await coll.backfill_feed_history(42, days_back=3)
    # First day was covered → skipped. Remaining 2 days each return 1 activity.
    assert total == 2
    assert len(fetched_dates) == 2
