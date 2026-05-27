from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ingestion.config import day_bounds
from ingestion.core.delays import random_delay
from ingestion.parsers import ensure_list, first_int, first_non_empty, normalize_activity_photos


class FollowingFeedScraper:
    def __init__(self, session, shutdown_event=None):
        self.session = session
        self.shutdown_event = shutdown_event
        # Configure delay range for feed requests
        if session is not None and hasattr(session, 'settings'):
            self._delay_range = (
                getattr(session.settings, "feed_delay_min_seconds", 1.5),
                getattr(session.settings, "feed_delay_max_seconds", 4.0),
            )
            self._debug_delays = getattr(session.settings, "debug_delays", False)
        else:
            # Default values for testing without a session
            self._delay_range = (1.5, 4.0)
            self._debug_delays = False

    def fetch_activities_for_date(self, athlete_id: int, date_string: str) -> list[dict]:
        day_start, day_end = day_bounds(date_string)
        before = day_end
        cursor = None
        results: list[dict] = []
        seen_ids: set[int] = set()
        page_number = 1

        while True:
            # Add random delay before each feed page request
            random_delay(self._delay_range, debug=self._debug_delays, shutdown_event=self.shutdown_event)
            
            params = {
                "feed_type": "following",
                "athlete_id": athlete_id,
                "before": before,
            }
            if cursor is not None:
                params["cursor"] = cursor
            response, payload = self.session.get_json("/dashboard/feed", **params)
            if response.status_code != 200:
                print(f"[feed] Stopped at page {page_number}: HTTP {response.status_code}")
                break

            page_items, next_cursor = self._extract_page(payload)
            if not page_items:
                print(f"[feed] Page {page_number}: no more feed items.")
                break
            print(f"[feed] Page {page_number}: received {len(page_items)} feed items.")

            reached_older = False
            page_matches = 0
            for raw_item in page_items:
                activity = self._normalize_activity(raw_item, source="following_feed")
                if not activity:
                    continue
                activity_id = int(activity["activity_id"])
                start_ts = int(datetime.fromisoformat(activity["start_date_utc"].replace("Z", "+00:00")).timestamp())
                if start_ts < day_start:
                    reached_older = True
                    continue
                if start_ts > day_end or activity_id in seen_ids:
                    continue
                seen_ids.add(activity_id)
                results.append(activity)
                page_matches += 1

            print(f"[feed] Page {page_number}: kept {page_matches} matching activities.")

            if reached_older or next_cursor is None:
                if reached_older:
                    print("[feed] Reached older activities than the requested date.")
                break
            cursor = next_cursor
            before = next_cursor
            page_number += 1

        return results

    def _extract_page(self, payload: Any) -> tuple[list[dict], int | None]:
        items = [item for item in ensure_list(payload) if isinstance(item, dict)]
        if isinstance(payload, dict):
            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
            cursor = first_non_empty(payload.get("cursor"), pagination.get("cursor"), pagination.get("next_cursor"))
            if not cursor and items:
                last_cursor_data = items[-1].get("cursorData") if isinstance(items[-1].get("cursorData"), dict) else {}
                cursor = first_non_empty(last_cursor_data.get("updated_at"), last_cursor_data.get("rank"))
            return items, first_int(cursor)
        return items, None

    def _normalize_activity(self, raw_item: dict, source: str) -> dict | None:
        activity_payload = raw_item.get("activity")
        entity = activity_payload if isinstance(activity_payload, dict) else raw_item.get("row") or raw_item
        if not isinstance(entity, dict):
            return None
        athlete_payload = entity.get("athlete") if isinstance(entity.get("athlete"), dict) else {}
        athlete = raw_item.get("athlete") if isinstance(raw_item.get("athlete"), dict) else athlete_payload
        map_info = entity.get("map") if isinstance(entity.get("map"), dict) else {}
        map_and_photos = entity.get("mapAndPhotos") if isinstance(entity.get("mapAndPhotos"), dict) else {}
        activity_map = map_and_photos.get("activityMap") if isinstance(map_and_photos.get("activityMap"), dict) else {}
        photo_list = first_non_empty(map_and_photos.get("photoList"), entity.get("photoList")) or []
        activity_id = first_non_empty(entity.get("id"), raw_item.get("entity_id"), raw_item.get("activity_id"))
        athlete_id = first_non_empty(
            athlete.get("id"),
            athlete.get("athleteId"),
            entity.get("athlete_id"),
            raw_item.get("athlete_id"),
        )
        start_date_utc = first_non_empty(entity.get("start_date"), entity.get("start_date_utc"), entity.get("startDate"))
        start_date_local = first_non_empty(entity.get("start_date_local"), entity.get("start_date_local_raw"))
        polyline = first_non_empty(
            entity.get("map_summary_polyline"),
            map_info.get("summary_polyline"),
            entity.get("summary_polyline"),
            activity_map.get("polyline"),
            activity_map.get("summary_polyline"),
            activity_map.get("url"),
        )
        if start_date_utc and not start_date_local:
            start_date_local = start_date_utc
        if not activity_id or not athlete_id or not start_date_local or not start_date_utc:
            return None

        athlete_name = first_non_empty(
            athlete.get("name"),
            athlete.get("athleteName"),
            f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip(),
            entity.get("athlete_name"),
        ) or f"Athlete {athlete_id}"
        activity_name = first_non_empty(entity.get("name"), entity.get("activity_name"), entity.get("activityName"))

        return {
            "activity_id": int(activity_id),
            "athlete_id": int(athlete_id),
            "athlete_name": athlete_name,
            "athlete_profile_image_url": first_non_empty(
                athlete.get("profile_image_url"),
                athlete.get("avatar_url"),
                athlete.get("avatarUrl"),
                athlete.get("profile_medium"),
            ),
            "activity_name": activity_name,
            "sport_type": entity.get("sport_type") or entity.get("type") or "Unknown",
            "start_date_local": start_date_local,
            "start_date_utc": start_date_utc,
            "elapsed_time": int(first_non_empty(entity.get("elapsed_time"), entity.get("elapsedTime"), 0) or 0),
            "start_latlng": first_non_empty(entity.get("start_latlng"), map_info.get("start_latlng")),
            "end_latlng": first_non_empty(entity.get("end_latlng"), map_info.get("end_latlng")),
            "map_summary_polyline": polyline,
            "source": source,
            "is_following": True,
            "is_renderable": bool(polyline),
            "activity_photos": normalize_activity_photos(
                photo_list,
                activity_id=int(activity_id),
                athlete_id=int(athlete_id),
                athlete_name=athlete_name,
                activity_name=activity_name,
                start_date_utc=start_date_utc,
                start_date_local=start_date_local,
                source=source,
            ),
        }
