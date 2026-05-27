from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ingestion.tools.diagnostics.runtime import bootstrap_requests_dependency_warnings

bootstrap_requests_dependency_warnings()
import requests

from ingestion.core.delays import random_delay
from ingestion.parsers import (
    ensure_list,
    extract_profile_feed_entries,
    first_int,
    first_non_empty,
    format_payload_shape,
    normalize_activity_photos,
)
from ingestion.core.scrapers.feed import FollowingFeedScraper
from ingestion.session import SessionError, _response_debug_summary

GRAPH_RANGE_RE = re.compile(r"#graph_date_range\?[^\"']*interval=(?P<interval>\d{6})[^\"']*year_offset=(?P<offset>\d+)")


@dataclass(slots=True)
class HistoryFetchIssue:
    code: str
    message: str
    debug: str | None = None


class HistoricalActivityScraper:
    def __init__(self, session, shutdown_event=None):
        self.session = session
        self.shutdown_event = shutdown_event
        self.feed_scraper = FollowingFeedScraper(session, shutdown_event=shutdown_event)
        # Configure delay range for backfill requests
        if session is not None and hasattr(session, 'settings'):
            self._delay_range = (
                getattr(session.settings, "backfill_delay_min_seconds", 2.0),
                getattr(session.settings, "backfill_delay_max_seconds", 5.0),
            )
            self._debug_delays = getattr(session.settings, "debug_delays", False)
        else:
            # Default values for testing without a session
            self._delay_range = (2.0, 5.0)
            self._debug_delays = False

    def fetch_batch(
        self,
        athlete_id: int,
        before: str | None = None,
        oldest_seen_utc: str | None = None,
        *,
        is_following: bool = True,
    ) -> tuple[list[dict], str | None, str | None, HistoryFetchIssue | None]:
        month_cursor = self._resolve_month_cursor(before, oldest_seen_utc)
        earliest_allowed_month = self._earliest_allowed_month()
        if month_cursor < earliest_allowed_month:
            return [], month_cursor, "complete", None
        
        # Add random delay before each history batch request
        random_delay(self._delay_range, debug=self._debug_delays, shutdown_event=self.shutdown_event)
        
        try:
            response, html = self.session.get_text(
                f"/athletes/{athlete_id}",
                chart_type="miles",
                interval_type="month",
                interval=month_cursor,
                year_offset="0",
            )
        except SessionError:
            raise
        except requests.RequestException as exc:
            issue = HistoryFetchIssue("request_exception", f"request failed: {exc}")
            self._emit_debug(issue)
            return [], month_cursor, "degraded", issue
        print(f"[history] Athlete {athlete_id}: requesting month {month_cursor}.")
        if response.status_code == 403:
            return [], month_cursor, "forbidden", None
        if response.status_code != 200:
            issue = self._issue_for_response(response, html)
            self._emit_debug(issue)
            return [], month_cursor, "degraded", issue
        if self._looks_like_login_page(html):
            issue = HistoryFetchIssue(
                "http_3xx_login",
                "redirected to login or received a login page",
                debug=_response_debug_summary(response),
            )
            self._emit_debug(issue)
            return [], month_cursor, "degraded", issue
        if not html.strip():
            issue = HistoryFetchIssue("blank_html", "blank profile page", debug=_response_debug_summary(response))
            self._emit_debug(issue)
            return [], month_cursor, "degraded", issue

        entries, _ = extract_profile_feed_entries(html)
        earliest_supported_month = self._extract_earliest_supported_month(html)
        earliest_boundary = max(earliest_allowed_month, earliest_supported_month or earliest_allowed_month)
        parsed_activities = self._extract_activities(entries, athlete_id, is_following=is_following)
        activities = self._filter_month(parsed_activities, month_cursor)
        next_cursor = self._previous_month(month_cursor)

        if month_cursor < earliest_boundary:
            return [], month_cursor, "complete", None
        if next_cursor < earliest_boundary:
            print(f"[history] Athlete {athlete_id}: month {month_cursor} reached earliest supported range.")
            return activities, next_cursor, "complete", None
        if entries and not parsed_activities:
            issue = HistoryFetchIssue(
                "parse_empty",
                "profile page loaded but no usable activities could be parsed",
                debug=self._summarize_parse_empty(entries),
            )
            self._emit_debug(issue)
            return [], next_cursor, "degraded", issue
        if not activities:
            print(f"[history] Athlete {athlete_id}: month {month_cursor} had no visible activities.")
            return [], next_cursor, "gap", None
        print(f"[history] Athlete {athlete_id}: month {month_cursor} returned {len(activities)} activities.")
        return activities, next_cursor, "active", None

    def _extract_activities(self, payload: Any, athlete_id: int, *, is_following: bool = True) -> list[dict]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return []

        candidates = ensure_list(payload)
        if not candidates:
            return []

        activities = []
        for item in candidates:
            normalized = None
            if isinstance(item, dict):
                normalized = self.feed_scraper._normalize_activity(item, source="historical_backfill")
            if normalized:
                normalized["is_following"] = is_following
                activities.append(normalized)
                continue

            map_info = item.get("map") if isinstance(item.get("map"), dict) else {}
            map_and_photos = item.get("mapAndPhotos") if isinstance(item.get("mapAndPhotos"), dict) else {}
            start_local = first_non_empty(item.get("start_date_local"), item.get("start_date_local_raw"))
            start_utc = first_non_empty(item.get("start_date"), item.get("start_date_utc"))
            polyline = first_non_empty(item.get("map_summary_polyline"), map_info.get("summary_polyline"))
            activity_id = first_int(first_non_empty(item.get("id"), item.get("activity_id")))
            athlete_name = first_non_empty(item.get("athlete_name"), item.get("athlete", {}).get("name")) or f"Athlete {athlete_id}"
            activity_name = item.get("name")
            if not activity_id or not start_local or not start_utc:
                continue
            activities.append(
                {
                    "activity_id": activity_id,
                    "athlete_id": athlete_id,
                    "athlete_name": athlete_name,
                    "athlete_profile_image_url": first_non_empty(item.get("avatar_url"), item.get("athlete", {}).get("avatar_url")),
                    "activity_name": activity_name,
                    "sport_type": item.get("sport_type") or item.get("type") or "Unknown",
                    "start_date_local": start_local,
                    "start_date_utc": start_utc,
                    "elapsed_time": int(item.get("elapsed_time") or 0),
                    "start_latlng": first_non_empty(item.get("start_latlng"), map_info.get("start_latlng")),
                    "end_latlng": first_non_empty(item.get("end_latlng"), map_info.get("end_latlng")),
                    "map_summary_polyline": polyline,
                    "source": "historical_backfill",
                    "is_following": is_following,
                    "is_renderable": bool(polyline),
                    "activity_photos": normalize_activity_photos(
                        first_non_empty(map_and_photos.get("photoList"), item.get("photoList")) or [],
                        activity_id=activity_id,
                        athlete_id=athlete_id,
                        athlete_name=athlete_name,
                        activity_name=activity_name,
                        start_date_utc=start_utc,
                        start_date_local=start_local,
                        source="historical_backfill",
                    ),
                }
            )
        activities.sort(
            key=lambda activity: datetime.fromisoformat(activity["start_date_utc"].replace("Z", "+00:00")),
            reverse=True,
        )
        return activities

    def _filter_month(self, activities: list[dict], month_cursor: str) -> list[dict]:
        filtered = []
        for activity in activities:
            if self._month_cursor_from_iso(activity["start_date_utc"]) == month_cursor:
                filtered.append(activity)
        return filtered

    def _resolve_month_cursor(self, before: str | None, oldest_seen_utc: str | None) -> str:
        if before and re.fullmatch(r"\d{6}", before):
            return before
        if before and re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", before):
            return self._previous_month(self._month_cursor_from_iso(before))
        if oldest_seen_utc:
            return self._previous_month(self._month_cursor_from_iso(oldest_seen_utc))
        return datetime.now(self.session.settings.timezone).strftime("%Y%m")

    def _extract_earliest_supported_month(self, html: str) -> str | None:
        intervals = [match.group("interval") for match in GRAPH_RANGE_RE.finditer(html)]
        if not intervals:
            return None
        earliest_year = min(int(interval[:4]) for interval in intervals)
        return f"{earliest_year}01"

    def _earliest_allowed_month(self) -> str:
        current_year = datetime.now(self.session.settings.timezone).year
        earliest_year = current_year - self.session.settings.backfill_year_cap
        return f"{earliest_year}01"

    def _month_cursor_from_iso(self, value: str) -> str:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(self.session.settings.timezone)
        return dt.strftime("%Y%m")

    def _previous_month(self, month_cursor: str) -> str:
        year = int(month_cursor[:4])
        month = int(month_cursor[4:])
        if month == 1:
            return f"{year - 1}12"
        return f"{year}{month - 1:02d}"

    def _issue_for_response(self, response: requests.Response, html: str) -> HistoryFetchIssue:
        debug = _response_debug_summary(response)
        if response.status_code == 429:
            return HistoryFetchIssue("http_429", "HTTP 429 (likely throttled)", debug=debug)
        if 300 <= response.status_code < 400:
            return HistoryFetchIssue("http_3xx_login", "redirected to login or another auth page", debug=debug)
        if 400 <= response.status_code < 500:
            return HistoryFetchIssue("http_4xx_other", f"HTTP {response.status_code}", debug=debug)
        if response.status_code >= 500:
            return HistoryFetchIssue("http_5xx", f"HTTP {response.status_code}", debug=debug)
        if not html.strip():
            return HistoryFetchIssue("blank_html", "blank profile page", debug=debug)
        return HistoryFetchIssue("request_exception", f"unexpected response: {debug}", debug=debug)

    def _looks_like_login_page(self, html: str) -> bool:
        lowered = html.lower()
        return "strava.com/login" in lowered or 'name="email"' in lowered or "log in to continue" in lowered

    def _summarize_parse_empty(self, entries: list[dict]) -> str:
        first_entry = entries[0] if entries else None
        return f"first_entry={format_payload_shape(first_entry)}"

    def _emit_debug(self, issue: HistoryFetchIssue) -> None:
        if getattr(self.session.settings, "debug_http", False):
            print(f"[history-debug] {issue.code}: {issue.debug or issue.message}")
