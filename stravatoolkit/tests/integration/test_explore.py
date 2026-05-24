"""Tests for the explore scraper — extraction, DB queries, and spider logic."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from ingestion import db
from ingestion.core.scrapers.explore_scraper import (
    ExploreResult,
    _extract_athlete_ids,
    _extract_segment_ids,
    _get_spider_seeds,
    _insert_stub,
    _is_known,
    run_explore_scraper,
)
from ingestion.db.queries.explore import (
    list_explore_segments,
    list_explore_stubs,
    promote_explore_athletes,
    save_explore_segment,
)


# ── HTML extraction ───────────────────────────────────────────────────────────

def test_extract_athlete_ids_from_anchor_tags():
    html = """
    <html><body>
      <a href="/athletes/111">Alice</a>
      <a href="/athletes/222/followers">Bob</a>
      <a href="/clubs/5">Club</a>
      <a href="/activities/999">Activity</a>
    </body></html>
    """
    ids = _extract_athlete_ids(html)
    assert ids == {111, 222}


def test_extract_athlete_ids_empty_page():
    assert _extract_athlete_ids("") == set()
    assert _extract_athlete_ids("<html><body>No links here</body></html>") == set()


def test_extract_athlete_ids_deduplicates():
    html = '<a href="/athletes/42">A</a><a href="/athletes/42">B</a><a href="/athletes/99">C</a>'
    ids = _extract_athlete_ids(html)
    assert ids == {42, 99}


def test_extract_segment_ids():
    html = '<a href="/segments/1001">Seg A</a><a href="/segments/2002">Seg B</a><a href="/athletes/5">Runner</a>'
    assert _extract_segment_ids(html) == {1001, 2002}


def test_extract_segment_ids_empty():
    assert _extract_segment_ids("<html>no segments</html>") == set()


# ── DB queries ────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test_explore.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


def test_save_and_list_explore_segments(conn):
    save_explore_segment(conn, 1001, "Run")
    save_explore_segment(conn, 2002, "Ride")
    save_explore_segment(conn, 1001, "Run")  # duplicate — should be ignored
    segments = list_explore_segments(conn)
    assert set(segments) == {1001, 2002}


def test_list_explore_segments_respects_limit(conn):
    for i in range(20):
        save_explore_segment(conn, 1000 + i)
    segments = list_explore_segments(conn, limit=5)
    assert len(segments) == 5


def test_list_explore_stubs_returns_untracked(conn):
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO athletes (athlete_id, name, is_private, is_following, is_tracked, first_seen_source, first_seen_at, last_seen_at, backfill_status) VALUES (?,?,0,0,0,'explore',?,?,'pending')",
        (101, "athlete_101", now, now),
    )
    conn.execute(
        "INSERT INTO athletes (athlete_id, name, is_private, is_following, is_tracked, first_seen_source, first_seen_at, last_seen_at, backfill_status) VALUES (?,?,0,0,0,'spider',?,?,'pending')",
        (102, "athlete_102", now, now),
    )
    # tracked athlete — should not appear
    db.upsert_athlete(conn, athlete_id=200, name="Tracked", source="following_roster", is_following=True, is_tracked=True)
    stubs = list_explore_stubs(conn)
    stub_ids = {s["athlete_id"] for s in stubs}
    assert stub_ids == {101, 102}


def test_promote_explore_athletes(conn):
    now = "2026-01-01T00:00:00+00:00"
    for aid in (101, 102, 103):
        conn.execute(
            "INSERT INTO athletes (athlete_id, name, is_private, is_following, is_tracked, first_seen_source, first_seen_at, last_seen_at, backfill_status) VALUES (?,?,0,0,0,'explore',?,?,'pending')",
            (aid, f"athlete_{aid}", now, now),
        )
    promoted = promote_explore_athletes(conn, [101, 103])
    assert promoted == 2
    rows = conn.execute("SELECT athlete_id, is_tracked FROM athletes ORDER BY athlete_id").fetchall()
    tracked = {r["athlete_id"] for r in rows if r["is_tracked"]}
    assert tracked == {101, 103}
    assert 102 not in tracked


def test_promote_empty_list_is_noop(conn):
    assert promote_explore_athletes(conn, []) == 0


def test_insert_stub_new_athlete(conn):
    now = "2026-01-01T00:00:00+00:00"
    added = _insert_stub(conn, 999, "explore", now)
    assert added is True
    row = conn.execute("SELECT * FROM athletes WHERE athlete_id = 999").fetchone()
    assert row is not None
    assert row["is_tracked"] == 0
    assert row["first_seen_source"] == "explore"


def test_insert_stub_existing_athlete_returns_false(conn):
    db.upsert_athlete(conn, athlete_id=888, name="Existing", source="following_roster", is_following=True, is_tracked=True)
    now = "2026-01-01T00:00:00+00:00"
    added = _insert_stub(conn, 888, "explore", now)
    assert added is False


def test_is_known(conn):
    db.upsert_athlete(conn, athlete_id=1, name="Known", source="following_roster", is_following=True, is_tracked=True)
    assert _is_known(conn, 1) is True
    assert _is_known(conn, 9999) is False


def test_get_spider_seeds_returns_recently_active(conn, tmp_path):
    """Seeds should only come from tracked+following athletes with activities."""
    from ingestion.db import save_activity
    db.upsert_athlete(conn, athlete_id=10, name="Active", source="following_roster", is_following=True, is_tracked=True)
    db.upsert_athlete(conn, athlete_id=20, name="Inactive", source="following_roster", is_following=True, is_tracked=True)
    activity = {
        "activity_id": 500,
        "athlete_id": 10,
        "athlete_name": "Active",
        "sport_type": "Run",
        "start_date_utc": "2026-05-01T04:00:00Z",
        "start_date_local": "2026-05-01T12:00:00+08:00",
        "source": "following_feed",
        "is_following": True,
    }
    transformed = {
        "stream_status": "incomplete",
        "privacy_zone_start": False,
        "privacy_zone_end": False,
        "truncation_point_start": None,
        "truncation_point_end": None,
        "path": [],
    }
    save_activity(conn, activity, transformed)
    seeds = _get_spider_seeds(conn, limit=10)
    assert 10 in seeds
    assert 20 not in seeds  # no activities


# ── Integration: run_explore_scraper with mocked session ──────────────────────

def _make_mock_session(pages: dict[str, str]) -> MagicMock:
    """Return a mock StravaSession where get_text returns canned HTML per path."""
    session = MagicMock()
    def _get_text(path, **kwargs):
        html = pages.get(path, "")
        return MagicMock(), html
    session.get_text.side_effect = _get_text
    return session


@pytest.fixture()
def no_network(monkeypatch):
    """Suppress real network calls inside the explore scraper for unit tests.

    Patches both the reference in explore_scraper AND the underlying socket
    check in delays, because Windows can ignore socket.setdefaulttimeout()
    for TCP SYN packets hitting DROP firewall rules (causing 20+ second hangs).
    """
    _instant_true = lambda *args, **kwargs: True
    monkeypatch.setattr("ingestion.core.delays._is_internet_available", _instant_true)
    monkeypatch.setattr("ingestion.core.delays.wait_for_internet", _instant_true)
    monkeypatch.setattr("ingestion.core.scrapers.explore_scraper.wait_for_internet", _instant_true)
    monkeypatch.setattr("ingestion.core.scrapers.explore_scraper.AdaptiveRateLimiter", MagicMock)


def test_run_explore_scraper_adds_new_stubs(conn, no_network):
    html_explore = """
    <html><body>
      <a href="/athletes/111">Runner A</a>
      <a href="/athletes/222">Runner B</a>
      <a href="/segments/5001">Hill Climb</a>
    </body></html>
    """
    pages = {
        "/explore/activities": html_explore,
        "/explore/running": "",
        "/explore/cycling": "",
    }
    session = _make_mock_session(pages)
    shutdown = Event()

    result = run_explore_scraper(session, conn, shutdown, spider=False)

    assert result.added == 2
    assert result.explore_page_ids >= 2
    stubs = list_explore_stubs(conn)
    stub_ids = {s["athlete_id"] for s in stubs}
    assert {111, 222}.issubset(stub_ids)
    segs = list_explore_segments(conn)
    assert 5001 in segs


def test_run_explore_scraper_does_not_duplicate_known_athletes(conn, no_network):
    db.upsert_athlete(conn, athlete_id=111, name="Known", source="following_roster", is_following=True, is_tracked=True)
    html = '<a href="/athletes/111">Known</a><a href="/athletes/333">New</a>'
    pages = {p: html for p in ("/explore/activities", "/explore/running", "/explore/cycling")}
    session = _make_mock_session(pages)
    shutdown = Event()

    result = run_explore_scraper(session, conn, shutdown, spider=False)

    assert result.added == 1  # only 333 is new
    stubs = list_explore_stubs(conn)
    assert all(s["athlete_id"] != 111 for s in stubs)


def test_run_explore_scraper_spider_follows_links(conn, no_network):
    """Spider visits seed athlete profiles at depth 0 and follows links at depth 1."""
    from ingestion.db import save_activity
    db.upsert_athlete(conn, athlete_id=10, name="Seed", source="following_roster", is_following=True, is_tracked=True)
    activity = {
        "activity_id": 600,
        "athlete_id": 10,
        "athlete_name": "Seed",
        "sport_type": "Run",
        "start_date_utc": "2026-05-01T04:00:00Z",
        "start_date_local": "2026-05-01T12:00:00+08:00",
        "source": "following_feed",
        "is_following": True,
    }
    transformed = {
        "stream_status": "incomplete",
        "privacy_zone_start": False,
        "privacy_zone_end": False,
        "truncation_point_start": None,
        "truncation_point_end": None,
        "path": [],
    }
    save_activity(conn, activity, transformed)

    html_profile = '<a href="/athletes/777">Spider Find A</a><a href="/athletes/888">Spider Find B</a>'
    pages = {
        "/explore/activities": "",
        "/explore/running": "",
        "/explore/cycling": "",
        "/athletes/10": html_profile,
        "/athletes/777": "",
        "/athletes/888": "",
    }
    session = _make_mock_session(pages)
    shutdown = Event()

    result = run_explore_scraper(session, conn, shutdown, spider=True)

    stubs = list_explore_stubs(conn)
    stub_ids = {s["athlete_id"] for s in stubs}
    assert {777, 888}.issubset(stub_ids)
    assert result.spider_ids >= 2


def test_run_explore_scraper_respects_shutdown(conn, no_network):
    shutdown = Event()
    shutdown.set()  # pre-set — scraper should exit immediately
    session = _make_mock_session({})
    result = run_explore_scraper(session, conn, shutdown, spider=False)
    assert result.pages_fetched == 0
