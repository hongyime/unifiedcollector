from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ingestion.core.scrapers import FollowRosterScraper, HistoricalActivityScraper
from ingestion.parsers import extract_profile_feed_entries
from ingestion.core.scrapers.feed import FollowingFeedScraper
from ingestion.session import _read_cookie_file


FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_following_feed_normalization_from_fixture() -> None:
    payload = json.loads((FIXTURES / "following_feed.json").read_text(encoding="utf-8"))
    scraper = FollowingFeedScraper(session=None)
    items, cursor = scraper._extract_page(payload)
    activity = scraper._normalize_activity(items[0], source="following_feed")

    assert cursor == 1712363400
    assert activity is not None
    assert activity["activity_id"] == 12345
    assert activity["athlete_id"] == 99
    assert activity["athlete_name"] == "Taylor Tan"
    assert activity["is_renderable"] is True


def test_following_roster_html_parser_fixture() -> None:
    html = (FIXTURES / "following_roster.html").read_text(encoding="utf-8")
    scraper = FollowRosterScraper(session=None)
    athletes = scraper._parse_following_html(html)

    assert [athlete["athlete_id"] for athlete in athletes] == [42, 43]
    assert athletes[0]["name"] == "Alex Lim"


def test_historical_activity_parser_fixture() -> None:
    payload = json.loads((FIXTURES / "historical_activities.json").read_text(encoding="utf-8"))
    scraper = HistoricalActivityScraper(session=None)
    activities = scraper._extract_activities(payload, athlete_id=42)

    assert len(activities) == 1
    assert activities[0]["activity_id"] == 888
    assert activities[0]["source"] == "historical_backfill"


def test_cookie_file_fixture() -> None:
    assert _read_cookie_file(str(FIXTURES / "cookies.txt")) == "test-cookie-value"


def test_extract_profile_feed_entries_from_microfrontend_html() -> None:
    html = """
    <div data-react-props='{&quot;appContext&quot;:{&quot;page&quot;:&quot;profile&quot;,&quot;feedType&quot;:&quot;profile&quot;,
    &quot;preFetchedEntries&quot;:[{&quot;entity&quot;:&quot;Activity&quot;,&quot;activity&quot;:{&quot;id&quot;:&quot;123&quot;,&quot;activityName&quot;:&quot;Easy Run&quot;,
    &quot;startDate&quot;:&quot;2026-04-06T01:00:00Z&quot;,&quot;elapsedTime&quot;:1200,&quot;athlete&quot;:{&quot;athleteId&quot;:&quot;42&quot;,&quot;athleteName&quot;:&quot;Jordan&quot;},
    &quot;mapAndPhotos&quot;:{&quot;activityMap&quot;:{&quot;polyline&quot;:&quot;abcd&quot;}}},&quot;cursorData&quot;:{&quot;updated_at&quot;:1775475932}}]}}'></div>
    """

    entries, app_context = extract_profile_feed_entries(html)

    assert app_context is not None
    assert app_context["page"] == "profile"
    assert len(entries) == 1
    assert entries[0]["activity"]["id"] == "123"


def test_extract_profile_feed_entries_from_next_data_html() -> None:
    html = """
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"page":"profile","feedType":"profile",
    "activities":[{"id":"456","name":"Next Run","start_date":"2026-04-06T01:00:00Z","type":"Run"}]}}}</script>
    """

    entries, app_context = extract_profile_feed_entries(html)

    assert app_context is not None
    assert app_context["source"] == "next_data"
    assert len(entries) == 1
    assert entries[0]["id"] == "456"


def test_extract_profile_feed_entries_from_inline_json_html() -> None:
    html = """
    <script>
    window.__INITIAL_STATE__ = {"page":"profile","feedType":"profile",
    "activities":[{"id":"789","name":"Inline Ride","start_date":"2026-04-06T01:00:00Z","type":"Ride"}]};
    </script>
    """

    entries, app_context = extract_profile_feed_entries(html)

    assert app_context is not None
    assert app_context["source"] == "inline_json"
    assert len(entries) == 1
    assert entries[0]["id"] == "789"


def test_historical_scraper_uses_month_cursor_helpers() -> None:
    scraper = HistoricalActivityScraper(
        session=SimpleNamespace(settings=SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), backfill_year_cap=25))
    )

    html = """
    <a href="/athletes/42#graph_date_range?chart_type=miles&amp;interval_type=week&amp;interval=202415&amp;year_offset=2">range</a>
    """

    assert scraper._extract_earliest_supported_month(html) == "202401"
    assert scraper._resolve_month_cursor("202503", None) == "202503"
    assert scraper._resolve_month_cursor("2025-04-06T11:16:11Z", None) == "202503"


def test_historical_scraper_caps_backfill_to_january_25_years_back(monkeypatch) -> None:
    scraper = HistoricalActivityScraper(
        session=SimpleNamespace(settings=SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), backfill_year_cap=25))
    )

    class FrozenDateTime:
        @classmethod
        def now(cls, tz):
            return __import__("datetime").datetime(2026, 4, 9, tzinfo=tz)

    monkeypatch.setattr("ingestion.core.scrapers.history.datetime", FrozenDateTime)

    assert scraper._earliest_allowed_month() == "200101"


def test_following_feed_normalization_keeps_photo_only_activity() -> None:
    scraper = FollowingFeedScraper(session=None)
    raw_item = {
        "activity": {
            "id": "123",
            "activityName": "Photo Walk",
            "type": "Walk",
            "startDate": "2026-04-06T01:00:00Z",
            "start_date_local": "2026-04-06T09:00:00+08:00",
            "elapsedTime": 1200,
            "athlete": {
                "athleteId": "42",
                "athleteName": "Jordan",
                "avatarUrl": "https://example.com/avatar.jpg",
            },
            "mapAndPhotos": {
                "photoList": [
                    {
                        "photo_id": "photo-1",
                        "activity_id": 123,
                        "owner_id": 42,
                        "thumbnail": "https://example.com/photo-thumb.jpg",
                        "large": "https://example.com/photo-large.jpg",
                        "caption_escaped": "nice day",
                    }
                ]
            },
        }
    }

    activity = scraper._normalize_activity(raw_item, source="following_feed")

    assert activity is not None
    assert activity["is_renderable"] is False
    assert activity["activity_photos"][0]["photo_id"] == "photo-1"


def test_historical_scraper_classifies_http_429() -> None:
    class FakeSession:
        settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), backfill_year_cap=25, debug_http=False)

        def get_text(self, path: str, **params):
            response = SimpleNamespace(
                status_code=429,
                headers={"Content-Type": "text/html"},
                url="https://www.strava.com/athletes/42",
            )
            return response, "rate limited"

    scraper = HistoricalActivityScraper(session=FakeSession())

    activities, next_cursor, status, issue = scraper.fetch_batch(42, before="202601")

    assert activities == []
    assert next_cursor == "202601"
    assert status == "degraded"
    assert issue is not None
    assert issue.code == "http_429"


def test_historical_scraper_classifies_parse_empty() -> None:
    html = """
    <div data-react-props='{&quot;appContext&quot;:{&quot;page&quot;:&quot;profile&quot;,&quot;feedType&quot;:&quot;profile&quot;,
    &quot;preFetchedEntries&quot;:[{&quot;entity&quot;:&quot;Activity&quot;,&quot;activity&quot;:{&quot;id&quot;:&quot;123&quot;}}]}}'></div>
    """

    class FakeSession:
        settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), backfill_year_cap=25, debug_http=False)

        def get_text(self, path: str, **params):
            response = SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "text/html"},
                url="https://www.strava.com/athletes/42",
            )
            return response, html

    scraper = HistoricalActivityScraper(session=FakeSession())

    activities, next_cursor, status, issue = scraper.fetch_batch(42, before="202601")

    assert activities == []
    assert next_cursor == "202512"
    assert status == "degraded"
    assert issue is not None
    assert issue.code == "parse_empty"
    assert issue.debug is not None
    assert "first_entry=" in issue.debug
    assert "activity_keys=id" in issue.debug
