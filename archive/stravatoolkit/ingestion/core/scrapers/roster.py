from __future__ import annotations

from typing import Any

from ingestion.core.delays import random_delay
from ingestion.parsers import (
    ensure_list,
    first_int,
    first_non_empty,
    parse_following_cards,
    parse_json_assignments,
    parse_next_data_json,
)


class FollowRosterScraper:
    def __init__(self, session, shutdown_event=None):
        self.session = session
        self.shutdown_event = shutdown_event
        # Configure delay range for roster requests
        if session is not None and hasattr(session, 'settings'):
            self._delay_range = (
                getattr(session.settings, "roster_delay_min_seconds", 1.5),
                getattr(session.settings, "roster_delay_max_seconds", 3.5),
            )
            self._debug_delays = getattr(session.settings, "debug_delays", False)
        else:
            # Default values for testing without a session
            self._delay_range = (1.5, 3.5)
            self._debug_delays = False

    def fetch_following_roster(self, athlete_id: int) -> list[dict]:
        page = 1
        roster: list[dict] = []
        seen_ids: set[int] = set()
        while True:
            # Add random delay before each roster page request
            random_delay(self._delay_range, debug=self._debug_delays, shutdown_event=self.shutdown_event)
            
            response, html = self.session.get_text(
                f"/athletes/{athlete_id}/follows",
                type="following",
                page=page,
            )
            if response.status_code != 200 or not html.strip():
                break
            athletes = self._parse_following_html(html)
            if not athletes:
                break
            print(f"[roster] Page {page}: found {len(athletes)} following entries.")
            for athlete in athletes:
                if athlete["athlete_id"] in seen_ids:
                    continue
                roster.append(athlete)
                seen_ids.add(athlete["athlete_id"])
            page += 1
        return roster

    def _parse_following_html(self, html: str) -> list[dict]:
        athletes = parse_following_cards(html)
        if athletes:
            return athletes

        for candidate in [parse_next_data_json(html), *parse_json_assignments(html)]:
            athletes = self._extract_athletes_from_data(candidate)
            if athletes:
                return athletes
        return []

    def _extract_athletes_from_data(self, payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            for key in ("props", "pageProps", "athletes", "models", "entries", "data"):
                value = payload.get(key)
                athletes = self._extract_athletes_from_data(value)
                if athletes:
                    return athletes
            athlete_id = first_int(first_non_empty(payload.get("id"), payload.get("athlete_id")))
            if athlete_id and first_non_empty(payload.get("name"), payload.get("fullname")):
                return [
                    {
                        "athlete_id": athlete_id,
                        "name": payload.get("name") or payload.get("fullname"),
                        "avatar_url": first_non_empty(payload.get("avatar_url"), payload.get("profile")),
                        "source": "following_roster",
                    }
                ]
            return []
        if isinstance(payload, list):
            athletes: list[dict] = []
            for item in payload:
                athletes.extend(self._extract_athletes_from_data(item))
            return athletes
        return []
